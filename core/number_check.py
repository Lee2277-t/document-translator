"""数字/金额一致性核对。

已落地：阿拉伯数字串（小数、千分位、百分号、货币前缀）的提取，以及原文/译文
的多重集合比对（缺失、新增两类疑点）。纯函数、无副作用，PDF / Word 管线已接入。

三个必须做对的归一（都是实测踩出来的）：
1. 全角先转半角再匹配。`40パーセント` 里的全角字符如果只在匹配后归一，正则根本
   抓不到 `%`，于是原文抽出「40」、译文抽出「40%」，凭空多一条疑点。
2. CJK 数量级要展开。`3億5000万` 与 `3.5亿` 是同一个数，不展开就是两条假疑点——
   长文档里这类占了一大半噪音。
3. 只查 ≥2 位有效数字。章节号、日期这类位置标记大多是个位数，靠这一条就滤掉了；
   试过再叠一层「第 N 章 / 3月31日」白名单，结果只在原文侧命中、译文侧不命中，
   反而制造出不对称的假疑点，已废。年份要保留（俄文年份翻错是已知问题）。

已完成：
- 欧陆写法 `3.000,00`（点分千位 + 逗号小数）归并成 ``3000.00``；
  先前会被正则切成两个数，德/法文档必刷假疑点。
- 拼写数字归一：英 / 德 / 法 / 俄的常见写法先换成阿拉伯数字再比对
  （见 ``core/numwords.py``）。

后续补（TODO）：
- 货币符号与数量级（million/billion）的联动核对；
- OCR 页的噪声抑制（`February2ox8` 这类粘连串不算数字疑点）。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .numwords import spelled_to_digits

_FULLWIDTH = str.maketrans("０１２３４５６７８９％　，．", "0123456789% .,")

#: 欧陆写法：点做千分位、逗号做小数点（德/法/西等国常见）。
#: `3.000,00` 不归并就过不了 ``_NUMBER_RE``——它会被切成 "3.000" 和 "00"
#: 两个数，于是原文比译文多半个数，自检凭空多一条「缺失」疑点。
#: 只在**出现两组以上三位**时才认定是千分位：单个 "1.234" 在英语里是小数，不能乱归，
#: 而这个模块不知道当前文档是什么语种，所以只能取这种两边都解释得通的安全写法。
#: 单个点组（`1.234`）保持原样——它在英语里是小数，在德语里是 1234，本模块
#: 无从得知文档语种，取"两种解释都对不上就别动"的策略。
_EURO_GROUP_RE = re.compile(
    r"(?<![\d.,])("
    r"\d{1,3}(?:\.\d{3})+,\d+"      # 3.000,00 / 1.234.567,89 —— 带逗号小数，必定是欧陆写法
    r"|\d{1,3}(?:\.\d{3}){2,}"      # 1.234.567 —— 两组以上点分千位，小数不会这么写
    r")(?![\d/,])"
)


def _collapse_euro(match: re.Match[str]) -> str:
    return match.group(0).replace(".", "").replace(",", ".")

#: 各语种「百分之」的写法，统一成一个 %。
#: 不归一的话，原文 `40 процентов` 抽到 40、译文 `40%` 抽到 40%，同一对数被当成
#: 两个数字，自检报告里成对刷出两条假疑点（实测多语种样例一次刷了 6 条）。
#: 尾部 `[a-zA-Zа-яА-Я]*` 用来吃掉印欧语的变格词尾（percent→percent，
#: процент→процентов/процента）。故意不用 `\w*`——中文也算 \w，会把后面的正文吃掉。
_PERCENT_RE = re.compile(
    r"(?:パーセント|percent|per\s+cent|prozent|por\s+ciento|pour\s+cent"
    r"|per\s+cento|percento|pct|процент|百分比|百分之)[a-zA-Zа-яА-Я]*",
    re.I,
)

_SCALE = {
    "十": 10,
    "百": 100,
    "千": 1000,
    "万": 10**4,
    "萬": 10**4,
    "亿": 10**8,
    "億": 10**8,
    "兆": 10**12,
}
_SCALE_CLASS = "".join(_SCALE)
#: 带 CJK 数量级的数字串。尾部允许一个「不带数字的裸单位」——`1千2百万` 的「万」
#: 前面没有数字，只写 `(?:\d+...[单位])+` 会把它漏在外面，算出来就少四个零。
_SCALED_RE = re.compile(
    rf"(?:\d+(?:\.\d+)?\s*[{_SCALE_CLASS}])+\s*[万亿億兆]?"
)
_PART_RE = re.compile(rf"(\d+(?:\.\d+)?)?\s*([{_SCALE_CLASS}])")

#: 千分位的每一组后面必须"不是数字"：否则 `1,500 1000000` 会被连读成 1500100。
#: 这个坑是拼写数字归一踩出来的——把 "million" 换成 1000000 之后紧跟在另一个数字后面，
#: 加上这个限制之后，相邻的两个数字不会再被粘成一个。
_NUMBER_RE = re.compile(
    r"(?:£|€|¥|\$|USD|GBP|EUR)?-?\d+(?:[ ,]\d{3}(?!\d))*(?:\.\d+)?%?"
)


def _normalize_text(text: str) -> str:
    text = text.translate(_FULLWIDTH)
    text = _PERCENT_RE.sub("%", text)
    # 欧陆写法必须先于数字提取归并，否则会被切成两半。
    text = _EURO_GROUP_RE.sub(_collapse_euro, text)
    # 拼写数字换成阿拉伯数字：两端都换才比得起来。
    # 原文 twenty three / dreiundzwanzig → 23，译文写「23」，对齐成功；
    # 不换的话原文抽不出数字，译文多一个 23，报告里是一条纯假的「新增」。
    text = spelled_to_digits(text)
    # `40 percent` 换完之后是 `40 %`，正则的 %? 要紧贴数字才吃得到，先把空格收掉。
    return re.sub(r"\s+%", "%", text)


def _expand_scaled(token: str) -> float | None:
    """把 3億5000万 这类带数量级的串算成一个数。标准的中日韩数字读法。

    `1千2百万` → 1200 * 10^4 = 1.2e7，与 `1200万` 等价。
    """
    parts = _PART_RE.findall(token)
    if not parts:
        return None
    total = 0.0
    section = 0.0
    for raw, unit in parts:
        value = float(raw) if raw else 0.0
        scale = _SCALE[unit]
        if scale >= 10**4:
            # 裸单位（`1千2百万` 的「万」）时 value 为 0，把已攒的段整体进位。
            section = (section + value) * scale
            total += section
            section = 0.0
        else:
            section += (value or 1.0) * scale
    return total + section


def _normalize_number(token: str) -> str:
    """归一成可比较的形式：全角转半角、去掉千分位逗号与货币前缀。"""
    token = token.translate(_FULLWIDTH).replace("\u00a0", " ").strip()
    token = re.sub(r"^(£|€|¥|\$|USD|GBP|EUR)", "", token)
    return token.replace(",", "").replace(" ", "")


def _digit_count(token: str) -> int:
    return sum(char.isdigit() for char in token)


def extract_numbers(text: str) -> list[str]:
    """按出现顺序抽出所有数字串（已归一化，CJK 数量级已展开）。"""
    prepared = _normalize_text(text)
    found: list[tuple[int, int, str]] = []

    for match in _SCALED_RE.finditer(prepared):
        value = _expand_scaled(match.group(0))
        if value is None:
            continue
        found.append((match.start(), match.end(), _format_value(value)))

    covered = [(start, end) for start, end, _ in found]
    for match in _NUMBER_RE.finditer(prepared):
        start, end = match.span()
        if any(start < stop and begin < end for begin, stop in covered):
            continue
        found.append((start, end, _normalize_number(match.group(0))))

    found.sort(key=lambda item: item[0])
    return [value for _, _, value in found]


def _format_value(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.6g}"


@dataclass
class NumberIssue:
    source: str  # 数字本身（归一化后）
    kind: str  # 缺失 / 新增
    detail: str = ""


def check_numbers(source: str, translated: str) -> list[NumberIssue]:
    """比对一段原文与其译文的数字多重集合。

    只看 ≥2 位有效数字的数字：个位数到处都是（章节号、序号），噪音太大。
    「缺失」= 译文里该数字比原文少；「新增」= 模型凭空造出来的数字。
    """
    wanted = Counter(
        number for number in extract_numbers(source) if _digit_count(number) >= 2
    )
    got = Counter(
        number for number in extract_numbers(translated) if _digit_count(number) >= 2
    )
    issues: list[NumberIssue] = []
    for number, count in sorted(wanted.items()):
        missing = count - got.get(number, 0)
        if missing > 0:
            issues.append(
                NumberIssue(
                    number,
                    "缺失",
                    f"原文出现 {count} 次，译文只出现 {count - missing} 次",
                )
            )
    for number, count in sorted(got.items()):
        extra = count - wanted.get(number, 0)
        if extra > 0:
            issues.append(
                NumberIssue(
                    number,
                    "新增",
                    f"译文出现 {count} 次，原文只有 {count - extra} 次",
                )
            )
    return issues
