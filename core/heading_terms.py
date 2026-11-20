from __future__ import annotations

import re

from .common import app_root

LATIN_OR_CYRILLIC_RE = re.compile(r"[A-Za-z\u00c0-\u024f\u0400-\u04ff]")
TRAILING_NUMBER_RE = re.compile(r"^(?P<base>.*?)[\s\-–—:：]*(?P<number>\d{1,3})$")
_EDGE_PUNCT_RE = re.compile(r"^[\s.。、,，;；:：!！?？\"'“”‘’()\[\]{}【】—–-]+|[\s.。、,，;；:：!！?？\"'“”‘’()\[\]{}【】—–-]+$")

# 常用文档标题对照表：短标题是小型离线模型最容易产生幻觉的地方，
# 命中词条时直接查表，既比模型稳，也能让纯标题文档不用加载多语种模型。
HEADINGS: dict[str, str] = {
    # 英文
    "contents": "目录",
    "table of contents": "目录",
    "list of tables": "表目录",
    "list of figures": "图目录",
    "foreword": "前言",
    "preface": "前言",
    "introduction": "引言",
    "appendix": "附录",
    "appendices": "附录",
    "table": "表",
    "figure": "图",
    "chart": "图",
    "references": "参考文献",
    "bibliography": "参考文献",
    "abstract": "摘要",
    "executive summary": "执行摘要",
    "summary": "概述",
    "conclusion": "结论",
    "acknowledgements": "致谢",
    "acknowledgments": "致谢",
    "glossary": "术语表",
    "index": "索引",
    "chapter": "第{}章",
    "section": "节",
    # 日文
    "目次": "目录",
    "はしがき": "前言",
    "前書き": "前言",
    "序論": "绪论",
    "はじめに": "引言",
    "序章": "序章",
    "結論": "结论",
    "結語": "结语",
    "おわりに": "结语",
    "付録": "附录",
    "表": "表",
    "図": "图",
    "参考文献": "参考文献",
    "引用文献": "参考文献",
    "要旨": "摘要",
    "概要": "概要",
    "まとめ": "总结",
    "謝辞": "致谢",
    "索引": "索引",
    "別添資料": "附件资料",
    # 韩文
    "목차": "目录",
    "서문": "前言",
    "머리말": "前言",
    "개요": "概述",
    "부록": "附录",
    "표": "表",
    "그림": "图",
    "참고 문헌": "参考文献",
    "참고문헌": "参考文献",
    "요약": "摘要",
    "결론": "结论",
    "감사의 글": "致谢",
    "색인": "索引",
    # 俄文
    "оглавление": "目录",
    "содержание": "目录",
    "предисловие": "前言",
    "введение": "引言",
    "приложение": "附录",
    "таблица": "表",
    "рисунок": "图",
    "список литературы": "参考文献",
    "аннотация": "摘要",
    "реферат": "摘要",
    "заключение": "结论",
    "выводы": "结论",
    "благодарности": "致谢",
    "указатель": "索引",
    "глава": "第{}章",
    "раздел": "节",
    # 德文
    "inhaltsverzeichnis": "目录",
    "vorwort": "前言",
    "einleitung": "引言",
    "anhang": "附录",
    "tabelle": "表",
    "abbildung": "图",
    "literaturverzeichnis": "参考文献",
    "zusammenfassung": "摘要",
    "fazit": "结论",
    "danksagung": "致谢",
    "kapitel": "第{}章",
    "abschnitt": "节",
    # 法文
    "table des matières": "目录",
    "sommaire": "目录",
    "préface": "前言",
    "avant-propos": "前言",
    "annexe": "附录",
    "tableau": "表",
    "résumé": "摘要",
    "remerciements": "致谢",
    "chapitre": "第{}章",
    # 西班牙文
    "índice": "目录",
    "prefacio": "前言",
    "apéndice": "附录",
    "agradecimientos": "致谢",
    "capítulo": "第{}章",
    "sección": "节",
    # 意大利文
    "indice": "目录",
    "prefazione": "前言",
    "introduzione": "引言",
    "appendice": "附录",
    "riassunto": "摘要",
    "ringraziamenti": "致谢",
    "capitolo": "第{}章",
    "sezione": "节",
    # 葡萄牙文
    "sumário": "目录",
    "prefácio": "前言",
    "apêndice": "附录",
    "tabela": "表",
    "agradecimentos": "致谢",
    "conclusão": "结论",
}

# 与英文同形、但需要在其他语种里单独给出的词条（法语 introduction、德语 index 等沿用同一译文）
SHARED = (
    ("introduction", "引言"),
    ("figure", "图"),
    ("index", "索引"),
    ("conclusion", "结论"),
    ("annex", "附录"),
)
for _word, _zh in SHARED:
    HEADINGS.setdefault(_word, _zh)

# 通用章节名：小模型对这类单词标题最容易复读（实测 Healthcare → 保健保健保健、
# Impact → 影响影响影响），查表比任何解码参数都稳。
GENERIC = (
    ("healthcare", "医疗保健"),
    ("medical", "医疗"),
    ("education", "教育"),
    ("environment", "环境"),
    ("impact", "影响"),
    ("example", "示例"),
    ("examples", "示例"),
    ("field", "领域"),
    ("background", "背景"),
    ("overview", "概述"),
    ("summary", "摘要"),
    ("scope", "适用范围"),
    ("purpose", "目的"),
    ("definition", "定义"),
    ("definitions", "定义"),
    ("notes", "说明"),
    ("note", "说明"),
    ("notice", "须知"),
    ("warning", "警告"),
    ("caution", "注意事项"),
    ("safety", "安全"),
    ("features", "功能特点"),
    ("benefits", "优势"),
    ("advantages", "优势"),
    ("results", "结果"),
    ("method", "方法"),
    ("methods", "方法"),
    ("materials", "材料"),
    ("discussion", "讨论"),
    ("recommendations", "建议"),
    ("contact", "联系方式"),
    ("contacts", "联系方式"),
    ("keywords", "关键词"),
    ("acknowledgments", "致谢"),
    ("abbreviation", "缩略语"),
    ("abbreviations", "缩略语"),
    ("symbols", "符号说明"),
)
for _word, _zh in GENERIC:
    HEADINGS.setdefault(_word, _zh)

# 缩略语标题：小模型常把单个缩写整句瞎译，直接给固定译文。
ACRONYMS = (
    ("ai", "人工智能"),
    ("ml", "机器学习"),
    ("iot", "物联网"),
    ("api", "接口"),
    ("sdk", "开发工具包"),
    ("faq", "常见问题"),
    ("kpi", "关键绩效指标"),
    ("roi", "投资回报率"),
    ("gdp", "国内生产总值"),
    ("co2", "二氧化碳"),
    ("r&d", "研发"),
)
for _word, _zh in ACRONYMS:
    HEADINGS.setdefault(_word, _zh)

# 复合标题拼接：「AI in Education」这种短语模型极易译成《教育法》，
# 但拆开后的每个词都在表里，按中文标题语序拼回去比瞎猜稳。
CONNECTIVES = (
    (" in ", "{b}中的{a}"),
    (" of ", "{b}的{a}"),
    (" for ", "{b}的{a}"),
    (" and ", "{a}与{b}"),
)


def _compose(key: str) -> str:
    for token, template in CONNECTIVES:
        parts = key.split(token)
        if len(parts) != 2:
            continue
        first = HEADINGS.get(parts[0].strip(), "")
        second = HEADINGS.get(parts[1].strip(), "")
        if not first or not second or "{}" in first or "{}" in second:
            continue
        return template.format(a=first, b=second)
    return ""


def _key(text: str) -> str:
    stripped = _EDGE_PUNCT_RE.sub("", text.strip())
    return re.sub(r"\s+", " ", stripped).lower()


def _resolve(key: str) -> str:
    direct = HEADINGS.get(key)
    if direct:
        return direct
    match = TRAILING_NUMBER_RE.match(key)
    if not match:
        return _compose(key)
    base = match.group("base").strip()
    number = match.group("number")
    template = HEADINGS.get(base)
    if not template:
        return ""
    if "{}" in template:
        return template.format(number)
    return f"{template}{number}"


# RapidOCR 会把「AI」认成「Al」（大写 A + 小写 l），扫描件的标题因此查不到表，
# 只能交给小模型，结果就是「教育中的阿尔」。只在整词、且原键查不到时试一次。
#: 说明性文字的标志：全角标点与括号。真术语里几乎不出现，出现就说明这行是散文。
_PROSE_RE = re.compile(r"[，。：；、？！（）「」【】《》…]")
#: 编号列表项：「1. xxx」「2）xxx」。术语表里的说明常这么写。
_LIST_PREFIX_RE = re.compile(r"^\d+\s*[.、)）]")

OCR_WORD_FIXES = {"al": "ai"}


def _ocr_variant(key: str) -> str:
    words = key.split()
    fixed = [OCR_WORD_FIXES.get(word, word) for word in words]
    return " ".join(fixed) if fixed != words else ""


def lookup(text: str) -> str:
    """短标题查表：命中返回中文，否则返回空字符串。"""
    key = _key(text)
    if not key or len(key) > 40:
        return ""
    result = _resolve(key)
    if result:
        return result
    variant = _ocr_variant(key)
    return _resolve(variant) if variant else ""


#: 反向词表（中文 → 外文）的惰性缓存。``load_user_terms`` 会把它清空重建。
_reverse_terms: dict[str, str] | None = None


def _build_reverse_terms() -> dict[str, str]:
    """把 ``HEADINGS`` 翻过来：中文标题 → 外文标题（v1.5.8 中译英方向）。

    只收值里没有任何占位符/编号模板的条目（``第{}章`` 这类反向推不出编号）。
    同一个中文对应多个外文时按**优先级**挑：用户词条 > 内置表里先出现的那个。
    内置表是按语种分块写的，英文在最前，所以这条规则等价于「英文优先」——
    ``目录`` 拿到 contents，而不是同表的 目次 / 목차 / índice / Inhaltsverzeichnis
    （只按长度挑会挑中日文或意大利文，中译英的标题被译成日文比不译还糟）。
    """
    pairs: dict[str, str] = {}
    ranks: dict[str, tuple[int, int]] = {}
    for index, (source, value) in enumerate(HEADINGS.items()):
        if not value or "{}" in value:
            continue
        key = _key(value)
        if not key or len(key) > 40:
            continue
        rank = (0 if source in USER_TERMS else 1, index)
        current = ranks.get(key)
        if current is None or rank < current:
            pairs[key] = source
            ranks[key] = rank
    # 用户词条最高优先：测试与「热改术语表」都会直接改 USER_TERMS 而不重建
    # HEADINGS，所以这里必须单独再扫一遍，后写覆盖内置条目。
    for source, value in USER_TERMS.items():
        if not source or not value or "{}" in value:
            continue
        key = _key(value)
        if not key or len(key) > 40:
            continue
        pairs[key] = source
    return pairs


def lookup_reverse(text: str) -> str:
    """中译英方向：中文标题查回外文；没命中返回空字符串。"""
    global _reverse_terms
    key = _key(text)
    if not key or len(key) > 40:
        return ""
    if _reverse_terms is None:
        _reverse_terms = _build_reverse_terms()
    return _reverse_terms.get(key, "")


def is_ambiguous_fragment(text: str) -> bool:
    """拉丁/西里尔字母的三词以内短片段，多语种小模型最容易译成无关句子。"""
    letters = LATIN_OR_CYRILLIC_RE.findall(text)
    if not letters:
        return False
    return len(text.split()) <= 3 and len(letters) <= 24


USER_FILE = app_root() / "术语表.txt"
user_count = 0

#: 用户术语表的原始条目（键已归一）。glossary 模块靠它在正文长句里做术语锁定，
#: 与 HEADINGS 分开存，避免把内置标题词（note、table…）误锁进正文。
USER_TERMS: dict[str, str] = {}


def load_user_terms() -> int:
    """读取工具目录下的「术语表.txt」，让常见错译不改代码就能修。

    每行写成  原文 = 中文  ，# 开头是注释；键按查表规则归一，所以大小写和
    首尾标点都不用管。用户词条优先级高于内置表。
    """
    global user_count, _reverse_terms
    if not USER_FILE.is_file():
        return 0
    try:
        lines = USER_FILE.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return 0
    count = 0
    for line in lines:
        item = line.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        source, _, target = item.partition("=")
        # 说明文字里也常出现等号（本文件自己就有），这类行不是术语，必须挡掉：
        # 否则「4. 不想被翻译的专有名词…（Project Atlas = Project Atlas）」会被
        # 当成一条术语，正文里再也匹配不到，还白白占掉一个词条名额。
        if _PROSE_RE.search(source) or _LIST_PREFIX_RE.match(item):
            continue
        key, value = _key(source), target.strip()
        if key and value and len(key) <= 40:
            HEADINGS[key] = value
            USER_TERMS[key] = value
            count += 1
    user_count = count
    # 用户词条也进 HEADINGS，反向词表必须跟着重建，否则中译英看不到新词条。
    _reverse_terms = None
    return count


load_user_terms()
