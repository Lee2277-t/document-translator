from __future__ import annotations

import re

#: 译后风格档位。离线小模型不接受提示词，所以「按指令控制风格」放在后处理做：
#: 先把机器译文最常见的中文毛病修掉（排版、标点、叠字），再按档位调语气与冗余。
STANDARD = "standard"
FORMAL = "formal"
CONCISE = "concise"

STYLES = (STANDARD, FORMAL, CONCISE)
STYLE_LABELS = {
    STANDARD: "标准",
    FORMAL: "正式商务",
    CONCISE: "简洁",
}

_CJK = "\u4e00-\u9fff\u3400-\u4dbf"

_CJK_TO_LATIN = re.compile(rf"(?<=[{_CJK}])(?=[A-Za-z0-9])")
# 右侧要带上 % 这类紧跟数字的符号，否则「12.4%左右」补不出空格。
_LATIN_TO_CJK = re.compile(rf"(?<=[A-Za-z0-9%])(?=[{_CJK}])")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([，。；：？！、])")
# 用 \1+ 而不是 \1\1，三个「的」也要一次塌成一个。
_DOUBLE_PARTICLE = re.compile(r"([的了是和在])\1+")
_EMPTY_BRACKETS = re.compile(r"[（(]\s*[）)]|[【]\s*[】]|\[\s*\]|\{\s*\}")

#: 正式商务：对外函件与合同的语气。长词先换，避免「你们」被「你」先吃掉。
_FORMAL_PAIRS = (
    ("你们", "贵方"),
    ("我们", "我方"),
    ("咱们", "我方"),
    ("不好意思", "抱歉"),
    ("谢谢", "感谢"),
    ("你好", "您好"),
    ("你的", "贵方的"),
    ("你", "您"),
)
_FORMAL_PLEASE = re.compile(r"(^|[。！？；\n])请")
_FORMAL_LEADING_PLEASE = re.compile(r"^请")

_CONCISE_RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"对于(.{1,12}?)来说"), r"对\1"),
    (re.compile(r"由于(.{1,12}?)的原因"), r"由于\1"),
    (re.compile(r"(.{1,12}?)的情况下"), r"\1时"),
    (re.compile(r"在(.{1,12}?)的过程中"), r"在\1时"),
    (re.compile(r"需要注意的是[，：]"), ""),
    (re.compile(r"换句话说[，：]"), "即，"),
    (re.compile(r"随着(.{1,10}?)的不断"), r"随着\1"),
)


def _tidy(text: str) -> str:
    """通用润色：中英/中数之间的空格、悬空空格、叠字、空括号。"""
    text = text.replace("\u00a0", " ")
    text = _CJK_TO_LATIN.sub(" ", text)
    text = _LATIN_TO_CJK.sub(" ", text)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = _DOUBLE_PARTICLE.sub(r"\1", text)
    text = _EMPTY_BRACKETS.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _apply_formal(text: str) -> str:
    for source, target in _FORMAL_PAIRS:
        if source in text:
            text = text.replace(source, target)
    text = _FORMAL_PLEASE.sub(r"\1敬请", text)
    text = _FORMAL_LEADING_PLEASE.sub("敬请", text)
    return text


def _apply_concise(text: str) -> str:
    for pattern, replacement in _CONCISE_RULES:
        text = pattern.sub(replacement, text)
    return _DOUBLE_PARTICLE.sub(r"\1", text)


def polish(text: str, style: str = STANDARD) -> str:
    """按档位润色一句译文。style 不认识时按标准档处理，绝不抛错。"""
    if not text:
        return text
    result = _tidy(text)
    if style == FORMAL:
        result = _apply_formal(result)
    elif style == CONCISE:
        result = _apply_concise(result)
    return _tidy(result) if style != STANDARD else result


def label(style: str) -> str:
    return STYLE_LABELS.get(style, STYLE_LABELS[STANDARD])
