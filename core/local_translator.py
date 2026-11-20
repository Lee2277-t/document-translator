from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .common import CancelledError, TranslationError, check_cancel
from .concurrency import DEFAULT_JOBS, JobGovernor, clamp_jobs
from .tuning import cap_int, warn_unused
from .file_log import FileLog, tee_log
from . import direction, glossary, mixed
from .glossary import hit_rows
from .no_translate import intro, lock_and_mask, unlock_and_restore
from .heading_terms import is_ambiguous_fragment
from .style import STANDARD, polish
from .lang_detect import ENGLISH, detect, language_label

if TYPE_CHECKING:  # 运行时靠注入，不必为类型名背一层导入
    from .tm import TranslationMemory
from .tm import fuzzy_hit  # v1.9.9：近似复用明细，四档同一形状
from .local_engines import ENGLISH_DIR, MULTILINGUAL_DIR, MarianEngine, NllbEngine
from .number_check import extract_numbers
from .translator import normalize_source, should_translate, split_for_api

#: 多语种模型的英文语种码。精细模式下英文段要拿给 NLLB 再译一遍做对比。
NLLB_ENGLISH = "eng_Latn"

MODEL_DIR = ENGLISH_DIR
MAX_CHUNK_CHARS = 1200
BATCH_ITEMS = 24
BATCH_CHARS = 6000
# 束搜索平均对数概率低于此值时，模型多半在瞎编，宁可保留原文
LOW_CONFIDENCE = -1.15

# 骨架（TODO）：离线质量档位。现在只有「small」一档在用；后续接大模型时按档位
# 切换模型路径 / 量化方式 / 批大小 / 峰值内存，别把参数散落在代码各处。
MODEL_PRESETS = {
    "small": {
        "english_dir": ENGLISH_DIR,  # opus-mt-en-zh-ct2（int8，150MB）
        "multilingual_dir": MULTILINGUAL_DIR,  # nllb-200-zh-ct2（int8，602MB）
        "batch_items": BATCH_ITEMS,
        "note": "现状：小、快、质量一般，两台模型不同时驻留",
    },
    "large": {
        # TODO: 更大模型（如 NLLB-200-3.3B 或更大的英中 OPUS-MT）的转换目录、
        #       量化方式（int8_float16?）、批大小与峰值内存预算；模型放到
        #       models/ 下同结构目录，路径约定与 fetch_multilingual_model.py 一致。
        "note": "待定：质量↑、内存与速度↓，需要 Lee 拍板取舍后再填",
    },
}

CJK_GAP_RE = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")
SPACED_LETTERS_RE = re.compile(r"\b(?:[A-Za-z]\s){1,}[A-Za-z]\b")
PUNCT_MAP = {",": "，", ";": "；", ":": "：", "?": "？", "!": "！"}
SENTENCE_END = ".!?;:。，！？；：”』」"
TRAILING_PERIOD_RE = re.compile(r"[。.]+$")
WORD_RE = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)?")

UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
SCALES = {"hundred": 100, "thousand": 10**3, "million": 10**6, "billion": 10**9, "trillion": 10**12}
BIG_SCALES = ("million", "billion", "trillion")
NUM_WORDS = set(UNITS) | set(TENS) | set(SCALES) | {
    "point", "and", "minus", "plus", "percent", "per", "cent",
}


def _collapse_spaced_letters(text: str) -> str:
    # 部分 PDF 原文本身带字距（D e m o），模型会照搬，需要拼回 Demo
    parts = []
    for part in re.split(r"\s{2,}", text):
        part = re.sub(r"\s+", " ", part.strip())
        parts.append(SPACED_LETTERS_RE.sub(lambda match: match.group(0).replace(" ", ""), part))
    return " ".join(part for part in parts if part)


def _polish(text: str) -> str:
    text = _collapse_spaced_letters(text.replace("\u00a0", " ").strip())
    text = CJK_GAP_RE.sub("", text)
    text = re.sub(
        r"\s*([,;:?!])(?=\s*[\u3400-\u9fff])",
        lambda match: PUNCT_MAP[match.group(1)],
        text,
    )
    text = re.sub(
        r"(?<=[\u3400-\u9fff])\s*([,;:?!])",
        lambda match: PUNCT_MAP[match.group(1)],
        text,
    )
    text = re.sub(r"\s+([，。；：？！、])", r"\1", text)
    text = re.sub(r"([，。；：？！、])\s+", r"\1", text)
    # 多语种模型习惯用半角点号断句，需要按中文习惯改写
    text = re.sub(r"\.(?=[\u3400-\u9fff\u3000-\u303f])", "。", text)
    text = re.sub(r"(?<=[\u3400-\u9fff])\s*\.\s*(?=$|[\u3400-\u9fff])", "。", text)
    if text.endswith(".") and re.search(r"[\u3400-\u9fff]", text[:-1]):
        text = text[:-1] + "。"
    # 多语种模型偶尔把句子截断在逗号上，去掉悬空的顿号
    return re.sub(r"[，、；,;]+$", "", text).strip()


def _looks_off_scale(source: str, translated: str) -> bool:
    # 「Table 3」「目录」这类短语容易被模型扩写成整句，长度离谱时保留原文。
    # 中译英不能按字符比：一个汉字常折成半个英文词，14 个汉字译成
    # "This is a confidential document, please do not divulge it."（47 个字母）
    # 是正常译文，按 3 倍字符会被误判成扩写整段丢弃。英文成品改按词数判。
    src = re.sub(r"\s+", "", source)
    if len(src) > 16:
        return False
    if direction.is_english():
        words = len(re.findall(r"[A-Za-z0-9']+", translated))
        return words > max(len(src) * 2, 6)
    dst = re.sub(r"\s+", "", translated)
    return len(dst) > max(len(src) * 3, 12)


def _is_latin_text(text: str) -> bool:
    latin = len(re.findall(r"[A-Za-z]", text))
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    return latin > cjk


def _looks_degenerate(text: str) -> bool:
    # 模型对无句末标点的短标题容易输出「目录目录」「导言 导言 导言」这类重复
    if _is_latin_text(text):
        # 拉丁字母只有 26 个，字符多样性规则是按中文标定的一律不适用：实测
        # 一句 73 字符的正常英文只剩 18 个不同字母，会被误判成复读丢掉。
        # 改按词判——正常句子用词分散，真复读（the the the）反复同一个词。
        words = re.findall(r"[A-Za-z0-9']+", text.lower())
        return len(words) >= 4 and len(set(words)) <= max(2, len(words) // 3)
    core = re.sub(r"[\s。，、；：！？.!?;:]+", "", text)
    if len(core) >= 12 and re.search(r"(.)\1{5,}", core):
        return True
    for unit in range(2, min(6, len(core) // 2 + 1)):
        if len(core) % unit == 0 and core[:unit] * (len(core) // unit) == core:
            return True
    if len(core) >= 8 and len(set(core)) <= 3:
        return True
    return len(core) >= 24 and len(set(core)) < len(core) * 0.25


def _collapse_repetition(text: str) -> str:
    """重复输出里最常用的一段往往就是正确译文，比保留原文更好用。"""
    if _is_latin_text(text):
        # 英文的重复单元是词不是字母：「machine learning machine learning」
        # 按字符滑窗拼不出可读片段，只能在词上找同一个 n-gram。
        tokens = re.findall(r"[A-Za-z0-9'’\-]+", text)
        if len(tokens) < 2:
            return ""
        for size in (1, 2, 3, 4):
            counts: dict[tuple[str, ...], int] = {}
            first: dict[tuple[str, ...], tuple[str, ...]] = {}
            for start in range(len(tokens) - size + 1):
                gram = tokens[start : start + size]
                key = tuple(word.lower() for word in gram)
                counts[key] = counts.get(key, 0) + 1
                first.setdefault(key, tuple(gram))
            if not counts:
                continue
            key, hits = max(counts.items(), key=lambda item: item[1])
            if hits * size >= len(tokens) * 0.8:
                return " ".join(first[key])
        return ""
    compact = re.sub(r"[\s。，、；：！？.!?;:]+", "", text)
    if len(compact) < 4:
        return ""
    for size in (2, 3, 4):
        counts: dict[str, int] = {}
        for start in range(0, len(compact) - size + 1):
            counts[compact[start : start + size]] = counts.get(compact[start : start + size], 0) + 1
        if not counts:
            continue
        gram, hits = max(counts.items(), key=lambda item: item[1])
        if hits * size >= len(compact) * 0.8:
            return gram
    return ""


def _salvage(source: str, translated: str) -> str:
    if not translated:
        return ""
    if _looks_off_scale(source, translated):
        collapsed = _collapse_repetition(translated)
        return collapsed if collapsed and not _looks_off_scale(source, collapsed) else ""
    if _looks_degenerate(translated):
        collapsed = _collapse_repetition(translated)
        return collapsed if collapsed and not _looks_off_scale(source, collapsed) else ""
    return translated


def _format(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return ("%.6f" % value).rstrip("0").rstrip(".")


def _parse_number(words: list[str], start: int) -> tuple[str, int] | None:
    """把 forty percent、three point two billion 这类拼写数字改成可读的阿拉伯数字。"""
    size = len(words)
    index = start
    sign = ""
    if words[index] in ("minus", "plus"):
        sign = "-" if words[index] == "minus" else "+"
        index += 1
    total = 0.0
    section = 0.0
    number = None
    scale_count = 0
    digit_count = 0
    fraction = None
    last_scale = ""
    tens_last = False
    while index < size:
        word = words[index]
        if word in UNITS:
            if fraction is not None:
                fraction.append(str(UNITS[word]))
                index += 1
                continue
            if tens_last and number is not None and UNITS[word] < 10:
                number += UNITS[word]
            elif number is not None and not tens_last:
                break
            else:
                number = UNITS[word]
            digit_count += 1
            tens_last = False
            index += 1
            continue
        if word in TENS:
            if fraction is not None or number is not None:
                break
            number = TENS[word]
            digit_count += 1
            tens_last = True
            index += 1
            continue
        if word == "hundred":
            if fraction is not None:
                break
            section += (number if number is not None else 1) * 100
            number = None
            scale_count += 1
            last_scale = "hundred"
            index += 1
            tens_last = False
            continue
        if word in SCALES:
            if fraction is not None:
                break
            scale = SCALES[word]
            if number is not None:
                section += number
            elif section == 0 and total == 0:
                break
            total += section * scale
            section = 0.0
            number = None
            scale_count += 1
            last_scale = word
            index += 1
            tens_last = False
            if index < size and words[index] == "and":
                index += 1
            continue
        if word == "point":
            if number is None and section == 0 and total == 0:
                break
            section += number or 0
            total += section
            section = 0.0
            number = None
            fraction = []
            index += 1
            continue
        if word == "and":
            if number is None and section == 0 and total == 0:
                break
            index += 1
            tens_last = False
            continue
        break
    value = total + section + (number or 0)
    end = index
    if fraction is not None:
        if not fraction:
            return None
        return ("%s%s.%s" % (sign, int(value), "".join(fraction)), end)
    percent = 0
    if end < size and words[end] == "percent":
        percent = end + 1
    elif end + 1 < size and words[end] == "per" and words[end + 1] == "cent":
        percent = end + 2
    if percent:
        if not digit_count:
            return None
        return ("%s%s percent" % (sign, _format(value)), percent)
    if not scale_count and digit_count < 2 and not sign:
        return None
    if last_scale in BIG_SCALES and value >= SCALES[last_scale] and not value % SCALES[last_scale]:
        return ("%s%s %s" % (sign, _format(value / SCALES[last_scale]), last_scale), end)
    return ("%s%s" % (sign, _format(value)), end)


def normalize_numbers(text: str) -> str:
    """离线英译中小模型算不对拼写数字，先换成阿拉伯数字再翻译。"""
    spans = [(match.start(), match.end()) for match in WORD_RE.finditer(text)]
    if not spans:
        return text
    flat: list[tuple[int, str]] = []
    for position, (start, end) in enumerate(spans):
        for piece in text[start:end].lower().replace("-", " ").split():
            flat.append((position, piece))
    words = [piece for _, piece in flat]
    if not any(word in NUM_WORDS for word in words):
        return text
    heads = set(UNITS) | set(TENS) | set(SCALES)
    edits: list[tuple[int, int, str]] = []
    index = 0
    while index < len(flat):
        if words[index] in NUM_WORDS and not (index and words[index - 1] in heads):
            parsed = _parse_number(words, index)
            if parsed:
                value, end = parsed
                last = flat[end - 1][0] if end else flat[index][0]
                edits.append((spans[flat[index][0]][0], spans[last][1], value))
                index = end
                continue
        index += 1
    result = text
    for start, end, value in reversed(edits):
        result = result[:start] + value + result[end:]
    return result


CJK_UNIT_CHARS = "十百千万萬만十亿億억兆조천백"
CJK_UNITS = {
    "十": 10, "百": 100, "千": 1000, "万": 10**4, "萬": 10**4, "만": 10**4,
    "십": 10, "백": 100, "천": 1000,
    "亿": 10**8, "億": 10**8, "억": 10**8, "兆": 10**12, "조": 10**12,
}
CJK_DIGITS = r"\d[\d,]*"
CJK_PAIR_RE = re.compile(rf"({CJK_DIGITS})\s*([{CJK_UNIT_CHARS}])")
CJK_RUN_RE = re.compile(rf"(?:{CJK_DIGITS}\s*[{CJK_UNIT_CHARS}]\s*){{2,}}[{CJK_UNIT_CHARS}]?")
CJK_BIG_ORDER = ((10**12, "兆"), (10**8, "亿"), (10**4, "万"))
CJK_NUMBER_LANGS = ("jpn_Jpan", "kor_Hang")


def normalize_cjk_numbers(text: str) -> str:
    """把「3億5000万」这类复合汉字数量先合并成「3.5億」再翻译。

    实测多语种模型读 3億5000万 会输出 350亿（错一个量级），读 3.5億 才输出 3.5亿；
    单一单位的写法（250万、40パーセント、2030年）不动，避免改坏原文。
    """

    def collapse(match) -> str:
        raw = match.group(0)
        body = raw.strip()
        total = 0.0
        for number, unit in CJK_PAIR_RE.findall(body):
            total += float(number.replace(",", "")) * CJK_UNITS[unit]
        if len(body) > 1 and body[-1] in CJK_UNITS and not body[-2].isdigit():
            # 「1千2百万」结尾的万修饰前面整串，是汉字数字的标准读法
            total *= CJK_UNITS[body[-1]]
        for magnitude, fallback in CJK_BIG_ORDER:
            if total >= magnitude:
                glyph = next((ch for ch in body if CJK_UNITS.get(ch) == magnitude), fallback)
                rendered = f"{total / magnitude:.10g}{glyph}"
                return rendered + (" " if raw[: -len(body) :] or raw[len(body) :] else "")
        return raw

    return CJK_RUN_RE.sub(collapse, text)


class LocalTranslator:
    """保密模式的本机翻译器：英文走轻量专用模型，其他语种按需加载多语种模型。"""

    #: 类级默认值。为什么不在 __init__ 里做完就算了：``test_rescue`` /
    #: ``test_term_hits`` 里有按 ``__new__`` 拼装的半实例（不加载几百 MB 模型
    #: 又要能调 summary），它们读不到实例属性。把默认值放在类上，半实例
    #: 也能得到正确初值——比在 summary() 里写 ``getattr(..., 0)`` 好：
    #: 后者会把「计数器忘了初始化」这个 bug 变成沉默的 0。
    tm = None
    tm_exact = 0
    tm_fuzzy = 0

    #: 批推理尺寸（一次送进模型的段数 / 累计字符 / 单段切开长度）。同样是
    #: 类属性打底：靠 ``__new__`` 拼装的半实例不该在 _make_batches 里撞
    #: AttributeError，而 24/6000/1200 是本机模型的实测上限，只允许被
    #: ``--batch-*`` 收紧。
    batch_items = BATCH_ITEMS
    batch_chars = BATCH_CHARS
    chunk_chars = MAX_CHUNK_CHARS

    def __init__(
        self,
        model_dir: str | os.PathLike[str] | None = None,
        multilingual_dir: str | os.PathLike[str] | None = None,
        stop_event=None,
        log: Callable[[str], None] | None = None,
        style: str = STANDARD,
        rescue: bool = True,
        tm: TranslationMemory | None = None,
        jobs: int = DEFAULT_JOBS,
        batch_items: int | None = None,
        batch_chars: int | None = None,
        chunk_chars: int | None = None,
        retry_delays: tuple[float, ...] | None = None,
    ) -> None:
        # 保密模式把日志同时落到文件：屏幕上滚过去就没了，事后无法复盘。
        # 挂在构造函数里而不是三个调用点各挂一次，是为了保证 GUI / CLI 都不会漏，
        # 也不会有人"顺手"把其中一个入口的日志摘掉。写进去的只有元信息。
        self._file_log = FileLog()
        self.log = tee_log(log, self._file_log)
        #: 并发度：离线档**接受 --jobs 但按 1 执行**，并且要说出来。
        #: 理由（诚实交代）：本档的翻译是 CTranslate2 的进程内批推理，它
        #: 自己已经把一批段一起送进模型并吃满 CPU 线程；外面再开线程并发
        #: 只是把内存峰值翻倍、把上下文切换吃掉，没有收益且有 OOM 风险。
        #: 宁可说清楚「没生效」，也不装作支持。
        self.jobs = clamp_jobs("offline", jobs)
        self.governor = JobGovernor("offline", jobs, log=self.log)
        # 说明口径统一由 JobGovernor.announce 出（core/concurrency.py 的
        # TIER_SINGLE_NOTE），四档一句话口径一致，不在这里再写一份。
        self.governor.announce()
        # 批尺寸旋钮（v1.6.9）：离线档一次送进模型的段数直接决定内存峰值，
        # 24/6000/1200 是这套小模型在本机实测的稳定上限，写大了夹回来并说清楚；
        # 写小了照办——小内存机器上主动收窄是真需求，不是调参癖。
        self.batch_items = cap_int(
            batch_items, label="单次批段数", engine="离线模型",
            default=BATCH_ITEMS, log=self.log,
        )
        self.batch_chars = cap_int(
            batch_chars, label="单次批字符数", engine="离线模型",
            default=BATCH_CHARS, log=self.log,
        )
        self.chunk_chars = cap_int(
            chunk_chars, label="单段字符数", engine="离线模型",
            default=MAX_CHUNK_CHARS, log=self.log,
        )
        if retry_delays is not None:
            warn_unused("offline", ["--retry-delays="], self.log)
        self.stop_event = stop_event
        self.style = style
        self.rescue = rescue
        self.rescued_count = 0
        self._rescue_warned = False
        self._english = MarianEngine(Path(model_dir) if model_dir else ENGLISH_DIR, self.log)
        self._multilingual = NllbEngine(
            Path(multilingual_dir) if multilingual_dir else MULTILINGUAL_DIR, self.log
        )
        self._memory: dict[str, str] = {}
        self.translated_count = 0
        self.skipped_count = 0
        self.cache_hits = 0
        self.failed_count = 0
        self.unsupported_count = 0
        self.no_model_count = 0
        self.term_count = 0
        #: 翻译记忆（可注入，**默认不接**）。保密模式零落盘：记忆库在 SQLite
        #: 里，连读都不许读，app.py 装配时保密模式给的就是 None，这里整段
        #: 跳过而不是"读不到就當沒事"。
        self.tm = tm
        self.tm_exact = 0
        self.tm_fuzzy = 0
        intro = glossary.intro()
        if intro:
            self.log(intro)
        self.fragment_count = 0
        self.dubious_count = 0
        self.tm_exact = 0
        self.tm_fuzzy = 0
        #: 近似复用明细（v1.9.9）：{段号, 原文, 相似度}，进自检报告。
        #: 保密模式不接记忆（``tm`` 为 None），这条明细也就恒为空。
        self.tm_fuzzy_hits: list[dict] = []
        self.language_counts: dict[str, int] = {}
        self._failure_logs = 0
        self._dubious_logs = 0
        self._unsupported_warned = False
        self._no_model_warned = False
        self._fragment_warned = False
        #: 术语命中明细（本次运行），字段与在线 Translator 一致：
        #: {段落号, 原文术语, 指定译法, 是否生效}。只存内存——
        #: 保密模式零落盘，自检报告写在成品旁不受影响。
        self.term_hits: list[dict] = []

    def summary(self) -> str:
        text = (
            f"翻译 {self.translated_count} 段，跳过 {self.skipped_count} 段，"
            f"内存复用 {self.cache_hits} 段，失败 {self.failed_count} 段"
        )
        if self.rescued_count:
            text += f"，换模型重试救回 {self.rescued_count} 段"
        if self.language_counts:
            detail = "、".join(
                f"{name} {count} 段"
                for name, count in sorted(self.language_counts.items(), key=lambda item: -item[1])
            )
            text += f"（{detail}）"
        if self.unsupported_count:
            text += f"，语种无法识别保留 {self.unsupported_count} 段"
        if self.no_model_count:
            text += f"，缺多语种模型保留 {self.no_model_count} 段"
        if self.term_count:
            text += f"，标题词条直译 {self.term_count} 段"
        if self.fragment_count:
            text += f"，短句存疑保留 {self.fragment_count} 段"
        if self.dubious_count:
            text += f"，模型把握不足保留 {self.dubious_count} 段"
        if self.tm_exact:
            text += f"，翻译记忆直通 {self.tm_exact} 段（完全一致，未进引擎）"
        if self.tm_fuzzy:
            text += f"，记忆近似复用 {self.tm_fuzzy} 段（待确认）"
        locked = intro()
        if locked:
            text += f"，{locked}"
        if self.term_hits:
            failed = sum(1 for hit in self.term_hits if not hit["ok"])
            text += f"，术语命中 {len(self.term_hits)} 处，{failed} 处未生效"
        return text

    def _prepare(
        self,
        text: str,
        language: str,
        hits: list[tuple[str, str, str]] | None = None,
    ) -> tuple[str, bool, dict[str, dict[str, str]]]:
        """免翻区掩码 + 术语锁定 + 句尾补点。返回组合映射（见 ``lock_and_mask``）。

        顺序与云端的 prepare 段一致：先掩免翻区，再锁术语（理由见
        ``core/no_translate.py`` 的「组合顺序」段）——两条链路必须同口径，
        否则同一份文档在保密模式与联网模式下会得到不同的 URL 处理。
        """
        stripped, mapping = lock_and_mask(text.strip(), hits)
        if language == ENGLISH:
            stripped = normalize_numbers(stripped)
        elif language in CJK_NUMBER_LANGS:
            stripped = normalize_cjk_numbers(stripped)
        if stripped and stripped[-1] not in SENTENCE_END:
            return f"{stripped}.", True, mapping
        return stripped, False, mapping

    def _decode(
        self, raw: str, drop_period: bool, mapping: dict | None = None
    ) -> str:
        if direction.use_chinese_postprocess():
            text = _polish(raw)
        else:
            # 译入英文：``_polish`` 会把英文标点全换成中文全角，必须整段跳过。
            text = raw
        text = unlock_and_restore(text, mapping or {})
        if drop_period:
            text = TRAILING_PERIOD_RE.sub("", text).rstrip()
        return polish(text, self.style) if direction.use_chinese_postprocess() else text

    def _record(
        self, index: int, source: str, translated: str, language: str, results: list,
        score: float = 0.0,
    ) -> None:
        if score and score < LOW_CONFIDENCE:
            results[index] = source
            self.dubious_count += 1
            if self._dubious_logs <= 3:
                self._dubious_logs += 1
                self.log(f"第 {index + 1} 段离线把握不足（模型自评 {score:.2f}），已保留原文")
            return
        usable = _salvage(source, translated)
        if not usable:
            self.failed_count += 1
            self._failure_logs += 1
            if self._failure_logs <= 5 or self._failure_logs % 25 == 0:
                self.log(f"第 {index + 1} 段未能生成有效译文，保留原文")
            results[index] = source
            return
        results[index] = usable
        self._memory[f"{language}|{source}"] = usable
        self.translated_count += 1
        label = "英文" if language == ENGLISH else language_label(language)
        self.language_counts[label] = self.language_counts.get(label, 0) + 1

    def _translate_group(
        self, engine, language: str, indices: list[int], texts: list[str], results: list
    ) -> None:
        sources = [normalize_source(texts[index]) for index in indices]
        prepared: list[
            tuple[str, bool, dict[str, str], list[tuple[str, str, str]]]
        ] = []
        for source in sources:
            hits: list[tuple[str, str, str]] = []
            text, drop, mapping = self._prepare(source, language, hits)
            prepared.append((text, drop, mapping, hits))
        try:
            raw_rows = self._translate(
                engine, language, [text for text, _, _, _ in prepared]
            )
            if self.rescue:
                raw_rows = self._rescue_rows(
                    language, [text for text, _, _, _ in prepared], raw_rows
                )
        except CancelledError:
            raise
        except Exception as exc:
            self.log(f"批量翻译遇到问题（{type(exc).__name__}），改为逐段重试")
            for index, source in zip(indices, sources):
                check_cancel(self.stop_event)
                self._translate_single(engine, language, index, source, results)
            return
        for index, source, (text, drop, mapping, hits), (raw, score) in zip(
            indices, sources, prepared, raw_rows
        ):
            translated = self._decode(raw, drop, mapping)
            self._record(
                index, source, translated, language, results, score
            )
            # 术语命中放在 _record 之后判：_record 可能因低置信分/退化输出
            # 把整段打回保留原文，那时 results[index] 是原文而不是译文——
            # 对照行必须照实显示未生效，判定依据是成品而不是中间译文
            # （QA 第 1 轮观察 2）。
            if hits:
                self.term_hits.extend(hit_rows(index + 1, hits, results[index]))

    def _translate(self, engine, language: str, texts: list[str]) -> list[tuple[str, float]]:
        return engine.translate(texts, language)

    def _candidate_ok(self, source: str, text: str) -> bool:
        """硬否决：空、回显原文、复读、丢了原文里的数字——这类一眼就是错的。

        模型自评分只能在同一量级的候选之间排序，单独看会挑错（实测 NLLB 把
        net profit 译成「净利息」仍然拿到更高的分），所以先过一遍硬约束。
        """
        if not text or not text.strip():
            return False
        if normalize_source(text) == normalize_source(source):
            return False
        if not _salvage(source, text):
            return False
        wanted = extract_numbers(source)
        if wanted:
            found = extract_numbers(text)
            if any(number not in found for number in wanted):
                return False
        return True

    def _rescue_rows(
        self, language: str, texts: list[str], primary_rows: list[tuple[str, float]]
    ) -> list[tuple[str, float]]:
        """主模型失手的段落，换另一个模型再试一次，成功才替换。

        不做「两个模型各译一遍再择优」：两个模型的自评分不在一个量级，实测
        NLLB 把 net profit 译成「净利息」仍然拿到比 Marian 更高的分，投票会在
        多数句子上挑错。所以第二模型只用于兜底——主模型已经判死（复读、回显、
        丢数字、分数过低）的段落，反正也要保留原文，换模型试试是净收益。
        """
        weak = [
            index
            for index, (source, (text, score)) in enumerate(zip(texts, primary_rows))
            if score < LOW_CONFIDENCE or not self._candidate_ok(source, text)
        ]
        if not weak:
            return primary_rows
        if not self._multilingual.available():
            if not self._rescue_warned:
                self._rescue_warned = True
                self.log("部分段落把握不足，安装多语种模型后可自动换模型重试。")
            return primary_rows
        alt_language = NLLB_ENGLISH if language == ENGLISH else language
        try:
            alt_rows = self._multilingual.translate(
                [texts[index] for index in weak], alt_language
            )
        except CancelledError:
            raise
        except Exception as exc:
            self.log(f"换模型重试失败（{type(exc).__name__}）")
            return primary_rows
        for index, (text, score) in zip(weak, alt_rows):
            source = texts[index]
            if score < LOW_CONFIDENCE or score <= primary_rows[index][1]:
                continue
            if not self._candidate_ok(source, text):
                continue
            primary_rows[index] = (text, score)
            self.rescued_count += 1
        return primary_rows

    def _translate_single(
        self, engine, language: str, index: int, source: str, results: list
    ) -> None:
        hits: list[tuple[str, str, str]] = []
        text, drop, mapping = self._prepare(source, language, hits)
        try:
            raw, score = self._translate(engine, language, [text])[0]
            translated = self._decode(raw, drop, mapping)
        except CancelledError:
            raise
        except Exception:
            self.failed_count += 1
            results[index] = source
            return
        self._record(index, source, translated, language, results, score)
        # 同 _translate_group：用成品 results[index] 判定，不用中间译文。
        if hits:
            self.term_hits.extend(hit_rows(index + 1, hits, results[index]))

    def _translate_long(
        self, engine, language: str, index: int, source: str, results: list
    ) -> None:
        pieces = split_for_api(source, self.chunk_chars)
        prepared: list[
            tuple[str, bool, dict[str, str], list[tuple[str, str, str]]]
        ] = []
        for piece in pieces:
            hits: list[tuple[str, str, str]] = []
            text, drop, mapping = self._prepare(piece, language, hits)
            prepared.append((text, drop, mapping, hits))
        decoded: list[str] = []
        all_hits: list[tuple[str, str, str]] = []
        score = 0.0
        try:
            raw_rows = self._translate(
                engine, language, [text for text, _, _, _ in prepared]
            )
            scores = []
            for position, ((text, drop, mapping, hits), (raw, chunk_score)) in enumerate(
                zip(prepared, raw_rows)
            ):
                decoded.append(
                    self._decode(
                        raw, drop and position == len(prepared) - 1, mapping
                    )
                )
                all_hits.extend(hits)
                scores.append(chunk_score)
            score = sum(scores) / len(scores) if scores else 0.0
        except CancelledError:
            raise
        except Exception:
            decoded = []
            all_hits = []
        joined = (
            _polish("".join(decoded))
            if direction.use_chinese_postprocess()
            else "".join(decoded)
        )
        if not decoded or any(not piece for piece in decoded):
            joined = ""
        self._record(index, source, joined, language, results, score)
        # 长段按块锁术语，但命中只按整段记一行：指定译法有没有进成品，
        # 要看成品 results[index]，分块各自记会重复。放在 _record 之后
        # 判，与另外两条链路一致（QA 第 1 轮观察 2）。
        if all_hits:
            self.term_hits.extend(hit_rows(index + 1, all_hits, results[index]))

    def _make_batches(self, indices: list[int], texts: list[str]) -> list[list[int]]:
        batches: list[list[int]] = []
        current: list[int] = []
        length = 0
        for index in indices:
            size = len(normalize_source(texts[index]))
            if current and (
                length + size > self.batch_chars or len(current) >= self.batch_items
            ):
                batches.append(current)
                current = []
                length = 0
            current.append(index)
            length += size
        if current:
            batches.append(current)
        return batches

    def _run_language(
        self,
        engine,
        language: str,
        indices: list[int],
        texts: list[str],
        results: list,
        resolved: int,
        total: int,
        progress: Callable[[int, int, str], None] | None,
    ) -> int:
        normal = [
            index for index in indices if len(normalize_source(texts[index])) <= self.chunk_chars
        ]
        normal_set = set(normal)
        long_items = [index for index in indices if index not in normal_set]
        for batch in self._make_batches(normal, texts):
            check_cancel(self.stop_event)
            self._translate_group(engine, language, batch, texts, results)
            resolved += len(batch)
            if progress is not None:
                progress(min(resolved, total), total, "")
        for index in long_items:
            check_cancel(self.stop_event)
            self._translate_long(engine, language, index, normalize_source(texts[index]), results)
            resolved += 1
            if progress is not None:
                progress(min(resolved, total), total, "")
        return resolved

    def translate_texts(
        self,
        texts: list[str],
        progress: Callable[[int, int, str], None] | None = None,
    ) -> list[str]:
        total = len(texts)
        results: list[str | None] = [None] * total
        groups: dict[str, list[int]] = {}
        resolved = 0
        has_multilingual = self._multilingual.available()
        # 译入英文时 Marian（英→中专用）一条都用不上，所有段落都走多语种模型。
        english_mode = direction.is_english()

        for index, source in enumerate(texts):
            check_cancel(self.stop_event)
            normalized = normalize_source(source)
            if not normalized:
                results[index] = source
                self.skipped_count += 1
                resolved += 1
                continue
            # 标题词条先查表，日文汉字标题（別添資料）不会被当成中文跳过
            term = direction.lookup_heading(normalized)
            if term:
                results[index] = term
                self.term_count += 1
                resolved += 1
                continue
            if not should_translate(normalized):
                results[index] = source
                self.skipped_count += 1
                resolved += 1
                continue
            language = detect(normalized)
            if english_mode:
                # 语种判定只用来给 NLLB 挑源语前缀。中文（以及拉丁占多的
                # 中英混排）一律按 zho_Hans 记——认不出源语时 NLLB 会乱译。
                if language in ("", ENGLISH):
                    language = "zho_Hans"
            elif not language:
                results[index] = source
                self.unsupported_count += 1
                resolved += 1
                if not self._unsupported_warned:
                    self._unsupported_warned = True
                    self.log("检测到无法识别语种的段落，已保留原文。")
                continue
            if language != ENGLISH and not has_multilingual:
                results[index] = source
                self.no_model_count += 1
                resolved += 1
                if not self._no_model_warned:
                    self._no_model_warned = True
                    if english_mode:
                        self.log("中译英需要多语种离线模型，请先运行「安装多语种离线模型.bat」。")
                    else:
                        self.log("本文档含非英文内容，需要先运行「安装多语种离线模型.bat」。")
                continue
            if not english_mode and language != ENGLISH and is_ambiguous_fragment(normalized):
                results[index] = source
                self.fragment_count += 1
                resolved += 1
                if not self._fragment_warned:
                    self._fragment_warned = True
                    self.log("非英文短句的离线译文不可靠，已保留原文，建议对照联网模式复核。")
                continue
            cached = self._memory.get(f"{language}|{normalized}")
            if cached is not None:
                results[index] = cached
                self.cache_hits += 1
                resolved += 1
                continue
            # 记忆查在内存之后：本进程内的重复片段先由 ``_memory`` 命中（它
            # 只在内存里，不落盘），只有真要进模型了才去问记忆库——记忆是
            # 跨进程/跨文档的资产，用它在保密模式下换一口气不划算。
            if self.tm is not None:
                remembered = self.tm.match(normalized)
                if remembered is not None:
                    results[index] = remembered.target
                    if remembered.ratio >= 1.0:
                        self.tm_exact += 1
                    else:
                        self.tm_fuzzy += 1  # 近似复用：用归用，不回写记忆
                        self.tm_fuzzy_hits.append(
                            fuzzy_hit(index + 1, normalized, remembered)
                        )
                    resolved += 1
                    continue
            groups.setdefault(language, []).append(index)

        if groups.get(ENGLISH) and not english_mode:
            self._english.load()
            resolved = self._run_language(
                self._english, ENGLISH, groups.pop(ENGLISH), texts, results, resolved, total, progress
            )
        # 两个模型不同时驻留，先释放英文模型再加载多语种模型，峰值内存少一半
        self._english.close()

        for language in sorted(groups, key=lambda code: -len(groups[code])):
            resolved = self._run_language(
                self._multilingual, language, groups[language], texts, results,
                resolved, total, progress,
            )
        self._multilingual.close()

        # 混排补译：整段被判成「中文，不用翻」的段落里夹的外文子句。
        # 放在模型关闭之后：没有混排段时只做几次正则扫描，不会多加载一次模型。
        mixed.fill_for_engine(
            self, texts, results, lambda spans: self.translate_texts(spans)
        )

        if (
            self.translated_count == 0
            and self.cache_hits == 0
            and (self.unsupported_count or self.no_model_count)
        ):
            raise TranslationError(
                "这份文档的内容无法用本机离线模型翻译，请安装多语种离线模型，或改用联网模式。"
            )

        if progress is not None:
            progress(total, total, "")
        return [
            results[index] if results[index] is not None else texts[index]
            for index in range(total)
        ]
