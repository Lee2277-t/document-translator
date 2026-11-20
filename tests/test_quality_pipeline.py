"""质量管线集成测试：分片→装配→翻译→校验→校改 全链路。"""

from __future__ import annotations

from core.quality.pipeline import Corrector, QualityPipeline
from core.quality.segments import segments_from_texts
from core.state import StateStore
from core.termbase import Termbase


class _CtxTranslator:
    """假翻译器：吃 ctx，把收到的上下文记下来供断言。"""

    def __init__(self, reply=None) -> None:
        self.ctxs: list = []
        self.reply = reply
        self.engine = None  # 不启用 LLM 校验层

    def translate_texts(self, texts, progress=None, ctx=None):
        self.ctxs.append(ctx)
        if self.reply is not None:
            return [self.reply.get(text, text) for text in texts]
        return [f"{text}（译）" for text in texts]


class _PlainTranslator:
    """旧式翻译器：签名里没有 ctx。"""

    def __init__(self) -> None:
        self.calls = 0

    def translate_texts(self, texts, progress=None):
        self.calls += 1
        return [f"{text}（译）" for text in texts]


class _FakeEngine:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def translate(self, text: str, ctx=None) -> str:
        return self.reply


def test_pipeline_runs_end_to_end(tmp_path) -> None:
    translator = _CtxTranslator()
    pipeline = QualityPipeline(
        translator, store=StateStore(str(tmp_path / "s.sqlite"))
    )
    result = pipeline.run(segments_from_texts(["你好世界", "第二章", "继续内容"]))

    assert len(result.translations) == 3
    assert result.scenes == 2
    assert result.translations[0] == "你好世界（译）"
    assert result.ok


def test_pipeline_injects_context_into_ctx_aware_translator(tmp_path) -> None:
    """命中术语与场景摘要必须真的到达引擎侧。"""
    store = StateStore(str(tmp_path / "s.sqlite"))
    termbase = Termbase(store)
    termbase.adopt("魔導書", "魔导书")

    translator = _CtxTranslator()
    pipeline = QualityPipeline(translator, termbase=termbase)
    pipeline.run(
        segments_from_texts(["他打开魔導書", "第2章", "又提到魔導書"])
    )

    first_ctx = translator.ctxs[0]
    assert first_ctx is not None
    assert first_ctx.terms == (("魔導書", "魔导书"),)
    assert first_ctx.terms_fingerprint
    # 第二个场景应拿到第一个场景的尾部作为摘要
    second_ctx = translator.ctxs[1]
    assert second_ctx.summary


def test_pipeline_works_with_legacy_translator(tmp_path) -> None:
    """旧翻译器（无 ctx 参数）不能被质量引擎带崩。"""
    translator = _PlainTranslator()
    pipeline = QualityPipeline(translator)
    result = pipeline.run(segments_from_texts(["hello", "world"]))
    assert result.translations == ["hello（译）", "world（译）"]


def test_missing_term_translation_triggers_correction(tmp_path) -> None:
    """术语漂移被抓到后，校改引擎应被调用并产出正确译名。"""
    store = StateStore(str(tmp_path / "s.sqlite"))
    termbase = Termbase(store)
    termbase.adopt("魔導書", "魔导书")

    translator = _CtxTranslator(reply={"他翻开魔導書。": "他翻开了那本书。"})
    translator.engine = _FakeEngine("他翻开了魔导书。")
    pipeline = QualityPipeline(translator, termbase=termbase)

    result = pipeline.run(segments_from_texts(["他翻开魔導書。"]))
    assert result.corrected == 1
    assert result.translations[0] == "他翻开了魔导书。"
    # 校改后重新校验，旧问题不应留在报告里
    assert not [
        issue for issue in result.issues if issue.dimension == "terminology"
    ]


def test_correction_rejected_when_it_makes_things_worse(tmp_path) -> None:
    """校改后问题变多：保留原译文。校改是改善，不是赌博。"""
    store = StateStore(str(tmp_path / "s.sqlite"))
    termbase = Termbase(store)
    termbase.adopt("魔導書", "魔导书")

    translator = _CtxTranslator(
        reply={"请看 Zxq0Z 与魔導書。": "请看 Zxq0Z 与那本书。"}
    )
    # 校改引擎把占位符弄丢了 → 重新校验后问题从 1 条变 2 条 → 必须回退原译文
    translator.engine = _FakeEngine("请看那本书。")
    pipeline = QualityPipeline(translator, termbase=termbase)

    result = pipeline.run(segments_from_texts(["请看 Zxq0Z 与魔導書。"]))
    assert result.corrected == 0
    assert result.translations[0] == "请看 Zxq0Z 与那本书。"


def test_failing_translator_keeps_source(tmp_path) -> None:
    """一个场景炸掉不拖垮整轮：该场景保留原文。"""

    class _Boom:
        def translate_texts(self, texts, progress=None, ctx=None):
            raise RuntimeError("模型服务挂了")

    pipeline = QualityPipeline(_Boom())
    result = pipeline.run(segments_from_texts(["a line", "another line"]))
    assert result.translations == ["a line", "another line"]
    assert result.issues


def test_progress_reaches_total(tmp_path) -> None:
    seen: list[tuple[int, int]] = []

    def progress(done: int, total: int, message: str) -> None:
        seen.append((done, total))

    pipeline = QualityPipeline(_CtxTranslator())
    pipeline.run(segments_from_texts(["one", "two", "three"]), progress=progress)
    assert seen[-1] == (3, 3)


def test_corrector_keeps_original_on_engine_failure() -> None:
    class _Boom:
        def translate(self, text, ctx=None):
            raise RuntimeError("nope")

    from core.quality.verifier import Issue

    corrector = Corrector(engine=_Boom())
    fixed = corrector.correct(
        "原文", "原译文", [Issue("major", "terminology", "s1", "术语未采用")]
    )
    assert fixed == "原译文"


def test_corrector_skips_when_no_issues() -> None:
    corrector = Corrector(engine=_FakeEngine("不该被调用"))
    assert corrector.correct("原文", "原译文", []) == "原译文"
