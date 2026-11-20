"""LocalLLMTranslator × 质量引擎集成测试。

最重要的一条：``quality=True`` 时 translate_texts 走管线，而管线内部
又会调翻译器——如果没包 _RawTranslator 就是无限递归。这个 bug 在单元
测试里看不见（管线测试用的是假翻译器），必须在这里钉住。
"""

from __future__ import annotations

import core.local_llm as llm
from core.local_llm import LocalLLMTranslator


class _FakeEngine:
    name = "local-llm"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def translate(self, text: str, ctx=None) -> str:
        self.calls.append(text)
        if text.startswith("你是翻译审校"):
            return text.split("当前译文：")[-1]  # 校改：原样返回
        if text.startswith("你是翻译质量审校"):
            return '{"issues": []}'
        return f"译:{text}"


def _translator(tmp_path, monkeypatch, engine, **kwargs) -> LocalLLMTranslator:
    monkeypatch.setattr(llm, "probe_local_llm", lambda *a, **k: (engine, "ok"))
    return LocalLLMTranslator(
        model="test-model",
        log=lambda message: None,
        cache_path=str(tmp_path / "cache.sqlite"),
        state_path=str(tmp_path / "cache.sqlite"),
        **kwargs,
    )


def test_quality_mode_runs_pipeline_without_recursion(tmp_path, monkeypatch) -> None:
    """quality=True：走管线，且不递归。"""
    engine = _FakeEngine()
    translator = _translator(tmp_path, monkeypatch, engine, quality=True)
    assert translator.quality_pipeline is not None

    out = translator.translate_texts(["hello world", "second line", "third line"])
    assert out == ["译:hello world", "译:second line", "译:third line"]
    # 每个源文只应被引擎翻译一次（递归会瞬间爆栈或重复调用）
    assert engine.calls.count("hello world") == 1


def test_quality_mode_off_uses_plain_path(tmp_path, monkeypatch) -> None:
    """quality=False：管线不构造，行为与旧版一致。"""
    engine = _FakeEngine()
    translator = _translator(tmp_path, monkeypatch, engine, quality=False)
    assert translator.quality_pipeline is None
    assert translator.translate_texts(["hello"]) == ["译:hello"]


def test_quality_pipeline_failure_falls_back_to_plain(tmp_path, monkeypatch) -> None:
    """管线自己炸掉时不能拉着文档一起死——退回普通翻译。"""

    class _BrokenPipeline:
        def run(self, segments, progress=None):
            raise RuntimeError("管线内部错误")

    engine = _FakeEngine()
    translator = _translator(tmp_path, monkeypatch, engine, quality=True)
    translator.quality_pipeline = _BrokenPipeline()

    assert translator.translate_texts(["hello"]) == ["译:hello"]


def test_engine_unavailable_skips_pipeline(tmp_path, monkeypatch) -> None:
    """引擎不可用时根本不建管线——直接走回退更直接。"""
    monkeypatch.setattr(llm, "probe_local_llm", lambda *a, **k: (None, "服务未启动"))
    translator = LocalLLMTranslator(
        model="test-model", log=lambda message: None, quality=True
    )
    assert translator.quality_pipeline is None
    assert translator.engine is None


def test_seeded_terms_reach_the_engine_prompt(tmp_path, monkeypatch) -> None:
    """术语表里的权威译名必须真的进到提示词里（播种→装配→注入全链路）。"""
    engine = _FakeEngine()
    translator = _translator(tmp_path, monkeypatch, engine, quality=True)

    # 直接往术语库播种一条，再检查装配结果
    pipeline = translator.quality_pipeline
    assert pipeline is not None
    pipeline.termbase.adopt("魔導書", "魔导书")
    assembled = pipeline.assembler.assemble(["他打开魔導書。"])
    assert assembled.context.terms == (("魔導書", "魔导书"),)
    assert "魔導書 → 魔导书" in assembled.block
