"""引擎统一接口：所有翻译后端（在线 / 离线小模型 / 本地大模型 / 付费 API）都实现它。

为什么不直接把所有引擎的签名改成 ``translate(text, ctx)``：

项目里现有的 ``EdgeEngine`` / ``BingEngine`` / ``MyMemoryEngine`` / Marian / NLLB
都已经在跑，而且带着各自的批量参数、熔断逻辑和测试用例。一次性改签名等于
在同一个 commit 里改五处实现 + 全部调用点，这种改法一旦出问题很难二分定位。

所以这里采用**能力探测**：引擎类上带一个 ``supports_ctx`` 标记，调用方通过
``call_one`` / ``call_batch`` 统一入口决定是否传上下文。旧引擎完全不用动
（默认 ``supports_ctx = False``），新引擎（阶段 2 的 LocalLLMEngine）继承
``Engine`` 并把标记打开即可。改一处，而不是改五处。
"""

from __future__ import annotations

from typing import Callable, Sequence

from .ctx import TranslationContext
from .common import TranslationError


class Engine:
    """翻译引擎基类。子类至少要实现 ``translate``。"""

    #: 引擎标识，进缓存键。同名引擎的译文可以互相复用。
    name = "base"
    #: 是否接受上下文。False 表示这是个逐句独立的引擎，传入 ctx 会被忽略。
    supports_ctx = False
    #: 人类可读的名字，日志与界面上显示。
    label = "引擎"

    def translate(self, text: str, ctx: TranslationContext | None = None) -> str:
        raise NotImplementedError

    def translate_batch(
        self, texts: Sequence[str], ctx: TranslationContext | None = None
    ) -> list[str]:
        """默认实现就是逐条调用。支持批量的引擎应覆写它以提高吞吐。"""
        return [self.translate(text, ctx) for text in texts]

    def available(self) -> bool:
        """引擎当前是否可用（模型加载了 / 服务连得上）。健康检查需要的结果。"""
        return True

    def describe(self) -> str:
        return f"{self.label}（{self.name}）"


def supports_context(engine: object) -> bool:
    return bool(getattr(engine, "supports_ctx", False))


def call_one(engine: object, text: str, ctx: TranslationContext | None = None) -> str:
    """单段翻译。按引擎能力决定是否传上下文。"""
    if supports_context(engine):
        result = engine.translate(text, ctx)  # type: ignore[attr-defined]
    else:
        result = engine.translate(text)  # type: ignore[attr-defined]
    if not isinstance(result, str):
        raise TranslationError(f"{getattr(engine, 'label', '引擎')}返回了非文本结果")
    return result


def call_batch(
    engine: object, texts: Sequence[str], ctx: TranslationContext | None = None
) -> list[str]:
    """批量翻译。

    * 引擎实现了 translate_batch → 用它（快）；
    * 引擎只有 translate → 退化成逐条调用；
    * 两种情况都做了一次数量校验：返回条数对不上时必须报错，
      否则会出现"第 7 段的译文被贴到第 8 段上"这种最难查的错位事故。
    """
    total = len(texts)
    if total == 0:
        return []
    with_ctx = supports_context(engine)
    batch_fn = getattr(engine, "translate_batch", None)
    if callable(batch_fn):
        if with_ctx:
            results = batch_fn(texts, ctx)
        else:
            results = batch_fn(texts)
    else:
        results = [call_one(engine, text, ctx) for text in texts]
    if not isinstance(results, list) or len(results) != total:
        raise TranslationError(
            f"批量翻译返回数量不符：送入 {total} 段，返回 {len(results) if isinstance(results, list) else '?'} 段"
        )
    return results
