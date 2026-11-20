"""Word 中英混排补翻的测试。

补测的起因：``_MIXED_RUNS_ENABLED`` 之前一直是 False，``_translate_mixed_paragraphs``
是个空桩（直接 return 0）。这意味着**合同、年报、说明书里最常见的中夹英条款会被
整段跳过**——主流程看到"这段有汉字"就不翻了。

这里验证的是两件用户能直接感知的事：
1. 英文子句确实被翻出来了，而且译法是**就地插在子句之后**，不是堆在段末；
2. 插入的新 run 完整继承了原 run 的字形（深拷贝的意义），只改字体与颜色。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from docx import Document
from docx.shared import Pt

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.word_translator import (  # noqa: E402
    CJK_FONT,
    _translate_mixed_paragraphs,
    extract_mixed_runs,
)


class _FakeTranslator:
    stop_event = None

    def translate_texts(self, texts, progress=None):
        table = {
            "the laws of the People's Republic of China": "中华人民共和国法律",
            "Party B shall keep confidential all business information": "乙方应保守全部商业秘密",
        }
        return [table.get(text, f"译：{text}") for text in texts]

    def summary(self) -> str:
        return "翻译 1 段"


def _doc() -> Document:
    doc = Document()
    doc.add_paragraph("本合同适用")
    return doc


def test_extract_mixed_runs_skips_urls_and_mails() -> None:
    assert extract_mixed_runs("详见 www.example.com 的说明") == []
    assert extract_mixed_runs("联系 admin@example.com 获取") == []


def test_clause_translation_is_inserted_in_place() -> None:
    """译法必须紧跟子句，而不是追加到段末——不然读者对不上是哪一条款。"""
    doc = Document()
    paragraph = doc.add_paragraph()
    paragraph.add_run("本合同适用 the laws of the People's Republic of China 之相关规定。")

    done = _translate_mixed_paragraphs(doc, _FakeTranslator())

    assert done == 1
    texts = [run.text for run in paragraph.runs]
    # 顺序：子句前的中文 → 子句本身（保留原文）→ 就地插入的译法 → 子句后的中文
    joined = "".join(texts)
    assert "（中华人民共和国法律）" in joined
    assert joined.index("the laws") < joined.index("（中华人民共和国法律）")
    assert joined.index("（中华人民共和国法律）") < joined.index("之相关规定")


def test_translated_run_inherits_original_formatting() -> None:
    """深拷贝原 run 的意义：字号/粗体要跟着继承，只有字体和颜色被覆盖。"""
    doc = Document()
    paragraph = doc.add_paragraph()
    run = paragraph.add_run(
        "乙方义务 Party B shall keep confidential all business information 列明如下。"
    )
    run.font.size = Pt(14)
    run.bold = True

    assert _translate_mixed_paragraphs(doc, _FakeTranslator()) == 1

    new_runs = [item for item in paragraph.runs if "乙方应保守" in item.text]
    assert len(new_runs) == 1
    new_run = new_runs[0]
    assert new_run.bold is True
    assert new_run.font.size == Pt(14)
    assert new_run.font.name == CJK_FONT


def test_pure_chinese_paragraph_is_untouched() -> None:
    doc = Document()
    doc.add_paragraph("本协议自双方签字之日起生效。")
    assert _translate_mixed_paragraphs(doc, _FakeTranslator()) == 0


def test_pure_english_paragraph_is_left_to_main_pipeline() -> None:
    """纯外文段落由主流程整段翻译，这里不重复处理，避免译两次。"""
    doc = Document()
    doc.add_paragraph("This entire paragraph is written in English for testing.")
    assert _translate_mixed_paragraphs(doc, _FakeTranslator()) == 0


def test_multiple_clauses_in_one_paragraph() -> None:
    """一段里有多个英文子句。

    实现里刻意安排了从右往左的处理顺序：往更靠右的位置插入，
    才不会把左边还没处理的子句的字符位置挤偏移。这条测试用来证明
    这个顺序不是多此一举——写错方向的话，第二个子句的译文会错位或被丢掉。
    """
    doc = Document()
    paragraph = doc.add_paragraph()
    paragraph.add_run(
        "甲方应 deliver the goods on time 并且乙方应 pay within thirty days 完成交易。"
    )

    assert _translate_mixed_paragraphs(doc, _FakeTranslator()) == 2

    joined = "".join(run.text for run in paragraph.runs)
    first = joined.index("deliver the goods")
    first_translation = joined.index("（译：deliver the goods on time）")
    second = joined.index("pay within thirty days")
    second_translation = joined.index("（译：pay within thirty days）")

    assert first < first_translation < second < second_translation


def test_translation_failure_does_not_break_the_call(tmp_path: Path) -> None:
    class _BrokenTranslator:
        stop_event = None

        def translate_texts(self, texts, progress=None):
            raise RuntimeError("通道不可用")

        def summary(self) -> str:
            return ""

    doc = Document()
    doc.add_paragraph("本合同适用 the laws of the People's Republic of China 之相关规定。")
    # 补翻属于增强项：失败应当是不声不响地少一处译法，而不是让整份文档翻不出来
    assert _translate_mixed_paragraphs(doc, _BrokenTranslator()) == 0


@pytest.mark.parametrize(
    "text",
    [
        "本合同适用条款。",
        "",
        "   ",
    ],
)
def test_texts_without_english_clauses(text: str) -> None:
    doc = Document()
    doc.add_paragraph(text)
    assert _translate_mixed_paragraphs(doc, _FakeTranslator()) == 0
