"""v1.4.2 位置信息的链路级回归：PDF 页码与 Word 脚注段号。

QA 独立补的测试缺口：``tests/test_term_consistency.py`` 钉住了
locations/places 的函数级口径与 PPT 链路，但两条真实链路的位置**正确性**
在 repo 里没有钉子——pdf 页码差 1（用 0 基 index 而非 index+1）、word
把脚注段落排除在段序之外，都不会有任何现有测试变红。这里用真实
``translate_pdf`` / ``translate_word`` 跑出来断，夹具全部自造。

打法与项目内既有测试一致：PyMuPDF/python-docx 现场构造源文件 + 假翻译器
（映射式，不联网、不加载模型），断言落在用户能感知的自检报告文字上。
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pymupdf
import pytest
from docx import Document

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.pdf_translator import translate_pdf  # noqa: E402
from core.translator import is_translatable  # noqa: E402
from core.word_translator import translate_word  # noqa: E402


class _MappingTranslator:
    """按「原文 → 译文」映射返回；没命中的给填充译文（与原文不同才会插中文）。"""

    stop_event = None

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    def translate_texts(self, texts, progress=None):
        return [self.mapping.get(text, "这是填充译文。") for text in texts]

    def summary(self) -> str:
        return "假翻译器"


def _read_report(output_path: str) -> str:
    report = Path(output_path).with_name(Path(output_path).stem + " 自检报告.txt")
    assert report.exists(), f"成品旁应生成自检报告：{report}"
    return report.read_text(encoding="utf-8")


def _issue_line(report_text: str) -> str:
    line = next(
        (line for line in report_text.splitlines() if "译名不一致" in line), ""
    )
    assert line, f"报告里应出现「译名不一致」疑点：{report_text}"
    return line


# 三段译文的中文二字词两两交集为空：收入={收入}、营收={营收}、收益={收益}
_PDF_MAPPING = {
    "Revenue rose sharply.": "收入大幅上升。",
    "Revenue kept rising steadily.": "营收持续增长。",
    "Revenue includes licences.": "收益包含许可内容。",
    "Welcome to the annual report.": "欢迎阅读年度报告。",
}


class TestPdfPlacesCarryRealPageNumbers:
    """PDF 疑点的 places 必须是真实页码（1 起），不是 0 基下标。"""

    def test_page_numbers_match_physical_pages(self, tmp_path: Path) -> None:
        # Arrange：4 页 PDF，Revenue 只在第 2/3/4 页各出现一次，第 1 页没有。
        # 若实现用 0 基 index，note 会写成「第 1 页、第 2 页、第 3 页」——一断言就红。
        doc = pymupdf.open()
        for line in (
            "Welcome to the annual report.",
            "Revenue rose sharply.",
            "Revenue kept rising steadily.",
            "Revenue includes licences.",
        ):
            page = doc.new_page(width=595, height=842)  # A4
            page.insert_text(
                pymupdf.Point(72, 72), line, fontsize=11, fontname="helv"
            )
        src = tmp_path / "sample.pdf"
        doc.save(str(src))
        doc.close()

        # Act
        out = translate_pdf(
            str(src), _MappingTranslator(_PDF_MAPPING), layout="zh", ocr_enabled=False
        )
        line = _issue_line(_read_report(out))

        # Assert：位置清单逐页对得上，且没有差 1 的痕迹
        assert "出现 3 次（第 2 页、第 3 页、第 4 页）" in line
        note = line[line.find("出现") :]
        assert "第 1 页" not in note, f"术语不在第 1 页，note 却提到了它（差 1？）：{note}"


# Word 侧：Revenue 出现在正文第 1/3/4 段 + 1 个脚注段（按收集顺序排第 6 位）。
_WORD_SENTENCES = {
    1: "Revenue grew in the first quarter of the year.",
    3: "Revenue fell across every region we track.",
    4: "Revenue includes both services and licences.",
}
_WORD_MAPPING = {
    _WORD_SENTENCES[1]: "第一季度收入增长。",
    _WORD_SENTENCES[3]: "我们追踪的每个区域的营收都下降了。",
    _WORD_SENTENCES[4]: "收益包含服务与许可两部分。",
    "Revenue disclosure rules were revised by the board.": "董事们修订了利润盈余披露规则。",
    "Nothing to see here, just filler for spacing.": "这里没有什么内容，只是填充而已。",
    "Another filler paragraph without any term.": "另一段不含任何术语的填充文本。",
}
_FOOTNOTE = "Revenue disclosure rules were revised by the board."

# python-docx 1.2.0 没有脚注 API，按 content type 手工往包里塞 footnotes.xml
# （与 tests/test_word_notes.py 同法，但部件内容自造）。
_FOOTNOTES_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>
  <w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>
  <w:footnote w:id="1"><w:p><w:r><w:t>{_FOOTNOTE}</w:t></w:r></w:p></w:footnote>
</w:footnotes>"""
_FOOTNOTE_CT = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
)


def _inject_footnote(source: Path, target: Path) -> None:
    with zipfile.ZipFile(str(source)) as zin, zipfile.ZipFile(
        str(target), "w", zipfile.ZIP_DEFLATED
    ) as zout:
        for name in zin.namelist():
            payload = zin.read(name)
            if name == "[Content_Types].xml":
                payload = payload.decode("utf-8").replace(
                    "</Types>",
                    f'<Override PartName="/word/footnotes.xml" '
                    f'ContentType="{_FOOTNOTE_CT}"/></Types>',
                ).encode("utf-8")
            elif name == "word/_rels/document.xml.rels":
                payload = payload.decode("utf-8").replace(
                    "</Relationships>",
                    '<Relationship Id="rIdFnQa99" '
                    'Type="http://schemas.openxmlformats.org/officeDocument'
                    '/2006/relationships/footnotes" Target="footnotes.xml"/>'
                    "</Relationships>",
                ).encode("utf-8")
            elif name == "word/document.xml":
                payload = payload.decode("utf-8").replace(
                    "</w:p>", '<w:r><w:footnoteReference w:id="1"/></w:r></w:p>', 1
                ).encode("utf-8")
            zout.writestr(name, payload)
        zout.writestr("word/footnotes.xml", _FOOTNOTES_XML)


class TestWordPlacesCountFootnoteParagraphs:
    """Word 疑点的 places 段号必须覆盖脚注段落（差 1 或整段漏计都会红）。"""

    def test_footnote_gets_its_own_sequence_number(self, tmp_path: Path) -> None:
        # Arrange：5 个正文段（Revenue 在第 1/3/4 段）+ 1 个含 Revenue 的脚注段。
        # _collect_paragraphs 的收集顺序是 正文 → 脚注/尾注 → 页眉页脚，
        # 脚注段因此应排第 6 位；本 docx 无页眉页脚。
        doc = Document()
        doc.add_paragraph(_WORD_SENTENCES[1])
        doc.add_paragraph("Nothing to see here, just filler for spacing.")
        doc.add_paragraph(_WORD_SENTENCES[3])
        doc.add_paragraph(_WORD_SENTENCES[4])
        doc.add_paragraph("Another filler paragraph without any term.")
        plain = tmp_path / "plain.docx"
        doc.save(str(plain))
        src = tmp_path / "with_note.docx"
        _inject_footnote(plain, src)

        # 独立枚举「真实段号」：正文可译段（文档序）+ 脚注段
        reopened = Document(str(src))
        expected_order = [
            paragraph.text
            for paragraph in reopened.paragraphs
            if is_translatable(paragraph.text)
        ] + [_FOOTNOTE]
        expected_places = [
            f"段落 {index}"
            for index, text in enumerate(expected_order, 1)
            if "Revenue" in text
        ]
        assert expected_places == ["段落 1", "段落 3", "段落 4", "段落 6"]

        # Act
        out = translate_word(str(src), _MappingTranslator(_WORD_MAPPING))
        line = _issue_line(_read_report(out))

        # Assert：段号逐一命中，且不存在 0 基/漏计脚注的错位形态
        assert "出现 4 次（段落 1、段落 3、段落 4、段落 6）" in line
        note = line[line.find("出现") :]
        for wrong in ("段落 0", "段落 2", "段落 5"):
            assert wrong not in note, f"出现错位段号 {wrong}（差 1 症状）：{note}"
