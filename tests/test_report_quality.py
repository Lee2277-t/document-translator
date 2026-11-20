"""自检报告的 MQM 段测试。"""

from __future__ import annotations

from pathlib import Path

from core.quality.verifier import Issue
from core.report import CheckReport, build_report, quality_payload


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_report_without_quality_data_has_no_mqm_section(tmp_path: Path) -> None:
    output = tmp_path / "out.docx"
    output.write_text("x", encoding="utf-8")
    report = CheckReport()
    report.add("保留原文", "段落 1", "some text", "未获得译文")

    path = build_report(str(output), report)
    assert path is not None
    text = _read(Path(path))
    assert "保留原文" in text
    assert "MQM" not in text, "没有质量数据时不该出现 MQM 段"


def test_mqm_section_with_critical(tmp_path: Path) -> None:
    output = tmp_path / "out.docx"
    output.write_text("x", encoding="utf-8")
    report = CheckReport()
    issues = [
        Issue("critical", "format", "s00001", "占位符丢失：Zxq0Z"),
        Issue("major", "terminology", "s00002", "术语「魔導書」应译为「魔导书」"),
    ]
    pending = [
        {"source": "魔導書", "target": "魔导书", "state": "disputed", "note": "发现不同译法"},
        {"source": "新語", "target": "", "state": "candidate", "note": ""},
    ]

    path = build_report(str(output), report, issues, pending)
    assert path is not None
    text = _read(Path(path))
    assert "── 质量引擎（MQM）──" in text
    assert "需人工" in text
    assert "Critical 1" in text
    assert "[Critical] 格式完整" in text
    assert "术语一致性" in text
    assert "未决术语" in text
    assert "存疑" in text and "候选" in text


def test_mqm_section_clean(tmp_path: Path) -> None:
    output = tmp_path / "out.docx"
    output.write_text("x", encoding="utf-8")
    report = CheckReport()

    path = build_report(str(output), report, [], [])
    assert path is not None
    text = _read(Path(path))
    assert "未发现需要复核的疑点。" in text
    assert "MQM" not in text


def test_quality_payload_from_plain_translator() -> None:
    """云端/离线翻译器没有质量属性：返回空，不抛异常。"""

    class _Plain:
        pass

    assert quality_payload(_Plain()) == ([], [])


def test_quality_payload_from_local_llm_shape() -> None:
    class _Rich:
        quality_issues = [Issue("major", "terminology", "s1", "x")]

        def pending_terms(self):
            return [{"source": "a", "target": "b", "state": "disputed"}]

    issues, pending = quality_payload(_Rich())
    assert len(issues) == 1
    assert pending[0]["source"] == "a"


def test_quality_payload_survives_broken_pending_terms() -> None:
    """pending_terms 炸掉不该让报告写不出来。"""

    class _Broken:
        quality_issues = []

        def pending_terms(self):
            raise RuntimeError("boom")

    issues, pending = quality_payload(_Broken())
    assert issues == [] and pending == []
