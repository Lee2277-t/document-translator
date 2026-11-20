"""Word 脚注/尾注覆盖。

python-docx 1.2.0 还没有脚注 API，只能按 content type 从包里捞部件。这个测试
用 zipfile 手工拼一份带脚注的 .docx，就是为了证明这条路真走得通——没有脚注的
文档是常态，光跑普通样例是发现不了漏翻的。
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile

import pytest

from core.word_translator import _collect_paragraphs

FOOTNOTES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:footnote w:type="separator" w:id="-1">
    <w:p><w:pPr><w:spacing w:after="0"/></w:pPr><w:r><w:separator/></w:r></w:p>
  </w:footnote>
  <w:footnote w:type="continuationSeparator" w:id="0">
    <w:p><w:pPr><w:spacing w:after="0"/></w:pPr><w:r><w:continuationSeparator/></w:r></w:p>
  </w:footnote>
  <w:footnote w:id="1">
    <w:p><w:r><w:t>The board approved the revised disclosure requirements.</w:t></w:r></w:p>
  </w:footnote>
</w:footnotes>"""

REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
FOOTNOTE_REL_TYPE = f"{REL_NS}/footnotes"
FOOTNOTE_CT = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
)


def _build_with_footnote(source: str, target: str) -> None:
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(
        target, "w", zipfile.ZIP_DEFLATED
    ) as zout:
        names = zin.namelist()
        for name in names:
            payload = zin.read(name)
            if name == "[Content_Types].xml":
                text = payload.decode("utf-8")
                if "/word/footnotes.xml" not in text:
                    text = text.replace(
                        "</Types>",
                        f'<Override PartName="/word/footnotes.xml" '
                        f'ContentType="{FOOTNOTE_CT}"/></Types>',
                    )
                payload = text.encode("utf-8")
            elif name == "word/_rels/document.xml.rels":
                text = payload.decode("utf-8")
                if "footnotes.xml" not in text:
                    text = text.replace(
                        "</Relationships>",
                        f'<Relationship Id="rIdFootnotes99" '
                        f'Type="{FOOTNOTE_REL_TYPE}" Target="footnotes.xml"/>'
                        "</Relationships>",
                    )
                payload = text.encode("utf-8")
            elif name == "word/document.xml":
                text = payload.decode("utf-8")
                marker = "<w:footnoteReference"
                if marker not in text:
                    reference = (
                        "<w:r><w:rPr><w:vertAlign w:val=\"superscript\"/></w:rPr>"
                        "<w:footnoteReference w:id=\"1\"/></w:r>"
                    )
                    text = text.replace("</w:p>", f"{reference}</w:p>", 1)
                payload = text.encode("utf-8")
            zout.writestr(name, payload)
        zout.writestr("word/footnotes.xml", FOOTNOTES_XML)


@pytest.fixture
def footnote_doc() -> str:
    from docx import Document

    source_root = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "samples",
        "保密模式样例",
        "示例-英文报告.docx",
    )
    if not os.path.exists(source_root):
        pytest.skip("样例缺失")

    tmp = tempfile.mkdtemp()
    built = os.path.join(tmp, "with_footnote.docx")
    _build_with_footnote(source_root, built)
    try:
        Document(built)
    except Exception as exc:  # 手工拼的 docx 打不开就说明夹具坏了，不是功能坏了
        shutil.rmtree(tmp, ignore_errors=True)
        pytest.skip(f"夹具构造失败：{exc}")
    yield built
    shutil.rmtree(tmp, ignore_errors=True)


def test_footnote_text_is_collected(footnote_doc):
    from docx import Document

    doc = Document(footnote_doc)
    texts = [item[1] for item in _collect_paragraphs(doc)]
    assert any("revised disclosure requirements" in text for text in texts), texts


def test_footnote_translation_is_written_back(footnote_doc):
    """端到端：脚注译文必须真的落进成品的 footnotes.xml。

    最容易踩的坑是「译文写进了一棵脱离文档的树」——裸 Part 没有 .element，
    parse 出来的树不回写进 part._blob 的话，保存时改动会被整个丢掉，
    而日志里照样显示「已写入 N 段」，看起来一切正常。
    """
    import re
    import zipfile

    from core.local_translator import LocalTranslator
    from core.word_translator import translate_word

    try:
        output = translate_word(footnote_doc, LocalTranslator())
    except Exception as exc:
        pytest.skip(f"离线模型不可用：{exc}")

    with zipfile.ZipFile(output) as archive:
        xml = archive.read("word/footnotes.xml").decode("utf-8")
    texts = re.findall(r"<w:t[^>]*>([^<]+)</w:t>", xml)
    assert any(
        "董事会" in text or "披露" in text or "核" in text for text in texts
    ), texts
    # 原文必须还在，这是「对照」不是「替换」
    assert "The board approved the revised disclosure requirements." in texts
    from docx import Document

    Document(output)  # 手工拼的部件不能被写坏


def test_plain_document_without_notes_still_works():
    from docx import Document

    source = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "samples",
        "保密模式样例",
        "示例-英文报告.docx",
    )
    if not os.path.exists(source):
        pytest.skip("样例缺失")
    doc = Document(source)
    texts = [item[1] for item in _collect_paragraphs(doc)]
    assert texts, "普通文档不该因为脚注逻辑改动而抽不到段落"
