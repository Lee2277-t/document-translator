"""Galgame 脚本前端：mtool / GalTransl JSON。

格式现实
--------
mtool 导出的 JSON 是「对象数组」，常见形态::

    [{"names": ["桜"], "message": "……", " ...": "……"}, ...]

字段名各版本不完全一致（``names`` / ``name`` / ``speaker``，
``message`` / ``text`` / ``original``），所以同样按**键名嗅探**，
不写死。译文写进新键 ``译文``——不覆盖任何原字段：
源文是用户唯一的真相，毁了我赔不起。

行数一致性同样硬约束：一个元素就是一行，翻译前后数量必须相同。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from .common import TranslationError, shown_path, unique_output_path

#: 键名嗅探表：角色 → 候选键（按优先级）。
_KEY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("speaker", ("names", "name", "speaker", "character", "キャラ")),
    ("source", ("message", "text", "original", "src", "原文", "japanese")),
)
#: 译文写入的键名。
TARGET_KEY = "译文"


def detect_keys(sample: dict) -> tuple[str | None, str | None]:
    """从样本对象里认出（说话人键, 原文键）。"""
    lowered = {str(key).lower(): key for key in sample}
    found: dict[str, str] = {}
    for role, hints in _KEY_HINTS:
        for hint in hints:
            if hint.lower() in lowered:
                found[role] = lowered[hint.lower()]
                break
    return found.get("speaker"), found.get("source")


def _speaker_of(value: Any) -> str:
    """names 可能是列表、字符串或 null。"""
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    return str(value or "").strip()


def translate_script_json(
    src_path: str,
    translator,
    log: Callable[[str], None] | None = None,
    progress=None,
) -> str:
    """翻译 mtool/GalTransl JSON 脚本，返回成品路径。"""
    log = log or (lambda message: None)
    progress = progress or (lambda done, total, message: None)
    path = Path(src_path)

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise TranslationError(f"JSON 解析失败：{exc}") from exc

    # 容忍两种外层形态：直接数组，或包在 {"lines": [...]} 里
    output_data = data
    if isinstance(data, dict):
        for key in ("lines", "data", "items", "scripts"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list) or not data:
        raise TranslationError("JSON 里没有找到字幕/脚本数组。")
    if not all(isinstance(row, dict) for row in data):
        raise TranslationError("JSON 数组里出现了非对象元素，无法识别为脚本格式。")

    speaker_key, source_key = detect_keys(data[0])
    if source_key is None:
        raise TranslationError(
            "没能识别出原文字段（找过 message/text/original 等常见键名）。"
        )

    jobs: list[tuple[int, str, str]] = []
    for index, row in enumerate(data):
        source = str(row.get(source_key) or "").strip()
        if not source:
            continue
        speaker = _speaker_of(row.get(speaker_key)) if speaker_key else ""
        jobs.append((index, source, speaker))
    if not jobs:
        raise TranslationError("所有行的原文字段都是空的。")

    log(f"已识别 {len(jobs)} 行待译文本，开始翻译…")

    from .quality.segments import Segment

    segments = [
        Segment(source=source, order=position, speaker=speaker)
        for position, (_index, source, speaker) in enumerate(jobs)
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
    for (index, source, _speaker), translated in zip(jobs, translations):
        if translated and translated.strip():
            data[index][TARGET_KEY] = translated.strip()
            written += 1

    output = unique_output_path(src_path, "双语", path.suffix or ".json")
    Path(output).write_text(
        json.dumps(output_data, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    log(f"脚本翻译完成：写入译文 {written} 行 → {output}")

    # v1.9.5：脚本成品也出自检报告（与文档同一套判据，见 report.lines_report）。
    from .report import lines_report

    report_pairs: list[tuple[str, str]] = []
    report_locations: list[str] = []
    kept_original: list[tuple[str, str, str]] = []
    for (index, source, _speaker), translated in zip(jobs, translations):
        location = f"第 {index + 1} 行"
        text = (translated or "").strip()
        if not text:
            kept_original.append((location, source, "引擎没有给出译文"))
            continue
        if re.sub(r"\s", "", text) == re.sub(r"\s", "", source):
            kept_original.append((location, source, "引擎回显原文，未获得译文"))
            continue
        report_pairs.append((source, text))
        report_locations.append(location)
    report_path, report_count = lines_report(
        output, report_pairs, report_locations, translator, kept=kept_original
    )
    if report_path:
        log(f"自检报告：{shown_path(report_path)}（{report_count} 条疑点）")

    # v1.5.4 译后编辑闭环：按行号回写译文键，其余字段原样不动。
    from .postedit import emit_edit_list

    edit_pairs: list[tuple[str, str]] = []
    edit_locations: list[str] = []
    edit_locators: list[dict] = []
    for (index, source, _speaker), translated in zip(jobs, translations):
        if not translated or not translated.strip():
            continue
        edit_pairs.append((source, str(data[index].get(TARGET_KEY) or "")))
        edit_locations.append(f"第 {index + 1} 行")
        edit_locators.append({"index": index})
    if edit_pairs:
        edit_path, _globals = emit_edit_list(
            output, edit_pairs, edit_locations, edit_locators, "json", "j"
        )
        log(f"译后编辑清单：{shown_path(edit_path)}（{len(edit_pairs)} 行可改）")
    return output
