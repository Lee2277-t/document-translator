"""混排段落切分与补译（F3，v1.5.9）。

为什么需要这一层
----------------
``should_translate`` 是**整段**判定：一段里汉字占比够高就整段跳过。合同、
年报、说明书里最常见的中夹英条款因此原文不动地留在译文里——Word/PDF
各自补过一次（A1），其余前端一直漏着。

译入英文时情况相反：一段以英文为主、夹着一句中文，同样整段跳过。
两种情况的共同点是**段落被判成单一源语言，段里另一种语言的子句没人管**。

这一层把「整段会被跳过」的段落切开：只把少数语种子句送进引擎，其余原文
原样保留，译法就地替换。整段会正常翻译的段落一律不碰——引擎按整段处理
更连贯，切碎反而伤质量，也会让缓存与术语命中口径变复杂。

与 Word/PDF 的关系
------------------
``foreign_spans`` 是唯一的外文子句抽取规则：Word 的「就地插入译法」和
PDF 的「run_map 拼回」都改用它，不再各留一份正则。引擎侧用
``fill_for_engine``：所有把整段交给引擎的前端（PPT / 图片 OCR / 字幕 /
JSON）自动获得混排补译。Word/PDF 自己先筛掉了不可译段，不会重复处理。

术语命中
--------
子句译文不记术语命中：``term_hits`` 的段落号是整段编号，子句级命中会
指到一个不存在的「段落 N」。宁可少记，也不给报表一条对不上的行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from . import direction
from .lang_detect import (
    ARABIC_RE,
    ARMENIAN_RE,
    BENGALI_RE,
    CJK_RE,
    CYRILLIC_RE,
    DEVANAGARI_RE,
    ETHIOPIC_RE,
    GEORGIAN_RE,
    GREEK_RE,
    GUJARATI_RE,
    GURMUKHI_RE,
    HANGUL_RE,
    HEBREW_RE,
    KANA_RE,
    KANNADA_RE,
    KHMER_RE,
    LAO_RE,
    LATIN_RE,
    MALAYALAM_RE,
    MYANMAR_RE,
    SINHALA_RE,
    TAMIL_RE,
    TELUGU_RE,
    THAI_RE,
)

#: 拉丁子句至少几个词才算「一句外文」。与 A1 的 Word/PDF 阈值一致：
#: 「OK」「AI」「USB-C」这类夹在中文里的缩写不是要补翻的句子。
MIN_LATIN_WORDS = 4

#: 其他文字（假名、谚文、西里尔…）至少几个字符。这些文字不靠空格分词。
MIN_FOREIGN_CHARS = 4

#: 译入英文时，汉字至少几个字符才值得补翻——一个「中」字可能只是标点用法。
MIN_CJK_CHARS = 4

#: 参与「非拉丁外文」判定的文字。顺序只在分类时用来区分不同文字，
#: 与 lang_detect.SCRIPT_CODES 保持同一组范围；新增文字时两处一起加。
_OTHER_SCRIPTS = (
    KANA_RE,
    HANGUL_RE,
    BENGALI_RE,
    GURMUKHI_RE,
    GUJARATI_RE,
    TAMIL_RE,
    TELUGU_RE,
    KANNADA_RE,
    MALAYALAM_RE,
    SINHALA_RE,
    THAI_RE,
    LAO_RE,
    MYANMAR_RE,
    ETHIOPIC_RE,
    KHMER_RE,
    GREEK_RE,
    GEORGIAN_RE,
    ARMENIAN_RE,
    HEBREW_RE,
    DEVANAGARI_RE,
    CYRILLIC_RE,
    ARABIC_RE,
)

#: 汉字排到标点里（全角括号、引号、句号）时会切断子句，读者看到的译法
#: 会缺半个括号。判定 CJK 片段时把常用中日韩标点一并算进去。
CJK_PUNCT_RE = re.compile(
    r"[\u3000-\u303f\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65]"
)

_CJK = -1
_LATIN = -2

#: 引擎计数器：子句补译是「一段拆成几次引擎调用」，子句级的计数不该
#: 记到整段口径上（否则「翻译 N 段」会凭空多出几段）。补完原样还原，
#: 再把整段从「跳过」挪到「翻译」。
_COUNTERS = (
    "translated_count",
    "skipped_count",
    "cache_hits",
    "failed_count",
    "term_count",
    "echo_count",
    "tm_exact",
    "tm_fuzzy",
    "unsupported_count",
    "no_model_count",
    "fragment_count",
)


@dataclass(frozen=True)
class Segment:
    """段落切片。``translate`` 为 True 的片段会送进引擎。"""

    text: str
    translate: bool


def _normalize(text: str) -> str:
    from .translator import normalize_source

    return normalize_source(text)


def _classify(ch: str) -> int | None:
    if CJK_RE.match(ch) or CJK_PUNCT_RE.match(ch):
        return _CJK
    if LATIN_RE.match(ch):
        return _LATIN
    for index, pattern in enumerate(_OTHER_SCRIPTS):
        if pattern.match(ch):
            return index
    return None


def _runs(text: str) -> list[tuple[int, int, int]]:
    """按文字种类切连续片段。空格/标点不打断片段，但不会成为片段边界。"""
    runs: list[tuple[int, int, int]] = []
    current = None
    start = end = 0
    for index, ch in enumerate(text):
        kind = _classify(ch)
        if kind is None:
            continue
        if current is None:
            current, start, end = kind, index, index + 1
        elif kind == current:
            end = index + 1
        else:
            runs.append((start, end, current))
            current, start, end = kind, index, index + 1
    if current is not None:
        runs.append((start, end, current))
    return runs


def _latin_is_clause(chunk: str) -> bool:
    lowered = chunk.lower()
    # URL / 邮箱/ 路径不是要翻译的外文；混在长句里的也整段跳过，
    # 与 A1 的 Word/PDF 口径一致（拆开送翻反而会把链接翻坏）。
    if "www." in lowered or "://" in lowered or "@" in lowered:
        return False
    words = [token for token in chunk.split() if re.search(r"[A-Za-z]{2}", token)]
    return len(words) >= MIN_LATIN_WORDS


def _chars_of_kind(text: str, kind: int) -> int:
    return sum(1 for ch in text if _classify(ch) == kind)


def foreign_spans(text: str) -> list[tuple[int, int, str]]:
    """返回段落里「少数语种子句」的（起, 止, 文本）。

    译入中文时少数语种是外文（拉丁短语 ≥4 词，或假名/西里尔等 ≥4 字）；
    译入英文时是汉字（≥4 字）与其他非拉丁文字。纯中文/纯英文段落返回空表。

    下标对应**传入的原文**，Word/PDF 依靠它把译法插回原位，因此这里
    不做归一化——归一化会改变字符偏移。
    """
    english = direction.is_english()
    spans: list[tuple[int, int, str]] = []
    for start, end, kind in _runs(text):
        chunk = text[start:end]
        if kind == _LATIN:
            if english or not _latin_is_clause(chunk):
                continue
        elif kind == _CJK:
            if not english or len(CJK_RE.findall(chunk)) < MIN_CJK_CHARS:
                continue
        elif _chars_of_kind(chunk, kind) < MIN_FOREIGN_CHARS:
            continue
        spans.append((start, end, chunk))
    return spans


def minority_spans(text: str) -> list[str]:
    """``foreign_spans`` 的纯文本版（去重，保留首次出现顺序）。"""
    seen: list[str] = []
    for _, _, chunk in foreign_spans(text):
        if chunk not in seen:
            seen.append(chunk)
    return seen


def plan(text: str) -> list[Segment] | None:
    """把「整段会被跳过」的混排段落切成保留/翻译片段。

    不满足条件时返回 None——调用方据此保持原有整段行为，零改动。
    """
    from .translator import is_protected_source, should_translate

    normalized = _normalize(text)
    if not normalized:
        return None
    if should_translate(normalized) or is_protected_source(normalized):
        return None
    spans = foreign_spans(normalized)
    if not spans:
        return None
    segments: list[Segment] = []
    cursor = 0
    for start, end, chunk in spans:
        if start > cursor:
            segments.append(Segment(normalized[cursor:start], False))
        segments.append(Segment(chunk, True))
        cursor = end
    if cursor < len(normalized):
        segments.append(Segment(normalized[cursor:], False))
    return segments


def join(segments: Sequence[Segment], translations: Iterable[str]) -> str:
    """按片段顺序拼回整段。空译文/原文回显的子句保留原文。"""
    iterator = iter(translations)
    output: list[str] = []
    for segment in segments:
        if not segment.translate:
            output.append(segment.text)
            continue
        translated = next(iterator, "")
        if not translated or not translated.strip():
            output.append(segment.text)
        elif _normalize(translated) == _normalize(segment.text):
            output.append(segment.text)
        else:
            output.append(translated.strip())
    return "".join(output)


def fill(
    texts: Sequence[str],
    results: list[str | None],
    translate: Callable[[list[str]], list[str]],
) -> int:
    """就地补译 ``results`` 里被整段跳过的混排段落，返回补上的段数。

    只处理 ``results[index] == texts[index]`` 的段落：已翻译、命中缓存、
    或失败后留了别的内容的段落都不碰。子句翻译失败时整段保持原样——
    补译是增强项，不该让整份文档翻不出来。
    """
    jobs: list[tuple[int, list[Segment]]] = []
    spans: list[str] = []
    for index, source in enumerate(texts):
        if results[index] != source:
            continue
        segments = plan(source)
        if not segments:
            continue
        jobs.append((index, segments))
        spans.extend(segment.text for segment in segments if segment.translate)
    if not spans:
        return 0
    try:
        translated = translate(spans)
    except Exception:
        return 0
    if not isinstance(translated, list) or len(translated) != len(spans):
        return 0
    cursor = 0
    filled = 0
    for index, segments in jobs:
        count = sum(1 for segment in segments if segment.translate)
        piece = translated[cursor : cursor + count]
        cursor += count
        rebuilt = join(segments, piece)
        if _normalize(rebuilt) != _normalize(texts[index]):
            results[index] = rebuilt
            filled += 1
    return filled


def fill_for_engine(
    engine: object,
    texts: Sequence[str],
    results: list[str | None],
    translate: Callable[[list[str]], list[str]],
) -> int:
    """引擎侧入口：带递归保护与计数器还原地调用 ``fill``。

    ``translate`` 通常是引擎自己的 ``translate_texts``。嵌套那一层会重新
    走一遍抽段判定，但子句本身是可译的，不会再次切分；``_mixed_depth``
    是第二道保险（万一子句还是被跳过的混排段，也不会无限递归）。
    """
    if getattr(engine, "_mixed_depth", False):
        return 0
    saved = {name: getattr(engine, name) for name in _COUNTERS if hasattr(engine, name)}
    hits = engine.term_hits if hasattr(engine, "term_hits") else None
    hit_count = len(hits) if hits is not None else 0
    engine._mixed_depth = True
    try:
        filled = fill(texts, results, translate)
    finally:
        engine._mixed_depth = False
        for name, value in saved.items():
            setattr(engine, name, value)
        if hits is not None:
            del hits[hit_count:]
    if filled:
        engine.translated_count = getattr(engine, "translated_count", 0) + filled
        engine.skipped_count = max(0, getattr(engine, "skipped_count", 0) - filled)
    return filled
