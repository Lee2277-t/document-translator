"""Galgame 脚本前端：SRT / VTT 字幕。

**不做游戏解包/注入**（Kotonoha 提案的非目标，这里继承）：输入是
GalTransl 生态里已经提取好的格式，输出写回同格式 + 质量标记。

SRT 的结构与坑
--------------
一个块 = 序号行 + 时间轴行 + 一到多行文本。翻译时必须保住：

1. **序号与时间轴原样**——改一个字符，播放器就可能整锅拒载；
2. **行结构**——原文两行译文就两行（行数校验在脚本模式下是 Critical，
   见 core/quality/verifier.py）；
3. **说话人行**——很多 Galgame 字幕导出成 ``桜：你好`` 的形式，
   冒号前是说话人。拆分后说话人喂给质量引擎当 speaker，
   译文里说话人要按原样拼回去（角色名不翻译）。

块与块之间的空行数量不强制（有写 1 行有写 2 行的），按"空行分隔"
解析更稳，输出统一一个空行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .common import TranslationError, shown_path, unique_output_path
from .quality.segments import Segment

#: 时间轴行。SRT：``00:00:01,000 --> 00:00:04,000``；VTT：``00:00:01.000 --> ...``
_TIMING_RE = re.compile(
    r"^\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}"
)
#: 说话人行：``名字：台词``。名字里不允许出现空格与标点，避免把正常台词拆错。
_SPEAKER_RE = re.compile(r"^([^\s:：]{1,12})[:：]\s*(.+)$", re.S)


@dataclass
class SubtitleBlock:
    """一个字幕块。timing 与 index 原样保留，翻译只动 lines。"""

    index: str
    timing: str
    lines: list[str] = field(default_factory=list)
    #: 说话人（若原文是「名字：台词」形式）
    speaker: str = ""
    #: 去掉说话人前缀后的纯台词
    text_lines: list[str] = field(default_factory=list)
    translated: list[str] = field(default_factory=list)

    @property
    def source(self) -> str:
        return "\n".join(self.text_lines)

    def to_segment(self, order: int) -> Segment:
        return Segment(source=self.source, order=order, speaker=self.speaker)

    def render(self) -> str:
        """拼回 SRT 块。说话人按原文形式还原。"""
        body: list[str] = []
        for line in self.translated:
            if self.speaker:
                body.append(f"{self.speaker}：{line}")
            else:
                body.append(line)
        if not body:
            body = list(self.lines)  # 没翻出来时保留原文，不丢块
        return "\n".join([self.index, self.timing, *body])


def parse_srt(text: str) -> list[SubtitleBlock]:
    """解析 SRT/VTT 文本。容错：缺序号行、时间轴格式略脏都能过。"""
    if not text or not text.strip():
        return []
    # VTT 开头可能有 WEBVTT 头，跳过
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[0].strip().upper().startswith("WEBVTT"):
        lines = lines[1:]

    blocks: list[SubtitleBlock] = []
    current: list[str] = []

    def flush() -> None:
        if not current:
            return
        block = _build_block(current)
        if block is not None:
            blocks.append(block)
        current.clear()

    for line in lines:
        if line.strip():
            current.append(line)
        else:
            flush()
    flush()
    return blocks


def _build_block(rows: list[str]) -> SubtitleBlock | None:
    """把连续的非空行组变成一个字幕块。"""
    timing_at = next(
        (position for position, row in enumerate(rows) if _TIMING_RE.match(row.strip())),
        None,
    )
    if timing_at is None:
        return None
    index = rows[0].strip() if timing_at > 0 else ""
    timing = rows[timing_at].strip()
    body = [row for row in rows[timing_at + 1 :]]
    if not body:
        return None

    block = SubtitleBlock(index=index, timing=timing, lines=body)
    for row in body:
        match = _SPEAKER_RE.match(row.strip())
        if match and not block.speaker:
            block.speaker = match.group(1).strip()
            block.text_lines.append(match.group(2).strip())
        else:
            block.text_lines.append(row)
    return block


def render_srt(blocks: Sequence[SubtitleBlock]) -> str:
    """拼回完整 SRT。块间统一一个空行。"""
    chunks = [block.render() for block in blocks]
    return "\n\n".join(chunks) + "\n"


def block_label(block: SubtitleBlock, position: int) -> str:
    """位置串里的块号：优先用文件自己的序号行，没有或不是数字才退回解析顺序。

    报告是让人「照着去文件里找」的。SRT 的序号行可能不从 1 起、可能跳号，VTT 那种干脆没有
    序号行——一律按解析顺序编号，前两种情况下会把人带到错块。
    """
    index = block.index.strip()
    return index if index.isdigit() else str(position)


def translate_srt(
    src_path: str,
    translator,
    log: Callable[[str], None] | None = None,
    progress=None,
) -> str:
    """翻译一个 SRT/VTT 文件，返回成品路径（《原名_双语.srt》）。"""
    log = log or (lambda message: None)
    progress = progress or (lambda done, total, message: None)
    path = Path(src_path)
    text = path.read_text(encoding="utf-8-sig")
    blocks = parse_srt(text)
    if not blocks:
        raise TranslationError("没有解析到任何字幕块，请确认这是 SRT/VTT 文件。")

    speakers = [block.speaker for block in blocks]
    segments = [block.to_segment(order) for order, block in enumerate(blocks)]
    log(f"已解析 {len(blocks)} 个字幕块，开始翻译…")

    # 质量引擎开启时走管线（带说话人，角色卡才能用上）；否则逐块翻译
    pipeline = getattr(translator, "quality_pipeline", None)
    if pipeline is not None:
        from .quality.pipeline import QualityPipeline  # noqa: F401 - 文档用途

        result = pipeline.run(segments, progress)
        translations = result.translations
    else:
        translations = translator.translate_texts(
            [segment.source for segment in segments], progress
        )

    kept_original: list[tuple[str, str, str]] = []
    mismatched: set[int] = set()
    for position, (block, translated) in enumerate(zip(blocks, translations), start=1):
        # 行数对齐：译文多行按行拆回，行数不匹配时整块回退原文
        # （行数校验在脚本模式下是 Critical，宁可保留原文也不丢块）
        rows = [row.strip() for row in translated.split("\n") if row.strip()]
        if len(rows) == len(block.text_lines):
            block.translated = rows
        else:
            block.translated = list(block.text_lines)
            mismatched.add(position)
            kept_original.append(
                (
                    f"块 #{block_label(block, position)}（{block.timing}）",
                    block.source,
                    "译文行数与原文不一致，已保留原文",
                )
            )
            log(f"块 {block.index or '?'} 行数不一致，已保留原文")

    output = unique_output_path(src_path, "双语", path.suffix or ".srt")
    Path(output).write_text(render_srt(blocks), encoding="utf-8")
    log(f"字幕翻译完成：{output}")

    # v1.9.5：字幕过去只有译后编辑清单、没有自检报告。台词里的数字与角色译名
    # 恰恰是最容易出错、又最不容易被人逐条看完的部分，和文档同一套判据。
    from .report import lines_report

    report_pairs: list[tuple[str, str]] = []
    report_locations: list[str] = []
    for position, block in enumerate(blocks, start=1):
        if not block.translated or position in mismatched:
            continue
        source = block.source
        translated = "\n".join(block.translated)
        location = f"块 #{block_label(block, position)}（{block.timing}）"
        if re.sub(r"\s", "", translated) == re.sub(r"\s", "", source):
            kept_original.append((location, source, "引擎回显原文，未获得译文"))
            continue
        report_pairs.append((source, translated))
        report_locations.append(location)
    report_path, report_count = lines_report(
        output, report_pairs, report_locations, translator, kept=kept_original
    )
    if report_path:
        log(f"自检报告：{shown_path(report_path)}（{report_count} 条疑点）")

    # v1.5.4 译后编辑闭环：字幕块按索引回写（时间轴与序号原样保留）。
    from .postedit import emit_edit_list

    edit_pairs: list[tuple[str, str]] = []
    edit_locations: list[str] = []
    edit_locators: list[dict] = []
    for position, block in enumerate(blocks):
        if not block.translated:
            continue
        edit_pairs.append((block.source, "\n".join(block.translated)))
        edit_locations.append(f"块 #{block_label(block, position + 1)}（{block.timing}）")
        edit_locators.append({"index": position})
    if edit_pairs:
        edit_path, _globals = emit_edit_list(
            output,
            edit_pairs,
            edit_locations,
            edit_locators,
            "srt" if path.suffix.lower() == ".srt" else "vtt",
            "b",
        )
        log(f"译后编辑清单：{shown_path(edit_path)}（{len(edit_pairs)} 块可改）")
    return output
