"""抽段判定回归。交接稿第 5 节设计决策 4：is_translatable 的存在理由。

这里每一条都是历史上踩过的坑，改 should_translate 前先跑这个。
"""
from __future__ import annotations

from core.translator import is_translatable, normalize_source, should_translate


def test_normalize_source_strips_nbsp_and_zwsp():
    """不换行空格换成普通空格，零宽空格直接删掉。"""
    assert normalize_source("a\u00a0b c ") == "a b c"
    assert normalize_source("a\u200bb") == "ab"


def test_ascii_noise_and_short_text_skipped():
    assert not should_translate("A")
    assert not should_translate("")
    assert not should_translate("12345")


def test_url_email_version_skipped():
    assert not should_translate("https://example.com/a/b")
    assert not should_translate("www.accaglobal.com/students")
    assert not should_translate("lee@example.com")
    assert not should_translate("v1.2.3")


def test_pure_chinese_skipped():
    assert not should_translate("这是一段纯中文")


def test_mostly_chinese_paragraph_skipped():
    """设计决策 21 的对立面：汉字占比高的段落不该翻。"""
    assert not should_translate("基本步骤与 skill 1 中类似，请注意")


def test_english_paragraph_translated():
    assert should_translate("The future of AI in education is promising.")


def test_japanese_kana_translated():
    assert should_translate("機械翻訳の品質評価について")


def test_russian_translated():
    assert should_translate("Оглавление и введение к документу")


def test_pure_kanji_japanese_heading_needs_term_table():
    """「目次」全是汉字，should_translate 会当中文跳过，靠词表救回来。

    这条就是 is_translatable 存在的唯一理由，删掉它等于让日文目录整份漏翻。
    """
    assert not should_translate("目次")
    assert is_translatable("目次")


def test_term_hit_equal_to_source_is_not_translatable():
    """译文和原文一样说明没真命中，不该为了它去翻一遍。"""
    assert not is_translatable("目录")


def test_symbol_soup_skipped():
    """无空格 + 两个以上符号 = 表格题号/科目缩写那类噪音，不翻。

    注意闸门只在「整串没有空白字符」时生效：带空格的 `Profit/(loss) for the year`
    是正常句子，照样要翻。
    """
    assert not should_translate("{1.6\\1.7\\1.8}_Profit/(loss)")
    assert should_translate("Profit/(loss) for the year was material")
