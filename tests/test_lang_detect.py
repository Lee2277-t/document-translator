"""语言检测多语种（F3，v1.5.9）。

两条线：
1. 以前只认 10 种文字，孟加拉文/泰米尔文/阿姆哈拉文等 NLLB 支持的语种
   一律落到「无法识别，保留原文」。这里补上脚本判定并锁住；
2. 中英混排不许再被 langdetect 猜成法/西/意——猜错的代价是 NLLB 按错误
   源语前缀整段瞎译，且译文「看着像那么回事」，极难发现。
"""

from __future__ import annotations

import pytest

from core import lang_detect


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("এই নথিটি বাংলা ভাষায় লেখা।", "ben_Beng"),
        ("ਇਹ ਦਸਤਾਵੇਜ਼ ਪੰਜਾਬੀ ਵਿੱਚ ਲਿਖਿਆ ਹੈ।", "pan_Guru"),
        ("આ દસ્તાવેજ ગુજરાતીમાં લખાયેલ છે.", "guj_Gujr"),
        ("இந்த ஆவணம் தமிழில் எழுதப்பட்டுள்ளது.", "tam_Taml"),
        ("ఈ పత్రం తెలుగులో రాయబడింది.", "tel_Telu"),
        ("ಈ ದಾಖಲೆ ಕನ್ನಡದಲ್ಲಿ ಬರೆಯಲಾಗಿದೆ.", "kan_Knda"),
        ("ഈ രേഖ മലയാളത്തിൽ എഴുതിയിരിക്കുന്നു.", "mal_Mlym"),
        ("මෙම ලේඛනය සිංහලෙන් ලියා ඇත.", "sin_Sinh"),
        ("ເອກະສານນີ້ຂຽນເປັນພາສາລາວ.", "lao_Laoo"),
        ("ဤစာရွက်စာတမ်းကို မြန်မာဘာသာဖြင့် ရေးထားသည်။", "mya_Mymr"),
        ("ឯកសារនេះសរសេរជាភាសាខ្មែរ។", "khm_Khmr"),
        ("ይህ ሰነድ በአማርኛ ተጽፏል።", "amh_Ethi"),
    ],
)
def test_new_scripts_are_detected(text: str, code: str) -> None:
    assert lang_detect.detect(text) == code


def test_new_language_labels() -> None:
    assert lang_detect.language_label("tam_Taml") == "泰米尔文"
    assert lang_detect.language_label("guj_Gujr") == "古吉拉特文"
    assert lang_detect.language_label("amh_Ethi") == "阿姆哈拉文"


def test_single_foreign_character_is_not_enough():
    """一个孟加拉文字符不足以说明整段是孟加拉文。"""
    assert lang_detect.detect("见附图 a") == ""


def test_mixed_cjk_latin_is_sent_to_english_model(monkeypatch) -> None:
    """中英混排：拉丁部分按英文走，不采信 langdetect 的法语结论。"""
    monkeypatch.setattr(lang_detect, "_guess", lambda _sample: ("fr", 0.99))
    text = "The calculation method is described in this document 以及附录 A 的说明文字。"
    assert lang_detect.detect(text) == "en"


def test_pure_latin_still_refines_when_confidence_is_high(monkeypatch) -> None:
    """闸门只拦混排，不拦真正的外语句子。"""
    monkeypatch.setattr(lang_detect, "_guess", lambda _sample: ("fr", 0.99))
    text = (
        "La méthode de calcul est décrite dans ce document et les résultats "
        "sont présentés ci-dessous."
    )
    assert lang_detect.detect(text) == "fra_Latn"


def test_low_confidence_latin_stays_english(monkeypatch) -> None:
    monkeypatch.setattr(lang_detect, "_guess", lambda _sample: ("it", 0.6))
    text = (
        "The calculation method is described in this document and the results "
        "are presented below."
    )
    assert lang_detect.detect(text) == "en"
