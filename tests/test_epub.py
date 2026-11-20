"""EPUB 双语翻译（v1.6.1）。

夹具是一个手工拼的最小 EPUB3：mimetype + container.xml + OPF + 两个章节 +
nav 导航 + NCX + CSS + 一张图。全部内容都为了让下面这些容易踩的坑有断言：
mimetype 必须第一个且不压缩、目录导航不能被翻、表格单元格不能多出一列、
列表不能多出一个项目符号、ruby 注音不能送进引擎、图片 alt 改成双语。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from core.common import TranslationError
from core.epub_translator import (
    TRANSLATION_CLASS,
    iter_translations,
    translate_epub,
)

NS = "http://www.w3.org/1999/xhtml"

CONTAINER = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:test</dc:identifier>
    <dc:title>Sample Annual Report</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
    <item id="cover" href="images/cover.png" media-type="image/png"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>"""

NAV = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Contents</title></head>
<body>
  <nav epub:type="toc">
    <h1>Contents</h1>
    <ol>
      <li><a href="ch1.xhtml">Revenue Recognition</a></li>
      <li><a href="ch2.xhtml">Revenue Details</a></li>
    </ol>
  </nav>
</body>
</html>"""

NCX = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:test"/></head>
  <docTitle><text>Sample Annual Report</text></docTitle>
  <navMap>
    <navPoint id="np1" playOrder="1">
      <navLabel><text>Revenue Recognition</text></navLabel>
      <content src="ch1.xhtml"/>
    </navPoint>
  </navMap>
</ncx>"""

CH1 = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en">
<head><title>Chapter One</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>
  <h2 id="c1">Revenue Recognition</h2>
  <p>Revenue rose sharply in the first quarter.</p>
  <p>See <a epub:type="noteref" href="#n1">note 1</a> for details.</p>
  <p><ruby>漢<rt>かん</rt></ruby>字 appears in a Japanese citation.</p>
  <div><p>Nested paragraph text.</p></div>
  <pre><code>print("hello")</code></pre>
  <p><img src="images/cover.png" alt="Annual revenue chart" title="Revenue chart"/></p>
</body>
</html>"""

CH2 = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en">
<head><title>Chapter Two</title></head>
<body>
  <table>
    <tr><th>Item</th><th>Amount</th></tr>
    <tr><td>Licence fees</td><td>120</td></tr>
  </table>
  <ul>
    <li>First point about revenue.</li>
    <li>Second point about revenue.</li>
  </ul>
  <p>Revenue includes licence fees.</p>
</body>
</html>"""

MAPPING = {
    "Revenue Recognition": "收入确认",
    "Revenue rose sharply in the first quarter.": "第一季度收入大幅上升。",
    "See note 1 for details.": "详情见注释 1。",
    "漢字 appears in a Japanese citation.": "汉字出现在一处日文引文中。",
    "Nested paragraph text.": "嵌套段落文字。",
    "Licence fees": "牌照费",
    "First point about revenue.": "关于收入的第一点。",
    "Second point about revenue.": "关于收入的第二点。",
    "Revenue includes licence fees.": "收入包含牌照费。",
    "Annual revenue chart": "年度收入图",
    "Revenue chart": "收入图表",
}


class _FakeTranslator:
    """只译表里有的句子；表外的原样返回，从而「跳过」得可断言。"""

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.seen: list[str] = []
        self.stop_event = None

    def translate_texts(self, texts, progress=None):
        self.seen.extend(texts)
        if progress is not None:
            progress(len(texts), len(texts), "")
        return [self.mapping.get(text, text) for text in texts]

    def summary(self) -> str:
        return "假翻译器"


def _build_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        stored = zipfile.ZipInfo("mimetype")
        stored.compress_type = zipfile.ZIP_STORED
        archive.writestr(stored, b"application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", OPF)
        archive.writestr("OEBPS/nav.xhtml", NAV)
        archive.writestr("OEBPS/toc.ncx", NCX)
        archive.writestr("OEBPS/ch1.xhtml", CH1)
        archive.writestr("OEBPS/ch2.xhtml", CH2)
        archive.writestr("OEBPS/style.css", "p { margin: 0; }\n")
        archive.writestr("OEBPS/images/cover.png", b"\x89PNG\r\n\x1a\nnot-a-real-png")


@pytest.fixture
def epub_path(tmp_path: Path) -> Path:
    path = tmp_path / "sample.epub"
    _build_epub(path)
    return path


@pytest.fixture
def translated(epub_path: Path) -> tuple[Path, _FakeTranslator]:
    translator = _FakeTranslator(MAPPING)
    output = translate_epub(str(epub_path), translator)
    return Path(output), translator


def _read(output: Path, name: str) -> bytes:
    with zipfile.ZipFile(output) as archive:
        return archive.read(name)


def _text(output: Path, name: str) -> str:
    return _read(output, name).decode("utf-8")


def _translated_block(text: str, translation: str) -> bool:
    """译文块的 class 与文字必须同时出现（避免只匹配到别处的同名文字）。"""
    return f'class="{TRANSLATION_CLASS}"' in text and translation in text


def test_output_name_and_mimetype_layout(translated) -> None:
    output, _translator = translated
    assert output.name == "sample_双语.epub"

    with zipfile.ZipFile(output) as archive:
        infos = archive.infolist()
    assert infos[0].filename == "mimetype", "mimetype 必须是第一个条目"
    assert infos[0].compress_type == zipfile.ZIP_STORED, "mimetype 不能压缩"
    assert _read(output, "mimetype") == b"application/epub+zip"


def test_untouched_entries_are_byte_identical(epub_path, translated) -> None:
    output, _translator = translated
    for name in (
        "META-INF/container.xml",
        "OEBPS/content.opf",
        "OEBPS/toc.ncx",
        "OEBPS/style.css",
        "OEBPS/images/cover.png",
    ):
        assert _read(output, name) == _read(epub_path, name), f"{name} 不该被改动"


def test_paragraph_translation_is_inserted_right_after_source(translated) -> None:
    output, _translator = translated
    from lxml import etree

    root = etree.fromstring(_read(output, "OEBPS/ch1.xhtml"))
    paragraphs = root.findall(f".//{{{NS}}}p")
    sources = [para for para in paragraphs if (para.text or "").startswith("Revenue rose")]
    assert sources, "原文段必须还在（这是对照，不是替换）"
    following = sources[0].getnext()
    assert following is not None
    assert following.get("class") == TRANSLATION_CLASS
    assert following.text == "第一季度收入大幅上升。"


def test_table_cell_keeps_column_count(translated) -> None:
    output, _translator = translated
    from lxml import etree

    root = etree.fromstring(_read(output, "OEBPS/ch2.xhtml"))
    assert len(root.findall(f".//{{{NS}}}td")) == 2, "单元格数必须不变，否则表格会多出一列"
    assert len(root.findall(f".//{{{NS}}}th")) == 2
    cell = next(
        td for td in root.findall(f".//{{{NS}}}td") if (td.text or "") == "Licence fees"
    )
    span = cell.find(f"{{{NS}}}span")
    assert span is not None and span.get("class") == TRANSLATION_CLASS
    assert span.text == "牌照费"


def test_list_keeps_item_count(translated) -> None:
    output, _translator = translated
    from lxml import etree

    root = etree.fromstring(_read(output, "OEBPS/ch2.xhtml"))
    items = root.findall(f".//{{{NS}}}li")
    assert len(items) == 2, "另插 <li> 会多出项目符号，译文必须接在原项内部"
    assert _translated_block(_text(output, "OEBPS/ch2.xhtml"), "关于收入的第一点。")


def test_nav_document_and_ncx_are_not_translated(translated) -> None:
    output, _translator = translated
    nav = _text(output, "OEBPS/nav.xhtml")
    assert "Revenue Recognition" in nav
    assert "收入确认" not in nav
    assert "Revenue Recognition" in _text(output, "OEBPS/toc.ncx")


def test_ruby_annotation_is_not_sent(translated) -> None:
    _output, translator = translated
    joined = "\n".join(translator.seen)
    assert "漢字 appears" in joined
    assert "かん" not in joined, "ruby 注音不该进引擎，否则译文里会混进假名"


def test_preformatted_code_is_left_alone(translated) -> None:
    output, translator = translated
    assert 'print("hello")' in _text(output, "OEBPS/ch1.xhtml")
    assert not any("print" in text for text in translator.seen)


def test_nested_div_paragraph_translated_exactly_once(translated) -> None:
    output, _translator = translated
    text = _text(output, "OEBPS/ch1.xhtml")
    assert text.count("嵌套段落文字。") == 1, "外层 div 与内层 p 不能各翻一遍"
    assert "Nested paragraph text." in text


def test_image_alt_becomes_bilingual(translated) -> None:
    output, _translator = translated
    from lxml import etree

    root = etree.fromstring(_read(output, "OEBPS/ch1.xhtml"))
    image = root.find(f".//{{{NS}}}img")
    assert image is not None
    assert image.get("alt") == "Annual revenue chart（年度收入图）"
    assert image.get("title") == "Revenue chart（收入图表）"


def test_report_and_edit_list_are_written(translated) -> None:
    output, _translator = translated
    assert output.with_name(output.stem + " 自检报告.txt").exists()
    edit_list = output.with_name(output.stem + " 译后编辑清单.json")
    assert edit_list.exists()
    payload = json.loads(edit_list.read_text(encoding="utf-8"))
    assert payload["items"]
    first = payload["items"][0]
    assert first["kind"] == "epub"
    assert first["locator"].get("file", "").endswith(".xhtml")


def test_edit_list_writeback_changes_only_translation_text(translated) -> None:
    from core.postedit import apply_edits, load_edit_list

    output, _translator = translated
    before = _text(output, "OEBPS/ch2.xhtml")
    styles = _read(output, "OEBPS/style.css")

    edit_list = output.with_name(output.stem + " 译后编辑清单.json")
    items = load_edit_list(str(edit_list))
    target = next(item for item in items if item.translation == "收入包含牌照费。")
    target.translation = "收入中已包含牌照费用。"

    result = apply_edits(str(output), [target])

    assert target.id in result.applied
    after = _text(output, "OEBPS/ch2.xhtml")
    assert "收入中已包含牌照费用。" in after
    assert "收入包含牌照费。" not in after
    assert "Revenue includes licence fees." in after, "原文必须还在"
    assert before != after
    assert _read(output, "OEBPS/style.css") == styles

    with zipfile.ZipFile(output) as archive:
        assert archive.infolist()[0].filename == "mimetype"


def test_second_pass_does_not_translate_own_output(epub_path, translated) -> None:
    output, _translator = translated
    again = _FakeTranslator(MAPPING)
    translate_epub(str(output), again)
    # 译文块自带 xlate-zh，二次翻译时不该被当原文再送一遍
    assert "第一季度收入大幅上升。" not in again.seen


def test_missing_container_raises_translation_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.epub"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("OEBPS/ch1.xhtml", CH1)
    with pytest.raises(TranslationError):
        translate_epub(str(path), _FakeTranslator(MAPPING))


def test_not_a_zip_raises_translation_error(tmp_path: Path) -> None:
    path = tmp_path / "fake.epub"
    path.write_text("这不是 zip", encoding="utf-8")
    with pytest.raises(TranslationError):
        translate_epub(str(path), _FakeTranslator(MAPPING))


def test_iter_translations_lists_every_block(translated) -> None:
    output, _translator = translated
    rows = iter_translations(str(output))
    texts = [text for _name, _index, text in rows]
    assert "收入包含牌照费。" in texts
    assert any(name == "OEBPS/ch1.xhtml" for name, _index, _text_ in rows)
    # 序号在文件内唯一且从 0 起
    indexes = [index for name, index, _text_ in rows if name == "OEBPS/ch2.xhtml"]
    assert indexes == sorted(indexes)
    assert indexes[0] == 0


# --------------------------------------------------------------------------
# 章节范围（v1.7.0）：--pages= 从「只对 PDF 生效」扩展到 EPUB 按 spine 挑章
# --------------------------------------------------------------------------


def _source_bytes(epub_path: Path, name: str) -> bytes:
    with zipfile.ZipFile(epub_path) as archive:
        return archive.read(name)


def test_chapter_range_sends_only_selected_chapters(epub_path: Path) -> None:
    translator = _FakeTranslator(MAPPING)
    output = Path(translate_epub(str(epub_path), translator, pages="1"))

    assert "Revenue Recognition" in translator.seen, translator.seen
    assert not any("Licence fees" in text for text in translator.seen), (
        "未选中的第二章不该送进引擎——离线档省的是实打实的耗时"
    )
    assert "收入确认" in _text(output, "OEBPS/ch1.xhtml")
    assert _read(output, "OEBPS/ch2.xhtml") == _source_bytes(
        epub_path, "OEBPS/ch2.xhtml"
    ), "EPUB 不重建包：没选中的章逐字节原样，成品章数等于源章数"


def test_chapter_range_counts_from_one_not_from_zero(epub_path: Path) -> None:
    """写「2」翻的是 spine 第二章。v1.6.8 栽过的「页号 vs 列表位置」错位，用第二章单独钉住。"""
    translator = _FakeTranslator(MAPPING)
    output = Path(translate_epub(str(epub_path), translator, pages="2"))

    assert any("Licence fees" in text for text in translator.seen), translator.seen
    assert "Revenue Recognition" not in translator.seen, translator.seen
    assert _read(output, "OEBPS/ch1.xhtml") == _source_bytes(
        epub_path, "OEBPS/ch1.xhtml"
    )
    assert "收入包含牌照费。" in _text(output, "OEBPS/ch2.xhtml")


def test_chapter_range_says_it_does_not_rebuild_the_package(epub_path: Path) -> None:
    """「成品还是 2 章」必须出声：不说的话，用户看到全本会以为范围没生效。"""
    logs: list[str] = []
    translate_epub(str(epub_path), _FakeTranslator(MAPPING), log=logs.append, pages="1")
    text = "\n".join(logs)
    assert "章节范围：第 1 章" in text, logs
    assert "正文共 2 章，选中 1 章" in text, logs
    assert "不重建包" in text and "成品还是 2 章" in text, logs


def test_chapter_range_blank_means_whole_book(epub_path: Path) -> None:
    """全空白 = 没填 = 整本都翻（口径与 translate_pdf 一致），且日志不该念「章节范围」。"""
    translator = _FakeTranslator(MAPPING)
    logs: list[str] = []
    output = Path(
        translate_epub(str(epub_path), translator, log=logs.append, pages="  ")
    )
    assert any("Licence fees" in text for text in translator.seen), translator.seen
    assert not any("章节范围" in line for line in logs), logs
    assert "收入包含牌照费。" in _text(output, "OEBPS/ch2.xhtml")


def test_chapter_range_out_of_range_names_epub_counter(epub_path: Path) -> None:
    with pytest.raises(TranslationError) as excinfo:
        translate_epub(str(epub_path), _FakeTranslator(MAPPING), pages="3")
    message = str(excinfo.value)
    assert "章节范围「3」超出文档章数" in message, message
    assert "这份 EPUB 只有 2 章，你写到第 3 章" in message, message
    assert "页" not in message, f"EPUB 的报错里出现了「页」：{message}"
