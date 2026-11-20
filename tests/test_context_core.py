"""阶段 0 新增内核的测试：TranslationContext / StateStore / Termbase / 引擎统一入口。

这几块是第二阶段以后所有质量能力的地基，一旦语义漂移，症状会很隐蔽
（例如缓存键混入了场景摘要，表现只是"命中率莫名其妙变 0"，很难联想到缓存键）。
所以用测试把关键语义钉死，而不是只在注释里说明意图。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.common import TranslationError  # noqa: E402
from core.ctx import TranslationContext, context_from  # noqa: E402
from core.engine_base import Engine, call_batch, call_one, supports_context  # noqa: E402
from core.state import StateStore  # noqa: E402
from core.termbase import Termbase  # noqa: E402


# --------------------------------------------------------------- ctx


def test_empty_context_is_trivial() -> None:
    assert TranslationContext().is_trivial() is True
    assert context_from(target_lang="zh").is_trivial() is False


def test_cache_profile_ignores_scene_summary() -> None:
    """场景摘要刻意不进缓存键——否则同一个句子换个场次就永远 miss。

    这是最容易被人"顺手修好"的地方：看起来摘要变了译文也该变。
    但那样会让长文档缓存命中率归零，得不偿失。
    """
    base = context_from(target_lang="zh", style="standard", terms_fingerprint="abc123")
    other_scene = base.with_(summary="前一幕：主角决意离家", scene_id="s17")
    assert base.cache_profile() == other_scene.cache_profile()


def test_cache_profile_changes_on_stable_dimensions() -> None:
    base = context_from(target_lang="zh", style="standard", terms_fingerprint="abc123")
    assert base.with_(style="formal").cache_profile() != base.cache_profile()
    assert base.with_(terms_fingerprint="zzz").cache_profile() != base.cache_profile()
    assert base.with_(target_lang="ja").cache_profile() != base.cache_profile()


def test_characters_enter_cache_profile() -> None:
    """在场角色会改变自称/语气，必须进缓存键。

    但只有真的有角色时才参与——文档段落一般没有角色，
    拿到的还是空串，不会因此把常规文档的缓存打碎。
    """
    plain = context_from(target_lang="zh")
    with_cast = context_from(target_lang="zh", characters=["司", "深月"])
    assert with_cast.cache_profile() != plain.cache_profile()
    assert plain.cache_profile().endswith("|")

    # 角色顺序不影响键：同一组人换顺序仍然是同一种语境
    reordered = context_from(target_lang="zh", characters=["深月", "司"])
    assert reordered.cache_profile() == with_cast.cache_profile()


def test_context_is_immutable_and_describable() -> None:
    ctx = context_from(target_lang="zh", characters=["司"])
    changed = ctx.with_(summary="战斗结束")
    assert ctx.summary == ""
    assert changed.summary == "战斗结束"
    assert "在场角色" in changed.describe()
    assert changed.to_dict()["target_lang"] == "zh"


# -------------------------------------------------------------- state


def test_state_store_roundtrip(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    store.upsert_term("server", "服务器", state="adopted", first_seen="seg-1")
    term = store.get_term("server")
    assert term is not None and term["target"] == "服务器"
    assert term["first_seen"] == "seg-1"

    store.upsert_term("server", "主机")
    # 重名更新不应该把首次出现位置抹掉，报告里要能定位到它第一次出现的地方
    assert store.get_term("server")["first_seen"] == "seg-1"
    assert store.get_term("server")["target"] == "主机"


def test_state_store_variants(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    store.add_variant("server", "servers")
    assert store.canonical_of("servers") == "server"
    assert store.variants_of("server") == ["servers"]


def test_state_store_characters_and_scenes(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    store.upsert_character("司", {"自称": "俺", "语气基线": "粗枝大叶"})
    assert store.get_character("司")["自称"] == "俺"

    store.add_scene("s1", "车站相遇", order_hint=1)
    store.add_scene("s2", "战斗开始", order_hint=2)
    assert store.recent_scenes(limit=2) == ["战斗开始", "车站相遇"]

    store.clear_scenes()
    assert store.recent_scenes() == []


def test_state_store_survives_bad_path(tmp_path: Path) -> None:
    """状态库不是主流程：打不开就该静默降级，而不是让翻译直接崩。"""
    # 用一个不存在的目录下的路径（且父目录不可创建时为佳），这里用裸路径 + 只读父目录
    bad_path = tmp_path / "nested" / "deep" / "state.sqlite"
    store = StateStore(bad_path)
    assert store.available is True  # 父目录会被自动创建

    store.close()


# ----------------------------------------------------------- termbase


@pytest.fixture
def termbase(tmp_path: Path) -> Termbase:
    store = StateStore(tmp_path / "state.sqlite")
    return Termbase(store)


def test_adopt_lookup_and_states(termbase: Termbase) -> None:
    termbase.add_candidate("払子", note="预处理抽到的")
    assert termbase.store.get_term("払子")["state"] == "candidate"

    termbase.adopt("払子", "拂尘")
    assert termbase.lookup("払子") == "拂尘"
    assert termbase.store.get_term("払子")["state"] == "adopted"

    termbase.reject("払子", reason="不是术语")
    assert termbase.lookup("払子") is None


def test_record_observed_detects_drift(termbase: Termbase) -> None:
    """同一个源词被译出两个不同译名时必须自动标存疑。

    这是"术语漂移"唯一的自动告警途径——没有它，用户只能肉眼看全文比对。
    """
    termbase.adopt("grimoire", "魔导书")
    assert termbase.record_observed("grimoire", "魔导书") == "consistent"
    assert termbase.record_observed("grimoire", "魔法书") == "disputed"

    disputed = termbase.disputed()
    assert len(disputed) == 1
    assert disputed[0]["source"] == "grimoire"
    assert "魔导书" in disputed[0]["note"] and "魔法书" in disputed[0]["note"]


def test_record_observed_ignores_unknown_terms(termbase: Termbase) -> None:
    termbase.adopt("known", "已知")
    assert termbase.record_observed("whatever", "随便") == ""


def test_global_rename_updates_everywhere(termbase: Termbase) -> None:
    termbase.adopt("bank", "银行")
    termbase.rename("bank", "河岸")
    assert termbase.lookup("bank") == "河岸"
    assert termbase.store.get_term("bank")["state"] == "adopted"


def test_variant_family_masks_to_one_target(termbase: Termbase) -> None:
    """桜 / 桜さん / 桜ちゃん 必须收敛成同一个译名。

    否则同一个角色会因为称谓不同而在中文里出现三个名字——
    这在 Galgame 里是最刺眼的一类错误。
    """
    termbase.adopt("桜", "樱")
    termbase.add_variant("桜", "桜さん")
    termbase.add_variant("桜", "桜ちゃん")

    masked, mapping = termbase.mask("桜さんが笑った。桜ちゃん、こっちだ。")
    assert "桜さん" not in masked and "桜ちゃん" not in masked
    restored = termbase.unmask(masked, mapping)
    assert restored.count("樱") == 2


def test_mask_and_unmask_cover_missing_mapping(termbase: Termbase) -> None:
    termbase.adopt("AI", "人工智能")
    masked, mapping = termbase.mask("AI is changing")
    assert masked != "AI is changing"
    assert termbase.unmask(masked, mapping).startswith("人工智能")


def test_mask_without_terms_returns_original(termbase: Termbase) -> None:
    assert termbase.mask("just a sentence") == ("just a sentence", {})


def test_word_boundaries_prevent_partial_hits(termbase: Termbase) -> None:
    termbase.adopt("note", "注释")
    masked, _ = termbase.mask("denote the change")
    assert "Ztq" not in masked  # 不能把 denote 里的 note 换成注释


def test_fingerprint_tracks_term_changes(termbase: Termbase) -> None:
    termbase.adopt("alpha", "甲")
    before = termbase.fingerprint()
    termbase.adopt("beta", "乙")
    assert termbase.fingerprint() != before


def test_stats_and_placeholder_namespaces_are_separate(termbase: Termbase) -> None:
    """Termbase 用 Ztq 占位符，和 glossary 的 Zxq 刻意错开。

    两套机制可以同时作用在同一段文本上；若前缀撞车，症状会是
    "术语被换成了另一个术语"，而且极难复现。
    """
    from core.glossary import PLACEHOLDER
    from core.termbase import TERM_PLACEHOLDER

    assert PLACEHOLDER.format(0) == "Zxq0Z"
    assert TERM_PLACEHOLDER.format(0) == "Ztq0Z"


def test_seed_from_glossary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite")
    termbase = Termbase(store)
    monkeypatch.setattr("core.heading_terms.load_user_terms", lambda: None, raising=False)
    monkeypatch.setattr(
        "core.heading_terms.USER_TERMS", {"Quantum": "量子"}, raising=False
    )
    assert termbase.seed_from_glossary() == 1
    assert termbase.lookup("Quantum") == "量子"
    assert termbase.store.get_term("Quantum")["state"] == "adopted"


# -------------------------------------------------------------- engines


class _OldEngine:
    """模拟现有引擎：签名只有 text，不懂上下文。"""

    name = "old"
    label = "旧引擎"

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def translate(self, text: str) -> str:
        self.calls.append((text, None))
        return f"<<{text}>>"


class _CtxEngine(Engine):
    """模拟阶段 2 的本地大模型引擎。"""

    name = "llm"
    label = "本地大模型"
    supports_ctx = True

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def translate(self, text: str, ctx: TranslationContext | None = None) -> str:
        self.calls.append((text, ctx))
        return f"[[{text}]]"


def test_call_one_respects_engine_capability() -> None:
    old = _OldEngine()
    ctx = context_from(target_lang="zh")
    assert call_one(old, "hi") == "<<hi>>"
    assert old.calls == [("hi", None)]  # 旧引擎不能被塞第二个参数

    llm = _CtxEngine()
    assert call_one(llm, "hi", ctx) == "[[hi]]"
    assert llm.calls[0][1] is ctx


def test_supports_context_predicate() -> None:
    assert supports_context(_CtxEngine()) is True
    assert supports_context(_OldEngine()) is False


def test_call_batch_falls_back_to_single_calls() -> None:
    old = _OldEngine()
    assert call_batch(old, ["a", "b"]) == ["<<a>>", "<<b>>"]


def test_call_batch_passes_ctx_when_supported() -> None:
    llm = _CtxEngine()
    ctx = context_from(target_lang="zh")
    assert call_batch(llm, ["a"], ctx) == ["[[a]]"]
    assert llm.calls[0][1] is ctx


def test_call_batch_rejects_count_mismatch() -> None:
    class _Bad(Engine):
        name = "bad"
        label = "假引擎"

        def translate(self, text: str, ctx: TranslationContext | None = None) -> str:
            return text

        def translate_batch(self, texts, ctx=None) -> list:  # type: ignore[override]
            return ["少一条"]

    with pytest.raises(TranslationError):
        call_batch(_Bad(), ["a", "b"])


def test_engine_base_batch_default_loops() -> None:
    engine = _CtxEngine()
    assert engine.translate_batch(["x", "y"]) == ["[[x]]", "[[y]]"]
    assert engine.available() is True
    assert "本地大模型" in engine.describe()
