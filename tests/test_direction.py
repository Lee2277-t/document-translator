"""翻译方向（v1.5.8 中译英）的契约测试。

方向是模块级全局开关（``core/direction.py``），所以每条测试都要自己
``set_target``，收尾由 conftest 的 ``_reset_direction`` 夹具统一复位——
不写复位也能跑，但别依赖它。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

from core import direction, glossary  # noqa: E402
from core.heading_terms import USER_TERMS, _build_reverse_terms  # noqa: E402
from core.local_translator import LocalTranslator  # noqa: E402
from core.local_engines import NllbEngine  # noqa: E402
from core.style import STANDARD  # noqa: E402
from core.translator import (  # noqa: E402
    TranslationCache,
    finish_text,
    guess_lang_pair,
    is_translatable,
    should_translate,
)


# ---------------------------------------------------------------- 开关本身


def test_default_is_chinese():
    assert direction.target() == direction.ZH
    assert direction.label() == "中文"
    assert not direction.is_english()


@pytest.mark.parametrize("code", ["en", "EN", " en-us ", "En-GB", "english", "英文"])
def test_english_aliases(code):
    assert direction.set_target(code) == direction.EN
    assert direction.is_english()


@pytest.mark.parametrize("code", ["zh", "zh-Hans", "zh-CN", "chinese", "中文"])
def test_chinese_aliases(code):
    direction.set_target("en")
    assert direction.set_target(code) == direction.ZH


def test_unknown_target_raises_and_keeps_current():
    direction.set_target("en")
    with pytest.raises(ValueError) as info:
        direction.set_target("ja")
    assert "ja" in str(info.value)
    # 认不出就报错、**不静默退回中文**：否则 --to=en 翻出中文最难查。
    assert direction.target() == direction.EN


def test_reset_returns_to_chinese():
    direction.set_target("en")
    direction.reset()
    assert direction.target() == direction.ZH


# ---------------------------------------------------------------- 各通道语言码


def test_engine_codes_follow_direction():
    assert (direction.web_code(), direction.deepl_code(), direction.nllb_target()) == (
        "zh-Hans",
        "ZH",
        "zho_Hans",
    )
    direction.set_target("en")
    assert (direction.web_code(), direction.deepl_code(), direction.nllb_target()) == (
        "en",
        "EN",
        "eng_Latn",
    )
    # 高速英→中模型是单向的，译入英文只能走多语种模型；源语前缀因此固定中文。
    assert direction.nllb_source() == "zho_Hans"


def test_mymemory_pair_targets_direction():
    assert guess_lang_pair("This is a test") == "en|zh-CN"
    direction.set_target("en")
    assert guess_lang_pair("这是中文") == "zh|en-US"
    assert guess_lang_pair("これはテスト") == "ja|en-US"
    assert guess_lang_pair("Это тест") == "ru|en-US"


def test_cache_key_separates_directions(tmp_path):
    cache = TranslationCache(str(tmp_path / "cache.sqlite"), profile="p")
    cache.set("edge", "同一句原文", "中文译文")
    assert cache.get("edge", "同一句原文") == "中文译文"
    direction.set_target("en")
    # 同一句原文换方向必须未命中：拿到上一次的译文比不翻更糟。
    assert cache.get("edge", "同一句原文") is None


# ---------------------------------------------------------------- 抽段判定


def test_english_target_skips_english_and_translates_chinese():
    direction.set_target("en")
    assert not should_translate("This is a test.")
    assert should_translate("这是一段需要翻译的中文。")
    assert should_translate("日本語のタイトル")
    assert should_translate("Этот документ на русском языке")


def test_english_target_mixed_segment_ratio_gate():
    direction.set_target("en")
    # 中文占一定比例：翻。
    assert should_translate("Python 是一种编程语言")
    # 英文句子里挂一个汉字：不翻（送进引擎只剩无谓改写）。
    assert not should_translate("This is a long English sentence 中")
    # 短混排照常翻：闸门只在拉丁字数 ≥8 时生效。
    assert should_translate("API 接口")


def test_english_target_still_respects_shared_guards():
    direction.set_target("en")
    for text in ("https://example.com/a", "lee@example.com", "v1.2.3", "E = mc\u00b2"):
        assert not should_translate(text), text


def test_chinese_heading_is_translatable_in_english_mode():
    direction.set_target("en")
    # 中文方向：「目录」被判成已是中文而跳过（v1.5.7 的老行为）。
    direction.reset()
    assert not should_translate("目录")
    assert not is_translatable("目录")
    # 英文方向：它是一段要翻的中文，而且整段命中反向标题词表。
    direction.set_target("en")
    assert should_translate("目录")
    assert is_translatable("目录")
    assert direction.lookup_heading("目录") == "contents"


def test_reverse_heading_prefers_english():
    reverse = _build_reverse_terms()
    # 同一个中文对应多国写法时取内置表里先出现的（= 英文块），
    # 不能按长度挑：那样会挑中 目次 / 목차 / índice。
    assert reverse["目录"] == "contents"
    assert reverse["引言"] == "introduction"
    assert reverse["参考文献"] == "references"
    assert reverse["图"] == "figure"


def test_reverse_heading_prefers_user_terms(monkeypatch):
    monkeypatch.setitem(USER_TERMS, "quantum computing", "量子计算")
    reverse = _build_reverse_terms()
    assert reverse["量子计算"] == "quantum computing"


# ---------------------------------------------------------------- 术语方向


def test_term_pairs_reverse_for_english(monkeypatch):
    monkeypatch.setitem(USER_TERMS, "net profit", "净利润")
    assert direction.term_pairs()["net profit"] == "净利润"
    direction.set_target("en")
    assert direction.term_pairs()["净利润"] == "net profit"


def test_glossary_locks_chinese_term_in_english_mode(monkeypatch):
    monkeypatch.setitem(USER_TERMS, "net profit", "净利润")
    direction.set_target("en")  # 换方向会触发 glossary.reload()
    masked, mapping = glossary.lock("净利润增长了百分之十五。")
    assert "净利润" not in masked
    assert list(mapping.values()) == ["net profit"]


# ---------------------------------------------------------------- 后处理闸门


def test_finish_text_skips_chinese_polish_for_english():
    assert finish_text("AI技术", STANDARD) == "AI 技术"
    direction.set_target("en")
    assert finish_text("AI technology", STANDARD) == "AI technology"


def test_offline_decode_keeps_ascii_punctuation_in_english_mode():
    translator = LocalTranslator.__new__(LocalTranslator)
    translator.style = STANDARD
    direction.set_target("en")
    assert translator._decode("测试文本.", False) == "测试文本."
    direction.reset()
    assert translator._decode("测试文本.", False) == "测试文本。"


def test_nllb_engine_target_token_follows_direction():
    assert NllbEngine().target_token == "zho_Hans"
    direction.set_target("en")
    assert NllbEngine().target_token == "eng_Latn"
    # 显式传入优先：精细模式的「英文段再译一遍」必须固定成中文。
    assert NllbEngine(target_token="zho_Hans").target_token == "zho_Hans"


# ---------------------------------------------------------------- 命令行入口


def _run_child(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PYTHON), "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def test_cli_rejects_unknown_target_end_to_end():
    script = (
        "import sys\n"
        'sys.argv = ["app", "--to=ja", "--version"]\n'
        "import app\n"
        "app.main()\n"
    )
    child = _run_child(script)
    assert child.returncode == 4, child.stdout + child.stderr
    assert "不支持的目标语言" in child.stdout


def test_cli_accepts_english_target():
    script = (
        "import sys\n"
        'sys.argv = ["app", "--to=en", "--version"]\n'
        "import app\n"
        "app.main()\n"
    )
    child = _run_child(script)
    assert child.returncode == 0, child.stdout + child.stderr
    import app

    assert f"v{app.VERSION}" in child.stdout


def test_offline_english_still_uses_local_translator(monkeypatch):
    import app

    monkeypatch.setattr(app, "OFFLINE", True)
    direction.set_target("en")
    translator = app._build_cli_translator(lambda _message: None)
    assert type(translator).__name__ == "LocalTranslator"


def test_off_scale_gate_keeps_normal_english_output():
    """中译英不能沿用中文的字符倍率闸门。

    实测踩到的坑：14 个汉字正常折成 47 个字母（
    "This is a confidential document, please do not divulge it."），
    按「超过原文 3 倍字符就保留原文」会被整段丢弃、译文全是中文原文。
    """
    from core.local_translator import _looks_off_scale

    source = "这是一份保密文件，请勿外传。"
    translated = "This is a confidential document, please do not divulge it."
    direction.set_target("en")
    assert not _looks_off_scale(source, translated)
    # 闸门不是被关掉：短标题被扩写成整句仍要拦下
    assert _looks_off_scale("表 3", "Table 3 shows the detailed results of the experiment.")


def test_off_scale_gate_keeps_chinese_rule_in_chinese_mode():
    from core.local_translator import _looks_off_scale

    direction.reset()
    assert _looks_off_scale("目录", "本章目录列出了全文的章节结构与页码")


def test_salvage_keeps_english_translation_of_chinese_source():
    from core.local_translator import _salvage

    direction.set_target("en")
    source = "这是一份保密文件，请勿外传。"
    translated = "This is a confidential document, please do not divulge it."
    assert _salvage(source, translated) == translated


def test_degenerate_gate_does_not_flag_ordinary_english_sentence():
    """拉丁输出的复读判定必须按词，不能沿用中文的字符多样性规则。

    实测踩到的坑（v1.6.5 冻结包离线中译英）：
    "This article introduces the application of machine learning in document
    translation." 去掉标点后 73 字符、只有 18 个不同字母，低于 ``< 0.25`` 的
    中文阈值，被当成复读丢弃，成品里这段仍是中文原文。
    """
    from core.local_translator import _looks_degenerate, _salvage

    direction.set_target("en")
    translated = (
        "This article introduces the application of machine learning "
        "in document translation."
    )
    assert not _looks_degenerate(translated)
    assert _salvage("本文介绍机器学习在文档翻译中的应用。", translated) == translated


def test_degenerate_gate_flags_word_level_repetition_in_english():
    from core.local_translator import _looks_degenerate

    direction.set_target("en")
    assert _looks_degenerate("the the the the")
    assert _looks_degenerate("machine learning machine learning machine")
    assert _looks_degenerate("Chapter One Chapter One Chapter One")
    # 正常句子反复用功能词也不算退化
    assert not _looks_degenerate("It is what it is")


def test_collapse_repetition_recovers_english_phrase():
    """英文的重复单元是词，折叠结果要能直接当译文用（保留原文大小写）。"""
    from core.local_translator import _collapse_repetition

    direction.set_target("en")
    assert _collapse_repetition("machine learning machine learning machine") == (
        "machine learning"
    )
    assert _collapse_repetition("Chapter One Chapter One Chapter One") == "Chapter One"
    assert _collapse_repetition("Confidential Document Confidential Document") == (
        "Confidential Document"
    )


def test_degenerate_gate_keeps_chinese_rule_in_chinese_mode():
    from core.local_translator import _looks_degenerate

    direction.reset()
    assert _looks_degenerate("目录目录")
    assert _looks_degenerate("导言 导言 导言")
    assert _looks_degenerate("一一一一一一一一一一一一")
    assert not _looks_degenerate("本章介绍文档翻译的整体流程")


_GUI_SCRIPT = """
import sys
sys.argv = ["app"]
import app
from core import direction

application = app.TranslatorApp()
try:
    assert application.target_var.get() == direction.ZH, application.target_var.get()
    application.target_var.set(direction.EN)
    application._on_target_change()
    assert direction.is_english(), "界面选了英文但方向没切"
    assert "目标语言：英文" in application.log_text.get("1.0", "end")
    assert application._only_target_radio.cget("text") == "仅英文", "版式标签应跟随方向"
    # 非法值：方向不变，单选按钮必须回显真实方向，不能显示「中文」却在译英文
    application.target_var.set("ja")
    application._on_target_change()
    assert direction.is_english(), "非法值不该改动已有方向"
    assert application.target_var.get() == direction.EN

    # 翻译进行中：拒绝切换并回显当前方向，避免同一份文档出现两种目标语言
    application.busy = True
    application.target_var.set(direction.ZH)
    application._on_target_change()
    assert direction.is_english(), "翻译进行中不该改方向"
    assert application.target_var.get() == direction.EN
    assert "翻译进行中" in application.log_text.get("1.0", "end")
    application.busy = False
    print("TARGET_GUI_OK")
finally:
    application.root.destroy()
"""


def test_gui_target_radio_switches_direction():
    result = subprocess.run(
        [str(PYTHON), "-c", _GUI_SCRIPT],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert "TARGET_GUI_OK" in result.stdout, (
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_word_mixed_paragraph_pass_is_off_in_english_mode():
    """中夹英补翻是中文方向的补偿，英文方向不该再碰文档。"""
    from core.word_translator import _translate_mixed_paragraphs

    direction.set_target("en")

    class _NeverTouched:
        def __getattr__(self, name):
            raise AssertionError(f"英文方向不该访问文档属性 {name}")

    assert _translate_mixed_paragraphs(_NeverTouched(), None) == 0


def test_english_pdf_page_is_not_sent_back_to_engine(tmp_path):
    """整页英文的 PDF 在英文方向没有要翻的内容，不该把英文子句再送一遍。"""
    import pymupdf

    from core.common import TranslationError
    from core.pdf_translator import translate_pdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text(
        pymupdf.Point(72, 72),
        "This is a long English sentence that should be left exactly as it is.",
        fontsize=11,
        fontname="helv",
    )
    src = tmp_path / "english.pdf"
    doc.save(src)
    doc.close()

    class _Counting:
        stop_event = None

        def __init__(self):
            self.count = 0

        def translate_texts(self, texts, progress=None):
            self.count += len(texts)
            return list(texts)

        def summary(self) -> str:
            return f"翻译 {self.count} 段"

    fake = _Counting()
    direction.set_target("en")
    with pytest.raises(TranslationError) as excinfo:
        translate_pdf(str(src), fake, layout="side", ocr_enabled=False)
    assert "没有找到需要翻译的外文内容" in str(excinfo.value)
    assert fake.count == 0
