"""图片翻译（对标欧路「拍照翻译」）：OCR 识别 → 翻译 → 原字擦除 → 叠中文。

管线与现实约束
--------------
* OCR 复用 PDF 扫描件同款 RapidOCR（离线、免费），不引新依赖；
* 擦除原字不是简单涂白——截取文字框**四周边缘**的中位色当背景，
  这样照片、彩色截图、带底色的图表都能干净地盖上译文；
* 中文字体从系统里找（微软雅黑 → 黑体 → 宋体），找不到就明确报错：
  用 PIL 默认字体渲染中文只会得到一排方块，比不翻更糟。

置信度低的识别结果直接丢弃：OCR 自己都没把握的文字，翻译出来只会
制造"看起来很确定的错译"。**但丢弃这件事不能是无声的**（v1.9.5）——
一张截图丢了一行小字，用户看到的是「翻得挺好」；现在丢掉的原文片段会
连同置信度进《自检报告》，日志里也点名，成品旁边那份报告与文档成品同口径。
v1.9.7 起每条丢掉的行还带着它在原图上的相对位置（横向/纵向百分比），
截图里没有可念的坐标，但「横向 25%~40%、纵向 40%~50%」足够人照着找。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .common import TranslationError, shown_path, unique_output_path
from .report import box_rect, drop_entries, lines_report

#: 低于此置信度的 OCR 结果丢弃。
MIN_OCR_SCORE = 0.55

#: 中文字体候选（按优先级）。都找不到就报错，不用默认字体糊中文。
FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyh.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)

#: 支持的图片格式。
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff")

#: 译文行距与内边距（像素，按原文字高缩放）。
_PADDING = 4


@dataclass
class OcrLine:
    """一个 OCR 文本行：框 + 文本 + 置信度。"""

    box: list[tuple[float, float]]
    text: str
    score: float

    @property
    def x0(self) -> float:
        return min(point[0] for point in self.box)

    @property
    def y0(self) -> float:
        return min(point[1] for point in self.box)

    @property
    def x1(self) -> float:
        return max(point[0] for point in self.box)

    @property
    def y1(self) -> float:
        return max(point[1] for point in self.box)

    @property
    def height(self) -> float:
        return max(1.0, self.y1 - self.y0)


def _get_ocr_engine():
    from .pdf_translator import _get_ocr_engine as _pdf_ocr

    return _pdf_ocr()


def _find_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:
                continue
    raise TranslationError(
        "系统里找不到中文字体（微软雅黑/黑体/宋体），无法在图片上渲染中文。"
    )


def _background_color(image: Image.Image, line: OcrLine) -> tuple[int, ...]:
    """取文字框四周边缘的中位色当背景——比直接涂白干净得多。"""
    width, height = image.size
    x0, y0, x1, y1 = int(line.x0), int(line.y0), int(line.x1), int(line.y1)
    margin = 3
    left = max(0, x0 - margin)
    right = min(width, x1 + margin)
    top = max(0, y0 - margin)
    bottom = min(height, y1 + margin)

    samples: list[tuple[int, ...]] = []
    for x in range(left, right):
        for y in (top, bottom - 1):
            if 0 <= x < width and 0 <= y < height:
                samples.append(image.getpixel((x, y)))
    for y in range(top, bottom):
        for x in (left, right - 1):
            if 0 <= x < width and 0 <= y < height:
                samples.append(image.getpixel((x, y)))
    if not samples:
        return (255, 255, 255)

    channels = len(samples[0]) if isinstance(samples[0], tuple) else 1
    if channels == 1:
        gray = sorted(samples)
        return (gray[len(gray) // 2],)
    per_channel = sorted(
        samples, key=lambda pixel: sum(pixel[:3])
    )
    median = per_channel[len(per_channel) // 2]
    return tuple(median[:3])


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    """按像素宽度换行。中文没有空格，逐字断行比按词断行更接近原排版。"""
    if not text:
        return [""]
    lines: list[str] = []
    current = ""
    for char in text:
        trial = current + char
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines


def _render_line(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    line: OcrLine,
    translated: str,
    font: ImageFont.FreeTypeFont,
) -> None:
    """擦除原字，在原位置渲染译文。字号按原字高自适应，超宽才换行。"""
    background = _background_color(image, line)
    x0, y0, x1, y1 = int(line.x0), int(line.y0), int(line.x1), int(line.y1)
    box = (x0 - _PADDING, y0 - _PADDING, x1 + _PADDING, y1 + _PADDING)
    draw.rectangle(box, fill=background)

    box_width = max(1, x1 - x0)
    box_height = max(1, y1 - y0)
    # 译文与原文的长度比决定字号：短译文用原字号，长译文按需缩小
    ratio = len(translated) / max(len(line.text), 1)
    font_size = max(10, int(box_height / max(ratio, 1.0)))
    if font_size > int(box_height * 1.6):
        font_size = int(box_height * 1.6)
    sized_font = _find_font(font_size)

    rows = _wrap_text(draw, translated, sized_font, box_width)
    # 行数超过框高就整体缩小字号重排，直到放得下
    while len(rows) * font_size > box_height * 2.2 and font_size > 10:
        font_size -= 1
        sized_font = _find_font(font_size)
        rows = _wrap_text(draw, translated, sized_font, box_width)

    ink = (20, 24, 32) if sum(background[:3]) > 380 else (245, 247, 250)
    cursor_y = y0
    for row in rows:
        draw.text((x0, cursor_y), row, font=sized_font, fill=ink)
        cursor_y += font_size + 2


def _cluster_lines(lines: Sequence[OcrLine]) -> list[list[OcrLine]]:
    """把同一行的多个 OCR 片段合成一行（RapidOCR 常把一行拆成几块）。"""
    if not lines:
        return []
    ordered = sorted(lines, key=lambda line: (line.y0, line.x0))
    groups: list[list[OcrLine]] = []
    for line in ordered:
        placed = False
        for group in groups:
            reference = group[0]
            # 垂直中心距小于半行高 → 同一行
            if abs((line.y0 + line.y1) / 2 - (reference.y0 + reference.y1) / 2) < (
                reference.height * 0.6
            ):
                group.append(line)
                placed = True
                break
        if not placed:
            groups.append([line])
    return [sorted(group, key=lambda line: line.x0) for group in groups]


def translate_image(
    src_path: str,
    translator,
    log: Callable[[str], None] | None = None,
    progress=None,
) -> str:
    """翻译一张图片：识别外文 → 译成中文 → 叠回原位置。返回成品路径。"""
    log = log or (lambda message: None)
    path = Path(src_path)
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise TranslationError(
            f"不支持的图片格式 {path.suffix}；目前支持：{'、'.join(IMAGE_SUFFIXES)}"
        )

    image = Image.open(src_path).convert("RGB")
    engine = _get_ocr_engine()
    array = np.asarray(image)
    result, _ = engine(array[:, :, ::-1])
    del array
    if not result:
        raise TranslationError("图片里没有识别到文字（可能不是含外文的截图/照片）。")

    lines: list[OcrLine] = []
    dropped: list[tuple[str, float, tuple[float, float, float, float] | None]] = []
    for item in result:
        try:
            box, text, score = item[0], str(item[1] or ""), float(item[2])
        except Exception:
            continue
        text = text.strip()
        if not text:
            continue
        if score < MIN_OCR_SCORE:
            dropped.append((text, score, box_rect(box)))
            continue
        lines.append(
            OcrLine(box=[(float(point[0]), float(point[1])) for point in box], text=text, score=score)
        )
    if not lines:
        raise TranslationError("识别到的文字置信度都过低，已放弃翻译这张图片。")

    rows = _cluster_lines(lines)
    sources = [" ".join(line.text for line in row) for row in rows]
    log(f"已识别 {len(rows)} 行文字，开始翻译…")

    translations = translator.translate_texts(sources, progress)

    draw = ImageDraw.Draw(image)
    placed = 0
    report_pairs: list[tuple[str, str]] = []
    report_locations: list[str] = []
    kept_original: list[tuple[str, str, str]] = []
    for position, (row, source, translated) in enumerate(
        zip(rows, sources, translations), start=1
    ):
        location = f"第 {position} 行"
        text = (translated or "").strip()
        if not text:
            kept_original.append((location, source, "引擎没有给出译文，原文未覆盖"))
            continue
        # 与原文完全相同的"译文"是引擎没干活，不覆盖（避免把原文擦掉）
        if re.sub(r"\s", "", text) == re.sub(r"\s", "", source):
            kept_original.append((location, source, "引擎回显原文，未获得译文"))
            continue
        report_pairs.append((source, text))
        report_locations.append(location)
        merged = _merge_row(row)
        _render_line(image, draw, merged, text, _find_font(16))
        placed += 1

    if placed == 0:
        raise TranslationError("所有识别行都没有得到有效译文，未生成图片。")

    output = unique_output_path(src_path, "中文", path.suffix)
    if path.suffix.lower() in (".jpg", ".jpeg"):
        image.save(output, quality=95)
    else:
        image.save(output)
    log(f"图片翻译完成：{placed} 行译文已叠到原位置 → {output}")

    # v1.9.5：图片成品也出《自检报告》。OCR 丢掉的行按条报出原文片段与置信度；
    # v1.9.7 起还带原图上的相对位置，v1.9.8 把这条口径与扫描页共用（core/report.drop_entries）。
    extra: list[tuple[str, str, str, str]] = []
    if dropped:
        extra = drop_entries(dropped, image.size)
        log(f"有 {len(dropped)} 处文字识别置信度过低，已跳过（详见自检报告）")

    report_path, report_count = lines_report(
        output,
        report_pairs,
        report_locations,
        translator,
        kept=kept_original,
        extra=extra,
    )
    if report_path:
        log(f"自检报告：{shown_path(report_path)}（{report_count} 条疑点）")
    return output


def _merge_row(row: Sequence[OcrLine]) -> OcrLine:
    """把一行的多个片段合成一个虚拟行：框取并集，文本按顺序拼接。"""
    boxes = [line.box for line in row]
    xs = [point[0] for box in boxes for point in box]
    ys = [point[1] for box in boxes for point in box]
    merged_box = [
        (min(xs), min(ys)),
        (max(xs), min(ys)),
        (max(xs), max(ys)),
        (min(xs), max(ys)),
    ]
    return OcrLine(
        box=merged_box,
        text=" ".join(line.text for line in row),
        score=min(line.score for line in row),
    )


def is_image_file(path: str) -> bool:
    return Path(path).suffix.lower() in IMAGE_SUFFIXES
