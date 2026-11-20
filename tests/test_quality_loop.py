"""人在回路测试：未决术语池、全局重命名、受影响场景增量重译。"""

from __future__ import annotations

from core.quality.pipeline import QualityPipeline
from core.quality.segments import segments_from_texts
from core.state import StateStore
from core.termbase import Termbase


class _CtxTranslator:
    """假翻译器：把「旧词」译成「旧译」，「新词」译成「新译」，供断言用。"""

    def __init__(self) -> None:
        self.engine = None
        self.seen: list[list[str]] = []

    def translate_texts(self, texts, progress=None, ctx=None):
        self.seen.append(list(texts))
        return [f"{text}的译文" for text in texts]


def _pipeline(tmp_path, translator) -> QualityPipeline:
    store = StateStore(str(tmp_path / "state.sqlite"))
    termbase = Termbase(store)
    return QualityPipeline(translator, termbase=termbase, store=store)


def test_pending_terms_lists_candidates_and_disputes(tmp_path) -> None:
    translator = _CtxTranslator()
    pipeline = _pipeline(tmp_path, translator)
    pipeline.termbase.add_candidate("新名词")  # 候选：只有源词
    pipeline.termbase.adopt("魔導書", "魔导书")
    pipeline.termbase.dispute("魔導書", "发现不同译法")

    pending = pipeline.pending_terms()
    sources = {row["source"] for row in pending}
    assert sources == {"新名词", "魔導書"}
    disputed = next(row for row in pending if row["source"] == "魔導書")
    assert disputed["state"] == "disputed"
    assert "发现不同译法" in disputed["note"]


def test_rename_retranslates_only_affected_scenes(tmp_path) -> None:
    translator = _CtxTranslator()
    pipeline = _pipeline(tmp_path, translator)
    pipeline.termbase.adopt("魔導書", "魔导书")

    pipeline.run(
        segments_from_texts(
            ["他翻开魔導書。", "第2章", "他们走进森林。", "又提到魔導書。"]
        )
    )
    assert translator.seen  # 已经跑过一轮

    retranslated, scene_count = pipeline.rename_and_retranslate(
        "魔導書", "魔法书"
    )
    # 重译单位是「场景」而不是「段落」：同一场里的段落共享上下文，
    # 只重译含词的那几句会让上下文与译文对不上。所以两个场景共 4 段。
    assert retranslated == 4
    assert scene_count == 2

    # 重译只应覆盖受影响的场景：两个场景各翻过一次（第一轮 + 重译轮），
    # 不存在"把没提到的场景也翻第三遍"的情况
    assert len(translator.seen) == 4  # 第一轮 2 个场景 + 重译轮 2 个场景
    calls_after = translator.seen[-2:]
    assert any("他们走进森林。" in batch for batch in calls_after), (
        "森林段与术语同场，理应随场景重译"
    )


def test_rename_without_hits_skips_retranslation(tmp_path) -> None:
    translator = _CtxTranslator()
    pipeline = _pipeline(tmp_path, translator)
    pipeline.termbase.adopt("魔導書", "魔导书")
    pipeline.run(segments_from_texts(["他们走进森林。"]))

    retranslated, scene_count = pipeline.rename_and_retranslate("魔導書", "魔法书")
    assert (retranslated, scene_count) == (0, 0)
    # 术语本身仍然改名成功了（后续翻译会用到新名）
    assert pipeline.termbase.lookup("魔導書") == "魔法书"


def test_rename_before_any_run_is_safe(tmp_path) -> None:
    """没跑过翻译就改名：不能崩，也不能假装重译了东西。"""
    translator = _CtxTranslator()
    pipeline = _pipeline(tmp_path, translator)
    pipeline.termbase.adopt("魔導書", "魔导书")
    retranslated, scene_count = pipeline.rename_and_retranslate("魔導書", "魔法书")
    assert (retranslated, scene_count) == (0, 0)
    assert pipeline.termbase.lookup("魔導書") == "魔法书"


def test_pending_terms_without_termbase(tmp_path) -> None:
    pipeline = QualityPipeline(_CtxTranslator())
    assert pipeline.pending_terms() == []
    assert pipeline.rename_and_retranslate("x", "y") == (0, 0)
