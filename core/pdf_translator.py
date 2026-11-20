from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from typing import Callable

import pymupdf

from . import direction, mixed
from .common import TranslationError, check_cancel, shown_path, unique_output_path
from .number_check import check_numbers
from .page_range import format_pages, parse_pages
from .report import (
    CheckReport,
    box_rect,
    build_report,
    drop_entries,
    fuzzy_entries,
    paid_usage_of,
    quality_payload,
    term_hits_of,
    tm_fuzzy_of,
)
from .term_check import check_term_consistency, issue_note
from .translator import (
    CJK_RE,
    Translator,
    is_translatable,
    normalize_source,
)

OCR_DPI = 200
OCR_MIN_SCORE = 0.45
RIGHT_GAP = 14.0
# 表格窄格里中文可能被迫缩到 6pt 以下，那种成品没法读，不如保留原文，
# 所以排版下限直接取「还认得出」的 7.5pt。
MIN_FONT_SIZE = 7.5
# insert_textbox 按字体名义行框（微软雅黑 1.32em）预留高度，比源文件自己的行距
# （1.2em）大得多，用它试排会把 18pt 正文整页压到 14～16pt。改成自己折行、自己按
# 基线落版，只需要装下「可见墨迹」：基线以上 0.833em、以下 0.125em。
INK_BELOW = 0.125
DEFAULT_LEADING = 1.2
MIN_LEADING = 1.05
MAX_LEADING = 1.8
CJK_FONT = "china-ss"
LOCAL_FONT_FILES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
)
LOCAL_FONT_FILE = next((path for path in LOCAL_FONT_FILES if os.path.exists(path)), "")
# 内置 china-ss 会把英文字母按全宽绘制（AI 显示成 A I），能用系统字体时优先嵌入
USE_LOCAL_FONT = bool(LOCAL_FONT_FILE)
BULLET_RE = re.compile(r"^(?:[•·▪◦●○■□◆◇‣∙*\-–—]|\(?\d{1,2}[.)])$")

# 引擎常把原文的项目符号一起带回来（⚫Supplement… → ⚫补充…），而系统字体不一定有
# 这个字形：缺字时 insert_textbox 会画一个空心方框，成品里就成了「□补充主要FSs」。
# 统一换成三个候选字体都有的近义字形，空格类直接归一。
SYMBOL_ALIASES = {
    "\u00a0": " ",
    "\u200b": "",
    "\u26ab": "\u25cf",
    "\u26aa": "\u25cb",
    "\u2b24": "\u25cf",
    "\u2022": "\u00b7",
    "\u2023": "\u00b7",
    "\u25e6": "\u25cb",
    "\u25aa": "\u25a0",
    "\u25fb": "\u25a1",
    "\u25b6": "\u25cf",
}
_SYMBOL_RE = re.compile("[" + "".join(SYMBOL_ALIASES) + "]")

# 排版要按字体量宽度、按基线逐行写字，所以全局复用同一个 Font 对象。
_TYPE_FONT: pymupdf.Font | None = None
_TYPE_FONT_LOCAL: bool | None = None
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.,;:/%()\-'’]*\s*|[^\s]\s*")


def _type_font() -> pymupdf.Font:
    global _TYPE_FONT, _TYPE_FONT_LOCAL
    want_local = USE_LOCAL_FONT and bool(LOCAL_FONT_FILE)
    if _TYPE_FONT is None or _TYPE_FONT_LOCAL != want_local:
        try:
            _TYPE_FONT = (
                pymupdf.Font(fontfile=LOCAL_FONT_FILE)
                if want_local
                else pymupdf.Font(CJK_FONT)
            )
        except Exception:
            _TYPE_FONT = pymupdf.Font(CJK_FONT)
            _TYPE_FONT_LOCAL = False
            want_local = False
    _TYPE_FONT_LOCAL = want_local
    return _TYPE_FONT


def _swap_uncovered(text: str) -> str:
    font = _type_font()

    def replace(match: re.Match) -> str:
        char = match.group(0)
        alias = SYMBOL_ALIASES[char]
        if char in ("\u00a0", "\u200b"):
            return alias
        return char if font.has_glyph(ord(char)) else alias

    return _SYMBOL_RE.sub(replace, text)


def _wrap_lines(text: str, size: float, width: float) -> list[str]:
    """按框宽折行。中文一个字一断，英文单词和数字串保持完整，
    不然 `IFRS 17` 会被拆成 `IFRS 1` + `7`。"""
    font = _type_font()
    lines: list[str] = []
    current = ""
    used = 0.0
    for token in _TOKEN_RE.findall(text):
        w = font.text_length(token, fontsize=size)
        if current and used + w > width + 0.01:
            lines.append(current.rstrip())
            current = ""
            used = 0.0
        if not current and not token.strip():
            continue
        current += token
        used += w
    if current.strip():
        lines.append(current.rstrip())
    return lines or [text.strip()]


def _line_leading(group: "TextGroup") -> float:
    """源文件自己的行距倍数（行首到行首 / 字号），译文照着它排才不像换了版式。"""
    rects = group.line_rects
    if len(rects) >= 2 and group.font_size > 0:
        gaps = [rects[index + 1].y0 - rects[index].y0 for index in range(len(rects) - 1)]
        return min(MAX_LEADING, max(MIN_LEADING, _median(gaps) / group.font_size))
    return DEFAULT_LEADING


def _needed_height(count: int, size: float, leading: float) -> float:
    """n 行这个字号的中文要占多高——只算可见墨迹，不算字体的名义行框。"""
    font = _type_font()
    return size * (font.ascender + INK_BELOW) + (count - 1) * leading * size


_OCR_ENGINE = None
_OCR_LOCK = threading.Lock()

try:
    import wordninja
except Exception:
    wordninja = None


def _fix_ocr_words(text: str) -> str:
    # RapidOCR renders the capital I of "AI" as a lowercase l. A standalone
    # "Al" is that misread whenever the next word starts lower-case; the real
    # Arabic transliteration ("Al Jazeera", "bin Al Salem") always takes a name.
    def ai(match: re.Match) -> str:
        tail = re.match(r"[,.;:!?\-–—]?\s*([A-Za-z0-9])", text[match.end():])
        if not tail:
            return match.group(0)
        return "AI" if tail.group(1).islower() or tail.group(1).isdigit() else match.group(0)

    text = re.sub(r"(?<![A-Za-z])Al(?![A-Za-z])", ai, text)
    # RapidOCR also drops spaces inside words, so re-segment long / camelCase
    # tokens before the text reaches the translation service.
    if wordninja is None:
        return text

    def repair(match: re.Match) -> str:
        token = match.group(0)
        if len(token) < 4 and not re.search(r"[a-z][A-Z]", token):
            return token
        parts = wordninja.split(token)
        return " ".join(parts) if len(parts) > 1 else token

    return re.sub(r"[A-Za-z]{2,}", repair, text)


def _mixed_runs(text: str) -> list[str]:
    """中英混排的整段会被判定成「已经是中文，不用翻」，段里的外文子句就这么漏过去了。
    抽取规则与 Word / 引擎侧共用 ``core.mixed``：把够长的少数语种片段挑出来单独送翻译，
    回来再拼进原段。"""
    return mixed.minority_spans(text)


@dataclass
class TextGroup:
    bbox: pymupdf.Rect
    text: str
    font_size: float
    color: tuple[float, float, float]
    align: int
    line_rects: list[pymupdf.Rect] = field(default_factory=list)
    cover_rects: list[pymupdf.Rect] = field(default_factory=list)
    cover_colors: list[tuple[float, float, float]] = field(default_factory=list)
    translatable: bool = False
    translation: str = ""
    runs: list[str] = field(default_factory=list)
    run_map: dict[str, str] = field(default_factory=dict)


@dataclass
class PageInfo:
    index: int
    mode: str
    groups: list[TextGroup] = field(default_factory=list)


def _median(values: list[float]) -> float:
    if not values:
        return 10.0
    values = sorted(values)
    return values[len(values) // 2]


def _color_from_int(value: int) -> tuple[float, float, float]:
    return (
        ((value >> 16) & 255) / 255.0,
        ((value >> 8) & 255) / 255.0,
        (value & 255) / 255.0,
    )


def _detect_align(
    lines: list[pymupdf.Rect],
    page_width: float,
    font_size: float,
    left_margin: float | None = None,
) -> int:
    """按每一行的左右边界判对齐。

    只看整段外框时，右对齐段落的外框和左对齐一模一样，德文/俄文文档里就是这么排错的：
    右对齐的行尾基本齐（行尾跨度接近零、行首参差），居中的行中点基本齐。
    """
    if not lines:
        return 0
    if len(lines) == 1:
        # 单行段落的行中点可能只是碰巧落在页面中线附近；只要行首和正文左边界齐，
        # 就按左对齐处理，否则整页正文会被误判成居中。
        if left_margin is not None and abs(
            lines[0].x0 - left_margin
        ) <= max(2.0, font_size * 0.3):
            return 0
        return _guess_align(lines[0], page_width, font_size)
    lefts = [rect.x0 for rect in lines]
    rights = [rect.x1 for rect in lines]
    mids = [(rect.x0 + rect.x1) / 2 for rect in lines]
    left_spread = max(lefts) - min(lefts)
    right_spread = max(rights) - min(rights)
    mid_spread = max(mids) - min(mids)
    tolerance = max(4.0, font_size * 0.4)
    if right_spread <= tolerance and left_spread > right_spread + tolerance * 3:
        return 2
    if (
        mid_spread <= tolerance
        and left_spread > mid_spread + tolerance * 3
        and right_spread > mid_spread + tolerance * 3
    ):
        return 1
    return 0


def _guess_align(bbox: pymupdf.Rect, page_width: float, font_size: float) -> int:
    width = bbox.width
    center = (bbox.x0 + bbox.x1) / 2
    if (
        abs(center - page_width / 2) < page_width * 0.04
        and width < page_width * 0.85
        and font_size >= 12
    ):
        return 1
    if (
        bbox.x1 > page_width * 0.9
        and bbox.x0 > page_width * 0.5
        and width < page_width * 0.5
    ):
        return 2
    return 0


def _expand(rect: pymupdf.Rect, amount: float = 1.2) -> pymupdf.Rect:
    return pymupdf.Rect(
        rect.x0 - amount, rect.y0 - amount, rect.x1 + amount, rect.y1 + amount
    )


def _continues(previous: dict, item: dict) -> bool:
    """这一行是不是上一行的续行：左对齐比行首，右对齐比行尾，居中比行中点。

    只比行首会把右对齐段落一行一段拆开，译文就得逐行重译，既慢又不成句。
    """
    rect = item["rect"]
    tolerance = max(3.0, item["last_height"] * 0.3)
    if -1.0 <= rect.x0 - previous["last_x0"] <= 22.0:
        return True
    return (
        abs(rect.x1 - previous["last_x1"]) <= tolerance
        or abs((rect.x0 + rect.x1) / 2 - previous["last_mid"]) <= tolerance
    )


_LAYOUT_STATE: dict[str, object] = {"ready": None}


def _layout_blocks(page: pymupdf.Page) -> list[tuple[pymupdf.Rect, str]] | None:
    """取 PyMuPDF 自带版面模型的分块（表格/项目符号/标题…），不可用就返回 None。

    模型已经在 pymupdf 包里（约 0.3 秒/页），不用额外装东西，保密模式同样能离线跑。
    """
    if _LAYOUT_STATE["ready"] is False:
        return None
    if _LAYOUT_STATE["ready"] is None:
        try:
            from importlib import import_module

            import_module("pymupdf.layout")  # 导入即激活版面识别
            _LAYOUT_STATE["ready"] = True
        except Exception:
            _LAYOUT_STATE["ready"] = False
            return None
    try:
        page.get_layout()
        raw = page.layout_information or []
    except Exception:
        _LAYOUT_STATE["ready"] = False
        return None
    blocks = [
        (pymupdf.Rect(item[:4]), str(item[4]))
        for item in raw
        if len(item) >= 5
    ]
    return blocks or None


def _block_index(blocks, rect: pymupdf.Rect) -> int | None:
    """找出包住这一行的最小版面块；包不住就返回 None，让上层退回纯几何合并。"""
    if not blocks:
        return None
    center_x = (rect.x0 + rect.x1) / 2
    center_y = (rect.y0 + rect.y1) / 2
    found: int | None = None
    smallest = 0.0
    for index, (box, _label) in enumerate(blocks):
        if not (box.x0 - 2 <= center_x <= box.x1 + 2):
            continue
        if not (box.y0 - 2 <= center_y <= box.y1 + 2):
            continue
        area = box.width * box.height
        if found is None or area < smallest:
            found, smallest = index, area
    return found


def _group_key(layout, pdf_index: int, rect: pymupdf.Rect) -> tuple:
    """两行该不该并成一组：先认版面块，认不到再退回 PDF 自己的文本块。

    有的导出器把折行的每一行都当成独立文本块，只看 PDF 块就会一段话被逐行拆开翻译。
    """
    block = _block_index(layout, rect)
    return ("L", block) if block is not None else ("P", pdf_index)


def _extract_text_groups(page: pymupdf.Page) -> list[TextGroup]:
    groups: list[TextGroup] = []
    page_width = page.rect.width
    layout = _layout_blocks(page)
    data = page.get_text("dict", sort=True)
    items: list[dict] = []
    for pdf_index, block in enumerate(data.get("blocks", [])):
        if block.get("type") != 0:
            continue
        lines: list[dict] = []
        for line in block.get("lines") or []:
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if not text.strip():
                continue
            sizes: list[float] = []
            colors: dict[int, int] = {}
            for span in line.get("spans", []):
                content = span.get("text", "")
                if not content.strip():
                    continue
                size = float(span.get("size", 10.0))
                sizes.extend([size] * max(1, len(content)))
                color = int(span.get("color", 0))
                colors[color] = colors.get(color, 0) + len(content)
            if not sizes:
                continue
            lines.append(
                {
                    "text": text.strip(),
                    "rect": pymupdf.Rect(line["bbox"]),
                    "sizes": sizes,
                    "colors": colors,
                }
            )
        if not lines:
            continue

        # A bullet glyph is often its own line; attach it to the text it marks.
        index = 0
        while index < len(lines):
            current = lines[index]
            if (
                BULLET_RE.match(current["text"])
                and index + 1 < len(lines)
                and lines[index + 1]["rect"].y0 - current["rect"].y1
                <= max(current["rect"].height, 6.0)
            ):
                following = lines[index + 1]
                merged_colors = dict(current["colors"])
                for key, value in following["colors"].items():
                    merged_colors[key] = merged_colors.get(key, 0) + value
                items.append(
                    {
                        "text": f"{current['text']} {following['text']}",
                        "rect": current["rect"] | following["rect"],
                        "cover_rects": [current["rect"], following["rect"]],
                        "sizes": current["sizes"] + following["sizes"],
                        "colors": merged_colors,
                        "key": _group_key(layout, pdf_index, following["rect"]),
                        "last_x0": following["rect"].x0,
                        "last_y1": following["rect"].y1,
                        "last_height": following["rect"].height,
                    }
                )
                index += 2
                continue
            items.append(
                {
                    "text": current["text"],
                    "rect": pymupdf.Rect(current["rect"]),
                    "cover_rects": [current["rect"]],
                    "sizes": current["sizes"],
                    "colors": current["colors"],
                    "key": _group_key(layout, pdf_index, current["rect"]),
                    "last_x0": current["rect"].x0,
                    "last_y1": current["rect"].y1,
                    "last_height": current["rect"].height,
                }
            )
            index += 1

    # Join wrapped lines of the same paragraph / table cell, but keep
    # separate list items and table columns apart.
    block_groups: list[dict] = []
    for item in items:
        if block_groups:
            previous = block_groups[-1]
            gap = item["rect"].y0 - previous["last_y1"]
            limit = max(3.0, previous["last_height"] * 0.55)
            if (
                gap <= limit
                and item["key"] == previous["key"]
                and _continues(previous, item)
            ):
                previous["rect"] |= item["rect"]
                previous["cover_rects"].extend(item["cover_rects"])
                previous["sizes"].extend(item["sizes"])
                for key, value in item["colors"].items():
                    previous["colors"][key] = (
                        previous["colors"].get(key, 0) + value
                    )
                previous["text_parts"].append(item["text"])
                previous["last_x0"] = item["last_x0"]
                previous["last_x1"] = item["rect"].x1
                previous["last_mid"] = (item["rect"].x0 + item["rect"].x1) / 2
                previous["last_y1"] = item["last_y1"]
                previous["last_height"] = item["last_height"]
                continue
        block_groups.append(
            {
                "rect": pymupdf.Rect(item["rect"]),
                "cover_rects": list(item["cover_rects"]),
                "sizes": list(item["sizes"]),
                "colors": dict(item["colors"]),
                "text_parts": [item["text"]],
                "key": item["key"],
                "last_x0": item["last_x0"],
                "last_x1": item["rect"].x1,
                "last_mid": (item["rect"].x0 + item["rect"].x1) / 2,
                "last_y1": item["last_y1"],
                "last_height": item["last_height"],
            }
        )

    line_starts = [rect.x0 for item in items for rect in item["cover_rects"]]
    left_margin = _median(line_starts) if len(line_starts) >= 3 else None

    for group in block_groups:
        text = " ".join(group["text_parts"])
        text = re.sub(r"(?<=[A-Za-z])-\s+(?=[a-z])", "", text)
        text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        bbox = group["rect"]
        font_size = min(max(_median(group["sizes"]), 5.0), 36.0)
        colors = group["colors"]
        color_int = max(colors, key=colors.get) if colors else 0
        groups.append(
            TextGroup(
                bbox=bbox,
                text=text,
                font_size=font_size,
                color=_color_from_int(color_int),
                align=_detect_align(
                    group["cover_rects"], page_width, font_size, left_margin
                ),
                line_rects=list(group["cover_rects"]),
                cover_rects=[_expand(rect) for rect in group["cover_rects"]],
                translatable=is_translatable(text),
            )
        )
    return groups


def _get_ocr_engine():
    global _OCR_ENGINE
    with _OCR_LOCK:
        if _OCR_ENGINE is None:
            from rapidocr_onnxruntime import RapidOCR

            _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def _join_ocr_text(texts: list[str]) -> str:
    joined = " ".join(texts)
    cjk = len(CJK_RE.findall(joined))
    latin = len(re.findall(r"[A-Za-z]", joined))
    if cjk and cjk / max(cjk + latin, 1) > 0.3:
        return "".join(texts)
    return joined


def _ocr_groups(
    page: pymupdf.Page, log: Callable[[str], None]
) -> tuple[
    list[TextGroup],
    list[tuple[str, float, tuple[float, float, float, float] | None]],
]:
    """返回（文本组, 被丢掉的低置信行）。

    低置信行过去是 ``continue`` 一句话带过：一页合同里少了半行金额，用户看到的
    是「翻得挺好」。现在把（原文片段, 置信度, 页内位置）原样交给调用方，逐条进
    《自检报告》（v1.9.5 起按页汇总，v1.9.8 起带页内相对位置）。
    """
    import numpy as np

    if _OCR_ENGINE is None:
        log("正在加载 OCR 识别引擎…")
    engine = _get_ocr_engine()
    pixmap = page.get_pixmap(dpi=OCR_DPI, alpha=False)
    array = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
        pixmap.height, pixmap.width, pixmap.n
    )
    if pixmap.n == 4:
        array = array[:, :, :3]
    image = np.ascontiguousarray(array[:, :, ::-1])
    result, _ = engine(image)
    del pixmap, array, image
    if not result:
        return [], []

    scale = 72.0 / OCR_DPI

    lines: list[tuple[pymupdf.Rect, str]] = []
    dropped: list[tuple[str, float, tuple[float, float, float, float] | None]] = []
    for item in result:
        try:
            box, text, score = item[0], item[1], float(item[2])
        except Exception:
            continue
        text = (text or "").strip()
        if not text:
            continue
        if score < OCR_MIN_SCORE:
            rect = box_rect(box)
            if rect is not None:
                rect = tuple(round(value * scale, 2) for value in rect)
            dropped.append((text, score, rect))
            continue
        xs = [point[0] for point in box]
        ys = [point[1] for point in box]
        rect = pymupdf.Rect(
            min(xs) * scale,
            min(ys) * scale,
            max(xs) * scale,
            max(ys) * scale,
        )
        if rect.width <= 1 or rect.height <= 1:
            continue
        text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
        text = _fix_ocr_words(text)
        lines.append((rect, text))

    lines.sort(key=lambda item: (round(item[0].y0, 1), item[0].x0))
    column_width: dict[int, float] = {}
    for rect, _ in lines:
        key = int(round(rect.x0 / 5.0))
        column_width[key] = max(column_width.get(key, 0.0), rect.width)

    raw_groups: list[dict] = []
    for rect, text in lines:
        column = int(round(rect.x0 / 5.0))
        if raw_groups:
            group = raw_groups[-1]
            gap = rect.y0 - group["bbox"].y1
            previous_rect, previous_text = group["lines"][-1]
            width = max(
                column_width.get(group["column"], previous_rect.width),
                previous_rect.width,
            )
            full_line = previous_rect.width >= width * 0.72
            ends_sentence = bool(
                re.search(r"[.!?;:。！？；：]$", previous_text)
            )
            limit = max(3.0, previous_rect.height * 0.55)
            if (
                column == group["column"]
                and gap <= limit
                and full_line
                and not ends_sentence
            ):
                group["lines"].append((rect, text))
                group["bbox"] |= rect
                group["heights"].append(rect.height)
                continue
        raw_groups.append(
            {
                "bbox": pymupdf.Rect(rect),
                "lines": [(rect, text)],
                "heights": [rect.height],
                "column": column,
            }
        )

    groups: list[TextGroup] = []
    ocr_left_margin = (
        _median([rect.x0 for rect, _ in lines]) if len(lines) >= 3 else None
    )
    for group in raw_groups:
        bbox = group["bbox"]
        median_height = _median(group["heights"])
        text = _join_ocr_text([line_text for _, line_text in group["lines"]])
        font_size = min(max(median_height * 0.82, 5.0), 24.0)
        line_rects = [rect for rect, _ in group["lines"]]
        groups.append(
            TextGroup(
                bbox=bbox,
                text=text,
                font_size=font_size,
                color=(0.0, 0.0, 0.0),
                align=_detect_align(
                    line_rects, page.rect.width, font_size, ocr_left_margin
                ),
                line_rects=list(line_rects),
                cover_rects=[_expand(rect, 1.5) for rect in line_rects],
                translatable=is_translatable(text),
            )
        )
    return groups, dropped


def _clean_scanned_page(
    page: pymupdf.Page, groups: list[TextGroup]
) -> bytes | None:
    # Remove the source-language ink and rebuild the paper underneath, so
    # Chinese can be laid over a clean scan without visible white patches.
    try:
        import cv2
        import numpy as np

        pixmap = page.get_pixmap(dpi=OCR_DPI, alpha=False)
        image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
            pixmap.height, pixmap.width, pixmap.n
        )[:, :, :3].copy()
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        scale_x = pixmap.width / max(page.rect.width, 1.0)
        scale_y = pixmap.height / max(page.rect.height, 1.0)

        # Keep table rules and other long straight lines.
        dark = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)[1]
        horizontal = cv2.morphologyEx(
            dark, cv2.MORPH_OPEN, np.ones((1, 60), np.uint8)
        )
        vertical = cv2.morphologyEx(
            dark, cv2.MORPH_OPEN, np.ones((60, 1), np.uint8)
        )
        rules = cv2.dilate(
            horizontal | vertical, np.ones((3, 3), np.uint8), iterations=1
        )

        mask = np.zeros_like(gray)
        for group in groups:
            bbox = group.bbox
            x_edge0 = int(bbox.x0 * scale_x)
            x_edge1 = int(bbox.x1 * scale_x)
            y0 = max(0, int(bbox.y0 * scale_y))
            y1 = min(pixmap.height, int(bbox.y1 * scale_y))
            in_table = False
            if bbox.width < 250:
                band = int(14 * scale_x)
                for edge in (x_edge0, x_edge1):
                    xa = max(0, edge - band)
                    xb = min(pixmap.width, edge + band)
                    if vertical[y0:y1, xa:xb].any():
                        in_table = True
                        break

            for rect in group.cover_rects:
                if in_table:
                    area = _expand(rect, 2.0)
                    x0 = max(0, int(area.x0 * scale_x) - 2)
                    x1 = min(pixmap.width, int(area.x1 * scale_x) + 2)
                    ry0 = max(0, int(area.y0 * scale_y) - 2)
                    ry1 = min(pixmap.height, int(area.y1 * scale_y) + 2)
                    region = gray[ry0:ry1, x0:x1]
                    if region.size == 0:
                        continue
                    background = np.percentile(region, 80)
                    difference = np.abs(
                        region.astype(np.int16) - int(background)
                    )
                    local = (difference > 28).astype(np.uint8) * 255
                    mask[ry0:ry1, x0:x1] = np.maximum(
                        mask[ry0:ry1, x0:x1], local
                    )
                else:
                    x0 = max(0, int(rect.x0 * scale_x) - 1)
                    x1 = min(pixmap.width, int(rect.x1 * scale_x) + 1)
                    ry0 = max(0, int(rect.y0 * scale_y) - 1)
                    ry1 = min(pixmap.height, int(rect.y1 * scale_y) + 1)
                    mask[ry0:ry1, x0:x1] = 255

        mask[rules > 0] = 0
        mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=2)
        mask[rules > 0] = 0
        cleaned = cv2.inpaint(image, mask, 4, cv2.INPAINT_TELEA)
        ok, encoded = cv2.imencode(
            ".png", cv2.cvtColor(cleaned, cv2.COLOR_RGB2BGR)
        )
        if not ok:
            return None
        return encoded.tobytes()
    except Exception:
        return None


def _is_scanned(page: pymupdf.Page) -> bool:
    text = page.get_text("text").strip()
    if len(text) >= 20:
        return False
    area = page.rect.width * page.rect.height
    if area <= 0:
        return False
    covered = 0.0
    try:
        for info in page.get_image_info():
            rect = pymupdf.Rect(info["bbox"]) & page.rect
            if rect.width > 0 and rect.height > 0:
                covered += rect.width * rect.height
    except Exception:
        return False
    return covered > area * 0.5


def _luminance(color: tuple[float, float, float]) -> float:
    return color[0] * 0.2126 + color[1] * 0.7152 + color[2] * 0.0722


def _sample_cover_colors(
    page: pymupdf.Page, groups: list[TextGroup]
) -> None:
    # Fill patches with the local background colour so table shading and
    # scanned paper tone are preserved.
    targets = [group for group in groups if group.translatable]
    if not any(group.cover_rects for group in targets):
        return
    try:
        import numpy as np

        pixmap = page.get_pixmap(dpi=110, alpha=False)
        pixels = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
            pixmap.height, pixmap.width, pixmap.n
        )
        if pixmap.n >= 3:
            pixels = pixels[:, :, :3]
    except Exception:
        return
    scale_x = pixmap.width / max(page.rect.width, 1.0)
    scale_y = pixmap.height / max(page.rect.height, 1.0)

    def sample(rect: pymupdf.Rect) -> tuple[float, float, float]:
        area = _expand(rect, 2.5)
        x0 = max(0, min(pixmap.width - 1, int(area.x0 * scale_x)))
        x1 = max(x0, min(pixmap.width - 1, int(area.x1 * scale_x)))
        y0 = max(0, min(pixmap.height - 1, int(area.y0 * scale_y)))
        y1 = max(y0, min(pixmap.height - 1, int(area.y1 * scale_y)))
        block = pixels[y0 : y1 + 1, x0 : x1 + 1].reshape(-1, 3)
        if block.size == 0:
            return (1.0, 1.0, 1.0)
        block = block.astype(np.int32)
        keys = (block[:, 0] // 16) * 256 + (block[:, 1] // 16) * 16 + (
            block[:, 2] // 16
        )
        counts = np.bincount(keys, minlength=4096)
        dominant = keys == int(counts.argmax())
        values = block[dominant].mean(axis=0)
        return tuple(float(value) / 255.0 for value in values)

    for group in targets:
        group.cover_colors = [sample(rect) for rect in group.cover_rects]


def _fit_size(
    rect: pymupdf.Rect,
    text: str,
    start_size: float,
    leading: float,
    min_size: float = MIN_FONT_SIZE,
) -> float | None:
    """二分找出该文本框装得下的最大字号。字号越大行越多、每行越高，
    占高对字号单调不减，所以 5～36 半磅一档最多试 7 次。
    纯算术（用 Font.text_length 量宽度），不再往临时页里真写一遍字。"""
    width = rect.x1 - rect.x0
    height = rect.y1 - rect.y0
    if width < 6 or height < 6:
        return None
    top = min(max(start_size, MIN_FONT_SIZE), 36.0)
    floor = max(min_size, MIN_FONT_SIZE)
    if floor > top:
        return None
    low, high = int(round(floor * 2)), int(round(top * 2))
    best = None
    while low <= high:
        middle = (low + high) // 2
        size = middle / 2
        count = len(_wrap_lines(text, size, width))
        if _needed_height(count, size, leading) <= height + 0.01:
            best = size
            low = middle + 1
        else:
            high = middle - 1
    return best

def _body_font_size(groups: list["TextGroup"]) -> float:
    """按字数取众数字号（0.5pt 一档），作为正文基准字号。整份文档算一次，
    各页正文才会统一；只在单页内取众数会导致这页 13pt、下页 14pt。"""
    weights: dict[float, float] = {}
    for group in groups:
        key = round(group.font_size * 2) / 2
        weights[key] = weights.get(key, 0.0) + len(group.text)
    if not weights:
        return 10.0
    return max(weights, key=lambda key: weights[key])


def _unify_size(size: float, body: float) -> float:
    """与正文相差 2pt 以内的字号并到正文字号，消掉同一页正文忽大忽小；
    明显更大的标题和明显更小的脚注保留自己的字号。"""
    return body if abs(size - body) <= 2.0 else size


def _shares_column(first: pymupdf.Rect, second: pymupdf.Rect) -> bool:
    return min(first.x1, second.x1) - max(first.x0, second.x0) > 1.0


def _shares_row(first: pymupdf.Rect, second: pymupdf.Rect) -> bool:
    return min(first.y1, second.y1) - max(first.y0, second.y0) > 1.0


def _widen(
    rect: pymupdf.Rect,
    page_rect: pymupdf.Rect,
    occupied: list[pymupdf.Rect],
    align: int,
) -> pymupdf.Rect:
    """源框只有一行原文那么宽时，中文按这个宽度折行会被压到八九磅
    （`IAS 2` → 「国际会计准则第2号」）。把框撑到本栏文字的最右边界：
    左边界取与本行同一左缩进的行，右边界被同一行里的别的文字挡住就退到它跟前。"""
    right = rect.x1
    left = rect.x0
    for other in occupied:
        if abs(other.x0 - rect.x0) <= 2.0:
            right = max(right, other.x1)
        if abs(other.x1 - rect.x1) <= 2.0:
            left = min(left, other.x0)
    right = min(right, page_rect.x1 - 6)
    left = max(left, page_rect.x0 + 6)
    for other in occupied:
        if not _shares_row(other, rect):
            continue
        if other.x0 > rect.x1 - 1:
            right = min(right, other.x0 - 1)
        if other.x1 < rect.x0 + 1:
            left = max(left, other.x1 + 1)
    if align == 0:
        left = rect.x0
    elif align == 2:
        right = rect.x1
    if right - left <= rect.x1 - rect.x0 + 1.0:
        return rect
    return pymupdf.Rect(left, rect.y0, right, rect.y1)


def _column_limit(
    rect: pymupdf.Rect, page_rect: pymupdf.Rect, occupied: list[pymupdf.Rect]
) -> float:
    """同栏内可以占到的最低位置：页边，或者下一条文字自己的行首。"""
    limit = page_rect.y1 - 6
    for other in occupied:
        if other.y0 >= rect.y1 - 1 and _shares_column(other, rect):
            limit = min(limit, other.y0 - 1)
    return limit


def _push_below_overlaps(
    rect: pymupdf.Rect,
    page_rect: pymupdf.Rect,
    occupied: list[pymupdf.Rect],
    skip: list[pymupdf.Rect],
) -> pymupdf.Rect:
    """源文件里本来就有两组文字叠在一起（叠印的页眉、重复的图层），照原位排版必然压字。
    把这一组整体挪到压着它的那组文字下方；下面也没地方可挪时保持原位。"""
    height = rect.height
    target = None
    for other in occupied:
        if any(other is item for item in skip) or other.y1 <= rect.y0:
            continue
        inter = rect & other
        if inter.is_empty or not _shares_column(rect, other):
            continue
        if inter.get_area() < min(rect.get_area(), other.get_area()) * 0.25:
            continue
        if target is None or other.y1 > target:
            target = other.y1
    if target is None:
        return rect
    moved = pymupdf.Rect(rect.x0, target + 1, rect.x1, target + 1 + height)
    if moved.y1 > page_rect.y1 - 6:
        return rect
    return moved


def _roomier_rects(rect: pymupdf.Rect, limit: float, step: float, steps: int):
    """把文本框向下延长，借用本段与下一段之间的空白；下边界由调用方算好（limit）。"""
    yield rect
    if limit <= rect.y1:
        return
    grown = pymupdf.Rect(rect)
    for _ in range(steps):
        if grown.y1 >= limit:
            return
        grown.y1 = min(grown.y1 + step, limit)
        yield pymupdf.Rect(grown)


def _fit_group(
    group: "TextGroup",
    text: str,
    start_size: float,
    page_rect: pymupdf.Rect,
    occupied: list[pymupdf.Rect],
) -> tuple[pymupdf.Rect, float | None]:
    """先保住整页统一的正文字号：原框装不下就向下借空白再试原字号，
    借不到才允许缩字号。返回实际使用的文本框。"""
    # 一行中文的可见墨迹只要 ~1.18em（基线以上 0.833 + 以下 0.125，加 0.225 的
    # 顶部空隙），源文件自己的行距就有 1.2em，所以原字号本来就放得下，不必再向
    # 下借行框之间的空白；借了反而会压到下一行。真正放不下的是「行数」——中文虽
    # 短但每字 1em，宽框里一行装得下，窄框就要折行，这时才缩字号。
    moved = _push_below_overlaps(
        pymupdf.Rect(group.bbox),
        page_rect,
        occupied,
        group.line_rects or [group.bbox],
    )
    leading = _line_leading(group)
    moved = _widen(moved, page_rect, occupied, group.align)
    limit = min(_column_limit(moved, page_rect, occupied), page_rect.y1 - 4.0)
    need = _needed_height(1, start_size, leading)
    if limit < moved.y0 + need:
        # 下一行贴得太紧时宁可多占半行，也不能整段不译——不译就是把英文留在成品里。
        limit = moved.y0 + need
    base = pymupdf.Rect(moved)
    base.y1 = max(base.y1, moved.y0 + need)
    step = max(2.0, leading * start_size)
    rects = list(_roomier_rects(base, limit, step, 6))
    best: tuple[pymupdf.Rect, float] | None = None
    for rect in rects:
        size = _fit_size(rect, text, start_size, leading)
        if size is None:
            continue
        if best is None or size > best[1]:
            best = (rect, size)
        if best[1] >= start_size:
            # 最小的够用框就能保住原字号，不必再往下借空白。
            break
    if best is None:
        return group.bbox, None
    rect, size = best
    if size < start_size:
        # 必须缩号时按整磅落档，避免同一页混出 9.5 / 10 / 10.5 三种正文。
        # fit 返回的是「装得下的最大字号」，向下取整必然仍装得下。
        size = max(MIN_FONT_SIZE, float(int(size)))
    return rect, size


def _translation_color(group: TextGroup) -> tuple[float, float, float]:
    color = group.color
    if group.cover_colors:
        background = tuple(
            sum(item[channel] for item in group.cover_colors)
            / len(group.cover_colors)
            for channel in range(3)
        )
        if abs(_luminance(color) - _luminance(background)) < 0.22:
            color = (
                (1.0, 1.0, 1.0)
                if _luminance(background) < 0.5
                else (0.0, 0.0, 0.0)
            )
    return color


def _insert_translation_text(
    page: pymupdf.Page,
    group: TextGroup,
    rect: pymupdf.Rect,
    offset_x: float,
    text: str,
    size: float,
) -> bool:
    """自己折行、自己按基线落版。用 insert_textbox 会让整页正文被压小一到两号
    （它按字体名义行框 1.32em 预留高度，源文件只有 1.2em），所以这里直接写 TextWriter。"""
    font = _type_font()
    width = rect.x1 - rect.x0
    lines = _wrap_lines(text, size, width)
    if not lines:
        return False
    leading = _line_leading(group)
    if _needed_height(len(lines), size, leading) > rect.y1 - rect.y0 + 0.01:
        return False
    writer = pymupdf.TextWriter(page.rect)
    baseline = rect.y0 + font.ascender * size
    for line in lines:
        line_width = font.text_length(line, fontsize=size)
        if group.align == 1:
            x = rect.x0 + max(0.0, (width - line_width) / 2)
        elif group.align == 2:
            x = rect.x1 - line_width
        else:
            x = rect.x0
        try:
            writer.append(
                pymupdf.Point(offset_x + x, baseline), line, font=font, fontsize=size
            )
        except Exception:
            return False
        baseline += leading * size
    writer.write_text(page, color=_translation_color(group))
    return True


def _draw_translation(
    page: pymupdf.Page,
    group: TextGroup,
    rect: pymupdf.Rect,
    offset_x: float,
    text: str,
    size: float,
) -> bool:
    for index, cover in enumerate(group.cover_rects):
        cover_rect = pymupdf.Rect(
            cover.x0 + offset_x,
            cover.y0,
            cover.x1 + offset_x,
            cover.y1,
        )
        if index < len(group.cover_colors):
            fill = group.cover_colors[index]
        else:
            fill = (1.0, 1.0, 1.0)
        page.draw_rect(
            cover_rect,
            color=None,
            fill=fill,
            overlay=True,
        )
    return _insert_translation_text(page, group, rect, offset_x, text, size)


def _link_still_present(link: dict, links: list[dict]) -> bool:
    """这个链接在擦除后的页面里是否还在。

    红action 对链接只有「整条删」和「原样留」两种结果（留着的坐标不变），
    所以按「同种类 + 同目标 + 矩形相交」判定即可，不必逐字节比对。
    """
    rect = link.get("from")
    if rect is None:
        return True
    rect = pymupdf.Rect(rect)
    for other in links:
        if other.get("kind") != link.get("kind"):
            continue
        if other.get("uri") != link.get("uri"):
            continue
        other_rect = other.get("from")
        if other_rect is None:
            continue
        if not (rect & pymupdf.Rect(other_rect)).is_empty:
            return True
    return False


def _restore_redacted_links(
    page: pymupdf.Page,
    kept_links: list[dict],
    log: Callable[[str], None],
    page_number: int,
) -> None:
    """把被红action 误删的链接按原样补回页面。

    MuPDF 的 apply_redactions 会把与擦除区相交的链接标注一并删掉（实测行为，
    与文字批注、表单 widget 不同——后两者能原地幸存）。「链接原样保留」是
    原位重排相对 zh 模式的本质优势（zh 重新拼合页面，链接本来就过不来），
    不能坏在这道擦除上，所以擦除后补记。只补真正丢掉的：没被擦除区碰到的
    链接本来就还在，重复插会出双份。
    """
    if not kept_links:
        return
    surviving = page.get_links()
    missing = [
        link for link in kept_links if not _link_still_present(link, surviving)
    ]
    if not missing:
        return
    failed = 0
    for link in missing:
        try:
            page.insert_link(link)
        except Exception:
            failed += 1
    if failed:
        log(f"第 {page_number} 页有 {failed} 个链接恢复失败，成品中这些链接可能缺失")


def _placement_matrix(
    source: pymupdf.Rect,
    dest: pymupdf.Rect,
) -> pymupdf.Matrix:
    """源页坐标 → 成品页坐标（平移 + 缩放）。

    两种重建版式都把源页整页贴进一个与源页等大的矩形，常规下这就是恒等或纯
    平移；写成通用矩阵是为了 CropBox 起点不在 (0, 0) 的源页不把热区整体挪偏
    （链接坐标跟着 page.rect 走，贴入框的原点恒为 0），也免得日后改版式时
    热区与文字对不上。
    """
    sx = dest.width / source.width if source.width else 1.0
    sy = dest.height / source.height if source.height else 1.0
    return pymupdf.Matrix(
        sx, 0.0, 0.0, sy, dest.x0 - source.x0 * sx, dest.y0 - source.y0 * sy
    )


def _map_link(
    raw: dict,
    src_rect: pymupdf.Rect,
    dest_rect: pymupdf.Rect,
    boxes: dict[int, tuple[int, pymupdf.Rect, pymupdf.Rect]],
    out_index: int,
) -> dict | None:
    """源页上的一条链接 → 成品页上的等价链接；搬不过来的返回 None。

    只有「跳网址」和「跳本文档某页」两类有对应物：启动程序、命名目标、
    跨文件跳转在成品里没有任何可指向的东西，无动作热区 MuPDF 也不接受写入
    （实测 insert_link 抛 ValueError）。
    """
    kind = raw.get("kind")
    matrix = _placement_matrix(src_rect, dest_rect)
    if kind == pymupdf.LINK_URI:
        mapped = dict(raw)
    elif kind == pymupdf.LINK_GOTO:
        if raw.get("remote"):
            return None
        target = raw.get("page")
        target = -1 if target is None else int(target)
        if target < 0:
            # 「跳本页」：成品里页号换了，必须写成显式页号——
            # page=-1 形态 insert_link 直接抛 TypeError（实测），
            # 而显式页号能正常落下。
            mapped = dict(raw, page=out_index)
            target_matrix = matrix
        else:
            hit = boxes.get(target)
            if hit is None:
                return None
            mapped = dict(raw, page=hit[0])
            target_matrix = _placement_matrix(hit[1], hit[2])
        to = raw.get("to")
        if to is not None:
            # to 是目标页坐标，要用目标页的落点映射，不是本页的。
            mapped["to"] = pymupdf.Point(to).transform(target_matrix)
    else:
        return None
    origin = raw.get("from")
    if origin is not None:
        mapped["from"] = pymupdf.Rect(origin).transform(matrix)
    # id / xref 是源文档对象的编号，在成品里没有意义；insert_link 见到 id 会先按
    # 「改现有那一条」处理，剥掉才一定走新增。当前一页对一页撞不上，剥掉是防日后
    # 改版式（多页并一页）时撞车。
    mapped.pop("id", None)
    mapped.pop("xref", None)
    return mapped


def _carry_links(
    doc: pymupdf.Document,
    out: pymupdf.Document,
    boxes: dict[int, tuple[int, pymupdf.Rect, pymupdf.Rect]],
    log: Callable[[str], None],
) -> int:
    """把源页的链接热区搬到重建出来的成品页上，返回搬过去的条数。

    side / zh 都用 show_pdf_page 拼页，而它只搬页面**外观**：一条链接都跟不过
    来（实测成品页 get_links() 为 0）。原文里的超链接与「跳到第 N 页」在对照版
    里全部点不动，是读者一戳就看出来的损失。inplace 不走这里——页面根本没重建，
    链接是原件自己的（被擦除误删的那部分由 _restore_redacted_links 补）。
    """
    carried = skipped = failed = 0
    for src_index, placement in boxes.items():
        out_index, src_rect, dest_rect = placement
        try:
            links = doc[src_index].get_links()
        except Exception:
            links = []
        if not links:
            continue
        page = out[out_index]
        for raw in links:
            mapped = _map_link(raw, src_rect, dest_rect, boxes, out_index)
            if mapped is None:
                skipped += 1
                continue
            try:
                page.insert_link(mapped)
                carried += 1
            except Exception:
                failed += 1
    # 这里不做「插完再读一遍校验」：新建文档的内存页对象在 insert_link 之后
    # 读不回刚插的链接（实测），照 get_links() 判定会全线误报失败。落盘后能读回，
    # 所以只按写入是否抛异常计数，成品以文件为准。
    if failed:
        log(f"{failed} 个链接未能写入成品，成品中这些链接可能缺失")
    if skipped:
        log(
            f"{skipped} 个链接在成品里没有可指向的目标"
            f"（跨文件跳转、启动程序、命名目标），已跳过"
        )
    return carried


#: 「重新拼页」搬不过去的批注类型：表单控件（Widget）带交互状态，Redact 是
#: 擦除指令本身，Popup 是别的批注的附注窗，其余是多媒体/打印标记。它们在成品
#: 里没有对应物，硬搬只会留下空壳，宁可跳过并报数。
_ANNOT_UNSUPPORTED = frozenset(
    {
        "Popup",
        "Widget",
        "Redact",
        "Link",
        "Screen",
        "Movie",
        "PrinterMark",
        "TrapNet",
        "Watermark",
        "3D",
        "Projection",
        "RichMedia",
    }
)

_QUAD_ANNOTS = {
    "Highlight": "add_highlight_annot",
    "Underline": "add_underline_annot",
    "Squiggly": "add_squiggly_annot",
    "StrikeOut": "add_strikeout_annot",
}


def _annot_name(doc: pymupdf.Document, annot: pymupdf.Annot) -> str:
    """批注的 /Name（便签图标名、标准图章名），读不到返回空串。"""
    try:
        kind, value = doc.xref_get_key(annot.xref, "Name")
    except Exception:
        return ""
    if kind != "name" or not value:
        return ""
    return value.lstrip("/")


def _freetext_fontsize(doc: pymupdf.Document, annot: pymupdf.Annot) -> float:
    """从批注的 /DA 里取字号；取不到就按 add_freetext_annot 的缺省 11pt。"""
    try:
        _kind, value = doc.xref_get_key(annot.xref, "DA")
    except Exception:
        return 11.0
    match = re.search(r"/[^\s/]+\s+([\d.]+)\s+Tf", value or "")
    return float(match.group(1)) if match else 11.0


def _set_annot_look(made: pymupdf.Annot, annot: pymupdf.Annot) -> None:
    """按源批注还原颜色、不透明度、边框、打印/缩放标志与作者信息。"""
    colors = annot.colors or {}
    stroke = tuple(colors.get("stroke") or ())
    fill = tuple(colors.get("fill") or ())
    if stroke or fill:
        made.set_colors(stroke=stroke or None, fill=fill or None)
    opacity = annot.opacity
    if opacity is not None and opacity >= 0:
        made.set_opacity(opacity)
    border = annot.border or {}
    width = border.get("width") or 0
    dashes = border.get("dashes") or None
    if width > 0 or dashes:
        made.set_border(width=width or 1.0, dashes=dashes)
    made.set_flags(annot.flags)
    info = annot.info or {}
    payload = {key: info[key] for key in ("title", "subject", "content") if info.get(key)}
    if payload:
        made.set_info(**payload)


def _rebuild_annot(
    doc: pymupdf.Document,
    page: pymupdf.Page,
    annot: pymupdf.Annot,
    matrix: pymupdf.Matrix,
) -> pymupdf.Annot | None:
    """在成品页上按同类型重建一条批注；搬不了的返回 None。

    逐类型重画而不是整对象拷贝：源批注的 /P（所属页）、/Popup（附注窗）与外观
    流 /AP 都指向源文档，把 xref 直接搬进新文档会留下断引用；重画会丢少量样式，
    但每一条都能在阅读器里正常显示，并且哪一类没搬得动可以如实报数。
    """
    name = annot.type[1]
    if name in _ANNOT_UNSUPPORTED:
        return None
    rect = pymupdf.Rect(annot.rect).transform(matrix)
    info = annot.info or {}
    if name == "Text":
        made = page.add_text_annot(rect.tl, info.get("content", ""))
        icon = _annot_name(doc, annot)
        if icon:
            try:
                made.set_name(icon)
            except Exception:
                pass
    elif name in _QUAD_ANNOTS:
        vertices = annot.vertices or []
        quads = []
        for start in range(0, len(vertices), 4):
            chunk = [
                pymupdf.Point(point).transform(matrix)
                for point in vertices[start : start + 4]
            ]
            if len(chunk) == 4:
                quads.append(pymupdf.Quad(chunk))
        if not quads:
            return None
        made = getattr(page, _QUAD_ANNOTS[name])(quads=quads)
    elif name in ("Square", "Circle"):
        adder = page.add_rect_annot if name == "Square" else page.add_circle_annot
        made = adder(rect)
    elif name == "Line":
        vertices = annot.vertices or []
        if len(vertices) < 2:
            return None
        made = page.add_line_annot(
            pymupdf.Point(vertices[0]).transform(matrix),
            pymupdf.Point(vertices[1]).transform(matrix),
        )
        ends = annot.line_ends
        if ends:
            made.set_line_ends(*ends)
    elif name in ("Polygon", "PolyLine"):
        vertices = annot.vertices or []
        if len(vertices) < 2:
            return None
        points = [pymupdf.Point(point).transform(matrix) for point in vertices]
        adder = page.add_polygon_annot if name == "Polygon" else page.add_polyline_annot
        made = adder(points)
    elif name == "Ink":
        strokes = [
            [
                (point.x, point.y)
                for point in (pymupdf.Point(raw).transform(matrix) for raw in stroke_points)
            ]
            for stroke_points in (annot.vertices or [])
        ]
        if not strokes:
            return None
        made = page.add_ink_annot(strokes)
    elif name == "FreeText":
        colors = annot.colors or {}
        stroke = tuple(colors.get("stroke") or ())
        fill = tuple(colors.get("fill") or ())
        made = page.add_freetext_annot(
            rect,
            info.get("content", ""),
            fontsize=_freetext_fontsize(doc, annot),
            text_color=stroke or None,
            fill_color=fill or None,
        )
    elif name == "Caret":
        made = page.add_caret_annot(rect.tl)
    elif name == "Stamp":
        stamp = getattr(pymupdf, f"STAMP_{_annot_name(doc, annot)}", None)
        if stamp is None:
            return None
        made = page.add_stamp_annot(rect, stamp=stamp)
    elif name == "FileAttachment":
        try:
            data = annot.get_file()
            file_info = annot.file_info or {}
        except Exception:
            return None
        if not data:
            return None
        made = page.add_file_annot(
            rect.tl,
            data,
            file_info.get("filename") or "attachment",
            ufilename=file_info.get("ufilename") or None,
            desc=file_info.get("desc") or None,
        )
    else:
        return None
    _set_annot_look(made, annot)
    made.update()
    return made


def _carry_annots(
    doc: pymupdf.Document,
    out: pymupdf.Document,
    boxes: dict[int, tuple[int, pymupdf.Rect, pymupdf.Rect]],
    log: Callable[[str], None],
) -> tuple[int, int]:
    """把重建版式丢掉的批注重画到成品页，返回（搬过去条数, 跳过条数）。

    side / zh 用 show_pdf_page 拼页，只搬页面外观；批注不属于外观，一条都跟不
    过来。合同与审阅稿里的高亮、便签、方框正是读者要的成果，丢了等于把审阅意
    见整份吞掉。inplace 不走这里——页面没重建，批注是原件自己的。
    """
    carried = 0
    skipped: dict[str, int] = {}

    for src_index, placement in boxes.items():
        out_index, src_rect, dest_rect = placement
        # 必须持住源页对象：list(page.annots()) 里的 Annot 是弱引用，页对象一旦
        # 被回收，后面的 annot.rect 会抛 ReferenceError（实测整页批注全被判跳过）。
        src_page = doc[src_index]
        try:
            annots = list(src_page.annots())
        except Exception:
            continue
        if not annots:
            continue
        page = out[out_index]
        matrix = _placement_matrix(src_rect, dest_rect)
        # 表单控件不在 annots() 里，单独清点：重建版式带不过去，不报数就是静默丢。
        try:
            widgets = sum(1 for _ in src_page.widgets())
        except Exception:
            widgets = 0
        if widgets:
            skipped["Widget"] = skipped.get("Widget", 0) + widgets
        for annot in annots:
            try:
                made = _rebuild_annot(doc, page, annot, matrix)
            except Exception:
                made = None
            if made is None:
                name = annot.type[1]
                skipped[name] = skipped.get(name, 0) + 1
            else:
                carried += 1
    if skipped:
        detail = "、".join(f"{name} {count} 条" for name, count in sorted(skipped.items()))
        log(f"{sum(skipped.values())} 条批注在成品里没有对应物（{detail}），已跳过")
    return carried, sum(skipped.values())


#: 成品形态。side = 左原文 / 右中文（页面宽度翻倍，逐段对着看直观）；
#: zh   = 只要中文版（页面尺寸与源文件一致，适合打印与传阅）；
#: inplace = 原位重排（决策 24 否决交错式重排后的「谨慎版」）：不新建文档拼合，
#: 直接在**原文档**上擦除原文文字、原位排入中文。页面尺寸、链接、书签、批注、
#: 表单、表格底纹与矢量图全部原样保留——当年否决重排的理由正是「丢表格底纹与
#: 矢量图」，本版式显式关掉红action 的图像/线稿擦除来兑现保真承诺。只处理
#: 文字版页面；扫描页没有文字层可换，整页保留不动（也不跑 OCR）。
LAYOUTS = ("side", "zh", "inplace")

#: 加密 PDF 的两种失败提示。界面层会在开跑前先问到密码，所以正常走不到这里；
#: 命令行批量时才会看到，直接给人话。
PDF_PASSWORD_REQUIRED = (
    "这个 PDF 有密码保护，需要先用 --password=你的密码 提供打开密码"
    "（或在 Acrobat 里另存一份无密码的副本）。"
)
PDF_PASSWORD_WRONG = (
    "这个 PDF 的密码不对，请重新输入，或用原密码在 Acrobat 里另存一份无密码的副本。"
)


def _remap_toc(
    toc: list[list],
    link_boxes: dict[int, tuple[int, pymupdf.Rect, pymupdf.Rect]],
) -> tuple[list[list], int]:
    """按「源页号 → 成品页号」改写书签目标页，返回（改写后的书签, 舍弃条数）。

    页范围会把成品裁成源文件的一部分，书签原样复制进去就会跳到别的章节，或者指向
    一个不存在的页号——PyMuPDF 不报错，读者在阅读器里看到的是「点了没反应」。
    层级也要收敛：父条目被舍弃后子条目会凭空深一级，`set_toc` 对这种跳级不保证买账。
    """
    kept: list[list] = []
    dropped = 0
    previous_level = 0
    for entry in toc:
        page = entry[2] if len(entry) > 2 else 0
        placement = link_boxes.get(page - 1) if page and page > 0 else None
        if placement is None:
            dropped += 1
            continue
        rewritten = list(entry)
        rewritten[2] = placement[0] + 1
        level = max(1, min(int(entry[0] or 1), previous_level + 1))
        rewritten[0] = level
        previous_level = level
        kept.append(rewritten)
    return kept, dropped


def translate_pdf(
    src_path: str,
    translator: Translator,
    progress: Callable[[float, int, str], None] | None = None,
    log: Callable[[str], None] | None = None,
    ocr_enabled: bool = True,
    layout: str = "side",
    password: str | None = None,
    pages: str | None = None,
) -> str:
    log = log or (lambda message: None)
    progress = progress or (lambda done, total, message: None)
    if layout not in LAYOUTS:
        layout = "side"
    zh_only = layout == "zh"
    inplace = layout == "inplace"

    doc = pymupdf.open(src_path)
    out = None
    try:
        if doc.needs_pass and not doc.authenticate(password or ""):
            raise TranslationError(
                PDF_PASSWORD_REQUIRED if not password else PDF_PASSWORD_WRONG
            )
        page_count = doc.page_count
        if page_count == 0:
            raise TranslationError("这个 PDF 没有页面内容。")

        if pages and pages.strip():
            selected = parse_pages(pages, page_count)
            selected_count = len(selected)
            log(
                f"页范围：按「{pages.strip()}」只处理第 {format_pages(selected)} 页"
                f"（{selected_count} 页，全文 {page_count} 页）。"
            )
            if inplace:
                log("原位重排下未选中的页面原样保留在成品里。")
            else:
                log("左右对照/仅中文是重建页面：成品里只有选中的这些页。")
        else:
            selected = list(range(page_count))
            selected_count = page_count

        progress(0, 100, "正在分析 PDF 版面…")
        scanned_count = 0
        #: 按页收集 OCR 丢掉的低置信行：页号 + [(原文片段, 置信度, 页内矩形)] + 页面宽高。
        #: 汇总进《自检报告》，不再让半行金额凭空消失（v1.9.5；v1.9.8 起带页内位置）。
        ocr_drops: list[
            tuple[
                int,
                list[tuple[str, float, tuple[float, float, float, float] | None]],
                tuple[float, float],
            ]
        ] = []
        page_infos: list[PageInfo] = []
        for position, index in enumerate(selected, start=1):
            check_cancel(translator.stop_event)
            page = doc[index]
            if page.rotation:
                log("提示：文档包含旋转页面，个别译文位置可能略有偏移。")
            if _is_scanned(page):
                scanned_count += 1
                if inplace:
                    # 原位重排在原文档上做文本级替换，扫描页没有文字层可擦除，
                    # 整页保留；也不跑 OCR——开了也用不上，顺带省时间。
                    log(
                        f"第 {index + 1} 页是扫描件，原位重排只处理文字版页面，"
                        f"已保留原页"
                    )
                    groups = []
                    mode = "copy"
                elif ocr_enabled:
                    try:
                        groups, dropped = _ocr_groups(page, log)
                        mode = "ocr"
                        if dropped:
                            ocr_drops.append(
                                (index + 1, dropped, (page.rect.width, page.rect.height))
                            )
                            log(
                                f"第 {index + 1} 页有 {len(dropped)} 处文字识别不清，"
                                f"已跳过（详见自检报告）"
                            )
                    except Exception as exc:
                        log(f"第 {index + 1} 页 OCR 失败（{type(exc).__name__}），保留原页")
                        groups = []
                        mode = "copy"
                else:
                    log(f"第 {index + 1} 页是扫描件，已跳过 OCR（保留原页）")
                    groups = []
                    mode = "copy"
            else:
                groups = _extract_text_groups(page)
                mode = "text" if groups else "copy"
            if groups:
                _sample_cover_colors(page, groups)
            page_infos.append(PageInfo(index=index, mode=mode, groups=groups))
            progress(
                position / selected_count * 40,
                100,
                f"正在分析版面 {position}/{selected_count} 页",
            )

        refs: list[tuple[int, int]] = []
        texts: list[str] = []
        run_refs: list[tuple[int, int, int]] = []
        run_texts: list[str] = []
        for position, page_info in enumerate(page_infos):
            # refs 里存的是「第几个 PageInfo」而不是页号：裁页后 page_infos 不再是
            # 全部页面，两者会从「恰好相等」变成「随机错位」。
            for group_index, group in enumerate(page_info.groups):
                if group.translatable:
                    refs.append((position, group_index))
                    texts.append(group.text)
                    continue
                if direction.is_english():
                    # 英文方向的「不可译段」就是本来就该保留的英文段；
                    # _mixed_runs 是中文方向的补偿（整段判成中文被跳过），
                    # 这边再抠子句只会把同一句英文送一遍翻译。
                    continue
                for run_index, run in enumerate(_mixed_runs(group.text)):
                    group.runs.append(run)
                    run_refs.append((position, group_index, run_index))
                    run_texts.append(run)
        pending_count = len(refs) + len(run_refs)

        if not pending_count:
            if inplace and scanned_count and not any(
                info.mode == "text" for info in page_infos
            ):
                # 全扫描文档：原位重排无从下手。说清楚换哪条路走，
                # 别落到「没有找到需要翻译的外文内容」这种文不对题的消息上。
                raise TranslationError(
                    "这份 PDF 没有文字版页面（全是扫描件），原位重排只处理文字版"
                    "页面；可改用「仅中文」版式并开启 OCR。"
                )
            if not inplace and scanned_count and not ocr_enabled:
                raise TranslationError("这份 PDF 是扫描件，需要开启 OCR 才能翻译。")
            if selected_count != page_count:
                raise TranslationError(
                    "没有找到需要翻译的外文内容"
                    f"（页范围「{pages.strip()}」只选中了 {selected_count} 页，"
                    "可以确认一下这些页是不是空白页或纯图片页）。"
                )
            raise TranslationError("没有找到需要翻译的外文内容。")

        scope = (
            f"共处理 {selected_count} 页（全文 {page_count} 页）"
            if selected_count != page_count
            else f"共 {page_count} 页"
        )
        log(f"{scope}，其中扫描页 {scanned_count} 页，需要翻译 {pending_count} 段文字。")

        def translation_progress(done: int, total: int, _message: str) -> None:
            progress(
                40 + done / max(total, 1) * 40,
                100,
                f"正在翻译 {done}/{total} 段",
            )

        translations = translator.translate_texts(
            texts + run_texts, progress=translation_progress
        )
        effective = 0
        for (slot, group_index), translated in zip(refs, translations[: len(refs)]):
            group = page_infos[slot].groups[group_index]
            group.translation = translated
            if translated.strip() and normalize_source(translated) != normalize_source(
                group.text
            ):
                effective += 1
        for (slot, group_index, run_index), translated in zip(
            run_refs, translations[len(refs):]
        ):
            group = page_infos[slot].groups[group_index]
            run = group.runs[run_index]
            if translated.strip() and normalize_source(translated) != normalize_source(run):
                group.run_map[run] = translated
                effective += 1
        if effective == 0:
            raise TranslationError("未能返回有效译文，请稍后重试。")

        progress(
            80,
            100,
            "正在原位生成中文版 PDF…"
            if inplace
            else ("正在生成中文版 PDF…" if zh_only else "正在生成左右对照 PDF…"),
        )
        # 原位重排不新建文档：直接在 doc 上改完另存，链接/书签/批注/表单才留得住。
        if not inplace:
            out = pymupdf.open()
        # 重建版式专用：源页号 → (成品页号, 源页矩形, 源页外观落在成品页的哪个框)。
        # 循环结束后按这张表搬链接，见 _carry_links。
        link_boxes: dict[int, tuple[int, pymupdf.Rect, pymupdf.Rect]] = {}
        carried_links = 0
        carried_annots = 0
        skipped_annots = 0
        fit_failures = 0
        # 原位重排专用：段2（放译文）失败计数。与 fit_failures 分开——
        # fit_failures 是「装不下、主动不擦」，place_failures 是「已擦除、
        # 译文没放上」，后者是内容损失，必须按「处」进完成消息与自检报告。
        place_failures = 0
        place_failure_pages: list[int] = []
        drawn = 0
        echoed = 0
        # 正文基准字号按整份文档算一次：按单页取众数会让这页 13pt、下页 14pt，
        # 读者看到的是「整本忽大忽小」，比同一页里差半磅更显眼。
        page_candidates: list[list[tuple[TextGroup, str]]] = []
        for page_info in page_infos:
            candidates: list[tuple[TextGroup, str]] = []
            for group in page_info.groups:
                if not group.translatable:
                    if not group.run_map:
                        continue
                    mixed = group.text
                    for run, chinese in group.run_map.items():
                        mixed = mixed.replace(run, chinese, 1)
                    translated = _swap_uncovered(mixed.strip())
                else:
                    translated = _swap_uncovered(group.translation.strip())
                if not translated:
                    continue
                if normalize_source(translated) == normalize_source(group.text):
                    echoed += 1
                    continue
                candidates.append((group, translated))
            page_candidates.append(candidates)
        body = _body_font_size(
            [group for items in page_candidates for group, _ in items]
        )

        for done, (page_info, candidates) in enumerate(
            zip(page_infos, page_candidates), start=1
        ):
            check_cancel(translator.stop_event)
            page = doc[page_info.index]
            width = page.rect.width
            height = page.rect.height
            right_x = width + RIGHT_GAP

            occupied: list[pymupdf.Rect] = []
            for group in page_info.groups:
                occupied.extend(group.line_rects or [group.bbox])

            placements: list[tuple[TextGroup, pymupdf.Rect, str, float]] = []
            for group, translated in candidates:
                start = _unify_size(group.font_size, body)
                rect, size = _fit_group(
                    group, translated, start, page.rect, occupied
                )
                if size is None:
                    fit_failures += 1
                    continue
                placements.append((group, rect, translated, size))
                occupied.append(rect)

            # 同页正文再并一次：这段 18pt、下段 17pt 比整页统一小一号更显眼。
            # 只并到正文档（差 2pt 以内的），表格窄格那种小字保留自己的字号。
            body_sizes = [
                size
                for group, _, _, size in placements
                if _unify_size(group.font_size, body) == body and size >= body - 2.0
            ]
            if body_sizes:
                unified = max(MIN_FONT_SIZE, float(int(min(body_sizes))))
                if unified < body:
                    placements = [
                        (
                            group,
                            rect,
                            item,
                            unified
                            if size > unified
                            and _unify_size(group.font_size, body) == body
                            else size,
                        )
                        for group, rect, item, size in placements
                    ]

            if inplace:
                # 原位重排（谨慎版）：只动文字版页面，直接在原页上擦除原文、
                # 原位排入译文；扫描页（ocr/copy）整体保留——不 OCR、不覆盖、
                # 不重排。红action 显式保留图像与线稿（表格底纹、矢量图不丢），
                # 页面不经过重新拼合，所以尺寸/链接/书签/批注/表单天然保留。
                if page_info.mode != "text" or not placements:
                    continue
                # 段1：擦除原文（不可逆）。这一步失败时原文都还在，可以诚实
                # 地说「保留原页」。QA v1.5.3 复核探针实测本段兜底已完全
                # 兑现，勿改坏。
                try:
                    # 红action 会把与擦除区相交的链接标注一并删掉（MuPDF 实测
                    # 行为），先记下来，擦除后按原样补回——「链接保留」是本版式
                    # 相对 zh 的本质优势，不能坏在这道擦除上。
                    kept_links = page.get_links()
                    for group, _, _, _ in placements:
                        for cover in group.cover_rects:
                            page.add_redact_annot(cover, fill=False)
                    page.apply_redactions(
                        images=pymupdf.PDF_REDACT_IMAGE_NONE,
                        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                    )
                    _restore_redacted_links(
                        page, kept_links, log, page_info.index + 1
                    )
                except Exception as exc:
                    # 擦除失败：原文还在，清掉残留的红action 标注（不 apply 的
                    # 擦除框在阅读器里是黑色标记），该页保留原页——这句话只在
                    # 这里说，因为只有这里它与事实相符。不中断整份文档。
                    # 注意：本段兜底（含文案）经 QA v1.5.3 探针实测兑现，勿改坏。
                    log(
                        f"第 {page_info.index + 1} 页原位替换失败"
                        f"（{type(exc).__name__}），该页保留原页"
                    )
                    try:
                        leftover = [
                            annot
                            for annot in page.annots()
                            if annot.type[0] == pymupdf.PDF_ANNOT_REDACT
                        ]
                        for annot in leftover:
                            page.delete_annot(annot)
                    except Exception:
                        pass
                    progress(
                        80 + done / selected_count * 18,
                        100,
                        f"正在生成第 {page_info.index + 1} 页（{done}/{selected_count}）",
                    )
                    continue
                # 段2：放译文。不可逆的擦除已经发生——这里出任何差池，原文都已
                # 不在页面上，绝不能再写「保留原页」这类与事实矛盾的话；必须
                # 诚实交代内容损失，按「处」计入 place_failures，让完成消息与
                # 自检报告都出声（B2：有用户可见后果的才出声，且不中断整份
                # 文档）。插入链内部对 writer.append 各自兜底返回 False，
                # 逃逸到这里的只有 write_text 级 pymupdf 故障。
                placed = 0
                failure: Exception | None = None
                try:
                    for group, rect, translated, size in placements:
                        if _insert_translation_text(
                            page, group, rect, 0, translated, size
                        ):
                            placed += 1
                except Exception as exc:
                    failure = exc
                drawn += placed
                # 抛异常时后续组也不再尝试，与 append 兜底返回 False 一样：
                # 擦了原文却没放上译文的组，都要向用户交代。
                lost = len(placements) - placed
                if lost:
                    place_failures += lost
                    if page_info.index + 1 not in place_failure_pages:
                        place_failure_pages.append(page_info.index + 1)
                    detail = (
                        f"（{type(failure).__name__}）"
                        if failure is not None
                        else ""
                    )
                    log(
                        f"第 {page_info.index + 1} 页译文放置失败{detail}："
                        f"该页 {lost} 处原文已擦除、译文未能放入，"
                        f"请重跑或改用「仅中文」版式"
                    )
                progress(
                    80 + done / selected_count * 18,
                    100,
                    f"正在生成第 {page_info.index + 1} 页（{done}/{selected_count}）",
                )
                continue

            # For born-digital pages, erase the source text with redactions so
            # table shading and graphics survive untouched, then typeset the
            # Chinese on top. Scanned pages must be patched with cover fills.
            work_doc = None
            redacted = False
            if page_info.mode == "text" and placements:
                try:
                    work_doc = pymupdf.open()
                    work_doc.insert_pdf(
                        doc,
                        from_page=page_info.index,
                        to_page=page_info.index,
                    )
                    work_page = work_doc[0]
                    for group, _, _, _ in placements:
                        for cover in group.cover_rects:
                            work_page.add_redact_annot(cover, fill=False)
                    work_page.apply_redactions(
                        images=pymupdf.PDF_REDACT_IMAGE_NONE,
                        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                    )
                    placed = 0
                    for group, rect, translated, size in placements:
                        if _insert_translation_text(
                            work_page, group, rect, 0, translated, size
                        ):
                            placed += 1
                    drawn += placed
                    redacted = True
                except Exception as exc:
                    log(
                        f"第 {page_info.index + 1} 页文字擦除失败"
                        f"（{type(exc).__name__}），改用底色覆盖"
                    )
                    if work_doc is not None:
                        try:
                            work_doc.close()
                        except Exception:
                            pass
                    work_doc = None
                    redacted = False

            out_index = out.page_count
            if zh_only:
                # 只要中文版：页面尺寸与源文件一致，打印与传阅都不用缩到一半。
                new_page = out.new_page(width=width, height=height)
                right_rect = pymupdf.Rect(0, 0, width, height)
            else:
                new_page = out.new_page(width=width * 2 + RIGHT_GAP, height=height)
                new_page.show_pdf_page(
                    pymupdf.Rect(0, 0, width, height), doc, page_info.index
                )
                right_rect = pymupdf.Rect(
                    right_x, 0, width * 2 + RIGHT_GAP, height
                )
            # 源页外观在成品页上的落点：两种版式都是「与源页等大的框」，
            # zh 是整页、side 是左半页。链接热区按这个框映射过去。
            link_boxes[page_info.index] = (
                out_index,
                page.rect,
                pymupdf.Rect(0, 0, width, height),
            )
            scan_image = None
            if page_info.mode == "ocr" and placements:
                scan_image = _clean_scanned_page(
                    page, [group for group, _, _, _ in placements]
                )
            if redacted and work_doc is not None:
                new_page.show_pdf_page(right_rect, work_doc, 0)
            elif scan_image is not None:
                new_page.insert_image(
                    right_rect, stream=scan_image, keep_proportion=False
                )
                for group, rect, translated, size in placements:
                    if _insert_translation_text(
                        new_page,
                        group,
                        rect,
                        0 if zh_only else right_x,
                        translated,
                        size,
                    ):
                        drawn += 1
                    else:
                        fit_failures += 1
            else:
                new_page.show_pdf_page(right_rect, doc, page_info.index)
                for group, rect, translated, size in placements:
                    if _draw_translation(
                        new_page,
                        group,
                        rect,
                        0 if zh_only else right_x,
                        translated,
                        size,
                    ):
                        drawn += 1
                    else:
                        fit_failures += 1
            if work_doc is not None:
                try:
                    work_doc.close()
                except Exception:
                    pass
            if not zh_only:
                new_page.draw_line(
                    pymupdf.Point(width + RIGHT_GAP / 2, 0),
                    pymupdf.Point(width + RIGHT_GAP / 2, height),
                    color=(0.82, 0.82, 0.82),
                    width=0.7,
                )
            progress(
                80 + done / selected_count * 18,
                100,
                f"正在生成第 {page_info.index + 1} 页（{done}/{selected_count}）",
            )

        try:
            metadata = {
                key: value
                for key, value in (doc.metadata or {}).items()
                if isinstance(value, str)
            }
            title = metadata.get("title") or os.path.splitext(
                os.path.basename(src_path)
            )[0]
            metadata["title"] = (
                f"{title}（中文版）"
                if zh_only or inplace
                else f"{title}（中英对照）"
            )
            if inplace:
                doc.set_metadata(metadata)
            else:
                out.set_metadata(metadata)
        except Exception as exc:
            # 元数据写不进去不影响阅读，但成品会丢掉文件标题。
            log(f"PDF 元数据写入失败（{type(exc).__name__}），成品可能没有标题")
        if not inplace:
            # inplace 的书签天然保留（页面一个都没重建），没有可复制的东西；
            # 若还去调 get_toc，一个与 inplace 无关的故障会打出误导性的
            # 「书签复制失败」，所以整个块在 inplace 下跳过。
            try:
                toc = doc.get_toc(simple=False)
                if toc and selected_count != page_count:
                    remapped, dropped = _remap_toc(toc, link_boxes)
                    if remapped:
                        out.set_toc(remapped)
                    if dropped:
                        log(
                            f"{dropped} 条书签指向页范围之外的页面，成品里没有对应页，"
                            "已从目录中舍弃"
                        )
                elif toc:
                    out.set_toc(toc)
            except Exception as exc:
                # 书签没了是用户一眼能看出来的损失（长文档没有目录很难翻），必须出声。
                log(f"书签复制失败（{type(exc).__name__}），成品可能没有目录")
            carried_links = _carry_links(doc, out, link_boxes, log)
            carried_annots, skipped_annots = _carry_annots(doc, out, link_boxes, log)

        output_path = unique_output_path(
            src_path,
            "原位" if inplace else ("中文" if zh_only else "双语"),
            ".pdf",
        )
        if inplace:
            # 原文档改完直接另存：页面一个都没重建，链接/书签/批注/表单/
            # 页面尺寸全是原件自己的，这就是相对 zh 模式的保真优势。
            try:
                doc.subset_fonts()
            except Exception:
                pass
            doc.save(output_path, garbage=3, deflate=True)
        else:
            try:
                out.subset_fonts()
            except Exception:
                pass
            out.save(output_path, garbage=3, deflate=True)
            try:
                out.close()
            except Exception:
                pass
        progress(100, 100, "完成")
        if inplace:
            message = (
                f"已生成原位重排 PDF：成功放置 {drawn} 段译文；"
                f"{translator.summary()}；页面尺寸、链接、书签、批注原样保留"
            )
        else:
            message = (
                f"已生成{'中文版' if zh_only else '对照'} PDF：成功放置 {drawn} 段译文；"
                f"{translator.summary()}"
            )
            if carried_links:
                # 对照版能点链接是本轮补上的缺口，要说出来，不然用户以为
                # 和以前一样点不动。
                message += f"；{carried_links} 个链接一并保留"
            if carried_annots:
                message += f"；{carried_annots} 条批注一并保留"
            if skipped_annots:
                # 表控件与自定义图章搬不过去；不说，用户会以为审阅成果全在。
                message += (
                    f"；{skipped_annots} 条批注类型在成品里没有对应物，已跳过（详见日志）"
                )
        if fit_failures:
            message += f"；{fit_failures} 处因空间不足保留原文"
        if echoed:
            message += f"；{echoed} 处译文与原文相同，保留原文"
        if place_failures:
            # 原位重排的放置失败是内容损失（原文已擦除），必须进完成消息
            message += f"；{place_failures} 处译文放置失败"
        if inplace and scanned_count:
            message += f"；{scanned_count} 个扫描页已原样保留"
        if ocr_drops:
            dropped_total = sum(len(items) for _page, items, _size in ocr_drops)
            message += f"；{dropped_total} 处文字识别不清已跳过（见自检报告）"

        report = CheckReport()
        pairs: list[tuple[str, str]] = []
        locations: list[str] = []
        #: 译后编辑清单的定位信息（页码 + 组序号）。PDF 成品是绘制文本，
        #: 清单只做疑点定位、不支持回写（apply_edits 对 .pdf 明确出声）。
        pdf_locators: list[dict] = []
        for page_info, items in zip(page_infos, page_candidates):
            for group, translated in items:
                for issue in check_numbers(group.text, translated):
                    report.add(
                        "数字疑点",
                        f"第 {page_info.index + 1} 页",
                        issue.source,
                        issue.detail,
                    )
                pairs.append((group.text, translated))
                locations.append(f"第 {page_info.index + 1} 页")
                pdf_locators.append(
                    {
                        "page": page_info.index,
                        "group": next(
                            (
                                position
                                for position, candidate in enumerate(page_info.groups)
                                if candidate is group
                            ),
                            -1,
                        ),
                    }
                )

        # v1.4.2：译名疑点带上出现位置（页码），报告的 note 里念出来。
        for issue in check_term_consistency(pairs, locations):
            report.add(
                "译名不一致",
                "全文",
                issue.term,
                issue_note(issue),
            )

        if fit_failures:
            report.add("保留原文", "全文", "", f"{fit_failures} 处因空间不足未放译文")
        if echoed:
            report.add("译文与原文相同", "全文", "", f"{echoed} 处")
        if place_failures:
            # 页面内容受损（原文擦了、译文没放上），用户必须知道，级别「高」
            numbers = "、".join(str(number) for number in place_failure_pages)
            report.add(
                "译文放置失败",
                f"第 {numbers} 页" if numbers else "全文",
                "",
                f"{place_failures} 处原文已擦除但译文未能放入，"
                f"请重跑或改用「仅中文」版式",
                severity="高",
            )
        for page_number, dropped, size in ocr_drops:
            for entry in drop_entries(dropped, size, prefix=f"第 {page_number} 页 · "):
                report.add(*entry)
        # 记忆近似复用（v1.9.9）：进本份报告的疑点清单，结论行「N 条」才数得准。
        for entry in fuzzy_entries(tm_fuzzy_of(translator)):
            report.add(*entry)
        quality_issues, pending_terms = quality_payload(translator)
        report_path = build_report(
            output_path,
            report,
            quality_issues,
            pending_terms,
            term_hits_of(translator),
            paid_usage_of(translator),
        )
        if report_path:
            message += f"；自检报告：{shown_path(report_path)}（{report.count} 条疑点）"
        if pairs:
            # v1.5.4 译后编辑闭环：PDF 只进清单做疑点定位，回写不支持
            # （绘制文本没有可寻址段落，apply_edits 会逐条出声）。
            from .postedit import emit_edit_list

            edit_path, _globals = emit_edit_list(
                output_path,
                pairs,
                locations,
                pdf_locators,
                "pdf",
                "f",
                report.issues,
            )
            message += f"；译后编辑清单：{shown_path(edit_path)}（仅定位，不回写）"
        log(message)
        return output_path
    except Exception:
        if out is not None:
            try:
                out.close()
            except Exception:
                pass
        raise
    finally:
        try:
            doc.close()
        except Exception:
            pass
