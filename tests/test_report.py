"""自检报告的分级、排序、上限，以及译名一致性检查。"""
from __future__ import annotations

import os
import tempfile

from core.report import MAX_ISSUES, CheckReport, build_report
from core.term_check import check_term_consistency


def _build(report: CheckReport) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "demo.docx")
        path = build_report(out, report)
        assert path is not None
        with open(path, encoding="utf-8") as handle:
            return handle.read()


def test_no_issues_still_writes_a_report():
    text = _build(CheckReport())
    assert "未发现需要复核的疑点" in text


def test_severity_is_grouped_and_counted():
    report = CheckReport()
    report.add("数字疑点", "第 9 页", "1500", "原文出现 2 次")
    report.add("字号缩小", "全文", "")
    text = _build(report)
    assert "[高] 数字疑点" in text
    assert "[低] 字号缩小" in text
    assert "高 1 条、低 1 条" in text


def test_high_severity_sorts_before_low_regardless_of_page():
    report = CheckReport()
    report.add("字号缩小", "第 2 页", "")
    report.add("数字疑点", "第 30 页", "1500")
    text = _build(report)
    assert text.index("[高]") < text.index("[低]")


def test_same_severity_sorts_by_page_number():
    report = CheckReport()
    report.add("数字疑点", "第 30 页", "a")
    report.add("数字疑点", "第 3 页", "b")
    text = _build(report)
    assert text.index("第 3 页") < text.index("第 30 页")


def test_issue_list_is_capped_with_a_note():
    report = CheckReport()
    for index in range(MAX_ISSUES + 25):
        report.add("数字疑点", f"第 {index + 1} 页", "x")
    text = _build(report)
    assert f"另有 25 条低优先级疑点未列出" in text


def test_term_consistency_flags_diverging_translations():
    """同一个词在三段里被译成了三样，交集为空 → 应该报出来。"""
    pairs = [
        ("Revenue grew in the first quarter of the year.", "第一季度收入增长。"),
        ("Revenue fell across every region we track.", "我们追踪的每个区域的营收都下降了。"),
        ("Revenue includes both services and licences.", "收益包含服务与许可两部分。"),
    ]
    issues = check_term_consistency(pairs)
    assert any(issue.term == "Revenue" for issue in issues)


def test_term_consistency_stays_quiet_when_consistent():
    pairs = [
        ("Revenue grew in the first quarter of the year.", "第一季度收入有所增长。"),
        ("Revenue fell across every region we track.", "我们追踪的每个区域的收入都下降了。"),
        ("Revenue includes both services and licences.", "收入包含服务与许可两部分。"),
    ]
    assert check_term_consistency(pairs) == []


def test_term_consistency_ignores_stopwords_and_rare_terms():
    """「the first quarter」不该被当成术语；只出现两次的词也不报。"""
    pairs = [
        ("The first quarter was strong and the second quarter was weak.", "第一季度强劲。"),
        ("The first quarter of the year sets the tone for the period.", "第一季度定调。"),
    ]
    assert check_term_consistency(pairs) == []


# ------------------------------------------------------------------ 逐行成品报告


def _read_lines_report(tmp_path, **kwargs) -> tuple[str, int, str]:
    from pathlib import Path

    from core.report import lines_report

    path, count = lines_report(str(tmp_path / "demo.srt"), **kwargs)
    assert path is not None
    return path, count, Path(path).read_text(encoding="utf-8")


def test_lines_report_writes_next_to_liner_output(tmp_path):
    path, count, text = _read_lines_report(
        tmp_path,
        pairs=[("Hello there", "你好")],
        locations=["块 #1（00:00:01,000）"],
    )
    assert path.endswith("demo 自检报告.txt")
    assert "未发现需要复核的疑点" in text
    assert count == 0


def test_lines_report_carries_numbers_kept_and_extra(tmp_path):
    _path, count, text = _read_lines_report(
        tmp_path,
        pairs=[("Revenue grew 12 percent.", "收入增长 21%。")],
        locations=["块 #1（00:00:01,000）"],
        kept=[("块 #2（00:00:05,000）", "How are you?", "译文行数与原文不一致，已保留原文")],
        extra=[
            (
                "OCR 识别不清",
                "整张图",
                "4,200",
                "1 处文字识别置信度过低（最低 0.30），已跳过、未翻译",
            )
        ],
    )
    assert "[高] 数字疑点" in text
    assert "块 #2（00:00:05,000）" in text
    assert "译文行数与原文不一致" in text
    assert "[中] OCR 识别不清" in text
    assert "最低 0.30" in text
    # 改了一个数字会成对报两条（原文有译文无、译文有原文无），加保留原文与 OCR 共 4 条
    assert count == 4


def test_drop_entries_share_one_shape_for_image_and_scan():
    """图片与扫描页共用一份口径：逐条带相对位置、按阅读顺序、超上限合并、坏框进「其余位置」。

    v1.9.8 把这条口径从 image_translator 提进 report，PDF 扫描页跟着用。
    """
    from core.report import drop_entries

    dropped = [
        ("lower", 0.20, (0.0, 80.0, 50.0, 90.0)),
        ("upper", 0.30, (0.0, 10.0, 50.0, 20.0)),
        ("no-box", 0.40, None),
    ]
    entries = drop_entries(dropped, (100.0, 100.0), prefix="第 1 页 · ")
    assert [entry[1] for entry in entries] == [
        "第 1 页 · 第 1 处（横向 0%~50%、纵向 10%~20%）",
        "第 1 页 · 第 2 处（横向 0%~50%、纵向 80%~90%）",
        "第 1 页 · 其余位置",
    ]
    assert entries[2][2] == "no-box"
    assert "另有 1 处" in entries[2][3]
    assert all(entry[0] == "OCR 识别不清" for entry in entries)

    capped = drop_entries(
        [(f"t{index}", 0.10, (0.0, float(index), 10.0, float(index) + 5)) for index in range(10)],
        (100.0, 100.0),
    )
    assert len(capped) == 9
    assert "另有 2 处" in capped[-1][3]


def test_fuzzy_entries_list_each_reused_paragraph_with_ratio():
    """记忆近似复用（v1.9.9）：汇总只念得出「N 段待确认」，用户照着找不到是哪几段。"""
    from core.report import fuzzy_entries

    entries = fuzzy_entries(
        [
            {"paragraph": 2, "source": "Please confirm the following terms?", "ratio": 0.93},
            {"paragraph": 1, "source": "The first quarter was strong.", "ratio": 0.97},
        ]
    )
    assert [entry[1] for entry in entries] == ["段落 1", "段落 2"]
    assert entries[0][2] == "The first quarter was strong."
    assert "97%" in entries[0][3]
    assert "93%" in entries[1][3]
    assert all(entry[0] == "记忆近似复用" for entry in entries)


def test_fuzzy_entries_caps_and_merges_the_rest():
    from core.report import MAX_TM_FUZZY, fuzzy_entries

    hits = [
        {"paragraph": index + 1, "source": f"s{index}", "ratio": 0.95}
        for index in range(MAX_TM_FUZZY + 2)
    ]
    entries = fuzzy_entries(hits)
    assert len(entries) == MAX_TM_FUZZY + 1
    assert entries[-1][1] == "其余 2 段"
    assert "另有 2 段" in entries[-1][3]
    assert "s12" in entries[-1][2]


def test_lines_report_maps_fuzzy_hits_onto_line_locations(tmp_path):
    """逐行成品有自己的位置口径：近似复用要念「第 2 行」，不是「段落 2」。"""

    class _Fuzzy:
        tm_fuzzy_hits = [{"paragraph": 2, "source": "How are you", "ratio": 0.93}]

    _path, count, text = _read_lines_report(
        tmp_path,
        pairs=[("Hello there", "你好"), ("How are you", "你好吗")],
        locations=["第 1 行", "第 2 行"],
        translator=_Fuzzy(),
    )
    assert "[中] 记忆近似复用 · 第 2 行" in text
    assert "相似度 93%" in text
    assert count == 1
