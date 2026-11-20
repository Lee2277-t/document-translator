"""Galbame 脚本前端：T++ 表格（GalTransl 事实标准之一）。

格式现实
--------
T++ 导出的 xlsx 没有统一 schema：有的带表头（``名前`` / ``原文`` / ``译文``），
有的第一行就是数据；说话人列有时在左有时在右。所以这里**按列内容嗅探**，
而不是写死列号——写死的列号遇到另一种导出就直接错位，而错位是静默的：
译文填到错误的列里，用户要到导入游戏时才发现。

嗅探规则（表头存在时按表头，否则按内容）：

* 说话人列：表头含 ``名前`` / ``话者`` / ``speaker`` / ``name``；
* 原文列：表头含 ``原文`` / ``日文`` / ``japanese`` / ``original`` / ``テキスト``；
* 译文列：表头含 ``译文`` / ``中文`` / ``翻译`` / ``translation`` / ``中国語``。
  找不到就新建一列，标题写 ``译文``——不动用户原有结构。

行数一致性是硬约束：输入 N 行输出必须 N 行（脚本模式行数校验是 Critical，
见 core/quality/verifier.py）。合并/拆行会让回写时整锅错位。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .common import TranslationError, shown_path, unique_output_path

#: 表头关键词 → 列角色。
_HEADER_HINTS: tuple[tuple[str, str], ...] = (
    ("speaker", ("名前", "話者", "话者", "speaker", "name", "キャラ")),
    ("source", ("原文", "日文", "日语", "japanese", "original", "テキスト", "テキスト", "text", "src")),
    ("target", ("译文", "譯文", "中文", "翻译", "訳文", "translation", "中国語", "zh", "dst")),
)

#: 无表头时的兜底：第 0 列当说话人（值都短），第 1 列当原文。
_FALLBACK_SPEAKER_COLUMN = 0
_FALLBACK_SOURCE_COLUMN = 1


def _classify_header(cell: str) -> str | None:
    lowered = (cell or "").strip().lower()
    if not lowered:
        return None
    for role, hints in _HEADER_HINTS:
        if any(hint.lower() in lowered for hint in hints):
            return role
    return None


def detect_columns(rows: list[list[str]]) -> tuple[int, int, int | None]:
    """返回（说话人列, 原文列, 译文列或 None）。找不到就按兜底规则。"""
    if not rows:
        return _FALLBACK_SPEAKER_COLUMN, _FALLBACK_SOURCE_COLUMN, None

    header = rows[0]
    found: dict[str, int] = {}
    for position, cell in enumerate(header):
        role = _classify_header(str(cell or ""))
        if role and role not in found:
            found[role] = position
    if "source" in found:
        speaker = found.get("speaker", -1)
        target = found.get("target")
        return speaker, found["source"], target

    # 没有可识别的表头：按内容兜底。第一列短且多行有值 → 说话人列
    width = max(len(row) for row in rows[:20])
    speaker = _FALLBACK_SPEAKER_COLUMN if width > 1 else -1
    return speaker, _FALLBACK_SOURCE_COLUMN, None


def _cell_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def translate_tpp(
    src_path: str,
    translator,
    log: Callable[[str], None] | None = None,
    progress=None,
) -> str:
    """翻译 T++ 表格脚本，回写译文列，返回成品路径。"""
    from openpyxl import load_workbook

    log = log or (lambda message: None)
    progress = progress or (lambda done, total, message: None)
    path = Path(src_path)

    workbook = load_workbook(src_path)
    sheet = workbook.active
    if sheet is None:
        raise TranslationError("这个 Excel 文件里没有工作表。")

    # 读出所有行（含表头行，交给 detect_columns 判断）
    all_rows: list[list[str]] = []
    for row in sheet.iter_rows(values_only=True):
        all_rows.append([_cell_text(value) for value in row])
    if len(all_rows) < 2:
        raise TranslationError("表格里没有数据行。")

    speaker_col, source_col, target_col = detect_columns(all_rows)
    if source_col < 0 or source_col >= len(all_rows[0]):
        raise TranslationError(
            "没能识别出原文列。请确认表头含「原文」一类的字样，或第一列是说话人、第二列是原文。"
        )

    # 表头行是否存在（第一行就是原文列且有值，且不像数据）——保守做法：
    # 只有第一行的原文列能被识别为表头关键词时才跳过它。
    header_row = all_rows[0]
    has_header = any(
        _classify_header(str(cell or "")) for cell in header_row
    )
    start = 1 if has_header else 0

    jobs: list[tuple[int, str, str]] = []  # (行号, 源文, 说话人)
    for offset, row in enumerate(all_rows[start:], start=start):
        if source_col >= len(row):
            continue
        source = row[source_col]
        if not source:
            continue
        speaker = ""
        if 0 <= speaker_col < len(row):
            speaker = row[speaker_col]
        jobs.append((offset + 1, source, speaker))  # openpyxl 行号从 1 起

    if not jobs:
        raise TranslationError("原文列里没有可翻译的内容。")

    if target_col is None:
        # 没有译文列：在最后一列后新增一列。有表头才写标题——
        # 无表头的表格多出一行标题会让数据整体错位一行。
        target_col = max(len(row) for row in all_rows)
        if has_header:
            sheet.cell(row=1, column=target_col + 1, value="译文")
    log(f"已识别 {len(jobs)} 行待译文本（原文第 {source_col + 1} 列），开始翻译…")

    from .quality.segments import Segment

    segments = [
        Segment(source=source, order=position, speaker=speaker)
        for position, (_row, source, speaker) in enumerate(jobs)
    ]
    pipeline = getattr(translator, "quality_pipeline", None)
    if pipeline is not None:
        result = pipeline.run(segments, progress)
        translations = result.translations
    else:
        translations = translator.translate_texts(
            [segment.source for segment in segments], progress
        )

    written = 0
    for (row_number, source, _speaker), translated in zip(jobs, translations):
        if not translated or not translated.strip():
            continue
        sheet.cell(row=row_number, column=target_col + 1, value=translated.strip())
        written += 1

    output = unique_output_path(src_path, "双语", path.suffix or ".xlsx")
    workbook.save(output)
    log(f"脚本翻译完成：写入译文 {written} 行 → {output}")

    # v1.9.5：T++ 表格成品也出自检报告（与文档同一套判据，见 report.lines_report）。
    from .report import lines_report

    report_pairs: list[tuple[str, str]] = []
    report_locations: list[str] = []
    kept_original: list[tuple[str, str, str]] = []
    for (row_number, source, _speaker), translated in zip(jobs, translations):
        location = f"第 {row_number} 行"
        text = (translated or "").strip()
        if not text:
            kept_original.append((location, source, "引擎没有给出译文"))
            continue
        if text.replace(" ", "") == source.replace(" ", ""):
            kept_original.append((location, source, "引擎回显原文，未获得译文"))
            continue
        report_pairs.append((source, text))
        report_locations.append(location)
    report_path, report_count = lines_report(
        output, report_pairs, report_locations, translator, kept=kept_original
    )
    if report_path:
        log(f"自检报告：{shown_path(report_path)}（{report_count} 条疑点）")

    # v1.5.4 译后编辑闭环：按单元格回写译文列，其余单元格原样不动。
    from .postedit import emit_edit_list

    edit_pairs: list[tuple[str, str]] = []
    edit_locations: list[str] = []
    edit_locators: list[dict] = []
    for (row_number, source, _speaker), translated in zip(jobs, translations):
        if not translated or not translated.strip():
            continue
        edit_pairs.append((source, translated.strip()))
        edit_locations.append(f"第 {row_number} 行")
        edit_locators.append({"row": row_number, "col": target_col + 1})
    if edit_pairs:
        edit_path, _globals = emit_edit_list(
            output, edit_pairs, edit_locations, edit_locators, "tpp", "x"
        )
        log(f"译后编辑清单：{shown_path(edit_path)}（{len(edit_pairs)} 行可改）")
    return output
