"""v1.4.2 质检口径统一：译名一致性的位置信息 + 术语未生效的残留提示。

三组钉子，全部 prove behavior（不是 prove exists）：
① PPT 链路真能报「译名不一致」且带页码——v1.3.0 补翻译链路时漏接的
   质检口子（word/pdf 都接了，只有 PPT 没接）；
② locations/places：不传时行为与旧版逐字一致、传了正确记录、长度不符
   当没传、去重与 5 个截断；
③ leftover：模型把 Zxq0Z 改写成 ZxqOZ 时真能抓到（含紧贴汉字的场景）、
   正常词不误报、去重与 3 个截断、报告 note 两个分支各按各的文案走。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from pptx import Presentation
from pptx.util import Inches

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import glossary  # noqa: E402
from core.glossary import hit_rows  # noqa: E402
from core.heading_terms import HEADINGS, USER_TERMS  # noqa: E402
from core.ppt_translator import translate_ppt  # noqa: E402
from core.report import CheckReport, build_report  # noqa: E402
from core.term_check import check_term_consistency, issue_note  # noqa: E402
from core.translator import Translator  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_glossary():
    """术语表是全局状态，测试改完必须还原，否则污染其他用例。"""
    saved_user = dict(USER_TERMS)
    saved_headings = dict(HEADINGS)
    saved_compiled = glossary._compiled
    yield
    USER_TERMS.clear()
    USER_TERMS.update(saved_user)
    HEADINGS.clear()
    HEADINGS.update(saved_headings)
    glossary._compiled = saved_compiled


def _set_terms(**pairs: str) -> None:
    USER_TERMS.clear()
    USER_TERMS.update({key.lower(): value for key, value in pairs.items()})
    for key, value in USER_TERMS.items():
        HEADINGS[key] = value
    glossary._compiled = None


# 同一个词（Revenue / quarter）在三段里被译成三样，中文二字词交集为空。
_DIVERGENT = {
    "Revenue grew in the first quarter of the year.": "第一季度收入增长。",
    "Revenue fell across every region we track.": "我们追踪的每个区域的营收都下降了。",
    "Revenue includes both services and licences.": "收益包含服务与许可两部分。",
}

# 译法统一（都含「收入」），不该报。
_CONSISTENT = {
    "Revenue grew in the first quarter of the year.": "第一季度收入有所增长。",
    "Revenue fell across every region we track.": "我们追踪的每个区域的收入都下降了。",
    "Revenue includes both services and licences.": "收入包含服务与许可两部分。",
}


def _revenue_issue(issues) -> object:
    return next(issue for issue in issues if issue.term == "Revenue")


# ---------------------------------------------------------------------------
# ① PPT 链路：译名一致性检查真能报，且带上页码
# ---------------------------------------------------------------------------


class _MappingTranslator:
    """按「原文 → 译文」映射返回；没命中的拼个前缀，保证与原文不同。"""

    stop_event = None

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    def translate_texts(self, texts, progress=None):
        return [self.mapping.get(text, f"译：{text}") for text in texts]


def _make_ppt(tmp_path: Path, texts: list[str]) -> str:
    """每段原文各占一页——位置标签要能区分页码，就得跨页放。"""
    prs = Presentation()
    for text in texts:
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
        box.text_frame.text = text
    src = str(tmp_path / "sample.pptx")
    prs.save(src)
    return src


def _read_report(output_path: str) -> str:
    report = Path(output_path).with_name(Path(output_path).stem + " 自检报告.txt")
    assert report.exists(), "成品旁应生成自检报告"
    return report.read_text(encoding="utf-8")


def test_ppt_reports_term_inconsistency_with_slide_numbers(tmp_path: Path) -> None:
    """PPT 也必须报译名不一致（v1.3.0 漏接口子），且 note 带序号。

    量词按容器说是 v1.7.0 定的口径：对一份 PPT 念「第 1 页」是假话，各翻译器的位置串
    本来就各说各的容器（Word 念「段落」、srt 念「块」、EPUB 念「章」），PPT 是唯一的例外。
    """
    src = _make_ppt(tmp_path, list(_DIVERGENT))
    out = translate_ppt(src, _MappingTranslator(_DIVERGENT))
    text = _read_report(out)

    assert "[中] 译名不一致 · 第 1 张" in text
    assert "Revenue" in text
    # note 念出出现位置：三段分别在第 1/2/3 张
    assert "出现 3 次（第 1 张、第 2 张、第 3 张），译法可能不统一" in text


def test_ppt_stays_quiet_when_translations_consistent(tmp_path: Path) -> None:
    """译法统一时不报——噪音太多这份报告就没人看了。"""
    src = _make_ppt(tmp_path, list(_CONSISTENT))
    out = translate_ppt(src, _MappingTranslator(_CONSISTENT))
    assert "译名不一致" not in _read_report(out)


# ---------------------------------------------------------------------------
# ② locations / places
# ---------------------------------------------------------------------------


def test_places_empty_and_note_unchanged_without_locations() -> None:
    """不传 locations：places 为空、note 与旧版逐字一致。"""
    issues = check_term_consistency(list(_DIVERGENT.items()))
    issue = _revenue_issue(issues)
    assert issue.places == []
    assert issue_note(issue) == (
        f"出现 {issue.occurrences} 次，译法可能不统一："
        + " / ".join(issue.samples)
    )


def test_places_recorded_when_locations_passed() -> None:
    issues = check_term_consistency(
        list(_DIVERGENT.items()), ["第 1 页", "第 2 页", "第 3 页"]
    )
    issue = _revenue_issue(issues)
    assert issue.places == ["第 1 页", "第 2 页", "第 3 页"]
    note = issue_note(issue)
    assert note.startswith("出现 3 次（第 1 页、第 2 页、第 3 页）")
    assert "译法可能不统一" in note


def test_mismatched_locations_are_ignored() -> None:
    """长度与 pairs 不一致 = 调用方串位了，错位的位置比没有位置更糟。"""
    issues = check_term_consistency(list(_DIVERGENT.items()), ["第 1 页"])
    assert _revenue_issue(issues).places == []


def test_places_dedupe_and_cap_at_five() -> None:
    """同段出现两次只记一次位置；超过 5 个截断，note 用「等」交代还有。"""
    translations = ["收入。", "利润。", "收益。", "营收。", "销售额。", "营业额。", "成果。"]
    pairs = [("Revenue grew steadily.", item) for item in translations]
    locations = ["段落 1", "段落 1", "段落 2", "段落 3", "段落 4", "段落 5", "段落 6"]
    issues = check_term_consistency(pairs, locations)
    issue = _revenue_issue(issues)
    assert issue.occurrences == 7
    assert issue.places == ["段落 1", "段落 2", "段落 3", "段落 4", "段落 5"]
    assert "出现 7 次（段落 1、段落 2、段落 3、段落 4、段落 5等）" in issue_note(issue)


# ---------------------------------------------------------------------------
# ③ leftover：残留占位符检测 + 报告 note 两个分支
# ---------------------------------------------------------------------------


def test_hit_rows_flags_leftover_mangled_placeholder() -> None:
    """模型把 Zxq0Z 改写成 ZxqOZ：unlock 换不回去，row 里记下残留 token。

    故意让 token 紧贴汉字——真实译文里占位符从来都是夹在中文中间的。
    """
    rows = hit_rows(3, [("Zxq0Z", "Net profit", "净利润")], "公司的 ZxqOZ 持续上升。")
    assert rows == [
        {
            "paragraph": 3,
            "source": "Net profit",
            "target": "净利润",
            "ok": False,
            "leftover": ["ZxqOZ"],
        }
    ]


def test_hit_rows_without_leftover_keeps_four_field_shape() -> None:
    """占位符被整个删掉：译文里什么也不剩，没有 leftover 键（旧契约不变）。"""
    rows = hit_rows(1, [("Zxq0Z", "Net profit", "净利润")], "利润上升。")
    assert rows == [
        {"paragraph": 1, "source": "Net profit", "target": "净利润", "ok": False}
    ]


def test_effective_hit_never_gets_leftover() -> None:
    """生效的命中不扫残留——正常译文不该被翻旧账。"""
    rows = hit_rows(1, [("Zxq0Z", "Net profit", "净利润")], "公司的净利润持续上升。")
    assert rows[0]["ok"] is True
    assert "leftover" not in rows[0]


def test_leftover_tokens_dedupe_and_cap_at_three() -> None:
    rows = hit_rows(
        1,
        [("Zxq0Z", "Net profit", "净利润")],
        "ZxqOZ 与 ZxqOZ、Ztq1Z、Zxq5Z、Zxq6Z 混在一起。",
    )
    assert rows[0]["leftover"] == ["ZxqOZ", "Ztq1Z", "Zxq5Z"]


def test_normal_words_are_not_leftovers() -> None:
    """普通英文单词（哪怕以 z 开头）不是残留标记——不误报。"""
    rows = hit_rows(1, [("Zxq0Z", "Net profit", "净利润")], "zero size puzzles")
    assert rows[0]["ok"] is False
    assert "leftover" not in rows[0]


# ---------------------------------------------------------------------------
# 端到端：假引擎改写占位符 → 命中带 leftover → 报告 note 念出标记
# ---------------------------------------------------------------------------


class _FakeChannel:
    """假在线通道，三种占位符命运：keep（原样穿过）/ mangle（改写成
    ZxqOZ，真实事故）/ drop（整个删掉）。"""

    name = "fake"
    label = "假通道"
    supports_ctx = False

    def __init__(self, mode: str = "keep") -> None:
        self.mode = mode

    def translate(self, text: str) -> str:
        return self._render(text)

    def translate_batch(self, texts: list[str]) -> list[str]:
        return [self._render(text) for text in texts]

    def _render(self, text: str) -> str:
        if self.mode == "mangle":
            return re.sub(r"(?i)zxq\d+z", "ZxqOZ", text) + "已译。"
        if self.mode == "drop":
            return "已译。"
        return f"{text}已译。"


@pytest.fixture
def translator(tmp_path: Path) -> Translator:
    tr = Translator(cache_path=str(tmp_path / "cache.sqlite"))
    tr._preflighted = True  # 跳过真实网络探活
    tr.bing_disabled = True  # 只让 edge 通道跑，避免走到备用链路
    return tr


def test_mangled_placeholder_reaches_report_with_leftover(
    translator: Translator, tmp_path: Path
) -> None:
    """端到端：改写占位符 → 命中记 leftover → 报告 note 指名道姓念出来。"""
    _set_terms(**{"net profit": "净利润"})
    translator.edge = _FakeChannel(mode="mangle")
    results = translator.translate_texts(["The Net profit rose."])
    assert "净利润" not in results[0]
    assert translator.term_hits[0]["ok"] is False
    assert translator.term_hits[0]["leftover"] == ["ZxqOZ"]

    output = tmp_path / "out.docx"
    output.write_text("x", encoding="utf-8")
    path = build_report(str(output), CheckReport(), term_hits=translator.term_hits)
    text = Path(path).read_text(encoding="utf-8")
    assert "[高] 术语未生效 · 段落 1" in text
    assert "译文里残留了未还原的标记 ZxqOZ" in text
    assert "指定译法没有进入译文" in text


def test_dropped_placeholder_keeps_original_note_verbatim(
    translator: Translator, tmp_path: Path
) -> None:
    """占位符被整个删掉：note 走无 leftover 分支，文案逐字不变。"""
    _set_terms(**{"net profit": "净利润"})
    translator.edge = _FakeChannel(mode="drop")
    results = translator.translate_texts(["The Net profit rose."])
    assert "净利润" not in results[0]
    assert "leftover" not in translator.term_hits[0]

    output = tmp_path / "out.docx"
    output.write_text("x", encoding="utf-8")
    path = build_report(str(output), CheckReport(), term_hits=translator.term_hits)
    text = Path(path).read_text(encoding="utf-8")
    assert "指定译法没有进入译文，占位符可能被模型改写或删除" in text
    assert "残留" not in text
