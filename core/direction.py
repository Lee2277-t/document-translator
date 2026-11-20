"""翻译方向：默认译入简体中文，v1.5.8 起支持译入英文。

为什么是模块级开关，而不是给每个函数加参数
------------------------------------------
方向是**一次运行一个**的全局事实（与 ``app.OFFLINE`` 同性质），而抽段判定
``should_translate`` / ``is_translatable`` 被 Word / PDF / PPT / 脚本四套前端、
十几处调用点共用。把 target 一路透传会改掉几乎每一个公开签名，而且任何一处
漏传的表现都是"这一段没翻"——静默错误，最难查。所以方向只在两个人口设置一次
（命令行 ``--to=`` / 界面「目标语言」下拉），其余模块统一读 ``direction.target()``。

三条口径
--------
1. **默认 ``zh``**：不设置时与 v1.5.7 的行为逐字节一致。
2. **只支持 zh / en 两种目标语**：本工具的定位是「外文 ↔ 中文」，不是通用翻译器。
   译入英文时，源语可以是中文/日文/韩文/俄文等非拉丁文字；已经是英文的段落
   会被跳过（``should_translate`` 里的方向分支）。
3. **方向进缓存键**：``cache_key_part()``。否则同一句原文先译中文再译英文，
   第二次会命中第一次的译文——比不翻更糟。
"""

from __future__ import annotations

import re

ZH = "zh"
EN = "en"
TARGETS = (ZH, EN)
LABELS = {ZH: "中文", EN: "英文"}

#: 各通道的目标语代码。全部集中在这里，改一处就够。
_WEB_CODES = {ZH: "zh-Hans", EN: "en"}
_MYMEMORY_CODES = {ZH: "zh-CN", EN: "en-US"}
_DEEPL_CODES = {ZH: "ZH", EN: "EN"}
_NLLB_TOKENS = {ZH: "zho_Hans", EN: "eng_Latn"}
#: 源语一侧的 NLLB 代码（译入英文时，中文源就是 zho_Hans）。
_NLLB_SOURCES = {ZH: "eng_Latn", EN: "zho_Hans"}

_current = ZH


def set_target(code: str) -> str:
    """设置本次运行的目标语。认不出就抛 ``ValueError``，绝不静默退回中文。

    调用方（CLI / 界面）负责把错误变成人话；这里不做"猜一个"的容错——
    用户写了 ``--to=en`` 却翻出中文，比直接报错难查得多。
    """
    global _current
    normalized = (code or "").strip().lower()
    aliases = {
        "zh": ZH, "zh-hans": ZH, "zh-cn": ZH, "chinese": ZH, "中文": ZH,
        "en": EN, "en-us": EN, "en-gb": EN, "english": EN, "英文": EN,
    }
    resolved = aliases.get(normalized)
    if resolved is None:
        raise ValueError(f"不支持的目标语言：{code}（只支持 zh / en）")
    changed = resolved != _current
    _current = resolved
    if changed:
        _reload_dependents()
    return _current


def reset() -> None:
    """回到默认方向。测试夹具与一次性进程用完复位。"""
    set_target(ZH)


def target() -> str:
    return _current


def is_english() -> bool:
    return _current == EN


def label() -> str:
    return LABELS[_current]


def web_code() -> str:
    """Edge / 微软网页通道的 ``to`` 参数。"""
    return _WEB_CODES[_current]


def mymemory_code() -> str:
    return _MYMEMORY_CODES[_current]


def deepl_code() -> str:
    return _DEEPL_CODES[_current]


def nllb_target() -> str:
    return _NLLB_TOKENS[_current]


def nllb_source(fallback: str = "") -> str:
    """源语的 NLLB 代码。中文源固定 ``zho_Hans``，其余沿用检测结果。"""
    if _current == EN:
        return _NLLB_SOURCES[EN]
    return fallback


def cache_key_part() -> str:
    return _WEB_CODES[_current]


def use_chinese_postprocess() -> bool:
    """风格润色、译名一致性、中文标点归一这些只对中文成品有意义。"""
    return _current == ZH


def term_pairs() -> dict[str, str]:
    """术语锁定的源词→译名。

    译入中文用术语表原方向（外文 → 中文）；译入英文时**整表反向**
    （中文 → 外文）。用户维护的是同一张表，两种方向各取各的一半——
    术语表里 ``net profit = 净利润``，中译英时就该把「净利润」锁成
    ``net profit``，而不是要求用户再维护一张反向表。
    """
    from .heading_terms import USER_TERMS

    if _current == ZH:
        return dict(USER_TERMS)
    reversed_pairs: dict[str, str] = {}
    for source, value in USER_TERMS.items():
        if not source or not value or source == value:
            continue
        # 反向时键是中文、值是外文；同一中文对应多个外文时按 _foreign_rank
        # 择优（拉丁字母优先，再取最短）——只按长度会挑中两个字的日文词。
        existing = reversed_pairs.get(value)
        if existing is None or _foreign_rank(source) < _foreign_rank(existing):
            reversed_pairs[value] = source
    return reversed_pairs


def _foreign_rank(source: str) -> tuple[int, int]:
    """反向术语的择优顺序：纯 ASCII 外文词优先，其次短。

    与 ``heading_terms._reverse_rank`` 同一套口径：用户术语表里同一个中文可能
    对应多个外文写法，挑日文/韩文或带重音的西文都算错译。
    """
    ascii_word = 0 if source.isascii() and re.search(r"[A-Za-z]", source) else 1
    return (ascii_word, len(source))


def lookup_heading(text: str) -> str:
    """标题词表查表，方向感知。"""
    from .heading_terms import lookup, lookup_reverse

    return lookup(text) if _current == ZH else lookup_reverse(text)


def _reload_dependents() -> None:
    """换方向后让依赖方向的缓存失效（目前只有术语锁的编译规则）。"""
    try:
        from . import glossary

        glossary.reload()
    except Exception:  # 术语模块不可用不该让方向切换失败
        pass
