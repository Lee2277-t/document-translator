"""免翻区：URL / 邮箱 / 版本号 / 编号 / 代码这类「不该翻的东西」的掩码还原。

为什么需要它（对应差距 C2）
---------------------------
段级（``core/translator.should_translate``、``looks_like_formula``）已经把
「整段要不要翻」管起来了，但它们管不到**句内**。
「详见 https://example.com/a?b=1 的说明」整段当然该翻，可 URL 会被免费
云端通道顺手在标点上做本土化，变成"详见 https://example。com/a?b=1 的说明"
——用户看到的就是链接打不开。于是把它抽成一个独立模块：
翻译前把这类片段换成占位符，译完原样换回。

职责边界（刻意不重叠）
----------------------
本模块**只做句内片段级**的保护。段级判定（一整段是不是公式、要不要跳过）
仍然归 ``core/translator.should_translate`` 与 ``looks_like_formula``，
本模块不碰 ``should_translate`` 的语义，也不改任何一个已有判定——上层跳过
的段落根本走不到这里。

占位符形状
----------
``Znt{n}Z``，与术语锁定 ``Zxq{n}Z``（core/glossary.py）、术语候选
``Ztq{n}Z``（core/termbase.py）**前缀刻意错开**：三兄弟同时在一段文字里
出现（标记不同来源的占位），互相换错的代价是整段译文串味——三个字母完全
不同就不会有任何"长得像"的误认。纯字母数字也是为了能 100% 穿过 NLLB /
Marian 的分词器（ Unicode 括号形状会被整个吃掉，见 glossary.py 的注释）。

三条保守默认（改之前先读懂，它们是刻意收着的）
----------------------------------------------
1. **只掩码整块 100% 确定的形状**。URL / 邮箱 / 版本号 / 编号 / 代码
   围栏——这些长得就是长那样，认错的概率接近零。宁可漏掩码（代价是翻坏
   一个链接），也不要把半句话当 Code 锁死（代价是整段没翻）。
2. **句内公式启发式默认关**（``latex_inline``）。``$x$`` 这种在普通文档里
   很可能是「$5 和 $10」，开着就会把正文切成碎片。要用得显式打开。
3. **``restore`` 遇到认不出的占位符一律原样留下，绝不盲删**。模型把
   ``Znt0Z`` 改写成 ``ZntOZ`` 时它就是认不出——留着起码用户能一眼看见
   "这里有个怪 token"，删了就永远找不回来。

组合顺序（为什么先本模块、后术语锁定）
--------------------------------------
``lock_and_mask`` 先掩本模块的形状，再交给 ``glossary.lock`` 锁术语：
术语被锁的意思是"换成用户指定的中文译名"，而 URL / 代码里的词不是自然
语言词汇——把 ``example.com`` 里的 ``example`` 换成它的中文译名会让链接
彻底失效。顺序反过来就是给自己挖坑。

CLI 开关见 ``app.py`` 的 ``--no-translate=default|+latex,+ident|off``，
解析函数 ``parse_rules_argument``（认不出的写法不做默认假设，抛人话错误）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from . import glossary

#: 免翻区占位符形状。``Znt`` 前缀刻意与 ``Zxq``（术语）、``Ztq``（术语候选）
#: 错开——三种占位符会同时出现在一段待译文本里，前缀相同就会互相换错。
PLACEHOLDER = "Znt{}Z"

#: 还原用的匹配式。要求中间必须是数字：模型把 ``Znt0Z`` 改写成 ``ZntOZ``
#: 时这里认不出（那就要么原样留着，要么由调用方处理），绝不放宽到字母——
#: 放宽会把正常单词 rahmenENNF 之类误当成占位符。
_PH_RE = re.compile(r"(?i)znt(\d+)z")

#: 默认生效的规则。见模块 docstring「三条保守默认」第 1 条。
DEFAULT_RULES: tuple[str, ...] = ("url", "email", "version", "numbering", "code_fence")

#: 默认关的规则。见模块 docstring「三条保守默认」第 2 条。
OPTIONAL_RULES: tuple[str, ...] = ("latex_inline", "identifier")

#: 给用户看的名称表（GUI 状态栏 / CLI 摘要 / 日志都从这里取，不各写一套）。
RULE_LABELS: dict[str, str] = {
    "url": "网址",
    "email": "邮箱",
    "version": "版本号",
    "numbering": "编号",
    "code_fence": "代码",
    "latex_inline": "行内公式",
    "identifier": "标识符",
}


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------


def _looks_like_math(body: str) -> bool:
    """``latex_inline`` 的二次判定：有没有一点像公式的证据。

    只靠 ``$…$`` 的形状判会把「花了 $5 到 $10」当成公式锁起来（第一个 $
    到第二个 $ 之间的正文全被吃掉）。所以要求里面至少有一样：LaTeX 命令
    （``\\frac``）、上下标、希腊字母或数字符号同现。为什么这条规则默认关：
    即便有这个二次判定，误伤的代价仍是"半句没翻"，而不开的代价只是"偶尔
    翻坏一个公式"——后者明显更轻。
    """
    if not body:
        return False
    if re.search(r"\\[A-Za-z]+", body):
        return True
    if re.search(r"[\^_=]", body):
        return True
    if re.search(r"[²³¹\u2070-\u209f]", body):
        return True
    return False


@dataclass(frozen=True)
class Rule:
    """一条掩码规则。

    ``pattern`` 负责"形状是不是"；``accept`` 是可选的二次判定，负责"内容
    像不像"——形状不可信的规则（如行内公式）必须配一个 accept。
    """

    name: str
    pattern: re.Pattern
    accept: Callable[[str], bool] | None = None


_RULE_BUILDERS: dict[str, Callable[[], Rule]] = {}


def _r(
    name: str,
    pattern: str,
    accept: Callable[[str], bool] | None = None,
    flags: int = re.I | re.X,
) -> None:
    """注册一条规则的构造方式（编译推迟到真正用到时，导入本模块不付代价）。

    ``flags`` 默认带 ``re.I``（``v1.0`` 与 ``V1.0``、``HTTPS://`` 都得认），
    但**标识符这条不能忽略大小写**：驼峰判据 ``(?:[A-Z][a-z0-9]+){2,}`` 在
    ``re.I`` 下会被回溯拆开匹配到任何普通英文单词（``Smith`` → ``Smi``+
    ``th`` 就算"两个驼峰段"），整段散文都会被当成代码锁死——这条规则默认关
    着就是因为它太容易误伤；开着的时候至少有大小写把关。
    """
    _RULE_BUILDERS[name] = lambda: Rule(
        name=name, pattern=re.compile(pattern, flags), accept=accept
    )


# URL：要有明确的协议头或 www 前缀才认——裸 ``example.com`` 也算网址的话，
# 版本号形状（1.2.3）与域名形状就再也分不开了。
# 最后一段刻意要求末字符**不是标点**："详见 https://a.com/x。" 里那个句号是
# 中文句子的句号，属于句子不属于网址，被网址顺走
# 就等于把这一段的句读抹掉了（模型看到的是一整句没有结尾标点的话）。
_r(
    "url",
    r"""
    (?<![\w@./-])
    (?:https?://|ftp://|www\.)
    [^\s<>"'\u3000）)】》」]*
    [^\s<>"'\u3000）)】》」.,;:!?？！，。；：]
    """,
)

# 邮箱：本地部分 + @ + 至少一个点。``@`` 后面要求有字母域 + 顶级域，
# 避免把 social mention（``@boss 你看一下``）当邮箱锁起来。
_r(
    "email",
    r"""
    (?<![\w.%+-])
    [A-Za-z0-9._%+-]+@
    [A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*
    \.[A-Za-z]{2,}
    """,
)

# 版本号：``v1.2`` / ``2.3.1`` / ``1.0.0-rc.1``。两侧不许再接数字字母点，
# 这样"第 3.2.1 节"里的编号也能被下面那条接走，两条规则各管一半不打架。
_r(
    "version",
    r"""
    (?<![0-9A-Za-z_.])
    [Vv]?\d+(?:\.\d+){1,3}
    (?:[-_][A-Za-z0-9.]+)?
    (?![0-9A-Za-z_])
    """,
)

# 编号：两级以上的点分编号（``3.2``/``2.1.4``）与括号里的列表标号（(1)/（a））。
# 为什么这两种算"整块确定"：它们出现的位置几乎总是在标题或列表行首，极少
# 是自然语言的入内容。
_r(
    "numbering",
    r"""
    (?<![\d.])
    \d+(?:\.\d+){1,5}\.?
    (?![\d.])
    |
    [（(]\s*\d{1,4}(?:\.\d+){1,3}\s*[）)]
    |
    [（(]\s*\d{1,3}[A-Za-z]?\s*[）)]
    """,
)

# 代码围栏（```…```）与行内代码（`code`）。围栏优先，避免行内正则先吃掉
# 围栏的第一个 ```。
_r(
    "code_fence",
    r"""
    ```[\s\S]*?```
    |
    (?<!`)`[^`\n]+`(?!`)
    """,
)

# 行内公式：$…$ / \(…\) / \[…\]，默认关（见 _looks_like_math 的注释）。
_r(
    "latex_inline",
    r"""
    (?<![\\\w])\$(?!\s)[^$\n]{1,120}\$
    |
    \\\([\s\S]{1,120}?\\\)
    |
    \\\[[\s\S]{1,120}?\\\]
    """,
    _looks_like_math,
)

# 标识符：点分路径（pandas.DataFrame）、蛇形名（max_retries）、
# 驼峰名（WordNet）。驼峰要求至少两段且中间不得有空格，普通英文标题
# （"John Smith"）不会被误判；句子首字母大写也不会（只有一段）。
_r(
    "identifier",
    r"""
    (?<![\w.])
    (?:[A-Za-z][A-Za-z0-9_]*\.){1,3}[A-Za-z][A-Za-z0-9_]*
    (?![\w.])
    |
    (?<![A-Za-z0-9_])
    [a-z][a-z0-9]*(?:_[a-z0-9]+){1,}
    (?![A-Za-z0-9_])
    |
    (?<![A-Za-z0-9_])
    (?:[A-Z][a-z0-9]+){2,}
    (?![A-Za-z0-9_])
    """,
    flags=re.X,  # 大小写敏感：见 _r 的 flags 说明
)

_CACHE: dict[tuple[str, ...], list[Rule]] = {}

#: 本次进程累计锁住的片段数（按类别）。为什么不逐段打日志：按段打印会把
#: 日志刷爆，用户根本看不见重点；做成累计以后，四档的 ``summary()`` 在收尾
#: 时念一句「免翻区：本次锁住 3 处（网址 1、版本号 2）」就够了——那是用户
#: 唯一会看的地方。测试之间用 ``reset_counts()`` 归零。
_LOCK_COUNTS: dict[str, int] = {}

#: CLI 显式设定的规则；None 表示"没设过，用 DEFAULT_RULES"。
_ACTIVE_RULES: tuple[str, ...] | None = None


def available_rules() -> tuple[str, ...]:
    """全部已知规则名（默认 + 可选）。"""
    return DEFAULT_RULES + OPTIONAL_RULES


def set_rules(names: Sequence[str] | None) -> None:
    """设定本次进程生效的规则。传 None 或空序列都表示"关掉"。

    关掉（``--no-translate=off``）必须是能说出口的状态：这里变成空规则元组
    以后，``mask`` 原样返回文本，与没接这个模块完全等价。
    """
    global _ACTIVE_RULES
    if names is None:
        _ACTIVE_RULES = None
        return
    cleaned = tuple(name for name in names if name in available_rules())
    _ACTIVE_RULES = cleaned


def reset_rules() -> None:
    """恢复出厂默认（测试与 GUI 重新设档用）。"""
    set_rules(None)


def active_rules() -> tuple[str, ...]:
    """当前生效的规则名（默认就是 ``DEFAULT_RULES``）。"""
    if _ACTIVE_RULES is None:
        return DEFAULT_RULES
    return _ACTIVE_RULES


def parse_rules_argument(value: str) -> tuple[str, ...]:
    """解析 ``--no-translate=...`` 的值，返回规则名元组（空元组 = 关）。

    支持三种写法：``default``（默认五条）、``off``（全关）、``+latex,+ident``
    （默认再加可选项；``%`` 是对 leading `+` 的宽容写法，写 ``latex`` 也算）。
    **认不出的写法不默认放行也不静默忽略**：抛 ``ValueError`` 说人话，由
    CLI 打印并给非零退出码——用户写了 ``--no-translate=math`` 却没生效，
    比直接告诉他"没有这条规则"更难排查。
    """
    value = (value or "").strip()
    if not value or value.lower() in ("default", "on"):
        return DEFAULT_RULES
    if value.lower() in ("off", "none"):
        return ()
    wanted: list[str] = []
    for token in value.replace("，", ",").split(","):
        token = token.strip().lstrip("+").strip()
        if not token:
            continue
        if token == "latex":
            token = "latex_inline"
        elif token == "ident":
            token = "identifier"
        if token not in available_rules():
            raise ValueError(
                f"--no-translate 没有「{token}」这条规则。"
                f"可选项：{', '.join(available_rules())}；"
                "写 default 用默认规则，写 off 关掉。"
            )
        if token not in wanted:
            wanted.append(token)
    if not wanted:
        return DEFAULT_RULES
    # 默认规则永远在前：`+latex` 的语义是"默认的那几条再多加一个"，
    # 不是"只要 latex"。
    ordered = [name for name in DEFAULT_RULES] + [
        name for name in wanted if name in OPTIONAL_RULES
    ]
    ordered += [name for name in wanted if name not in ordered]
    return tuple(ordered)


def compile_rules(names: Iterable[str] | None = None) -> list[Rule]:
    """按规则名编译（带缓存）。``names=None`` 用当前生效规则。

    按名字编译而不是返回一个写死的列表，是为了让"关掉某条规则"成为一个
    真实可测的状态（``--no-translate=off`` 走的就是空列表）。
    """
    key = tuple(names) if names is not None else tuple(active_rules())
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    rules: list[Rule] = []
    for name in key:
        builder = _RULE_BUILDERS.get(name)
        if builder is None:
            continue
        rules.append(builder())
    _CACHE[key] = rules
    return rules


# ---------------------------------------------------------------------------
# 掩码 / 还原
# ---------------------------------------------------------------------------


def mask(
    text: str, rules: Iterable[str] | None = None
) -> tuple[str, dict[str, str]]:
    """把文本里属于免翻区的片段换成占位符，返回 ``(掩码后文本, {占位符: 原文})``。

    编号从 0 起、跨规则连续递增——同一个占位符形状在同一段里必须唯一，
    否则还原时映射会互相覆盖。二次判定（``Rule.accept``）通不过的片段原样
    留着不动：判不准的宁可让它被引擎翻坏，也不要顺手锁死半句话。
    """
    if not text:
        return text, {}
    mapping: dict[str, str] = {}
    masked = text
    for rule in compile_rules(rules):
        def _replace(match: re.Match, _rule: Rule = rule) -> str:
            piece = match.group(0)
            if _rule.accept is not None and not _rule.accept(piece):
                return piece
            token = PLACEHOLDER.format(len(mapping))
            mapping[token] = piece
            _LOCK_COUNTS[_rule.name] = _LOCK_COUNTS.get(_rule.name, 0) + 1
            return token

        masked = rule.pattern.sub(_replace, masked)
    return masked, mapping


def reset_counts() -> None:
    """把累计计数归零（测试隔离用；生产只有一个要人看的收尾点）。"""
    _LOCK_COUNTS.clear()


def restore(text: str, mapping: dict[str, str]) -> str:
    """把占位符换回原文。**认不出的占位符原样留下**，绝不盲删。

    模型改写占位符（``Znt0Z`` → ``ZntOZ``）时换不回去，但那段怪 token 至少
    还在译文里，用户/自检报告看得见；删掉就成了"这一段丢了几个字"。
    """
    if not text or not mapping:
        return text
    return _PH_RE.sub(
        lambda match: mapping.get(f"Znt{match.group(1)}Z", match.group(0)), text
    )


def _label_for(piece: str) -> str:
    """反查一段原文属于哪条规则（``intro`` 出声用），查不到给空串。"""
    for rule in compile_rules(available_rules()):
        match = rule.pattern.search(piece)
        if match is None or match.group(0) != piece:
            continue
        if rule.accept is not None and not rule.accept(piece):
            continue
        return RULE_LABELS.get(rule.name, rule.name)
    return ""


def _format(counts: dict[str, int], total: int) -> str:
    labels = [
        f"{RULE_LABELS.get(name, name)} {count} 处" for name, count in counts.items()
    ]
    labels.sort()
    return f"免翻区：本次锁住 {total} 处原文（{'、'.join(labels)}），翻译后原样还原。"


def intro(mapping: dict[str, str] | None = None) -> str | None:
    """给用户的一句交代（仿 ``core/glossary.intro``）：锁了几处、都是什么。

    ``mapping`` 给定就描述这一批掩码（调用方想当场说明时用）；不给参数则
    描述**本次进程累计**的结果——四档翻译器的 ``summary()`` 走这条路：掩码
    是逐段发生的，逐段出声会把日志刷爆，收尾念一句才有人看。没锁任何东西
    时返回 None（调用方据此决定要不要浪费一行字）。
    """
    if mapping is not None:
        if not mapping:
            return None
        counts: dict[str, int] = {}
        for piece in mapping.values():
            label = _label_for(piece) or "其他"
            counts[label] = counts.get(label, 0) + 1
        return _format(counts, len(mapping))
    if not _LOCK_COUNTS:
        return None
    return _format(dict(_LOCK_COUNTS), sum(_LOCK_COUNTS.values()))


# ---------------------------------------------------------------------------
# 与术语锁定的组合 façade
# ---------------------------------------------------------------------------

#: 组合映射的两个固定键。把两个模块的映射装进同一个返回值，是为了让
#: 三档翻译器各自只需要改两行调用（lock_and_mask / unlock_and_restore），
#: 而不是每档各维护一对 mapping。
TERMS_KEY = "terms"
LOCKS_KEY = "locks"


def lock_and_mask(
    text: str, hits: list[tuple[str, str, str]] | None = None
) -> tuple[str, dict[str, dict[str, str]]]:
    """免翻区 → 术语锁定的组合入口。``hits`` 语义与 ``glossary.lock`` 一致。

    顺序见模块 docstring「组合顺序」：先保护 URL/代码，再锁术语。
    返回 ``(待译文本, {"terms": {…}, "locks": {…}})``。
    """
    protected, locks = mask(text)
    masked, terms = glossary.lock(protected, hits)
    return masked, {TERMS_KEY: terms, LOCKS_KEY: locks}


def unlock_and_restore(text: str, mapping: Any) -> str:
    """``lock_and_mask`` 的逆操作：先换回术语译名，再还原免翻区原文。

    ``mapping`` 允许是组合映射（dict，含 ``terms`` / ``locks`` 两个键）；
    缺任何一半都安全（空 dict 或 None 一律原样返回），不需要调用方记顺序。
    """
    if not text:
        return text
    if not mapping:
        return text
    if isinstance(mapping, dict) and (TERMS_KEY in mapping or LOCKS_KEY in mapping):
        terms = dict(mapping.get(TERMS_KEY) or {})
        locks = dict(mapping.get(LOCKS_KEY) or {})
    else:
        # 容错：调用方塞进来的也许只是术语映射（老代码路径）
        terms, locks = dict(mapping), {}
    text = glossary.unlock(text, terms)
    return restore(text, locks)
