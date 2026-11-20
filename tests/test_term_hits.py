"""术语命中对照表：哪段话命中了术语表、指定译法是什么、译文里用上了没有。

盯住的是一条真实事故链：模型把占位符 ``Zxq0Z`` 改写成 ``ZxqOZ`` 或整个
丢掉时，``unlock`` 换不回去，译文里就没有指定译法——此前工具没有任何检测，
用户以为自己填了术语表就万事大吉。这里钉住整条链：lock 记原文匹配词 →
翻译器记命中 → 生效判定 → 报告渲染 → summary 汇总。

保密模式（LocalTranslator）的对称打法也在本文件里，两条链路产出的字段
必须完全一致，否则报告会出现两种口径。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import glossary  # noqa: E402
from core.heading_terms import HEADINGS, USER_TERMS  # noqa: E402
from core.local_translator import (  # noqa: E402
    ENGLISH,
    LOW_CONFIDENCE,
    LocalTranslator,
    MAX_CHUNK_CHARS,
)
from core.report import CheckReport, build_report, term_hits_of  # noqa: E402
from core.style import STANDARD  # noqa: E402
from core.translator import Translator  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_glossary():
    """术语表现是全局状态，测试改完必须还原，否则污染其他用例。"""
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
    # load_user_terms 把用户词条同时写进 HEADINGS——lookup 只查 HEADINGS。
    # 测试照搬这个行为，否则「整段恰好就是一条术语」走不到 lookup 分支。
    for key, value in USER_TERMS.items():
        HEADINGS[key] = value
    glossary._compiled = None


def _hit(paragraph: int = 1, source: str = "Net profit", target: str = "净利润",
         ok: bool = True) -> dict:
    return {"paragraph": paragraph, "source": source, "target": target, "ok": ok}


class _FakeChannel:
    """假在线通道。

    ``keep_placeholder=False`` 时刻意把占位符弄丢——真实事故里模型会把
    ``Zxq0Z`` 改写成 ``ZxqOZ`` 或整个删掉，``unlock`` 换不回去，
    「术语未生效」此前没有任何检测，这个假通道就是复现那条事故链。
    """

    name = "fake"
    label = "假通道"
    supports_ctx = False

    def __init__(self, keep_placeholder: bool = True, mangle: bool = False) -> None:
        self.keep_placeholder = keep_placeholder
        self.mangle = mangle
        self.batch_calls: list[list[str]] = []
        self.single_calls: list[str] = []

    def translate(self, text: str) -> str:
        self.single_calls.append(text)
        return self._render(text)

    def translate_batch(self, texts: list[str]) -> list[str]:
        self.batch_calls.append(list(texts))
        return [self._render(text) for text in texts]

    def _render(self, text: str) -> str:
        if self.keep_placeholder:
            return f"{text}已译。"
        if self.mangle:
            # 占位符被改写：Zxq0Z → ZxqOZ，unlock 的 \d+ 匹配不上
            return re.sub(r"(?i)zxq\d+z", "ZxqOZ", text) + "已译。"
        # 占位符整个丢掉：译文里再也不会有指定译法
        return "已译。"


@pytest.fixture
def translator(tmp_path: Path) -> Translator:
    tr = Translator(cache_path=str(tmp_path / "cache.sqlite"))
    tr._preflighted = True  # 跳过真实网络探活
    tr.bing_disabled = True  # 只让 edge 通道跑，避免走到备用链路
    return tr


# ---------------------------------------------------------------------------
# glossary.lock 的细粒度输出
# ---------------------------------------------------------------------------


def test_lock_records_source_word_with_original_casing():
    """报告要「原文术语」列，mapping 里没有这个信息，必须由 lock 记。"""
    _set_terms(**{"net profit": "净利润"})
    hits: list[tuple[str, str, str]] = []
    masked, mapping = glossary.lock("The Net profit rose.", hits)
    assert masked == "The Zxq0Z rose."
    assert mapping == {"Zxq0Z": "净利润"}
    # 记的是原文里的样子（Net profit），不是归一化后的键（net profit）
    assert hits == [("Zxq0Z", "Net profit", "净利润")]
    # 不传 hits 时行为与之前完全一致（test_glossary 钉住的契约）
    assert glossary.lock("The Net profit rose.") == (masked, mapping)


def test_lock_without_hits_stays_silent():
    _set_terms(**{"net profit": "净利润"})
    assert glossary.lock("Nothing to lock here.") == ("Nothing to lock here.", {})


# ---------------------------------------------------------------------------
# 在线 Translator 的命中记录
# ---------------------------------------------------------------------------


def test_exact_lookup_hit_is_recorded_as_effective(translator: Translator) -> None:
    """整段恰好就是一条术语：译文直接是指定译法，必然「生效」。"""
    _set_terms(**{"net profit": "净利润"})
    translator.edge = _FakeChannel()
    results = translator.translate_texts(["Net profit", "The report is ready."])
    assert results[0] == "净利润"
    assert translator.term_hits == [
        {"paragraph": 1, "source": "Net profit", "target": "净利润", "ok": True}
    ]


def test_batch_hit_is_recorded_when_placeholder_survives(translator: Translator) -> None:
    """批量通道：占位符完好穿过引擎，译文里有指定译法 → 生效。"""
    _set_terms(**{"net profit": "净利润"})
    translator.edge = _FakeChannel()
    results = translator.translate_texts(["The Net profit rose.", "Nothing here."])
    assert "净利润" in results[0]
    assert translator.term_hits == [
        {"paragraph": 1, "source": "Net profit", "target": "净利润", "ok": True}
    ]
    assert "术语命中 1 处，0 处未生效" in translator.summary()


def test_missing_placeholder_is_flagged_as_not_effective(translator: Translator) -> None:
    """核心验收：模型把占位符整个丢掉，译文里没有指定译法 → 未生效。"""
    _set_terms(**{"net profit": "净利润"})
    translator.edge = _FakeChannel(keep_placeholder=False)
    results = translator.translate_texts(["The Net profit rose."])
    assert "净利润" not in results[0]
    assert translator.term_hits == [
        {"paragraph": 1, "source": "Net profit", "target": "净利润", "ok": False}
    ]
    assert "术语命中 1 处，1 处未生效" in translator.summary()


def test_mangled_placeholder_is_flagged_as_not_effective(translator: Translator) -> None:
    """模型把 Zxq0Z 改写成 ZxqOZ：unlock 的 \\d+ 匹配不上，同样未生效。"""
    _set_terms(**{"net profit": "净利润"})
    translator.edge = _FakeChannel(keep_placeholder=False, mangle=True)
    results = translator.translate_texts(["The Net profit rose."])
    assert "净利润" not in results[0]
    assert translator.term_hits[0]["ok"] is False


def test_cached_segment_is_not_recorded(translator: Translator) -> None:
    """缓存命中的段不记——报告反映本次运行，不是历史缓存的去向。"""
    _set_terms(**{"net profit": "净利润"})
    translator.edge = _FakeChannel()
    translator.translate_texts(["The Net profit rose."])
    assert len(translator.term_hits) == 1
    translator.translate_texts(["The Net profit rose."])
    assert translator.cache_hits == 1
    assert len(translator.term_hits) == 1


def test_direct_single_call_without_index_records_nothing(translator: Translator) -> None:
    """直接调 _translate_one（测试/CLI）没有段号上下文，行为与之前一致。"""
    _set_terms(**{"net profit": "净利润"})
    translator.edge_disabled = False
    translator.edge = _FakeChannel()
    translated, ok, engine = translator._translate_one("The Net profit rose.")
    assert "净利润" in translated
    assert ok is True and engine == "edge"
    assert translator.term_hits == []


def test_fallback_single_path_records_hits(translator: Translator) -> None:
    """两条批量通道都歇菜时的逐段兜底链路也要记命中。"""
    _set_terms(**{"net profit": "净利润"})
    translator.edge_disabled = True
    translator.mymemory = _FakeChannel()
    results = translator.translate_texts(["The Net profit rose."])
    assert "净利润" in results[0]
    assert translator.term_hits == [
        {"paragraph": 1, "source": "Net profit", "target": "净利润", "ok": True}
    ]


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------


def test_report_renders_term_hit_section(tmp_path: Path) -> None:
    output = tmp_path / "out.docx"
    output.write_text("x", encoding="utf-8")
    hits = [_hit(1, ok=True), _hit(2, "fiscal year", "会计年度", ok=False)]
    path = build_report(str(output), CheckReport(), term_hits=hits)
    assert path is not None
    text = Path(path).read_text(encoding="utf-8")
    assert "── 术语命中对照 ──" in text
    assert "共 2 处命中术语表，1 处未生效。" in text
    assert "段落 1 | Net profit → 净利润" in text
    assert "段落 2 | fiscal year → 会计年度" in text
    assert "[!] 译文未含指定译法" in text


def test_report_without_term_hits_has_no_section(tmp_path: Path) -> None:
    output = tmp_path / "out.docx"
    output.write_text("x", encoding="utf-8")
    path = build_report(str(output), CheckReport())
    assert path is not None
    text = Path(path).read_text(encoding="utf-8")
    assert "术语命中对照" not in text
    assert "术语表" not in text


def test_failed_hit_becomes_high_severity_issue(tmp_path: Path) -> None:
    """未生效的命中必须同时是一条「高」级疑点，不能只躺在对照表里。"""
    output = tmp_path / "out.docx"
    output.write_text("x", encoding="utf-8")
    path = build_report(
        str(output), CheckReport(), term_hits=[_hit(2, "fiscal year", "会计年度", False)]
    )
    assert path is not None
    text = Path(path).read_text(encoding="utf-8")
    assert "[高] 术语未生效 · 段落 2" in text
    assert "占位符可能被模型改写或删除" in text
    assert "共 1 条疑点（高 1 条）" in text


def test_term_hit_section_truncates_at_40(tmp_path: Path) -> None:
    output = tmp_path / "out.docx"
    output.write_text("x", encoding="utf-8")
    hits = [_hit(i + 1, f"term{i}", f"译法{i}") for i in range(45)]
    path = build_report(str(output), CheckReport(), term_hits=hits)
    assert path is not None
    text = Path(path).read_text(encoding="utf-8")
    assert "共 45 处命中术语表，0 处未生效。" in text
    assert "另有 5 处未列出" in text


def test_term_hits_of_plain_object() -> None:
    """没有 term_hits 属性的翻译器（本地大模型档）：返回空，不抛异常。"""

    class _Plain:
        pass

    assert term_hits_of(_Plain()) == []


def test_term_hits_of_reads_translator_attribute() -> None:
    class _Rich:
        term_hits = [_hit()]

    assert term_hits_of(_Rich()) == [_hit()]


# ---------------------------------------------------------------------------
# 保密模式 LocalTranslator 的对称打法
# ---------------------------------------------------------------------------


class _StubEngine:
    def __init__(self, rows: list[tuple[str, float]]) -> None:
        self.rows = rows
        self.calls: list[list[str]] = []

    def translate(self, texts, language=""):
        self.calls.append(list(texts))
        return self.rows[: len(texts)]

    def available(self) -> bool:
        return True


def _make_local(rows: list[tuple[str, float]]):
    """按 test_rescue 的套路拼一个不加载真实模型的 LocalTranslator。"""
    translator = LocalTranslator.__new__(LocalTranslator)
    translator.style = STANDARD
    translator.rescue = False
    translator.log = lambda message: None
    translator.term_hits = []
    translator._multilingual = _StubEngine([])
    translator.translated_count = 0
    translator.skipped_count = 0
    translator.cache_hits = 0
    translator.failed_count = 0
    translator.rescued_count = 0
    translator.unsupported_count = 0
    translator.no_model_count = 0
    translator.term_count = 0
    translator.fragment_count = 0
    translator.dubious_count = 0
    translator._dubious_logs = 0
    translator._failure_logs = 0
    translator.language_counts = {}
    translator._memory = {}
    return translator, _StubEngine(rows)


def test_local_translator_records_effective_hit() -> None:
    _set_terms(**{"net profit": "净利润"})
    translator, engine = _make_local([("Zxq0Z上升。", -0.5)])
    results = [None]
    translator._translate_group(engine, ENGLISH, [0], ["The net profit rose."], results)
    assert results[0] == "净利润上升。"
    assert translator.term_hits == [
        {"paragraph": 1, "source": "net profit", "target": "净利润", "ok": True}
    ]


def test_local_translator_flags_lost_placeholder() -> None:
    """保密模式同样要检出「术语未生效」——字段与在线链路逐字一致。"""
    _set_terms(**{"net profit": "净利润"})
    translator, engine = _make_local([("利润上升。", -0.5)])
    results = [None]
    translator._translate_group(engine, ENGLISH, [0], ["The net profit rose."], results)
    assert results[0] == "利润上升。"
    assert translator.term_hits == [
        {"paragraph": 1, "source": "net profit", "target": "净利润", "ok": False}
    ]


def test_local_hit_judged_against_final_output() -> None:
    """QA 第 1 轮观察 2：占位符幸存、但段落因低置信分被打回保留原文时，
    对照行必须显示未生效——判定依据是成品 ``results[index]``，不是
    ``_decode`` 出来的中间译文。修复前这里会误报 ok=True。
    """
    _set_terms(**{"net profit": "净利润"})
    translator, engine = _make_local([("Zxq0Z上升。", LOW_CONFIDENCE - 0.1)])
    results = [None]
    translator._translate_group(engine, ENGLISH, [0], ["The net profit rose."], results)
    # 中间译文是好的，但整段因低置信分被打回保留原文
    assert results[0] == "The net profit rose."
    assert translator.dubious_count == 1
    assert translator.term_hits == [
        {"paragraph": 1, "source": "net profit", "target": "净利润", "ok": False}
    ]


def test_local_summary_mentions_term_hits() -> None:
    translator, _ = _make_local([])
    translator.term_hits = [_hit(1, ok=True), _hit(2, ok=False)]
    assert "术语命中 2 处，1 处未生效" in translator.summary()


class _ChineseEchoEngine:
    """模拟真实离线模型：回中文译文并原样保留占位符，记录到达引擎的每个批次。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def translate(self, texts, language=""):
        self.calls.append(list(texts))
        rows = []
        for text in texts:
            match = re.search(r"(?i)zxq\d+z", text)
            token = match.group(0) if match else ""
            rows.append((f"公司业绩持续增长，详情如{token}所述。", -0.5))
        return rows

    def available(self) -> bool:
        return True


def test_local_long_segment_reaches_engine_and_is_translated() -> None:
    """>1200 字符长段回归（QA 补的钉子，v1.4.1 修复对应）。

    修复前 ``_translate_long`` 里 ``[text for text, _ in prepared]`` 拿 2 元组
    解包 3 元组，ValueError 被 ``except Exception`` 吞掉——QA 已用
    ``git show HEAD:core/local_translator.py`` 的快照对比证实：引擎 0 次调用、
    段被静默保留原文、failed_count=1。这里钉住修复后的行为：长段按块锁术语、
    全部送达引擎、拼回整段产出译文，命中按整段只记一行。
    """
    _set_terms(**{"net profit": "净利润"})
    filler = " ".join(
        f"Sentence {i} describes the quarterly results in detail." for i in range(60)
    )
    source = "The net profit rose. " + filler
    assert len(source) > MAX_CHUNK_CHARS  # 超过 1200，_run_language 才会路由到这里
    translator, _ = _make_local([])
    engine = _ChineseEchoEngine()
    results = [None]
    translator._translate_long(engine, ENGLISH, 0, source, results)
    # 段落实战到达引擎：至少一个批次，且带术语的首块确实锁成了占位符
    assert engine.calls and all(engine.calls)
    assert any("Zxq0Z" in piece for call in engine.calls for piece in call)
    # 产出译文：不是保留原文，指定译法进了成品
    assert results[0] != source
    assert "净利润" in results[0]
    assert translator.translated_count == 1
    assert translator.failed_count == 0
    # 长段按块锁术语，但命中只按整段记一行（分块各记会重复）
    assert translator.term_hits == [
        {"paragraph": 1, "source": "net profit", "target": "净利润", "ok": True}
    ]


def test_ok_judgment_survives_style_spacing(translator: Translator) -> None:
    """钉子测试（QA  Round 1 发现的源码 bug，待工程师修复后转绿）。

    规格（交接稿决策 49① + ``hit_rows`` docstring）：未生效 = 指定译法没进译文。
    译法跨中英/中数字边界时（真实术语表里极常见：AI技术、5G网络、3D打印、
    A4纸），``style.polish`` 的 ``_tidy`` 会在边界插一个空格——「AI技术」被
    正确换回、只是润色成「AI 技术」。此时指定译法确实进了译文，``ok`` 必须
    是 True；当前实现对润色后的文本做 ``target in translated``，会把这类
    正确译文误报成「[高] 术语未生效」，正是本功能要防的假警报。
    """
    _set_terms(**{"ai tech": "AI技术"})
    translator.edge = _FakeChannel()
    results = translator.translate_texts(["Our AI tech lead."])
    # 润色后带空格，但指定译法确实进了译文
    assert "AI 技术" in results[0]
    assert translator.term_hits == [
        {"paragraph": 1, "source": "AI tech", "target": "AI技术", "ok": True}
    ]
