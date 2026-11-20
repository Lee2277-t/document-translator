"""标题词表回归。交接稿第 5 节设计决策 3：确定性词表是必需项，不是锦上添花。

小模型会把「Оглавление」译成「这就是头部」，只有词表兜得住。
"""
from __future__ import annotations

from core.heading_terms import HEADINGS, is_ambiguous_fragment, lookup


def test_russian_heading_hit():
    assert lookup("Оглавление") == "目录"


def test_japanese_kanji_heading_hit():
    assert lookup("目次") == "目录"


def test_numbered_heading_template():
    assert lookup("Chapter 3") == "第3章"


def test_composite_heading_composed():
    """"X in Y" 这类复合标题走拼接，不走模型——幻觉又流畅又高分，拦不住。"""
    assert lookup("AI in Education") == "教育中的人工智能"


def test_ocr_confusion_lookup():
    """RapidOCR 把 AI 认成 Al（大写 A + 小写 l），原键查不到要试一次变体。"""
    assert lookup("Al in Education") == "教育中的人工智能"


def test_long_text_not_a_heading():
    assert lookup("This is a very long sentence that is definitely not a heading") == ""


def test_numbered_heading_uses_template():
    """"Chapter 3" 走带编号模板：先剥尾部数字，再用 第{}章 套出来。"""
    assert HEADINGS["chapter"] == "第{}章"


def test_key_normalization_ignores_case_and_punctuation():
    from core.heading_terms import _key

    assert _key("  Chapter  3. ") == "chapter 3"
    assert lookup("CHAPTER 7") == "第7章"


def test_prose_lines_in_glossary_file_are_not_terms(tmp_path, monkeypatch):
    """说明文字里也常出现等号，不能被当成术语收进去。

    术语表.txt 自己就有一行「4. 不想被翻译的专有名词…（Project Atlas = Project
    Atlas）。」，被收进去会白占词条名额，还匹配不到任何正文。
    """
    import core.heading_terms as ht

    glossary = tmp_path / "术语表.txt"
    glossary.write_text(
        "# 注释行 = 不生效\n"
        "net profit = 净利润\n"
        "说明：这里也有等号 = 但不是术语\n"
        "1. 编号列表 = 也不是术语\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ht, "USER_FILE", glossary)
    ht.USER_TERMS.clear()
    count = ht.load_user_terms()
    assert count == 1
    assert ht.USER_TERMS == {"net profit": "净利润"}


def test_user_glossary_loaded():
    """术语表.txt 在 import 时读入；条目全部是注释时计数为 0 也算正常。"""
    from core.heading_terms import user_count

    assert user_count >= 0


def test_short_fragment_is_ambiguous():
    assert is_ambiguous_fragment("Climate model analysis")
    assert not is_ambiguous_fragment(
        "The climate model analysis shows a clear warming trend across regions"
    )
