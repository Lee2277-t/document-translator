"""LocalLLMTranslator（本地大模型档）测试。

全部用假引擎与假探测，不连 Ollama、不联网——这一档的可用性逻辑
（探测、回退、救回、缓存）必须能在 CI 里稳定验证。
"""

from __future__ import annotations

from pathlib import Path

import core.local_llm as llm
from core.local_llm import LocalLLMTranslator, probe_local_llm


class _FakeEngine:
    """假本地大模型引擎：按脚本返回，可模拟中途挂掉。"""

    name = "local-llm"

    def __init__(self, script: dict[str, str] | None = None, boom: set[str] | None = None):
        self.script = script or {}
        self.boom = boom or set()
        self.calls: list[str] = []

    def translate(self, text: str, ctx=None) -> str:
        self.calls.append(text)
        if text in self.boom:
            raise llm.TranslationError("模拟服务中断")
        return self.script.get(text, f"译:{text}")


def _translator(tmp_path: Path, monkeypatch, engine, **kwargs) -> LocalLLMTranslator:
    monkeypatch.setattr(
        llm, "probe_local_llm", lambda *a, **k: (engine, "ok")
    )
    return LocalLLMTranslator(
        model="test-model",
        stop_event=None,
        log=lambda message: None,
        style="standard",
        cache_path=str(tmp_path / "cache.sqlite"),
        **kwargs,
    )


def test_engine_unavailable_falls_back_to_local(tmp_path: Path, monkeypatch) -> None:
    """探测失败：整批交给回退引擎，并且回退只构造一次。"""
    monkeypatch.setattr(llm, "probe_local_llm", lambda *a, **k: (None, "服务未启动"))

    from core.local_translator import LocalTranslator

    calls = {"n": 0}

    def fake_translate_texts(self, texts, progress=None):
        calls["n"] += 1
        return [f"离线:{text}" for text in texts]

    monkeypatch.setattr(
        LocalTranslator, "translate_texts", fake_translate_texts
    )

    tr = _translator(tmp_path, monkeypatch, None)
    assert tr.engine is None
    out = tr.translate_texts(["hello world", "second line"])
    assert out == ["离线:hello world", "离线:second line"]
    assert calls["n"] == 1
    assert "已回退" in tr.summary()


def test_happy_path_uses_engine_and_cache(tmp_path: Path, monkeypatch) -> None:
    """引擎可用：走引擎、写缓存，第二次同句直接命中缓存不再调引擎。"""
    engine = _FakeEngine({"hello": "你好"})
    tr = _translator(tmp_path, monkeypatch, engine)

    first = tr.translate_texts(["hello"])
    assert first == ["你好"]
    assert tr.translated_count == 1
    assert engine.calls == ["hello"]

    second = tr.translate_texts(["hello"])
    assert second == ["你好"]
    assert tr.cache_hits == 1
    assert engine.calls == ["hello"], "缓存命中时不应再打模型"


def test_cache_profile_includes_model(tmp_path: Path, monkeypatch) -> None:
    """缓存键必须带模型名：换模型等于换译者，旧译文不能混用。"""
    engine = _FakeEngine({"hello": "你好"})
    tr = _translator(tmp_path, monkeypatch, engine)
    assert "test-model" in tr._cache_profile()


def test_mid_batch_failure_rescues_only_failed(tmp_path: Path, monkeypatch) -> None:
    """中途挂掉：只把失败的段交给回退，已翻好的不重翻。"""
    engine = _FakeEngine(boom={"bad line"})
    tr = _translator(tmp_path, monkeypatch, engine)

    from core.local_translator import LocalTranslator

    rescued: list[list[str]] = []

    def fake_translate_texts(self, texts, progress=None):
        rescued.append(list(texts))
        return [f"救回:{text}" for text in texts]

    monkeypatch.setattr(
        LocalTranslator, "translate_texts", fake_translate_texts
    )

    out = tr.translate_texts(["good line", "bad line", "another good"])
    assert out == ["译:good line", "救回:bad line", "译:another good"]
    assert rescued == [["bad line"]], "只应救回失败那段"
    assert tr.rescued_count == 1
    assert tr.failed_count == 0, "救回后失败计数要扣掉"


def test_all_fail_switches_wholesale(tmp_path: Path, monkeypatch) -> None:
    """整批全挂：一次性整体切换回退，而不是一段段撞墙。"""
    engine = _FakeEngine(boom={"alpha line", "beta line"})
    tr = _translator(tmp_path, monkeypatch, engine, fallback="cloud")

    import core.translator as translator_module

    calls = {"n": 0}

    def fake_translate_texts(self, texts, progress=None):
        calls["n"] += 1
        return [f"救回:{text}" for text in texts]

    monkeypatch.setattr(
        translator_module.Translator, "translate_texts", fake_translate_texts
    )

    out = tr.translate_texts(["alpha line", "beta line"])
    assert out == ["救回:alpha line", "救回:beta line"]
    assert calls["n"] == 1
    assert tr.rescued_count == 2


def test_empty_and_unchanged_segments_skip_engine(tmp_path: Path, monkeypatch) -> None:
    """空段与无需翻译的段不进引擎——不浪费本地推理。"""
    engine = _FakeEngine()
    tr = _translator(tmp_path, monkeypatch, engine)
    out = tr.translate_texts(["", "   ", "123"])
    assert out == ["", "   ", "123"]
    assert engine.calls == []
    assert tr.skipped_count == 3


def test_probe_reports_missing_model(tmp_path: Path, monkeypatch) -> None:
    """探测到服务但没有目标模型：原因里要列出本机可用模型。"""
    monkeypatch.setattr(
        llm.LocalLLMEngine,
        "installed_models",
        lambda self: ["qwen2.5:3b", "gemma2:2b"],
    )
    engine, reason = probe_local_llm(model="qwen2.5:7b")
    assert engine is None
    assert "qwen2.5:3b" in reason and "gemma2:2b" in reason


def test_probe_accepts_tagged_model_name() -> None:
    """qwen2.5:7b 与 qwen2.5:7b-instruct 算同一个模型（Ollama tag 差异）。"""
    engine = llm.LocalLLMEngine(model="qwen2.5:7b")
    assert engine._matches(["qwen2.5:7b-instruct-q4_K_M"])
    assert not engine._matches(["qwen2.5:3b"])
