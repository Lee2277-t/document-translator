"""Word 复杂对象：批注 / 文本框 / 修订。

这三类东西以前是静默漏翻的：
- 批注住在单独的 comments part 里，正文走查看不到；
- 文本框里的段落虽然能被 ``iter(w:p)`` 扫到，但 ``mc:AlternateContent`` 会把同一段
  文字存两份（Choice 的 DrawingML + Fallback 的 VML），收两份就会翻两遍、插两遍；
- 修订里的「删除文字」是 ``w:delText``（不该翻），「插入文字」是 ``w:t``（该翻）,
  两者混在一段里，靠的是抽取层对标签的区分。

这里全部用假翻译器：验证的是「收得到、写得回、不重复」，不是模型质量。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import qn

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.word_translator import _collect_paragraphs, translate_word  # noqa: E402

_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:v="urn:schemas-microsoft-com:vml"'
)

_TXBX_SENTENCE = "Textbox caption inside the drawing."
_TXBX_TRANSLATION = "图形内的文本框标题。"


def _textbox_paragraph(text: str) -> str:
    """一段带 ``mc:AlternateContent`` 的文本框：Choice 与 Fallback 是同一段文字。"""
    content = f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
    return (
        f"<w:p {_NS}>"
        "<w:r><mc:AlternateContent>"
        '<mc:Choice Requires="wps"><w:drawing><wps:txbx><w:txbxContent>'
        f"{content}</w:txbxContent></wps:txbx></w:drawing></mc:Choice>"
        "<mc:Fallback><w:pict><v:textbox><w:txbxContent>"
        f"{content}</w:txbxContent></v:textbox></w:pict></mc:Fallback>"
        "</mc:AlternateContent></w:r>"
        "</w:p>"
    )


def _revision_paragraph(kept: str, inserted: str, deleted: str) -> str:
    return (
        f"<w:p {_NS}>"
        f"<w:r><w:t>{kept}</w:t></w:r>"
        '<w:ins w:id="1" w:author="Lee" w:date="2026-10-01T00:00:00Z">'
        f"<w:r><w:t> {inserted}</w:t></w:r></w:ins>"
        '<w:del w:id="2" w:author="Lee" w:date="2026-10-01T00:00:00Z">'
        f"<w:r><w:delText> {deleted}</w:delText></w:r></w:del>"
        "</w:p>"
    )


class _RecordingTranslator:
    """记录送翻文本的假翻译器，便于断言「哪些文字进了引擎」。"""

    stop_event = None

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.seen: list[str] = []

    def translate_texts(self, texts, progress=None) -> list[str]:
        self.seen.extend(texts)
        return [self.mapping.get(text, f"译：{text}") for text in texts]

    def summary(self) -> str:
        return "假翻译器"


def _textbox_doc(path: Path) -> Path:
    doc = Document()
    doc.add_paragraph("A plain body paragraph for the baseline.")
    doc.element.body.append(parse_xml(_textbox_paragraph(_TXBX_SENTENCE)))
    doc.save(str(path))
    return path


def _comment_doc(path: Path) -> Path:
    doc = Document()
    body = doc.add_paragraph("The board approved the revised figures.")
    doc.add_comment(
        runs=body.runs,
        text="Please double-check the revenue line before signing off.",
        author="Lee",
        initials="L",
    )
    doc.save(str(path))
    return path


# ---------------------------------------------------------------------------
# 文本框
# ---------------------------------------------------------------------------


def test_textbox_is_collected_once_despite_alternate_content(tmp_path: Path) -> None:
    """Choice 与 Fallback 是同一段文字，收两份就会翻两遍、插两遍。"""
    doc = Document(str(_textbox_doc(tmp_path / "txbx.docx")))
    texts = [item[1] for item in _collect_paragraphs(doc)]
    assert texts.count(_TXBX_SENTENCE) == 1, texts


def test_textbox_entry_is_tagged_as_textbox(tmp_path: Path) -> None:
    doc = Document(str(_textbox_doc(tmp_path / "txbx.docx")))
    kinds = {item[1]: item[2] for item in _collect_paragraphs(doc)}
    assert kinds[_TXBX_SENTENCE] == "textbox"
    assert kinds["A plain body paragraph for the baseline."] == "body"


def test_textbox_translation_stays_inside_the_shape(tmp_path: Path) -> None:
    """文本框是固定外形，译文必须接在原段内；另插一段会被 Word 裁掉。"""
    src = _textbox_doc(tmp_path / "txbx.docx")
    translator = _RecordingTranslator({_TXBX_SENTENCE: _TXBX_TRANSLATION})
    out = translate_word(str(src), translator)

    doc = Document(out)
    boxes = list(doc.element.body.iter(qn("w:txbxContent")))
    assert len(boxes) == 2, "夹具本身就是 Choice + Fallback 两份"
    translated = [
        box
        for box in boxes
        if _TXBX_TRANSLATION
        in "".join(node.text or "" for node in box.iter(qn("w:t")))
    ]
    assert len(translated) == 1, "只该翻 Word 实际显示的那一份（Choice）"
    paragraphs = list(translated[0].iter(qn("w:p")))
    assert len(paragraphs) == 1, "文本框里不该多出一个段落"
    text = "".join(node.text or "" for node in paragraphs[0].iter(qn("w:t")))
    assert _TXBX_SENTENCE in text
    assert _TXBX_TRANSLATION in text


# ---------------------------------------------------------------------------
# 批注
# ---------------------------------------------------------------------------


def test_comment_paragraph_is_collected_and_tagged(tmp_path: Path) -> None:
    doc = Document(str(_comment_doc(tmp_path / "comment.docx")))
    entries = _collect_paragraphs(doc)
    comment_texts = [text for _obj, text, kind in entries if kind == "comment"]
    assert comment_texts == [
        "Please double-check the revenue line before signing off."
    ], entries


def test_comment_translation_is_written_into_the_comments_part(
    tmp_path: Path,
) -> None:
    src = _comment_doc(tmp_path / "comment.docx")
    comment = "Please double-check the revenue line before signing off."
    translator = _RecordingTranslator({comment: "请在签字前复核收入这一行。"})
    out = translate_word(str(src), translator)

    reopened = Document(out)
    paragraphs = [para.text for item in reopened.comments for para in item.paragraphs]
    assert comment in paragraphs, paragraphs
    assert "请在签字前复核收入这一行。" in paragraphs, paragraphs


def test_comment_translation_is_not_offered_for_in_body_edit(tmp_path: Path) -> None:
    """批注不在正文段号体系里，进译后编辑清单会按错位置回写。"""
    src = _comment_doc(tmp_path / "comment.docx")
    comment = "Please double-check the revenue line before signing off."
    translator = _RecordingTranslator({comment: "请在签字前复核收入这一行。"})
    out = translate_word(str(src), translator)

    edit_list = Path(out).with_name(Path(out).stem + " 译后编辑清单.txt")
    if edit_list.exists():
        text = edit_list.read_text(encoding="utf-8")
        assert "double-check the revenue line" not in text


# ---------------------------------------------------------------------------
# 修订
# ---------------------------------------------------------------------------


def test_inserted_text_is_translated_and_deleted_text_is_not(tmp_path: Path) -> None:
    kept = "Revenue figures were reviewed by the board."
    inserted = "The audit team confirmed the final numbers."
    deleted = "Old wording that should never be translated."
    doc = Document()
    doc.add_paragraph("A plain body paragraph for the baseline.")
    doc.element.body.append(parse_xml(_revision_paragraph(kept, inserted, deleted)))
    src = tmp_path / "revisions.docx"
    doc.save(str(src))

    translator = _RecordingTranslator(
        {f"{kept} {inserted}": "董事会复核了收入数据，审计团队确认了最终数字。"}
    )
    out = translate_word(str(src), translator)

    assert any(inserted in text for text in translator.seen), translator.seen
    assert all(deleted not in text for text in translator.seen), translator.seen

    texts = [para.text for para in Document(out).paragraphs]
    assert "董事会复核了收入数据，审计团队确认了最终数字。" in texts, texts
    # 修订标记本身不动：原文段还在，成品保留修订状态
    assert any(kept in text for text in texts), texts


def test_revision_presence_is_reported_in_the_log(tmp_path: Path) -> None:
    doc = Document()
    doc.add_paragraph("A plain body paragraph for the baseline.")
    doc.element.body.append(
        parse_xml(_revision_paragraph("Revenue grew.", "Audit passed.", "Draft text."))
    )
    src = tmp_path / "revisions.docx"
    doc.save(str(src))

    lines: list[str] = []
    translate_word(str(src), _RecordingTranslator({}), log=lines.append)
    assert any("修订" in line for line in lines), lines


def test_plain_document_has_no_object_entries() -> None:
    """没有批注/文本框/修订的普通文档，行为不变（全是 body）。"""
    doc = Document()
    doc.add_paragraph("Revenue grew sharply in the third quarter.")
    entries = _collect_paragraphs(doc)
    assert [kind for _obj, _text, kind in entries] == ["body"]
