from __future__ import annotations

import re

from .heading_terms import load_user_terms
from . import direction

#: 占位符形状。
#: 必须是纯字母数字的连续串——实测带 Unicode 括号的形状（⟦T1⟧）会被 NLLB / Marian
#: 的分词器整个吃掉，而 Zxq0Z 这类能 100% 穿过解码再原样取回。
#: 思路来自 DeepL Glossary 与 WPS 365 术语库：翻译前把术语换成占位符，译完换回
#: 官方译法，这样整篇文档里同一个术语只有一种译法，不受模型随机性影响。
PLACEHOLDER = "Zxq{}Z"
_PH_RE = re.compile(r"(?i)zxq(\d+)z")

#: 残留占位符的形状（v1.4.2）：模型把 ``Zxq0Z`` 改写成 ``ZxqOZ`` 时，
#: ``_PH_RE``（要求中间是数字）认不出来，``unlock`` 也就换不回去，译文里
#: 就留下这种怪 token。与 ``core/quality/verifier.py::_find_placeholders``
#: 同族（首尾字母 Z、中间字母数字），但不要求中间必须是数字——改写正是
#: 把数字段换成了字母，要求数字就永远抓不到它。
#: 边界用前后查找而不是 ``\b``：占位符常常紧贴汉字（「详情如ZxqOZ所述」），
#: ``\b`` 在汉字与字母之间不成立，用 ``\b`` 会整片漏掉。
_LEFTOVER_RE = re.compile(r"(?i)(?<![A-Za-z0-9])z[a-z0-9]{2,5}z(?![A-Za-z0-9])")

_ASCII_WORD = "A-Za-z0-9"
_CYRILLIC = "\u0410-\u044f\u0400-\u04ff"
_MAX_TERMS = 80
_MIN_LEN = 2

#: 术语屈折的最短词长。短词的 ``-s/-ing`` 形态在通用语里到处都是
#: （``runs a business``、``using a hammer``），把它们一律锁成术语译法，
#: 误伤比漏锁更疼；而技术术语几乎都是长词（transformer、container、protocol）。
#: 形态只做「不换词性」的复数/过去式/现在分词，``-er/-est`` 明确不做——
#: ``report → reporter``、``use → user`` 会把派生词一起吃掉，那是另一个词，
#: 锁进译法就是错的（见 ``_inflected_forms``）。
_INFLECT_MIN_LEN = 5

#: 屈折匹配总开关（默认开）。关掉只认术语表里的原形，是「某条术语的
#: 变形被锁错了」时用户的退路，对应 CLI ``--no-term-inflection``。
_inflection_on = True

#: 术语里至少要有一个真正的文字字符，纯符号条目不进正文替换（误伤太大）。
_HAS_LETTER = re.compile(r"[A-Za-z\u00c0-\u024f\u0400-\u04ff\u3040-\u9fff]")

#: 句内锁定的高危词：它们是英语里的通用词，整篇文档到处都是。
#: 一条术语如果**每个词**都在这个集合里（例如 `note = 注释`），句内锁定会造出
#: "Please 注释 that…" 这种灾难，所以这类条目不参与句内替换，只在整段恰好就是
#: 这条术语时才生效（那是原来的 lookup 逻辑，不受影响）。
#: 只要有一个词不在集合里（`net profit` 的 profit），就照常锁定。
_RISKY_WORDS = frozenset(
    """
    a an the and or but if while of in on at to for from by with without
    this that these those there here it its is are was were be been being
    please may can could will would shall should must might
    use used using make made take taken give given get got
    see seen show shown find found
    include including exclude require required provide provided
    follow following above below before after over under
    note notes form forms table tables state states
    case cases point points term terms part parts line lines
    field fields file files order orders rate rates
    other others more most less least same such each both either neither
    any all some new old good bad high low large small first last next
    may main general common special current present subject object
    value values type types level levels set sets group groups
    """.split()
)

_compiled: list[tuple[re.Pattern, str]] | None = None
_skipped: list[str] = []
_inflected: list[str] = []


def _bounds(term: str) -> tuple[str, str]:
    """按术语首尾字符的脚本决定要不要词边界。

    拉丁/西里尔术语加边界，避免在别的单词里被误命中（note 不该命中 denote）；
    中文术语本身没有词边界，直接子串替换。
    """
    first, last = term[0], term[-1]
    if first.isascii() and (first.isalpha() or first.isdigit()):
        return rf"(?<![{_ASCII_WORD}])", rf"(?![{_ASCII_WORD}])"
    if any("\u0400" <= char <= "\u04ff" for char in (first, last)):
        return rf"(?<![{_CYRILLIC}])", rf"(?![{_CYRILLIC}])"
    return "", ""


def _is_risky(term: str) -> bool:
    """整条术语是否由通用词构成。

    `note`、`please note`、`in case` 这类每个词都是通用词，句内替换必然误伤；
    `net profit`、`AI`、`EBITDA` 只要有一个词不是通用词，就照常锁定。
    中文/日文条目不判——它们不在 _RISKY_WORDS 里，自然返回 False。
    """
    words = [word for word in re.split(r"\s+", term.strip()) if word]
    if not words:
        return True
    lowered = [word.strip(",.;:").lower() for word in words]
    return all(word in _RISKY_WORDS for word in lowered)


def skipped() -> list[str]:
    """被判为高危、不参与句内替换的术语。界面/日志用来给用户一个交代。"""
    rules()
    return list(_skipped)


def inflected() -> list[str]:
    """因为带上词形变化而多认了复数/时态的术语（intro 用来给用户交代）。"""
    rules()
    return list(_inflected)


def set_inflection(enabled: bool) -> bool:
    """开/关术语屈折匹配，返回生效后的状态（CLI ``--no-term-inflection`` 用）。

    关完必须把已编译的规则表丢掉，否则变形还留在旧的 pattern 里。
    """
    global _inflection_on, _compiled
    _inflection_on = bool(enabled)
    _compiled = None
    return _inflection_on


def inflection_on() -> bool:
    return _inflection_on


def _inflected_forms(word: str) -> list[str]:
    """一个英文单词的受控屈折形态：复数/过去式过去分词/现在分词。

    只做规则变形、不查词典：规则造出的假想形态（``transformered`` 这类）在
    正文里根本不存在，多写一条只是正则长一点，不会误锁任何东西；真正危险的
    是 ``-er`` 这种会撞上另一个词的尾巴（``report`` 的变形撞上 ``reporter``），
    所以宁可少匹配，不可错锁。
    """
    base = word.lower()
    forms = {base}
    if len(base) >= 2 and base.endswith("y") and base[-2] not in "aeiou":
        stem = base[:-1]
        forms |= {stem + "ies", stem + "ied", base + "ing"}
    elif base.endswith("e"):
        forms |= {base + "s", base + "d", base[:-1] + "ing"}
    elif base.endswith(("s", "x", "z", "ch", "sh")):
        forms |= {base + "es", base + "ed", base + "ing"}
    else:
        forms |= {base + "s", base + "ed", base + "ing"}
    return sorted(forms, key=lambda item: (-len(item), item))


_SINGLE_WORD = re.compile(r"[A-Za-z]+")


def _wants_inflection(term: str) -> bool:
    """这条术语该不该带上词形变化（判定口径只写这一处，供 pattern 与 intro 共用）。"""
    if not _inflection_on:
        return False
    words = [word for word in re.split(r"\s+", term.strip()) if word]
    return (
        len(words) == 1
        and len(words[0]) >= _INFLECT_MIN_LEN
        and _SINGLE_WORD.fullmatch(words[0]) is not None
    )


def term_pattern(term: str) -> re.Pattern:
    """一条术语在源文里的命中判定——正文锁定与质量校验共用这一份（v1.6.4，G2/G3）。

    校验侧原先写的是 ``term in text``（纯子串），于是 ``note`` 会在 ``denote``
    里命中、报出一堆「术语未生效」的假问题；边界怎么判、要不要认屈折，
    从此全项目只有这一处说法。单个 ASCII 长词额外带一组受控变形（G3）。
    """
    words = [word for word in re.split(r"\s+", term.strip()) if word]
    if _wants_inflection(term):
        forms = "|".join(re.escape(f) for f in _inflected_forms(words[0]))
        core = f"(?:{forms})"
    else:
        core = r"\s+".join(re.escape(word) for word in words)
    left, right = _bounds(term) if words else ("", "")
    return re.compile(left + core + right, re.I)


def in_source(term: str, text: str) -> bool:
    """术语是否真的出现在源文里（按词边界与屈折规则判，不用纯子串）。"""
    if not term or not text or not term.strip():
        return False
    return term_pattern(term).search(text) is not None


def _compile() -> list[tuple[re.Pattern, str]]:
    global _compiled
    rules: list[tuple[re.Pattern, str]] = []
    global _skipped, _inflected
    _skipped = []
    _inflected = []
    # 长术语优先，否则 "net profit" 会被 "profit" 先吃掉一半。
    # 方向为 en 时这里拿到的是反向表（中文 → 外文），用户维护的仍是同一张表。
    pairs = direction.term_pairs()
    items = sorted(pairs.items(), key=lambda item: (-len(item[0]), item[0]))
    for key, value in items[:_MAX_TERMS]:
        if len(key) < _MIN_LEN or not _HAS_LETTER.search(key):
            continue
        if _is_risky(key):
            _skipped.append(key)
            continue
        try:
            pattern = term_pattern(key)
        except re.error:
            continue
        rules.append((pattern, value))
        if _wants_inflection(key):
            _inflected.append(key)
    _compiled = rules
    return rules


def reload() -> int:
    """重新读一次术语表并重建规则，改完文件不用重启程序。"""
    global _compiled
    _compiled = None
    load_user_terms()
    return len(_compile())


def rules() -> list[tuple[re.Pattern, str]]:
    return _compiled if _compiled is not None else _compile()


def enabled() -> bool:
    return bool(rules())


def intro() -> str | None:
    """给用户的一句交代：术语锁定开了几条、有几条没生效、为什么。

    术语表有两条静默丢弃规则（超过 80 条截断、原文超过 40 字符跳过），不告诉用户
    的话，他写了 90 条术语、最后 10 条一直没生效也无从察觉。
    """
    total = len(rules())
    risky = skipped()
    flexed = inflected()
    dropped = max(0, len(direction.term_pairs()) - total - len(risky))
    if not total and not risky and not dropped:
        return None
    parts = [f"术语锁定：{total} 条对正文生效"]
    if risky:
        sample = "、".join(risky[:3]) + ("…" if len(risky) > 3 else "")
        parts.append(
            f"{sample} 等 {len(risky)} 条是通用词，只在整段恰好就是它时才替换"
            f"（避免把 Please note that 译成「请注释」）"
        )
    if flexed:
        sample = "、".join(flexed[:3]) + ("…" if len(flexed) > 3 else "")
        parts.append(
            f"{sample} 等 {len(flexed)} 条连带词形变化一起锁（复数、过去式、"
            f"进行时都认；只想锁原形请加 --no-term-inflection）"
        )
    if dropped:
        parts.append(f"{dropped} 条未生效（超过 80 条上限，或原文长于 40 字符）")
    return "；".join(parts) + "。"


def fingerprint() -> str:
    """术语表当前内容的指纹。

    在线模式把译文存进 SQLite，缓存键里必须带这个指纹：否则用户在 术语表.txt
    里加一条术语后重翻同一份文件，会直接命中旧缓存拿到没有新术语的译文，
    而界面上「翻译完成」看起来一切正常——是最难发现的一类失效。
    """
    import hashlib

    rules()  # 确保规则已按当前术语表重建
    raw = "\0".join(
        f"{pattern.pattern}={value}" for pattern, value in rules()
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def lock(
    text: str, hits: list[tuple[str, str, str]] | None = None
) -> tuple[str, dict[str, str]]:
    """把原文里命中的术语换成占位符，返回（掩蔽后文本, {占位符: 官方译法}）。

    ``hits`` 是可选的细粒度输出：传一个列表进来，每命中一条术语就追加
    ``(占位符, 原文匹配词, 官方译法)``。自检报告的「术语命中对照表」需要
    「原文术语」列，而 mapping 只有「占位符 → 官方译法」——模型把占位符
    改写（ZxqOZ）或整个丢掉之后，从译文里根本反推不出原文匹配的是哪个词，
    所以必须在替换发生的这一刻记下来。不传（现有调用方全部不传）时行为
    与之前完全一致，``test_glossary`` 钉住的契约不变。
    """
    if not text or not rules():
        return text, {}
    masked = text
    mapping: dict[str, str] = {}
    for pattern, value in rules():
        token = PLACEHOLDER.format(len(mapping))
        new_text, count = pattern.subn(token, masked)
        if count:
            if hits is not None:
                # 在被换掉的文本上取回匹配片段，保留原文里的大小写
                # （记 "Net profit" 而不是归一化后的 "net profit"）。
                found = pattern.search(masked)
                if found is not None:
                    hits.append((token, found.group(0), value))
            masked = new_text
            mapping[token] = value
    return masked, mapping


def _squeeze(text: str) -> str:
    """挤掉全部空白，专供「指定译法是否在译文里」的比对用。"""
    return re.sub(r"\s+", "", text)


def _leftover_tokens(text: str, limit: int = 3) -> list[str]:
    """译文里残留的类占位符 token（v1.4.2）。

    模型把占位符改写（``Zxq0Z`` → ``ZxqOZ``）后 ``unlock`` 换不回去，
    译文里就剩这种怪 token。去重、保序、最多 ``limit`` 个——「术语未生效」
    的报告 note 要指名道姓念出残留了什么，用户才不用自己全文去找。
    """
    found: list[str] = []
    for token in _LEFTOVER_RE.findall(text or ""):
        if token not in found:
            found.append(token)
        if len(found) >= limit:
            break
    return found


def target_present(target: str, translated: str) -> bool:
    """指定译法是否出现在译文里，且对润色插的空格免疫。

    ``style.polish`` 的 ``_tidy`` 会在中英/中数字边界补一个空格（「AI技术」→
    「AI 技术」）。那是译法已经正确换回之后的排版结果，不是术语丢失；
    直接做 ``target in translated`` 会把这类完全正确的译文批量误报成
    「术语未生效」。自检报告与质量校验共用这一处判定（v1.6.4，G2）。
    """
    if not target or not translated:
        return False
    return target in translated or _squeeze(target) in _squeeze(translated)


def hit_rows(
    paragraph: int, hits: list[tuple[str, str, str]], translated: str
) -> list[dict]:
    """把 ``lock`` 收集的命中整理成自检报告用的行。

    每行是 ``{段落号, 原文术语, 指定译法, 是否生效}``。是否生效 =
    指定译法是否出现在译文里：模型把占位符改写或整个丢掉时，
    ``unlock`` 换不回去，译文里就没有指定译法，这一行的 ``ok`` 就是
    False——那是用户必须人工看一眼的地方。放在这里而不是各翻译器里
    各写一遍，是为了让在线 / 保密两条链路产出的字段完全一致
    （两个翻译器都已经 import 本模块，不会引入循环依赖）。

    v1.4.2：未生效时顺手在译文里找残留的类占位符 token（``ZxqOZ`` 这种
    被改写后换不回去的产物），有则记 ``leftover`` 字段（去重、最多 3 个），
    报告的「术语未生效」note 会把它念出来。key 只在有值时出现——老调用方
    与既有测试钉住的四字段契约不变（v1.3.0 教训的同一套打法）。

    **判定必须对排版性空白免疫**：``style.polish`` 的 ``_tidy`` 会在
    中英/中数字边界插一个空格（「AI技术」→「AI 技术」、「3D打印」→
    「3D 打印」）。那是指定译法被正确换回之后润色的结果，不是术语
    丢失；对润色后的译文直接做 ``target in translated`` 会把这类完全
    正确的译文批量误报成「术语未生效」。润色是译文管线的一环，报告的
    判定口径就必须对齐最终成品——所以先把两边的空白全部挤掉再比。
    """
    rows: list[dict] = []
    for _token, source, target in hits:
        ok = target_present(target, translated)
        row = {
            "paragraph": paragraph,
            "source": source,
            "target": target,
            "ok": ok,
        }
        if not ok:
            leftover = _leftover_tokens(translated)
            if leftover:
                row["leftover"] = leftover
        rows.append(row)
    return rows


def unlock(text: str, mapping: dict[str, str]) -> str:
    """把占位符换回官方译法，并收掉模型在占位符两侧留下的空格。"""
    if not text or not mapping:
        return text
    result = _PH_RE.sub(
        lambda match: mapping.get(f"Zxq{match.group(1)}Z", match.group(0)), text
    )
    result = re.sub(r"\s+([，。；：？！、])", r"\1", result)
    result = re.sub(r"([（(])\s+|\s+([）)])", r"\1\2", result)
    result = re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])", "", result)
    return result
