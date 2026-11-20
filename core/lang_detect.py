from __future__ import annotations

import re

KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
HANGUL_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")
BENGALI_RE = re.compile(r"[\u0980-\u09ff]")
GURMUKHI_RE = re.compile(r"[\u0a00-\u0a7f]")
GUJARATI_RE = re.compile(r"[\u0a80-\u0aff]")
TAMIL_RE = re.compile(r"[\u0b80-\u0bff]")
TELUGU_RE = re.compile(r"[\u0c00-\u0c7f]")
KANNADA_RE = re.compile(r"[\u0c80-\u0cff]")
MALAYALAM_RE = re.compile(r"[\u0d00-\u0d7f]")
SINHALA_RE = re.compile(r"[\u0d80-\u0dff]")
THAI_RE = re.compile(r"[\u0e00-\u0e7f]")
LAO_RE = re.compile(r"[\u0e80-\u0eff]")
MYANMAR_RE = re.compile(r"[\u1000-\u109f]")
ETHIOPIC_RE = re.compile(r"[\u1200-\u137f]")
KHMER_RE = re.compile(r"[\u1780-\u17ff]")
CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
ARABIC_RE = re.compile(r"[\u0600-\u06ff]")
HEBREW_RE = re.compile(r"[\u0590-\u05ff]")
DEVANAGARI_RE = re.compile(r"[\u0900-\u097f]")
GREEK_RE = re.compile(r"[\u0370-\u03ff]")
GEORGIAN_RE = re.compile(r"[\u10a0-\u10ff]")
ARMENIAN_RE = re.compile(r"[\u0530-\u058f]")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
LATIN_RE = re.compile(r"[A-Za-z\u00c0-\u024f]")

# langdetect 的 ISO-639-1 代码 → NLLB 语言标记
NLLB_CODES = {
    "en": "eng_Latn",
    "ja": "jpn_Jpan",
    "ko": "kor_Hang",
    "ru": "rus_Cyrl",
    "uk": "ukr_Cyrl",
    "be": "bel_Cyrl",
    "bg": "bul_Cyrl",
    "sr": "srp_Cyrl",
    "mn": "khk_Cyrl",
    "kk": "kaz_Cyrl",
    "fr": "fra_Latn",
    "de": "deu_Latn",
    "es": "spa_Latn",
    "it": "ita_Latn",
    "pt": "por_Latn",
    "nl": "nld_Latn",
    "pl": "pol_Latn",
    "sv": "swe_Latn",
    "da": "dan_Latn",
    "no": "nob_Latn",
    "fi": "fin_Latn",
    "cs": "ces_Latn",
    "sk": "slk_Latn",
    "hu": "hun_Latn",
    "ro": "ron_Latn",
    "hr": "hrv_Latn",
    "sl": "slv_Latn",
    "et": "est_Latn",
    "lv": "lvs_Latn",
    "lt": "lit_Latn",
    "el": "ell_Grek",
    "tr": "tur_Latn",
    "ar": "arb_Arab",
    "fa": "pes_Arab",
    "ur": "urd_Arab",
    "ps": "pbt_Arab",
    "ku": "kmr_Latn",
    "he": "heb_Hebr",
    "yi": "ydd_Hebr",
    "hi": "hin_Deva",
    "mr": "mar_Deva",
    "ne": "npi_Deva",
    "bn": "ben_Beng",
    "gu": "guj_Gujr",
    "or": "ory_Orya",
    "pa": "pan_Guru",
    "ta": "tam_Taml",
    "te": "tel_Telu",
    "ml": "mal_Mlym",
    "kn": "kan_Knda",
    "si": "sin_Sinh",
    "th": "tha_Thai",
    "lo": "lao_Laoo",
    "km": "khm_Khmr",
    "my": "mya_Mymr",
    "vi": "vie_Latn",
    "id": "ind_Latn",
    "ms": "zsm_Latn",
    "tl": "tgl_Latn",
    "jv": "jav_Latn",
    "sw": "swh_Latn",
    "am": "amh_Ethi",
    "ka": "kat_Geor",
    "hy": "hye_Armn",
    "az": "azj_Latn",
    "uz": "uzn_Latn",
    "af": "afr_Latn",
    "ca": "cat_Latn",
    "gl": "glg_Latn",
    "eu": "eus_Latn",
    "is": "isl_Latn",
    "ga": "gle_Latn",
    "cy": "cym_Latn",
}

# 命中即判定语种的文字（NLLB 代码，langdetect 只作二次确认）
SCRIPT_CODES = (
    (KANA_RE, "jpn_Jpan"),
    (HANGUL_RE, "kor_Hang"),
    (BENGALI_RE, "ben_Beng"),
    (GURMUKHI_RE, "pan_Guru"),
    (GUJARATI_RE, "guj_Gujr"),
    (TAMIL_RE, "tam_Taml"),
    (TELUGU_RE, "tel_Telu"),
    (KANNADA_RE, "kan_Knda"),
    (MALAYALAM_RE, "mal_Mlym"),
    (SINHALA_RE, "sin_Sinh"),
    (THAI_RE, "tha_Thai"),
    (LAO_RE, "lao_Laoo"),
    (MYANMAR_RE, "mya_Mymr"),
    (ETHIOPIC_RE, "amh_Ethi"),
    (KHMER_RE, "khm_Khmr"),
    (GREEK_RE, "ell_Grek"),
    (GEORGIAN_RE, "kat_Geor"),
    (ARMENIAN_RE, "hye_Armn"),
    (HEBREW_RE, "heb_Hebr"),
    (DEVANAGARI_RE, "hin_Deva"),
)

ENGLISH = "en"
MIN_MATCH = 2

# NLLB 语种标记 → 界面显示用的语种名
LANGUAGE_NAMES = {
    "jpn_Jpan": "日文", "kor_Hang": "韩文", "rus_Cyrl": "俄文", "ukr_Cyrl": "乌克兰文",
    "bel_Cyrl": "白俄文", "bul_Cyrl": "保加利亚文", "srp_Cyrl": "塞尔维亚文",
    "khk_Cyrl": "蒙古文", "kaz_Cyrl": "哈萨克文", "fra_Latn": "法文", "deu_Latn": "德文",
    "spa_Latn": "西班牙文", "ita_Latn": "意大利文", "por_Latn": "葡萄牙文",
    "nld_Latn": "荷兰文", "pol_Latn": "波兰文", "swe_Latn": "瑞典文", "dan_Latn": "丹麦文",
    "nob_Latn": "挪威文", "fin_Latn": "芬兰文", "ces_Latn": "捷克文", "slk_Latn": "斯洛伐克文",
    "hun_Latn": "匈牙利文", "ron_Latn": "罗马尼亚文", "hrv_Latn": "克罗地亚文",
    "slv_Latn": "斯洛文尼亚文", "est_Latn": "爱沙尼亚文", "lvs_Latn": "拉脱维亚文",
    "lit_Latn": "立陶宛文", "ell_Grek": "希腊文", "tur_Latn": "土耳其文",
    "arb_Arab": "阿拉伯文", "pes_Arab": "波斯文", "urd_Arab": "乌尔都文", "pbt_Arab": "普什图文",
    "kmr_Latn": "库尔德文", "heb_Hebr": "希伯来文", "ydd_Hebr": "意第绪文",
    "hin_Deva": "印地文", "mar_Deva": "马拉地文", "npi_Deva": "尼泊尔文", "ben_Beng": "孟加拉文",
    "guj_Gujr": "古吉拉特文", "ory_Orya": "奥里亚文",
    "pan_Guru": "旁遮普文", "tam_Taml": "泰米尔文", "tel_Telu": "泰卢固文",
    "mal_Mlym": "马拉雅拉姆文", "kan_Knda": "卡纳达文", "sin_Sinh": "僧伽罗文",
    "tha_Thai": "泰文", "lao_Laoo": "老挝文", "khm_Khmr": "柬埔寨文", "mya_Mymr": "缅甸文",
    "vie_Latn": "越南文", "ind_Latn": "印尼文", "zsm_Latn": "马来文", "tgl_Latn": "菲律宾文",
    "jav_Latn": "爪哇文", "swh_Latn": "斯瓦希里文", "amh_Ethi": "阿姆哈拉文",
    "kat_Geor": "格鲁吉亚文", "hye_Armn": "亚美尼亚文", "azj_Latn": "阿塞拜疆文",
    "uzn_Latn": "乌兹别克文", "afr_Latn": "南非荷兰文", "cat_Latn": "加泰罗尼亚文",
    "glg_Latn": "加利西亚文", "eus_Latn": "巴斯克文", "isl_Latn": "冰岛文",
    "gle_Latn": "爱尔兰文", "cym_Latn": "威尔士文", "eng_Latn": "英文",
}


def language_label(code: str) -> str:
    if not code:
        return "未知语种"
    if code == ENGLISH:
        return "英文"
    return LANGUAGE_NAMES.get(code, code.split("_")[0])


def _dominant(text: str) -> str:
    """按字符数最多的文字给出语种，脚本本身能确定的语种不必再猜。"""
    counts = {code: len(pattern.findall(text)) for pattern, code in SCRIPT_CODES}
    counts["rus_Cyrl"] = len(CYRILLIC_RE.findall(text))
    counts["arb_Arab"] = len(ARABIC_RE.findall(text))
    counts["eng_Latn"] = len(LATIN_RE.findall(text))
    best_code, best_count = "", 0
    for code, count in counts.items():
        if count > best_count:
            best_code, best_count = code, count
    if best_count < MIN_MATCH:
        return ""
    if best_code == "eng_Latn":
        cjk = len(CJK_RE.findall(text))
        if cjk > best_count or best_count < sum(counts.values()) * 0.5:
            return ""
    return best_code


def detect(text: str) -> str:
    """返回 NLLB 语种标记，英文返回 "en"（走更快的英译中模型），无法判断返回 ""。"""
    script = _dominant(text)
    if not script:
        return ""
    sample = re.sub(r"[0-9\s\W]+", " ", text).strip()
    if script == "eng_Latn":
        if _has_cjk(text):
            # 中英混排：拉丁部分一律按英文送翻。langdetect 对「英文 + 汉字」
            # 这种文本经常给出意大利文/印尼文的高置信度结论，照它挑源语前缀
            # 会让 NLLB 整段出鬼话，而且译文看起来「像那么回事」，很难察觉。
            return ENGLISH
        return _refine_latin(sample) or ENGLISH
    if script in ("rus_Cyrl", "arb_Arab", "hin_Deva"):
        return _refine(sample, script)
    return script


def _has_cjk(text: str) -> bool:
    """汉字/假名/谚文至少两个字符——一个「中」字当标点用的句子不算混排。"""
    return (
        len(CJK_RE.findall(text)) + len(KANA_RE.findall(text)) + len(HANGUL_RE.findall(text))
        >= MIN_MATCH
    )


def _guess(sample: str) -> tuple[str, float]:
    try:
        from langdetect import detect_langs
    except Exception:
        return "", 0.0
    try:
        best = detect_langs(sample)[0]
    except Exception:
        return "", 0.0
    return getattr(best, "lang", "") or "", float(getattr(best, "prob", 0.0) or 0.0)


def _refine_latin(sample: str) -> str:
    """拉丁字母语种最容易混，短语一律交给英文模型。

    标题、表头这类短片段（Climate model analysis、Confidential · Demo Document）
    几乎每次都会被猜成意大利文或西班牙文，误判的代价是整份英文文档多加载 800MB 模型，
    所以只接受足够长、且置信度很高的非英文结论。
    """
    if len(LATIN_RE.findall(sample)) < 40:
        return ""
    lang, prob = _guess(sample)
    if lang == ENGLISH or prob < 0.9:
        return ""
    return NLLB_CODES.get(lang, "")


def _refine(sample: str, fallback: str) -> str:
    """文字已能确定语系，只有足够长的句子才值得再细分语种。"""
    if len(sample) < 12:
        return fallback
    lang, prob = _guess(sample)
    if prob >= 0.5:
        return NLLB_CODES.get(lang, fallback)
    return fallback
