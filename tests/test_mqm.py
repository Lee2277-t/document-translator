"""MQM 报告测试：分级、维度、通过/存疑/需人工判定。"""

from __future__ import annotations

from core.mqm import (
    dimension_breakdown,
    render_lines,
    summarize,
)
from core.quality.verifier import Issue


def _issue(severity: str, dimension: str, segment_id: str = "s1") -> Issue:
    return Issue(
        severity=severity,
        dimension=dimension,
        segment_id=segment_id,
        detail=f"{severity}/{dimension} 问题",
    )


def test_empty_summary_passes() -> None:
    summary = summarize([])
    assert summary.verdict() == "通过"
    assert summary.total == 0


def test_critical_forces_manual_review() -> None:
    summary = summarize([_issue("critical", "format"), _issue("minor", "persona")])
    assert summary.verdict() == "需人工"
    assert summary.critical == 1


def test_major_is_doubtful() -> None:
    summary = summarize([_issue("major", "terminology")])
    assert summary.verdict() == "存疑"


def test_minor_only_passes() -> None:
    summary = summarize([_issue("minor", "persona"), _issue("minor", "coherence")])
    assert summary.verdict() == "通过"
    assert summary.minor == 2


def test_one_line_mentions_counts_and_verdict() -> None:
    summary = summarize([_issue("critical", "format"), _issue("major", "terminology")])
    line = summary.one_line()
    assert "Critical 1" in line
    assert "Major 1" in line
    assert "需人工" in line


def test_dimension_breakdown_order_is_stable() -> None:
    issues = [
        _issue("major", "coherence"),
        _issue("major", "terminology"),
        _issue("major", "terminology"),
    ]
    breakdown = dimension_breakdown(issues)
    assert breakdown == [("术语一致性", 2), ("叙事连贯", 1)]


def test_render_lines_sort_critical_first() -> None:
    issues = [
        _issue("minor", "persona", "s2"),
        _issue("critical", "format", "s3"),
        _issue("major", "terminology", "s1"),
    ]
    lines = render_lines(issues)
    assert lines[0].startswith("[Critical]")
    assert "格式完整" in lines[0]
    assert lines[1].startswith("[Major]")


def test_render_lines_truncates() -> None:
    issues = [_issue("minor", "persona", f"s{index}") for index in range(10)]
    lines = render_lines(issues, limit=3)
    assert len(lines) == 4  # 3 条 + 1 行截断提示
    assert "另有 7 条" in lines[-1]
