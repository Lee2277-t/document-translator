"""送翻译前的分段与切句规则（v1.6.4，G1；v1.8.0 起规则表可由用户编辑）。

原先这套规则直接写死在 ``core/translator.py::split_for_api`` 里：只按拉丁标点
（``.!?;:``）切句，一句超长时只会「找最后一个空格下刀，找不到就从中间硬切」。
纯英文原文这么切没问题，但两种真实场景会踩穿它：

1. **中译英方向**（v1.5.8）——原文是中文，句末标点是全角「。！？」，
   原正则一个都不认，整段中文一句也切不开；
2. **保密离线档**——``MAX_CHUNK_CHARS = 1200`` 比在线档的 4000 小得多，
   中文长段更容易超限，而中文词与词之间没有空格，
   ``rfind(" ")`` 必然落空，于是退化成「正好在 limit 处一刀劈开」。

劈口落在中文词中间时，两半各自送翻、结果再拼回来，成品在劈口处必然错乱
（「人工智」+「能」被当成两个不同的词各译一遍）。本模块把这两件事收在
一起：按标点找自然句边界（中西文标点都认），非切不可时也只把连续拉丁串
（单词、数字、版本号、日期）整串挪到下一片，不劈开它。

v1.8.0 起，「哪些字符算句末标点」与「哪些位置不许下刀」由
``core/segment_rules.py`` 那张可编辑表决定；**表为空时逐字走下面的内置规则**，
一个字符都不变。
"""

from __future__ import annotations

from . import segment_rules

#: 全角句末标点：紧跟其后即可切开，不需要空格（中文没有句间空格）。
_CJK_STOP = "。！？；："

#: 切开后要跟着前半句一起带走的收尾符号（引号、括号），否则右半边孤零零挂在下一片开头。
_CJK_CLOSE = "”』）」』】》’"
_LATIN_CLOSE = "\"')]}»’"

#: 半角句末标点：必须「后面紧跟空白」才算句边界，这样 "U.S."、"e.g." 与
#: 小数点不会被误切（原实现靠 ``(?<=[.!?;:])\\s+`` 的前查做到了这一点，口径保留）。
_LATIN_STOP = ".!?;:"

#: 退让上限：为了不在拉丁串中间下刀，最多往前退这么多字符。
#: 超过就说明遇到超长 token（URL、base64 片段），硬切反而比整串挪走更省事。
_MAX_BACKOFF = 24


def _rules():
    return segment_rules.active()


def _blocked(text: str, end: int, rules) -> bool:
    """候选切点（``end`` = 句末标点之后一位）的前文命中「不许下刀」时返回真。

    只看固定回看窗口以内的前文：规则表本来就在防「这一个点该不该切」，
    窗口越长越容易撞上远处的无关字符，也把正则的回溯代价放大。
    """
    if not rules.blockers:
        return False
    window = text[max(0, end - segment_rules.LOOKBACK):end]
    return any(pattern.search(window) for pattern in rules.blockers)


def _is_run_char(char: str) -> bool:
    """连续拉丁串的组成字符：ASCII 字母数字，外加把它们连成整串的 ``.,%/:-``。

    ``container``、``3.14``、``v1.6.4``、``2026-10-28``、``50%``、``a/b`` 都是
    一个整体——劈在任何一处，翻译端都会把它当成两个陌生的词。
    """
    if char.isascii() and char.isalnum():
        return True
    return char in ".,%/:-"



def _is_decimal(text: str, index: int) -> bool:
    """小数点判定：两侧都是数字（3.14），或前一位是数字且后面是数字串（1.200 之类）。"""
    prev = text[index - 1] if index > 0 else ""
    nxt = text[index + 1] if index + 1 < len(text) else ""
    return prev.isdigit() and nxt.isdigit()


def _skip_close(text: str, index: int, closers: str) -> int:
    """把紧跟在标点后的收尾符号（引号/括号）并进前半句，返回真正的切点。"""
    while index < len(text) and text[index] in closers:
        index += 1
    return index


def split_sentences(text: str) -> list[str]:
    """按「中文标点紧跟切、西文标点须跟空白切、换行必切」拆出句子。

    返回的片段保留原样（不 strip、不合并空白），调用方 ``split_for_api``
    负责去空白与拼片；空片段一律丢掉。
    """
    rules = _rules()
    cjk_stop = _CJK_STOP + rules.cjk_extra
    latin_stop = _LATIN_STOP + rules.latin_extra
    parts: list[str] = []
    start = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in cjk_stop:
            end = index + 1
            if _blocked(text, end, rules):
                index += 1
                continue
            cut = _skip_close(text, end, _CJK_CLOSE)
            parts.append(text[start:cut])
            start = index = cut
            continue
        if char in latin_stop:
            if char == "." and _is_decimal(text, index):
                index += 1
                continue
            nxt = text[index + 1] if index + 1 < length else ""
            if nxt in " \t\n":
                end = index + 1
                if _blocked(text, end, rules):
                    index += 1
                    continue
                cut = _skip_close(text, end, _LATIN_CLOSE)
                parts.append(text[start:cut])
                # 句间空白两边都不属于（原 re.split(r"(?<=[.!?;:])\\s+") 把它当
                # 分隔符吃掉），留着会让每个后续片段挂一个前导空格。
                while cut < length and text[cut] in " \t":
                    cut += 1
                start = index = cut
                continue
            index += 1
            continue
        if char == "\n":
            if text[start:index].strip():
                parts.append(text[start:index])
            index += 1
            start = index
            continue
        index += 1
    if text[start:].strip():
        parts.append(text[start:])
    return [part for part in parts if part.strip()]


def _backoff_to_run_start(text: str, limit: int) -> int | None:
    """切点落在连续拉丁串内部时，退到该串起点；退不动（串太长）返回 None。"""
    if limit <= 0 or limit >= len(text):
        return None
    if not (_is_run_char(text[limit - 1]) and _is_run_char(text[limit])):
        return None
    back = limit
    while back > 0 and _is_run_char(text[back - 1]):
        back -= 1
    if back == 0 or limit - back > _MAX_BACKOFF:
        return None
    return back


def safe_cut(text: str, limit: int) -> tuple[str, str]:
    """把超长文本切成「前一片（长度 ≤ limit）」与「后一片」，尽量切在自然边界。

    三档取向，从最理想到最保守：

    1. ``limit`` 之内有句末标点 → 在最后那个标点之后下刀（中文长段主要靠这条）；
    2. 否则找最后一个空白下刀（西文原文，与原实现同口径）；
    3. 连空白都没有 → 硬切在 ``limit``，但如果劈口正好落在连续拉丁串中间，
       把整个串挪到后一片（宁可前一片短几个字符，也不把 ``container`` 切成
       ``conta`` + ``iner``）。
    """
    if len(text) <= limit:
        return text, ""
    rules = _rules()
    cjk_stop = _CJK_STOP + rules.cjk_extra
    latin_stop = _LATIN_STOP + rules.latin_extra
    window = text[:limit]
    cut: int | None = None
    for offset in range(len(window)):
        index = len(window) - offset - 1
        char = window[index]
        if char in cjk_stop:
            if _blocked(window, index + 1, rules):
                continue
            cut = _skip_close(window, index + 1, _CJK_CLOSE)
            break
        if char in latin_stop and not (char == "." and _is_decimal(window, index)):
            following = window[index + 1] if index + 1 < len(window) else " "
            if following in " \t" and not _blocked(window, index + 1, rules):
                cut = _skip_close(window, index + 1, _LATIN_CLOSE)
                break
    if cut is None:
        space = window.rfind(" ")
        if space >= limit // 2:
            cut = space
    if cut is None:
        cut = limit
        shifted = _backoff_to_run_start(text, limit)
        if shifted is not None:
            cut = shifted
    if cut <= 0:
        cut = limit
    return text[:cut].strip(), text[cut:].lstrip()
