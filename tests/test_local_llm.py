"""本地大模型引擎的测试（只用回环服务和测试替身，无需安装模型）。

这几条测试守护的是三条硬约束，其余都可以商量：
1. 占位符与换行不能丢——丢了就变成"术语被译掉 / 段落粘成一坨"；
2. 服务不可用时必须**给出人话原因**并回退，不能卡住用户；
3. 批量翻译不能错位——宁可慢，也不能把第 7 段的译文贴到第 8 段上。
"""

from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.local_llm as local_llm  # noqa: E402
from core.common import TranslationError  # noqa: E402
from core.ctx import context_from  # noqa: E402
from core.local_llm import (  # noqa: E402
    LocalLLMEngine,
    build_context_block,
    clean_translation,
    probe_local_llm,
)


def _reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> LocalLLMEngine:
    """一个 HTTP 层已被替换好的引擎，返回固定译文并记录请求体。"""
    captured: list[dict] = []

    def fake_post(url: str, payload: dict, timeout: float):
        captured.append(payload)
        return _reply("这是译文")

    monkeypatch.setattr(local_llm, "_http_post_json", fake_post)
    engine = LocalLLMEngine(model="qwen2.5:7b")
    engine.captured = captured  # type: ignore[attr-defined]
    return engine


def test_translate_returns_cleaned_text(engine: LocalLLMEngine) -> None:
    assert engine.translate("Hello") == "这是译文"


def test_model_decorations_are_stripped() -> None:
    """模型特别爱在开头加「译文：」，或给整段套一层引号。"""
    assert clean_translation("译文：这是内容") == "这是内容"
    assert clean_translation('"这是内容"') == "这是内容"
    assert clean_translation("Chinese translation: 这是内容") == "这是内容"


def test_placeholder_and_newlines_survive(engine: LocalLLMEngine) -> None:
    """占位符掉了就意味着术语被译掉，这是最严重的失效形态之一。"""
    import core.local_llm as module

    def fake_post(url: str, payload: dict, timeout: float):
        return _reply("我方已净值 Zxq0Z 增长了 12%")

    module._http_post_json = fake_post  # type: ignore[assignment]
    result = engine.translate("Our net profit rose 12%")
    assert "Zxq0Z" in result
    assert "12%" in result


def test_context_is_injected_into_system_prompt(engine: LocalLLMEngine) -> None:
    ctx = context_from(target_lang="zh", characters=["司"])
    ctx = ctx.with_(
        terms=(("net profit", "净利润"),),
        character_sheets=(("司", "自称「俺」，语气粗枝大叶"),),
        summary="上一幕两人决裂",
    )
    engine.translate("He left.", ctx=ctx)
    system = engine.captured[0]["messages"][0]["content"]  # type: ignore[attr-defined]
    assert "净利润" in system
    assert "司" in system and "粗枝大叶" in system
    assert "上一幕两人决裂" in system


def test_context_without_content_produces_no_block() -> None:
    assert build_context_block(None) == ""
    assert build_context_block(context_from()) == ""


def test_context_block_respects_budget() -> None:
    """预算不够时先砍摘要——它没有术语和人物设定那么要紧。"""
    ctx = context_from(target_lang="zh").with_(
        terms=tuple((f"term{i}", f"词条{i}") for i in range(40)),
        character_sheets=tuple((f"角色{i}", "设定" * 40) for i in range(10)),
        summary="很长的摘要" * 200,
    )
    block = build_context_block(ctx, budget=900)
    assert "必须使用的译名" in block
    assert "角色设定" not in block  # 角色卡没有被丢掉，只是摘要被挤掉了
    assert "前文摘要" not in block


def test_batch_keeps_line_alignment(engine: LocalLLMEngine) -> None:
    results = engine.translate_batch(["one", "two", "three"])
    assert len(results) == 3
    assert results == ["这是译文", "这是译文", "这是译文"]


def test_empty_translation_raises_so_caller_can_fall_back(
    engine: LocalLLMEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """返回空等于没翻：必须报错让上层换引擎，而不是把空白塞进文档。"""
    monkeypatch.setattr(
        local_llm, "_http_post_json", lambda url, payload, timeout: _reply("   ")
    )
    with pytest.raises(TranslationError):
        engine.translate("Hello")


def test_connection_failure_raises_translation_error(
    engine: LocalLLMEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(url: str, payload: dict, timeout: float):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(local_llm, "_http_post_json", explode)
    with pytest.raises(TranslationError):
        engine.translate("Hello")


# ------------------------------------------------------------------ 自检


def test_probe_reports_service_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        local_llm,
        "_http_get_json",
        lambda url, timeout: (_ for _ in ()).throw(RuntimeError("refused")),
    )
    engine, reason = probe_local_llm()
    assert engine is None
    assert "连接失败" in reason


def test_probe_reports_missing_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """起了服务、但没拉模型是新手最常撞到的情况，提示里要给出可用模型名单。"""
    monkeypatch.setattr(
        local_llm,
        "_http_get_json",
        lambda url, timeout: {"data": [{"id": "llama3:8b"}]},
    )
    engine, reason = probe_local_llm(model="qwen2.5:7b")
    assert engine is None
    assert "没找到模型" in reason
    assert "llama3:8b" in reason


def test_probe_ok_when_model_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        local_llm,
        "_http_get_json",
        lambda url, timeout: {"data": [{"id": "qwen2.5:7b-instruct"}]},
    )
    engine, reason = probe_local_llm(model="qwen2.5:7b")
    assert reason == "ok"
    assert engine is not None and engine.available() is True


def test_model_name_tolerates_tags() -> None:
    """``qwen2.5:7b`` 与 ``qwen2.5:7b-instruct`` 应当算同一个模型的两种写法。"""
    engine = LocalLLMEngine(model="qwen2.5:7b")
    assert engine._matches(["qwen2.5:7b-instruct", "llama3"]) is True
    assert engine._matches(["llama3", "mistral"]) is False


def test_loopback_request_does_not_use_proxy_or_follow_redirect(monkeypatch):
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            paths.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(307)
            self.send_header("Location", "/unexpected")
            self.end_headers()

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        service = Thread(target=server.serve_forever, daemon=True)
        service.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/translate"
            with pytest.raises(requests.HTTPError, match="重定向"):
                local_llm._http_post_json(url, {"text": "Synthetic test"}, timeout=2)
        finally:
            server.shutdown()
            service.join()
    assert paths == ["/translate"]
