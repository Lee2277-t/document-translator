"""译名一致性检查（初版）。

背景：术语表只对「整段就是一条术语」生效（heading_terms.lookup），正文长句里的
专业词管不到。于是同一个词在 340 页文档里被译成三四种，读者根本没法用，而现有
自检报告一条都报不出来。Zotero 生态的插件把它做成「自动术语提取」，这里用不了
那么重的方案，改用轻量的共现统计。

做法（共现交集法）：
1. 从所有原文段里挑候选术语：长度 ≥5 的英文词/词组，出现 ≥3 次，不是停用词；
2. 取出「含该术语的原文段」对应的译文，算它们两两之间的中文二字词交集；
3. 交集为空 → 这些段很可能把同一个词译成了不同的东西，报「译名不一致」。

判定刻意保守：只报「完全没有任何共同二字词」的情况，宁可漏报。误报的代价是用户
白看一条，漏报的代价是译名混乱——两者都还能接受，但噪音太多这份报告就没人看了。

v1.4.2：``check_term_consistency`` 新增可选 ``locations``（与 pairs 等长的位置
标签，如「段落 3」「第 2 页」）。疑点因此带上 ``TermIssue.places``——报告的
价值是「去哪段看」，只给词不给位置等于让用户全文 Ctrl+F。不传 locations 时
places 为空、行为与之前完全一致。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

_MIN_TERM_LEN = 5
_MIN_OCCURRENCE = 3
_MAX_TERMS = 40
_MAX_ISSUES = 12
#: 每个疑点最多记几个出现位置：note 里列太多反而没人看，前几个够定位了。
_MAX_PLACES = 5

_STOPWORDS = frozenset(
    """the and for that this with from have been will would could should there
    their which while where these those about into over under between during
    within without through because before after above below both each other
    more most some such only very also than then when what were they them
    your ours its his her not but you all can are was has had our out who
    how why any few own too nor now per via may must shall""".split()
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _terms(source: str) -> list[str]:
    """候选术语 = 长度 ≥5 的单个词 + 两个词首字母都大写的专有名词。

    不用「任意连续两词」：`Revenue grew` 这种根本不是术语，还会把真正的
    `Revenue` 吃掉（正则贪心匹配），这正是第一版踩的坑。
    """
    words = _WORD_RE.findall(source)
    found = []
    for index, word in enumerate(words):
        if len(word) >= _MIN_TERM_LEN and word.lower() not in _STOPWORDS:
            found.append(word)
        if index + 1 < len(words):
            following = words[index + 1]
            if word[0].isupper() and following[0].isupper():
                found.append(f"{word} {following}")
    return found


def _cjk_bigrams(text: str) -> set[str]:
    chars = "".join(_CJK_RE.findall(text))
    return {chars[i : i + 2] for i in range(len(chars) - 1)}


@dataclass
class TermIssue:
    term: str  # 疑似译名不统一的原文术语
    occurrences: int  # 在原文里出现的段数
    samples: list[str]  # 几条译文样本，供人工判断
    #: 该术语出现的位置标签（如「段落 3」「第 2 页」），最多 _MAX_PLACES 个。
    #: v1.4.2 追加在字段末尾（v1.3.0 教训：新字段不插在已有字段中间）；
    #: 不传 locations 时为空列表，老调用方看到的行为与之前完全一致。
    places: list[str] = field(default_factory=list)


def _unique_places(places: list[str], limit: int = _MAX_PLACES) -> list[str]:
    """位置标签去重（保序）并截断。

    同一个词在同一段里出现两次只记一次位置——报告要的是「去哪些段看」，
    不是「这个词一共出现几次」（那个数在 occurrences 里）。
    """
    found: list[str] = []
    for place in places:
        if place and place not in found:
            found.append(place)
        if len(found) >= limit:
            break
    return found


def check_term_consistency(
    pairs: list[tuple[str, str]], locations: list[str] | None = None
) -> list[TermIssue]:
    """pairs 是 [(原文段, 译文段), ...]，顺序与文档一致。

    ``locations`` 是与 pairs 等长的位置标签列表（如「段落 3」「第 2 页」）。
    传入时每个疑点带上该术语出现的位置（``TermIssue.places``），报告的 note
    才能告诉用户「去哪段看」。不传、或长度与 pairs 不一致时 places 为空、
    行为与之前完全一致——长度不一致说明调用方串位了，错位的位置比没有
    位置更糟，宁可不用。
    """
    if len(pairs) < _MIN_OCCURRENCE:
        return []

    counts: Counter[str] = Counter()
    per_term: dict[str, list[str]] = defaultdict(list)
    per_place: dict[str, list[str]] = defaultdict(list)
    usable = (
        locations
        if locations is not None and len(locations) == len(pairs)
        else None
    )
    for index, (source, translated) in enumerate(pairs):
        if not translated or not translated.strip():
            continue
        place = usable[index] if usable is not None else ""
        for term in _terms(source):
            counts[term] += 1
            per_term[term].append(translated)
            if place:
                per_place[term].append(place)

    issues: list[TermIssue] = []
    for term, total in counts.most_common(_MAX_TERMS):
        if total < _MIN_OCCURRENCE:
            continue
        translations = per_term[term]
        if len(translations) < 2:
            continue
        shared = _cjk_bigrams(translations[0])
        if not shared:
            continue
        for other in translations[1:]:
            shared &= _cjk_bigrams(other)
            if not shared:
                break
        if shared:
            continue
        issues.append(
            TermIssue(
                term=term,
                occurrences=total,
                samples=[t.strip()[:40] for t in translations[:3]],
                places=_unique_places(per_place[term]),
            )
        )
        if len(issues) >= _MAX_ISSUES:
            break
    return issues


def issue_note(issue: TermIssue) -> str:
    """自检报告里「译名不一致」疑点的 note。

    带上出现位置（调用方传了 locations 才有）：用户知道有这个词之后，
    下一个动作永远是「它在哪几段」——报告的价值就是省掉这一步。
    不传 locations 时与旧版逐字一致。
    """
    head = f"出现 {issue.occurrences} 次"
    if issue.places:
        listed = "、".join(issue.places)
        # places 有上限，被截断时用一个「等」字交代「还有」
        if issue.occurrences > len(issue.places):
            listed += "等"
        head += f"（{listed}）"
    return f"{head}，译法可能不统一：" + " / ".join(issue.samples)
