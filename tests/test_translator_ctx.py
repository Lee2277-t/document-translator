"""在线 Translator 主路径的集成测试。

写这组测试的起因是阶段 0 的自查：改造 ``translate_texts`` / ``_batch_pass`` /
``_translate_one_raw`` 之后跑全量 pytest 是全绿的，但 grep 发现**没有任何一条测试
触及这条链路**——"全绿"根本不能证明重构没写坏，只能证明别的地方没坏。

这类"测试看着多、主路径却是空白"的洞最容易制造假安全感，所以这里专门补<｜hy_place▁holder▁no▁813｜>线：
正常路径、带上下文路径、以及曾经真实存在过的崩溃 bug（Bing 熔断计数器未初始化）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.ctx import context_from  # noqa: E402
from core.translator import Translator  # noqa: E402


class _FakeChannel:
    """假的在线通道。默认不懂上下文（与真实 Edge/Bing 一致）。"""

    name = "fake"
    label = "假通道"
    supports_ctx = False

    def __init__(self) -> None:
        self.batch_calls: list[tuple] = []
        self.single_calls: list[tuple] = []
        self.fail = False

    def translate(self, text: str) -> str:
        self.single_calls.append((text, None))
        if self.fail:
            raise RuntimeError("网络炸了")
        return f"译：{text}"

    def translate_batch(self, texts: list[str]) -> list[str]:
        self.batch_calls.append((list(texts), None))
        if self.fail:
            raise RuntimeError("批量也炸了")
        return [f"译：{text}" for text in texts]


class _FakeCtxChannel(_FakeChannel):
    """懂上下文的假通道，模拟阶段 2 的本地大模型引擎。"""

    supports_ctx = True

    def translate(self, text: str, ctx=None) -> str:
        self.single_calls.append((text, ctx))
        return f"译：{text}"

    def translate_batch(self, texts: list[str], ctx=None) -> list[str]:
        self.batch_calls.append((list(texts), ctx))
        return [f"译：{text}" for text in texts]


@pytest.fixture
def translator(tmp_path: Path) -> Translator:
    tr = Translator(cache_path=str(tmp_path / "cache.sqlite"))
    tr._preflighted = True  # 跳过真实网络探活
    tr.bing_disabled = True  # 只让 edge 通道跑，避免走到备用链路
    return tr


def test_translate_texts_runs_without_context(translator: Translator) -> None:
    """不带上下文时必须和改造前行为一致——这是阶段 0 的验收底线。"""
    channel = _FakeChannel()
    translator.edge = channel
    results = translator.translate_texts(["The report is ready.", "Please review it."])
    assert results == ["译：The report is ready.", "译：Please review it."]
    assert channel.batch_calls, "应该走批量路径"
    # 旧引擎签名只接受 texts，第二位是 None 表示我们没给它塞多余参数
    assert channel.batch_calls[0][1] is None


def test_translate_texts_forwards_context_to_capable_engine(
    translator: Translator,
) -> None:
    channel = _FakeCtxChannel()
    translator.edge = channel
    ctx = context_from(target_lang="zh", style="formal", summary="上一幕：决裂")
    results = translator.translate_texts(["He left the room."], ctx=ctx)
    assert results == ["译：He left the room."]
    assert channel.batch_calls[0][1] is ctx


def test_context_changes_cache_profile_but_summary_does_not(
    tmp_path: Path, translator: Translator
) -> None:
    """同一句原文，换了角色必须重译；只换场景摘要应当命中同一次缓存。"""
    channel = _FakeCtxChannel()
    translator.edge = channel

    first = context_from(target_lang="zh", characters=["司"])
    translator.translate_texts(["I will go."], ctx=first)
    profile_after_cast = translator.cache._profile

    # 只换场景摘要：profile 不变 → 同一份缓存继续有效
    same_cast_other_scene = first.with_(summary="场景切换了，摘要完全不同")
    translator.translate_texts(["I will go."], ctx=same_cast_other_scene)
    assert translator.cache._profile == profile_after_cast

    # 换角色：profile 必须变，否则会拿到上一个角色语气的译文
    other_cast = context_from(target_lang="zh", characters=["深月"])
    translator.translate_texts(["I will go."], ctx=other_cast)
    assert translator.cache._profile != profile_after_cast


def test_bing_failure_counter_is_initialized(translator: Translator) -> None:
    """回归保护：``_bing_failures_in_row`` 曾经只在代码里被读写，从未被初始化。

    症状是"微软通道连续失败第三次时抛 AttributeError"，本该优雅切换到备用通道的
    请求会被整批丢掉，而且只在特定网络故障下才出现——所以必须有测试盯着。
    """
    tr = translator
    tr.edge_disabled = True
    tr.bing_disabled = False

    broken = _FakeChannel()
    broken.fail = True
    tr.bing = broken

    fallback_ok = _FakeChannel()
    tr.mymemory = fallback_ok

    for _ in range(3):
        tr._translate_one_raw("anything")
    assert tr.bing_disabled is True, "连续三次失败后应当停用微软通道"
    assert tr._bing_failures_in_row >= 3
    assert fallback_ok.single_calls, "应当回落到备用通道而不是抛异常"


def test_single_segment_path_works(translator: Translator) -> None:
    channel = _FakeCtxChannel()
    translator.edge_disabled = False
    translator.edge = channel
    translated, ok, engine = translator._translate_one("Direct call.")
    assert translated == "译：Direct call."
    assert ok is True and engine == "edge"
