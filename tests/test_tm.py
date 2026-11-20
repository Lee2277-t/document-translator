"""翻译记忆测试：精确命中、模糊匹配、阈值、预筛性能语义。

v1.5.6 追加三组语义（四处被改造的地方都必须有钉子）：
1. **长度归一化惩罚**：`_similarity` 的 input–output 形状与"短 vs 长"的分歧；
2. **match() API**：`ratio/origin/confirmed` 三字段，100% 直通与"近似复用
   待确认"的分级依据；
3. **四档接线**：100% 直通不进引擎、模糊用了但不回写、译后编辑传播、
   保密模式不参与。
"""

from __future__ import annotations

import sqlite3
from difflib import SequenceMatcher
from pathlib import Path

import pytest

from core.state import StateStore, _migrate_tm_columns
from core.tm import Match, TranslationMemory, _similarity, _tokens


ROOT = Path(__file__).resolve().parent.parent


def _tm(tmp_path) -> TranslationMemory:
    return TranslationMemory(StateStore(str(tmp_path / "state.sqlite")))


def test_exact_hit(tmp_path) -> None:
    tm = _tm(tmp_path)
    tm.add("请确认以下条款", "请确认以下条款（译文）")
    assert tm.lookup("请确认以下条款") == "请确认以下条款（译文）"


def test_fuzzy_hit_above_threshold(tmp_path) -> None:
    tm = _tm(tmp_path)
    tm.add("请确认以下条款是否有效", "请确认以下条款是否有效（译文）")
    # 只差一个标点的句子应命中（相似度 ≈0.96）
    assert tm.lookup("请确认以下条款是否有效。") == "请确认以下条款是否有效（译文）"


def test_one_char_difference_in_cjk_stays_below_threshold(tmp_path) -> None:
    """「是否」→「有无」这种一字之差，相似度只有 0.76，低于默认阈值。

    这不是缺陷而是取舍：中文里换一个字常常就是换意思（是否/有无、
    收到/未收），复用比不翻更危险。测试钉住这条边界。
    """
    tm = _tm(tmp_path)
    tm.add("请确认以下条款是否有效", "译文甲")
    assert tm.lookup("请确认以下条款有无效") is None


def test_cjk_bigrams_allow_prefilter(tmp_path) -> None:
    """中文必须切二元组做预筛——整段切会让两个不同句子永远无交集，
    模糊匹配对中文直接失效（踩过，回归测试钉住）。"""
    tokens_a = _tokens("请确认以下条款")
    tokens_b = _tokens("请确认下述条款")
    assert tokens_a & tokens_b, "中文句子之间应有二元组交集"
    assert _tokens("请确认以下条款是否有效") & _tokens("请确认下述条款有无效力")


def test_below_threshold_returns_none(tmp_path) -> None:
    tm = _tm(tmp_path)
    tm.add("请确认以下条款", "译文甲")
    # 完全不同的句子不该命中——错用比不用更危险
    assert tm.lookup("今天天气很好我们出去玩吧") is None


def test_threshold_is_configurable(tmp_path) -> None:
    tm = TranslationMemory(StateStore(str(tmp_path / "s.sqlite")), threshold=0.5)
    tm.add("请确认以下条款是否有效", "译文")
    hit = tm.lookup("请确认下述条款有无效力")
    assert hit == "译文"


def test_empty_and_identical_entries_rejected(tmp_path) -> None:
    tm = _tm(tmp_path)
    tm.add("", "")
    tm.add("same", "same")  # 译文与原文相同：没有记忆价值
    assert tm.store.count_tm() == 0


def test_add_updates_existing(tmp_path) -> None:
    tm = _tm(tmp_path)
    tm.add("句子", "旧译")
    tm.add("句子", "新译")
    assert tm.lookup("句子") == "新译"
    assert tm.store.count_tm() == 1


def test_prefilter_length_gate(tmp_path) -> None:
    """长度差过大的候选连相似度都不算——这是预筛存在的意义。"""
    tm = _tm(tmp_path)
    tm.add("短句", "短")
    assert tm._cheap_prefilter("短句", "短句扩展了很多内容变得很长", {"短句"}) is False
    assert tm._cheap_prefilter("短句", "短句", {"短句"}) is True


def test_stats_count_hits_and_misses(tmp_path) -> None:
    tm = _tm(tmp_path)
    tm.add("句子甲", "译文甲")
    tm.lookup("句子甲")
    tm.lookup("完全不相关的另一句话在这里")
    stats = tm.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["entries"] == 1


# ---------------------------------------------------------------------------
# 六、长度归一化惩罚（v1.5.6）
# ---------------------------------------------------------------------------


def test_similarity_is_raw_ratio_when_gap_under_twenty_percent() -> None:
    """长度差在 20% 以内不打折——"多一个句号"这类必须完全不受惩罚影响。

    这也是既有 ``test_fuzzy_hit_above_threshold`` 赖以成立的前提：那条用例
    的两句话长度差约 7.7%，惩罚一旦误伤它就红了。
    """
    source = "请确认以下条款是否有效"
    candidate = "请确认以下条款是否有效。"
    raw = SequenceMatcher(None, source, candidate).ratio()
    assert _similarity(source, candidate) == pytest.approx(raw)


def test_similarity_penalizes_length_gap_over_twenty_percent() -> None:
    """长度差超过 20% 才打折，且打折幅度有明确公式（差多大扣多少）。

    为什么要有这个惩罚：``SequenceMatcher.ratio`` 对短句天然偏心，两句都短
    时"像"得虚高（例如 7 字对 5 字仍有 0.83），照单全收会把原文都比中枢短
    的记忆句当成"一样"。变异检验：删掉 ``_similarity`` 里的惩罚项，下面
    两个断言立刻全红。
    """
    source = "请确认以下条款"
    candidate = "请确认以下"
    raw = SequenceMatcher(None, source, candidate).ratio()
    gap = abs(len(source) - len(candidate)) / max(len(source), len(candidate))
    assert gap > 0.2, "这条用例本身依赖 gap 超过 20%"
    assert _similarity(source, candidate) < raw
    assert _similarity(source, candidate) == pytest.approx(raw * (1 - 0.5 * (gap - 0.2)))


def test_length_gap_penalty_can_turn_a_hit_into_a_miss(tmp_path) -> None:
    """行为层面：同一对句子在"打折前"够阈值、"打折后"不够。

    原始相似度 0.8333 ≥ 阈值 0.81（去掉惩罚就命中），长度差 28.6% 打折后
    0.7976 < 0.81（现在不该命中）。选一个中间的阈值，是为了让这条用例对
    **两个方向**的偏移都敏感。
    """
    memory = TranslationMemory(
        StateStore(str(tmp_path / "state.sqlite")), threshold=0.81
    )
    memory.add("请确认以下", "库里那条译文")
    raw = SequenceMatcher(None, "请确认以下条款", "请确认以下").ratio()
    assert raw >= 0.81, "这条用例的前提是原始相似度够阈值"
    assert memory.lookup("请确认以下条款") is None


# ---------------------------------------------------------------------------
# 七、match() API：ratio / origin / confirmed
# ---------------------------------------------------------------------------


def test_match_exact_reports_ratio_one_and_metadata(tmp_path) -> None:
    memory = _tm(tmp_path)
    memory.add("句子甲", "译文甲")
    memory.confirm("句子乙", "译文乙")
    hit = memory.match("句子乙")
    assert isinstance(hit, Match)
    assert hit.target == "译文乙"
    assert hit.ratio == 1.0  # 100% 直通的判据
    assert hit.origin == "human"
    assert hit.confirmed is True


def test_match_fuzzy_reports_ratio_below_one(tmp_path) -> None:
    memory = _tm(tmp_path)
    memory.add("Please confirm the following terms.", "请确认以下条款。")
    hit = memory.match("Please confirm the following terms?")
    assert hit is not None
    assert "请确认以下条款" in hit.target
    assert 0.92 <= hit.ratio < 1.0  # 近似复用：用归用，但要标"待确认"
    assert hit.confirmed is False


def test_lookup_delegates_to_match(tmp_path) -> None:
    """旧接口只能拿到字符串，必须与 match() 结论一致（不能两套匹配口径）。"""
    memory = _tm(tmp_path)
    memory.add("句子甲", "译文甲")
    hit = memory.match("句子甲")
    assert hit is not None
    assert memory.lookup("句子甲") == hit.target


def test_add_records_origin_and_src_lang(tmp_path) -> None:
    store = StateStore(str(tmp_path / "state.sqlite"))
    memory = TranslationMemory(store)
    memory.add("Please confirm.", "请确认。", origin="tmx", src_lang="en")
    rows = store.all_tm_full()
    assert len(rows) == 1
    assert rows[0]["origin"] == "tmx"
    assert rows[0]["src_lang"] == "en"
    assert rows[0]["confirmed"] is False


def test_unknown_origin_falls_back_to_auto(tmp_path) -> None:
    """认不出的来源不给莫名背书：一律按最保守的 auto 记。"""
    store = StateStore(str(tmp_path / "state.sqlite"))
    store.upsert_tm("句子甲", "译文甲", origin="天上掉下来的")
    assert store.all_tm_full()[0]["origin"] == "auto"


def test_migrate_tm_columns_is_idempotent(tmp_path) -> None:
    """迁移必须幂等：老三列结构的库反复补也不能报错、不能重复补列。"""
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(str(path))
    old.execute(
        "CREATE TABLE IF NOT EXISTS tm ("
        " source TEXT PRIMARY KEY, target TEXT NOT NULL, updated_at INTEGER NOT NULL)"
    )
    old.execute("INSERT INTO tm VALUES ('句子甲', '译文甲', 1)")
    old.commit()
    added = _migrate_tm_columns(old)
    old.commit()
    names = {row[1] for row in old.execute("PRAGMA table_info(tm)")}
    old.close()
    assert {"origin", "confirmed", "src_lang"} <= names
    assert added == ["origin", "confirmed", "src_lang"]

    # 第二次补：空列表，且不抛 duplicate column name
    again = sqlite3.connect(str(path))
    assert _migrate_tm_columns(again) == []
    again.close()

    # 老数据带着新列的默认值能用（Roundtrip 一次）
    store = StateStore(str(path))
    store.upsert_tm("句子甲", "新译文", origin="human", confirmed=1)
    row = store.all_tm_full()[0]
    assert row["target"] == "新译文"
    assert row["origin"] == "human" and row["confirmed"] is True
    store.close()


# ---------------------------------------------------------------------------
# 八、四档接线（<｜hy_place▁holder▁no▁813｜>线：记忆查询在缓存之前、100% 直通、模糊不回写）
# ---------------------------------------------------------------------------


class _CountingChannel:
    """假的在线通道：记录每次真正到达引擎的批次。

    "100% 直通"的验收标准就是它**一次都没被调用**——数错一次就说明直通
    分支没接上（或者接在了缓存之后，等于没接）。
    """

    name = "fake"
    label = "假通道"
    supports_ctx = False

    def __init__(self) -> None:
        self.batch_calls: list[list[str]] = []

    def translate(self, text: str) -> str:
        return f"引擎译：{text}"

    def translate_batch(self, texts: list[str]) -> list[str]:
        self.batch_calls.append(list(texts))
        return [f"引擎译：{text}" for text in texts]


def _cloud(tmp_path, memory):
    from core.translator import Translator

    cloud = Translator(cache_path=str(tmp_path / "cache.sqlite"), tm=memory)
    cloud._preflighted = True  # 跳过真实网络探活
    cloud.bing_disabled = True  # 只让 edge 通道跑
    cloud.edge = _CountingChannel()
    return cloud


def test_exact_match_bypasses_the_engine_entirely(tmp_path) -> None:
    """100% 直通：记忆里有的句子不该消耗一次网络往返。"""
    store = StateStore(str(tmp_path / "state.sqlite"))
    memory = TranslationMemory(store)
    memory.add("Please confirm the following terms.", "请确认以下条款。")
    cloud = _cloud(tmp_path, memory)

    results = cloud.translate_texts(["Please confirm the following terms."])
    assert results == ["请确认以下条款。"]
    assert cloud.edge.batch_calls == [], "100% 命中的句子不该进引擎"
    assert cloud.tm_exact == 1
    assert cloud.tm_fuzzy == 0
    assert cloud.tm_fuzzy_hits == [], "完全一致的句子不是「近似复用」，别进报告"
    assert "翻译记忆直通 1 段" in cloud.summary()


def test_fuzzy_match_is_applied_but_never_written_back(tmp_path) -> None:
    """模糊命中：用是可以用的，但**绝不**把近似句写回记忆。

    这是「只存通过校验的译文」的连续性要求：近似句之间的差异往往正是关键
    信息，回写一次就把一处疑虑复印成一片。变异检验：把 fuzzy 分支改成
    「也 add 一条」→ 下面两行断言立刻变红。
    """
    store = StateStore(str(tmp_path / "state.sqlite"))
    memory = TranslationMemory(store)
    memory.add("Please confirm the following terms.", "请确认以下条款。")
    cloud = _cloud(tmp_path, memory)

    variant = "Please confirm the following terms?"
    results = cloud.translate_texts([variant])
    assert results == ["请确认以下条款。"]
    assert cloud.edge.batch_calls == [], "近似命中也不该进引擎"
    assert cloud.tm_fuzzy == 1 and cloud.tm_exact == 0
    # 库里不能多出一条「variant → 同一译文」的记忆
    assert store.count_tm() == 1
    assert store.get_tm(variant) is None
    assert "待确认" in cloud.summary()
    # v1.9.9：汇总里只数「N 段（待确认）」，用户照着找不到——逐段记下位置与相似度
    assert [hit["paragraph"] for hit in cloud.tm_fuzzy_hits] == [1]
    assert cloud.tm_fuzzy_hits[0]["source"] == variant
    assert cloud.tm_fuzzy_hits[0]["ratio"] == pytest.approx(0.9714285714285714)
    assert cloud.tm_fuzzy_hits[0]["ratio"] < 1.0


def test_memory_is_consulted_before_the_cache(tmp_path) -> None:
    """记忆查询必须排在缓存之前：命中一次连 SQLite 缓存都不必问。

    更实际的意义是：两者都命中时按记忆的结果走（记忆是跨文档的资产，
    缓存只是上次的结果），顺序摆对了才省钱也省心。
    """
    store = StateStore(str(tmp_path / "state.sqlite"))
    memory = TranslationMemory(store)
    memory.add("Please confirm the following terms.", "记忆里的译法")
    cloud = _cloud(tmp_path, memory)
    cloud.cache.set("edge", "Please confirm the following terms.", "缓存里的译法")

    results = cloud.translate_texts(["Please confirm the following terms."])
    assert results == ["记忆里的译法"]
    assert cloud.cache_hits == 0


def test_postedit_write_back_propagates_to_memory(tmp_path) -> None:
    """译后编辑「确认一次、全文生效」的落点：手改段以 human + confirmed 进记忆。

    用真实成品（SRT）走完整回路是想钉住两件事：只有**真的写回成品**的人工
    译文才配得上 human/confirmed（写回失败却记进记忆，等于给用户虚假自信）；
    以及这确实是全项目唯一自动写记忆的路径。
    """
    from core.postedit import apply_edits, edit_list_path, load_edit_list
    from core.script_srt import translate_srt

    class _StubTranslator:
        stop_event = None

        def translate_texts(self, texts, progress=None):
            return ["机器译法" for _ in texts]

        def summary(self) -> str:
            return "假译"

    source = tmp_path / "demo.srt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nPlease confirm the terms.\n",
        encoding="utf-8",
    )
    output = translate_srt(str(source), _StubTranslator(), log=lambda message: None)
    items = load_edit_list(edit_list_path(output))
    items[0].translation = "请人工确认以下条款。"
    assert items[0].pending

    store = StateStore(str(tmp_path / "state.sqlite"))
    memory = TranslationMemory(store)
    logs: list[str] = []
    result = apply_edits(output, [items[0]], log=logs.append, tm=memory)
    assert result.applied == ["b1"]

    rows = store.all_tm_full()
    assert len(rows) == 1
    assert rows[0]["source"] == "Please confirm the terms."
    assert rows[0]["target"] == "请人工确认以下条款。"
    assert rows[0]["origin"] == "human"
    assert rows[0]["confirmed"] is True
    # 写了记忆要说出来，不然用户不知道"改一句、全文生效"真的发生了
    assert any("译文已入库" in line for line in logs)


def test_postedit_without_memory_is_silent_and_safe(tmp_path) -> None:
    """没给记忆对象（保密模式）时照常写回成品，不碰任何记忆。

    这是保密模式的硬要求：记忆读写需要落盘 SQLite，保密模式连碰都不能碰。
    """
    from core.postedit import apply_edits, edit_list_path, load_edit_list
    from core.script_srt import translate_srt

    class _StubTranslator:
        stop_event = None

        def translate_texts(self, texts, progress=None):
            return ["机器译法" for _ in texts]

        def summary(self) -> str:
            return "假译"

    source = tmp_path / "demo.srt"
    source.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nPlease confirm the terms.\n",
        encoding="utf-8",
    )
    output = translate_srt(str(source), _StubTranslator(), log=lambda message: None)
    items = load_edit_list(edit_list_path(output))
    items[0].translation = "请人工确认以下条款。"
    store = StateStore(str(tmp_path / "state.sqlite"))
    logs: list[str] = []
    result = apply_edits(output, [items[0]], log=logs.append)
    assert result.applied == ["b1"]
    assert store.count_tm() == 0
    assert not any("记忆" in line for line in logs)


def test_offline_translator_does_not_touch_the_memory(tmp_path) -> None:
    """保密模式零落盘：LocalTranslator 不注入记忆，pipeline 里也不读不写。"""
    from core.local_translator import LocalTranslator

    offline = LocalTranslator(log=lambda message: None, tm=None)
    assert offline.tm is None
    # 类级默认值兜住了 __new__ 拼装的测试替身（见 core/local_translator.py）
    assert LocalTranslator.tm is None and LocalTranslator.tm_exact == 0


def test_shared_tm_is_none_in_offline_mode(monkeypatch) -> None:
    """保密模式的管线内不注入记忆——而且 UI/CLI 都要拿到同一个结论。

    这里盯的是装配层（app._shared_tm）：四档都从它取实例，它返回 None 才是
    "保密模式不参与"的开关所在。
    """
    import app

    monkeypatch.setattr(app, "_SHARED_TM", None)
    monkeypatch.setattr(app, "_SHARED_TM_BUILT", False)
    monkeypatch.setattr(app, "OFFLINE", True)
    assert app._shared_tm(lambda message: None) is None
    monkeypatch.setattr(app, "OFFLINE", False)
    assert app._shared_tm(lambda message: None) is not None


def test_threshold_one_reuses_only_exact_matches(tmp_path) -> None:
    """``--tm-threshold=1.0`` 的语义：只认完全一致，近似句老老实实重新送引擎。

    这是 v1.9.10 给「只想要 100% 直通」的人留的口子——门槛挂在记忆实例上，
    所以这里从构造参数走到翻译结果，钉住「夹上来的值真的被用到」。
    """
    store = StateStore(str(tmp_path / "state.sqlite"))
    memory = TranslationMemory(store, threshold=1.0)
    memory.add("Please confirm the following terms.", "请确认以下条款。")
    cloud = _cloud(tmp_path, memory)

    variant = "Please confirm the following terms?"
    results = cloud.translate_texts([variant])
    assert results == [f"引擎译：{variant}"]
    assert cloud.edge.batch_calls == [[variant]], "门槛 1.0 时近似句必须回引擎"
    assert cloud.tm_fuzzy == 0 and cloud.tm_fuzzy_hits == []


def test_shared_memory_honours_the_cli_threshold(monkeypatch, tmp_path) -> None:
    """命令行写的门槛要真的落到四档共用的那一个记忆实例上。

    只测 ``TranslationMemory(threshold=...)`` 不够：``app._shared_tm()`` 才是调用方，
    它漏传参数的症状是「开关写了、行为没变」——与 v1.6.3 之前的老毛病同款。
    """
    import app

    monkeypatch.setattr(app, "OFFLINE", False)
    monkeypatch.setattr(app, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(app, "_SHARED_TM", None)
    monkeypatch.setattr(app, "_SHARED_TM_BUILT", False)

    monkeypatch.setattr(app, "CLI_TM_THRESHOLD", 0.99)
    memory = app._shared_tm()
    assert memory is not None and memory.threshold == 0.99

    from core.tm import DEFAULT_THRESHOLD

    monkeypatch.setattr(app, "_SHARED_TM", None)
    monkeypatch.setattr(app, "_SHARED_TM_BUILT", False)
    monkeypatch.setattr(app, "CLI_TM_THRESHOLD", None)
    default_memory = app._shared_tm()
    assert default_memory is not None and default_memory.threshold == DEFAULT_THRESHOLD
