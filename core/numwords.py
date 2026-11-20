"""拼写数字 → 阿拉伯数字（多语种），以及供离线引擎共用的英文词表。

为什么单独成文件
----------------
``core/number_check.py`` 是给自检报告用的纯函数，PDF / Word 管线都会调它，
它**不能** ``import local_translator``——那会把 ctranslate2 和几百 MB 的模型
拖进每一次自检调用。

但词表如果维护两份，早晚会出现"自检报告算出来的数，和离线引擎算出来不一样"
这种最难查的诡异现象。所以这里的分工是：

* **词表共用**：``EN_UNITS`` / ``EN_TENS`` / ``EN_SCALES`` 由本模块导出，
  ``local_translator`` 从这里导入，数据只有一份；
* **逻辑不共用**：两套elfth行为的目标不同——离线引擎要把 "three million"
  保留成 "3 million" 喂给模型（小模型认得带单位的写法），而自检报告要算出
  3000000 这个值去参与比大小。强行合并会让两边都别扭，所以各写各的。

已知边界
--------
* 德语复合形式（einundzwanzig）与法语的 soixante-dix 系列在驱动 tallies
  里有特殊处理，但**变格/性数变化只覆盖了常见几种**（俄语的 одна/два、
  德语的 Millionen）。没覆盖到的会保持原样，而不是猜错——对自检来说，
  漏报远好过把 45 猜成 54 这种反向错误。
"""

from __future__ import annotations

import re


# --------------------------------------------------------------- 英文词表

#: 0–19。与 local_translator 原本的定义**逐项一致**，不要让两边漂移。
EN_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}

#: 20–90 的整十位是独立词，不在 units 里。
EN_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

#: hundred 特殊处理（累计进当前段而不是单独进位），这里只放 ≥1000 的量级。
EN_SCALES = {
    "thousand": 10**3, "million": 10**6, "billion": 10**9, "trillion": 10**12,
}

EN_HUNDRED = "hundred"


# ------------------------------------------------------------- 多语种词表

#: (小于 100 的词, 进位词)。同一个值可能有多个写法（俄语变格、德语复数）。
#: 故意只收最常遇到的形式——宁漏勿错。
_SMALL: dict[str, int] = dict(EN_UNITS)
_SMALL.update(EN_TENS)
_SMALL.update(
    {
        # 德语
        "null": 0, "eins": 1, "ein": 1, "eine": 1, "zwei": 2, "drei": 3, "vier": 4,
        "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9,
        "zehn": 10, "elf": 11, "zwölf": 12, "dreizehn": 13,
        "vierzehn": 14, "fünfzehn": 15, "sechzehn": 16,
        "siebzehn": 17, "achtzehn": 18, "neunzehn": 19,
        "zwanzig": 20, "dreißig": 30, "vierzig": 40, "fünfzig": 50,
        "sechzig": 60, "siebzig": 70, "achtzig": 80, "neunzig": 90,
        # 法语
        "zéro": 0, "un": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
        "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10, "onze": 11,
        "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16,
        "vingt": 20, "trente": 30, "quarante": 40, "cinquante": 50,
        "soixante": 60,
        # 俄语（含阴性形式 одна / две，数字在阴性名词前时会用到）
        "ноль": 0, "один": 1, "одна": 1, "одной": 1, "два": 2, "две": 2,
        "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
        "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11,
        "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
        "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
        "восемнадцать": 18, "девятнадцать": 19,
        "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
        "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80,
        "девяносто": 90,
        "сто": 100, "двести": 200, "триста": 300, "четыреста": 400,
    }
)

#: ≥1000 的量级词（直接进位到 total）。
_MULT: dict[str, int] = {
    # 英 / 德 / 法 / 俄 的「千」
    "thousand": 10**3, "tausend": 10**3, "mille": 10**3,
    "тысяча": 10**3, "тысячи": 10**3, "тысяч": 10**3,
    "million": 10**6, "millionen": 10**6, "millions": 10**6,
    "миллион": 10**6, "миллиона": 10**6, "миллионов": 10**6,
    "billion": 10**9, "milliarde": 10**9, "milliarden": 10**9,
    "milliard": 10**9, "milliards": 10**9,
    "миллиард": 10**9, "миллиарда": 10**9, "миллиардов": 10**9,
    "trillion": 10**12, "billionen": 10**12, "billions": 10**12,
}

#: 「百」与 hundred 同层：累计进当前段而不是进位到 total。
_HUNDRED_WORDS = {"hundred": 100, "hundert": 100, "cent": 100}

#: 法语的两处历史遗留写法，按词序**：不属于严格的加法规则。
#: soixante-dix = 70，quatre-vingts = 80，这两个必须整段先换掉，
#: 否则按加法会算成 60+10=70（巧了对）、4+20=24（错的离谱）。
_FR_QUIRKS: list[tuple[str, int]] = [
    ("soixante et onze", 71),
    ("soixante dix", 70),
    ("quatre vingt", 80),
    ("quatre vingts", 80),
]

_ALL_WORDS = set(_SMALL) | set(_MULT) | set(_HUNDRED_WORDS)

#: 用于「这段文本值不值得处理」的快速扫描。长词在前，避免 un 抢先命中 une 这类。
_WORD_SCAN_RE = re.compile(
    "|".join(sorted((re.escape(word) for word in _ALL_WORDS), key=len, reverse=True)),
    re.I,
)

_TOKEN_RE = re.compile(
    r"[A-Za-zÀ-ÿА-яЁё]+(?:[-’'][A-Za-zÀ-ÿА-яЁё]+)*"
)


def _preprocess(text: str) -> str:
    """把连字符写法和德语复合写法拆成可分词的形式。

    * `quatre-vingts` → `quatre vingts`
    * `vingt-trois`   → `vingt trois`
    * `einundzwanzig` → `ein und zwanzig`（德语是先个位再十位，
      但本模块的累加规则是个位+十位相加，刚好对得上）
    """
    text = re.sub(r"(?<=[A-Za-zÀ-ÿ])-(?=[A-Za-zÀ-ÿ])", " ", text)
    return _split_german_compounds(text)


def _split_german_compounds(text: str) -> str:
    """只拆真正的德语数字复合词（einundzwanzig → ein und zwanzig）。

    这里刻意不做全局的 und 拆分：那样会把 Freund、Bund 这类普通词一起拆开，
    属于典型的"为修一个 bug 顺手改坏无关内容"。只有当 und 左右两边
    都是已知数字词时才动它。
    """

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if "und" not in token:
            return token
        left, _, right = token.partition("und")
        if left.lower() in _SMALL and right.lower() in _ALL_WORDS:
            return f"{left} und {right}"
        return token

    return _TOKEN_RE.sub(replace, text)


def _apply_quirks(text: str) -> str:
    for phrase, value in _FR_QUIRKS:
        # 先处理长形式，避免 "soixante et onze" 被 "soixante dix" 抢先
        pattern = re.compile(re.escape(phrase), re.I)

        def replace(_match: re.Match[str]) -> str:
            return f" {value} "

        text = pattern.sub(replace, text)
    return text


def _value_of_runs(tokens: list[str]) -> list[tuple[int, int, str]]:
    """把连续的拼写数字词合并成阿拉伯数字，返回 (起, 止(不含), 数字串)。"""
    runs: list[tuple[int, int, str]] = []
    index = 0
    size = len(tokens)
    while index < size:
        if tokens[index].lower() not in _ALL_WORDS:
            index += 1
            continue
        start = index
        total = 0.0
        section = 0.0
        touched_scale = False
        while index < size:
            word = tokens[index].lower()
            if word in _SMALL:
                section += _SMALL[word]
            elif word in _HUNDRED_WORDS:
                section = (section if section else 1) * _HUNDRED_WORDS[word]
            elif word in _MULT:
                total += (section if section else 1) * _MULT[word]
                section = 0.0
                touched_scale = True
            elif word in ("and", "und", "et") and index + 1 < size:
                nxt = tokens[index + 1].lower()
                if nxt in _ALL_WORDS:
                    index += 1
                    continue
                break
            else:
                break
            index += 1
        value = total + section
        # 只有「两位数以上」或「带量级」才换：把单独出现的 one/two 也换成 1/2
        # 会制造一堆毫无意义的个位数字噪音。
        significant = touched_scale or value >= 10
        if significant and index > start:
            rendered = str(int(value)) if value == int(value) else f"{value:g}"
            runs.append((start, index, rendered))
    return runs


def spelled_to_digits(text: str) -> str:
    """把文本里各语种的拼写数字换成阿拉伯数字，便于参与数值比对。

    换不出来的一律保持原样——宁可让自检漏报，也不要把 45 猜成 54
    这种反向错误（后者会让用户对整份报告失去信任）。

    注意：返回结果可能与原文有细微差异（连字符会被换成空格，如 state-of-art →
    state of art），所以它**只用于数字比对**，不要拿回去当原文展示。
    不含任何数字词的文本会原样返回，避免为了几个数字把整篇原文的标点都改一遍。
    """
    if not text:
        return text
    # 廉价判断必须是**子串**扫描而不是整词命中：德语复合词（einundzwanzig）和
    # 法语特殊写法（quatre-vingts）都不会作为整词出现在词表里，整词判断会把它们
    # 直接跳过。一次编译好的正则扫描代价很低。
    if not _WORD_SCAN_RE.search(text):
        return text
    prepared = _apply_quirks(_preprocess(text))
    replacements = _value_of_runs(_TOKEN_RE.findall(prepared))
    if not replacements:
        return prepared
    tokens = list(_TOKEN_RE.finditer(prepared))
    pieces: list[str] = []
    cursor = 0
    for start, end, rendered in replacements:
        begin = tokens[start].start()
        stop = tokens[end - 1].end()
        pieces.append(prepared[cursor:begin])
        pieces.append(rendered)
        cursor = stop
    pieces.append(prepared[cursor:])
    return "".join(pieces)
