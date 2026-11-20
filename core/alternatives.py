"""备选译文（v1.6.2，E4）：同一段让引擎再摇一条不同说法，并排比、点选落地。

为什么不是「预先给每段生成两条」：那会把引擎调用量翻倍，而绝大多数段没人
会看第二遍。备选是**按需**的——在译后编辑窗口里对着某一段点「要备选译文」，
一次一条、最多攒三条，候选跟着这一段存在清单里。

三条口径（与交接稿 §11 对齐，勿推翻）：

- **重摇必须绕开缓存**。不绕开的话引擎原样把上次的译文还回来，所谓「备选」
  和现译一字不差，等于骗人。``bypass_cache`` 在这一次调用期间把翻译器的
  cache 换成只读空壳（读永远落空、写直接丢弃），不动缓存里的正式译文。
- **确定性引擎给不出变体就直说**。离线小模型、DeepL 这类引擎每次结果相同，
  重摇后与现译一致时抛 ``TranslationError`` 让人话说明，不伪造候选、也不把
  同一条存两次。本地大模型档（采样解码）才有真正的不同说法。
- **选候选不算确认通过**。``pick_candidate`` 只把文字填进 ``translation``，
  仍要走既有的「保存写回」；不选就等于没改（``pending`` 为 False、不进翻译
  记忆）。备选是「多一个选项」，不是「自动采用更好的那条」——引擎没有能力
  判断哪条更好，替用户拍板就是越权。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from .common import TranslationError
from .translator import normalize_source

#: 一段最多攒几条候选。三条以上就成了「选择过载」，而且候选越多越没人细看。
MAX_CANDIDATES = 3

_NO_DIFFERENCE = (
    "这一档引擎给不出不同说法（同一句每次结果相同）。"
    "换「本地大模型」档再试，或直接在译文框里手改。"
)


class _EmptyCache:
    """临时顶替 cache 的空壳：读永远落空、写直接丢弃，其余属性透传。

    为什么透传其余属性：``Translator._sync_cache_profile`` 会读 ``_profile``
    决定要不要换缓存实例，``LocalLLMTranslator`` 构造期会读 ``path`` 去开
    state 库——这些都不是「读写缓存」，不该被这次重摇影响。
    """

    def __init__(self, original: Any) -> None:
        self._original = original

    def get(self, *args: Any, **kwargs: Any) -> None:
        return None

    def set(self, *args: Any, **kwargs: Any) -> Any:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


@contextmanager
def bypass_cache(translator: Any) -> Iterator[None]:
    """这期间该翻译器的缓存读写全部失效（重摇专用，出范围自动还原）。

    连**已经建好的回退档**一起换壳：本地大模型档在不可用时会回退到免费云端
    或离线小模型，那一层有自己独立的一份缓存——只换外层等于没换，重摇拿到
    的还是回退档上次的译文。（回退档在调用过程中才懒建的，这一轮管不到；
    这种情况下 ``generate_alternative`` 会把「与现译相同」如实报出来。）
    """
    layers = [translator]
    fallback = getattr(translator, "_fallback", None)
    if fallback is not None:
        layers.append(fallback)
    swapped: list[tuple[Any, Any]] = []
    for layer in layers:
        cache = getattr(layer, "cache", None)
        if cache is None or isinstance(cache, _EmptyCache):
            continue
        layer.cache = _EmptyCache(cache)
        swapped.append((layer, cache))
    try:
        yield
    finally:
        for layer, cache in swapped:
            layer.cache = cache


def generate_alternative(translator: Any, item: Any) -> str:
    """用同一个翻译器把这段原文重摇一次，返回一条与现译不同的候选。

    抛 ``TranslationError``：没有原文 / 引擎返回空 / 与现译相同（确定性引擎）。
    """
    source = (item.source or "").strip()
    if not source:
        raise TranslationError("这段没有原文，生不出备选译文。")
    with bypass_cache(translator):
        results = translator.translate_texts([source])
    text = (results[0] if results else "").strip()
    if not text:
        raise TranslationError("引擎没有返回备选译文，请稍后重试。")
    known = {normalize_source(item.translation), normalize_source(item.applied)}
    known.discard("")
    if normalize_source(text) in known:
        raise TranslationError(_NO_DIFFERENCE)
    return text


def add_candidate(item: Any, text: str) -> bool:
    """把一条候选收进这一段（去重、封顶 ``MAX_CANDIDATES``）。收了返回 True。

    去重口径包括现译与上次写回值：候选区里出现一条和正文一样的文字，用户
    点它只会得到一个「看起来没变化」的待写回，纯属噪音。
    """
    text = (text or "").strip()
    if not text:
        return False
    wanted = normalize_source(text)
    seen = {normalize_source(item.translation), normalize_source(item.applied)}
    seen.update(normalize_source(candidate) for candidate in item.candidates)
    seen.discard("")
    if wanted in seen:
        return False
    item.candidates.append(text)
    del item.candidates[:-MAX_CANDIDATES]
    return True


def pick_candidate(item: Any, index: int) -> str:
    """选用第 index 条候选：只改 ``translation``（``applied`` 不动 → 待写回）。"""
    if index < 0 or index >= len(item.candidates):
        raise TranslationError("没有这条备选译文（可能已被替换），请刷新后再选。")
    chosen = item.candidates[index]
    item.translation = chosen
    return chosen
