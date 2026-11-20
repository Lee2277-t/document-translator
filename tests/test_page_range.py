"""文档范围（v1.6.8 的 PDF 页范围 / v1.7.0 扩展到 PPT 按张、EPUB 按章）。

对标里唯一还缺的高频功能：PDFMathTranslate 的 `-p/--pages`、Doc2Doc 的页码区间。
这里测三层，缺一层都会漏掉真实的坏法：

* 解析器本身——写错的人正赶着交差，报错必须点名是哪一段看不懂、正确写法长什么样；
  分隔符/区间符的全角变体必须认（用户不会为一个输入框特意切英文输入法）。
* **裁页之后各处下标还对不对**——这是 v1.6.8 真正的风险。`page_infos` 过去恒等于
  「全部页面」，页号与列表下标恰好相等，代码里就有人拿页号去索引这个列表
  （实测：第一版接通后 `refs` 立刻 IndexError）。页范围一进来，这个巧合就不成立了，
  所以成品页数、书签目标页、原位重排的未选中页都要按成品文件本身断言，
  不能只看日志。
* **量词与分发**（v1.7.0）——同一个数字落在 PPT 上是「张」、落在 EPUB 上是「章」，
  报错继续念「页」就是假话；而 `app.py` 少传一个 `pages=` 形参不报错、只安静地翻全本，
  所以界面上那一栏到三种 translate_* 的接线也各有一条断言。

跑法： ./.venv\\Scripts\\python.exe -X utf8 -m pytest tests -q
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

from core.common import TranslationError  # noqa: E402
from core import page_range  # noqa: E402
from core.pdf_translator import RIGHT_GAP, translate_pdf  # noqa: E402

CHAPTER = "Quarterly revenue rose 12 percent to 4,200 million dollars."
#: 每页一个只属于该页的记号，用来在成品里认出「这一页来自源文件第几页」。
#: 单独一行写在正文上方：整句太长会越出页面右边界，而 get_text 按页面裁切，
#: 挤在一行里断言会被截断的尾词骗过去。
MARKS = ["alpha", "bravo", "charlie", "delta", "echo"]
W, H = 595.0, 842.0


class _FakeTranslator:
    """回显原文的假引擎：译文里带着源句，成品页上因此能查到该页的记号。"""

    stop_event = None

    def __init__(self) -> None:
        self.count = 0

    def translate_texts(self, texts, progress=None):
        self.count += len(texts)
        return [f"译文：{text}" for text in texts]

    def summary(self) -> str:
        return f"翻译 {self.count} 段"


def _make_source(
    tmp_path: Path, pages: int = 5, toc: list[list] | None = None, blank_last: bool = False
) -> Path:
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page(width=W, height=H)
        if blank_last and index == pages - 1:
            continue
        page.insert_text(pymupdf.Point(72, 96), MARKS[index % len(MARKS)], fontsize=11)
        page.insert_text(pymupdf.Point(72, 120), CHAPTER, fontsize=11)
    if toc is not None:
        doc.set_toc(toc)
    path = tmp_path / "ranged.pdf"
    doc.save(path, garbage=3, deflate=True)
    doc.close()
    return path


def _texts(path: Path) -> list[str]:
    doc = pymupdf.open(path)
    try:
        return [doc[index].get_text() for index in range(doc.page_count)]
    finally:
        doc.close()


# --------------------------------------------------------------------------
# 解析器：写法与报错
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "spec,expected",
    [
        ("1", "第 1 页"),
        ("1-3", "第 1-3 页"),
        ("1-3,5", "第 1-3 页、第 5 页"),
        # 全角区间符与全角分隔符：中文输入法下打出来的就是这一串
        ("1－3，5", "第 1-3 页、第 5 页"),
        ("1~3、5", "第 1-3 页、第 5 页"),
        (" 2 - 4 ", "第 2-4 页"),
        ("2 4", "第 2 页、第 4 页"),
        ("-3", "第 1-3 页"),
        ("8-", "第 8 页到最后一页"),
    ],
)
def test_describe_accepts_common_spellings(spec: str, expected: str) -> None:
    assert page_range.describe(spec) == expected


def test_describe_whole_document_is_implicit_when_empty() -> None:
    # 留空 = 整份文档，这条口径由调用方（界面 / translate_pdf）负责，
    # 解析器只处理非空写法，这里锁住「空写法不会被当成第 0 页」。
    with pytest.raises(TranslationError):
        page_range.describe("")
    with pytest.raises(TranslationError):
        page_range.describe("   ")


@pytest.mark.parametrize(
    "spec,fragment",
    [
        ("5-3", "起始页 5 大于结束页 3"),
        ("abc", "看不懂页范围里的「abc」"),
        ("0", "页码从 1 开始"),
        ("-", "看不懂页范围里的「-」"),
        ("1-3;x", "看不懂页范围里的「x」"),
        # isdigit() 认上标数字，int() 不认：这条守卫防止「²」变成崩溃
        ("²", "看不懂页范围里的「²」"),
    ],
)
def test_describe_rejects_bad_spellings(spec: str, fragment: str) -> None:
    with pytest.raises(TranslationError) as excinfo:
        page_range.describe(spec)
    assert fragment in str(excinfo.value)


def test_unreadable_token_also_shows_the_correct_spelling() -> None:
    # 「看不懂」这类报错的用户正赶着交差，光说哪一段看不懂不够，
    # 必须同一次就把正确写法摆在他眼前。反序那条自带改法（分开写 3,5），不在此列。
    with pytest.raises(TranslationError) as excinfo:
        page_range.describe("1-3,x")
    assert "正确写法如 1-3,5" in str(excinfo.value)


def test_parse_pages_sorts_and_deduplicates() -> None:
    assert page_range.parse_pages("3,1-2,2", 5) == [0, 1, 2]
    assert page_range.parse_pages("-2", 5) == [0, 1]
    assert page_range.parse_pages("4-", 6) == [3, 4, 5]


def test_parse_pages_rejects_out_of_range_without_clamping() -> None:
    # 静默夹取会让用户以为真翻到了第 9 页，必须报错
    with pytest.raises(TranslationError) as excinfo:
        page_range.parse_pages("9", 5)
    assert "这份 PDF 只有 5 页" in str(excinfo.value)
    with pytest.raises(TranslationError) as excinfo:
        page_range.parse_pages("2-9", 5)
    assert "页范围「2-9」超出文档页数" in str(excinfo.value)


def test_format_pages_compresses_runs() -> None:
    assert page_range.format_pages([0, 1, 2, 4, 6, 7]) == "1-3、5、7-8"
    assert page_range.format_pages([]) == "无"


# --------------------------------------------------------------------------
# 量词：同一个数字落在 PPT/EPUB 上不能说成「页」（v1.7.0）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unit,expected",
    [
        (page_range.SLIDES, "第 2 张、第 4 张"),
        (page_range.CHAPTERS, "第 2 章、第 4 章"),
        (page_range.NEUTRAL, "第 2 页、第 4 页"),
        (page_range.PAGES, "第 2 页、第 4 页"),
    ],
)
def test_describe_uses_the_container_counter(unit, expected: str) -> None:
    assert page_range.describe("2 4", unit) == expected


def test_open_ended_range_reads_as_the_container_counter() -> None:
    assert page_range.describe("3-", page_range.PAGES) == "第 3 页到最后一页"
    assert page_range.describe("3-", page_range.SLIDES) == "第 3 张到最后一张"
    assert page_range.describe("3-", page_range.CHAPTERS) == "第 3 章到最后一章"


def test_reversed_range_offers_the_fix_in_the_container_counter() -> None:
    with pytest.raises(TranslationError) as excinfo:
        page_range.describe("5-3", page_range.SLIDES)
    message = str(excinfo.value)
    assert "幻灯片范围「5-3」的起始张 5 大于结束张 3" in message, message
    assert "想同时要这两张请分开写：3,5" in message, message
    assert "页" not in message, f"PPT 的报错里出现了「页」：{message}"


def test_zero_is_explained_with_the_container_sequence_word() -> None:
    with pytest.raises(TranslationError) as excinfo:
        page_range.describe("0", page_range.CHAPTERS)
    assert "序号从 1 开始，没有第 0 章" in str(excinfo.value)


def test_out_of_range_names_the_container_and_its_counter() -> None:
    with pytest.raises(TranslationError) as excinfo:
        page_range.parse_pages("7", 6, page_range.SLIDES)
    assert (
        str(excinfo.value)
        == "幻灯片范围「7」超出文档张数：这份 PPT 只有 6 张，你写到第 7 张。"
    )
    with pytest.raises(TranslationError) as excinfo:
        page_range.parse_pages("7", 6, page_range.CHAPTERS)
    assert "这份 EPUB 只有 6 章，你写到第 7 章" in str(excinfo.value)


def test_empty_container_message_names_the_container_kind() -> None:
    with pytest.raises(TranslationError) as excinfo:
        page_range.parse_pages("1", 0, page_range.SLIDES)
    assert str(excinfo.value) == "这份 PPT 没有幻灯片，谈不上幻灯片范围。"


def test_pdf_wording_survives_the_generalisation() -> None:
    """PDF 这两句是 v1.6.8 封版验收逐字断言过的，泛化成 Unit 不许改动一个字。"""
    with pytest.raises(TranslationError) as excinfo:
        page_range.parse_pages("9", 5)
    assert str(excinfo.value) == "页范围「9」超出文档页数：这份 PDF 只有 5 页，你写到第 9 页。"
    with pytest.raises(TranslationError) as excinfo:
        page_range.parse_pages("abc", 5)
    assert str(excinfo.value) == (
        "看不懂页范围里的「abc」。正确写法如 1-3,5；第 8 页到最后一页写 8-；"
        "整份文档请把这一栏留空。"
    )


def test_scope_note_and_suffixes_are_the_single_source_for_the_batch_echo() -> None:
    """界面与 CLI 广播「这一栏管哪几种格式」只准从这里取，免得两处各写一套口径。"""
    assert page_range.SCOPE_NOTE == "PDF 按页、PPT 按张、EPUB 按章，各按自己那份文件的总数校验"
    assert set(page_range.RANGE_SUFFIXES) == {".pdf", ".pptx", ".epub"}
    assert ".docx" not in page_range.RANGE_SUFFIXES, "Word 没有自然页界，不该被算进认范围的格式"


# --------------------------------------------------------------------------
# 端到端：成品里的页与书签
# --------------------------------------------------------------------------

def test_zh_layout_outputs_only_selected_pages(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    out = Path(
        translate_pdf(
            src,
            _FakeTranslator(),
            layout="zh",
            ocr_enabled=False,
            pages="2-3",
        )
    )
    texts = _texts(out)
    assert len(texts) == 2, "重建版式的成品应当只有选中的两页"
    assert "bravo" in texts[0] and "charlie" in texts[1]
    assert "alpha" not in "".join(texts)


def test_side_layout_outputs_only_selected_pages(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    out = Path(
        translate_pdf(
            src,
            _FakeTranslator(),
            layout="side",
            ocr_enabled=False,
            pages="1,4-",
        )
    )
    texts = _texts(out)
    assert len(texts) == 3, "第 1 页 + 第 4 到最后一页 = 三页"
    assert [mark in "".join(texts) for mark in MARKS] == [
        True,
        False,
        False,
        True,
        True,
    ]
    # 对照版仍是「左原文右译文」的加宽页，裁页不该改变版式
    doc = pymupdf.open(out)
    try:
        assert doc[0].rect.width == pytest.approx(W * 2 + RIGHT_GAP)
    finally:
        doc.close()


def test_inplace_keeps_unselected_pages_untouched(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    out = Path(
        translate_pdf(
            src,
            _FakeTranslator(),
            layout="inplace",
            ocr_enabled=False,
            pages="2-3",
        )
    )
    texts = _texts(out)
    assert len(texts) == 5, "原位重排不重建页面：成品仍是全部五页"
    assert "译文" not in texts[0] and "alpha" in texts[0]
    assert "译文" in texts[1] and "译文" in texts[2]
    assert "echo" in texts[4] and "译文" not in texts[4]


def test_toc_is_remapped_and_out_of_range_entries_dropped(tmp_path: Path) -> None:
    src = _make_source(
        tmp_path,
        toc=[[1, "Chapter 1", 1], [1, "Chapter 2", 2], [2, "Chapter 3", 3], [1, "Chapter 5", 5]],
    )
    logs: list[str] = []
    out = Path(
        translate_pdf(
            src,
            _FakeTranslator(),
            log=logs.append,
            layout="zh",
            ocr_enabled=False,
            pages="2-3",
        )
    )
    doc = pymupdf.open(out)
    try:
        assert doc.get_toc() == [[1, "Chapter 2", 1], [2, "Chapter 3", 2]]
    finally:
        doc.close()
    assert any("书签" in line and "舍弃" in line for line in logs), logs


def test_toc_is_copied_verbatim_without_page_range(tmp_path: Path) -> None:
    toc = [[1, "Chapter 1", 1], [1, "Chapter 3", 3]]
    src = _make_source(tmp_path, toc=toc)
    out = Path(
        translate_pdf(src, _FakeTranslator(), layout="zh", ocr_enabled=False, pages=None)
    )
    doc = pymupdf.open(out)
    try:
        assert doc.get_toc() == toc
    finally:
        doc.close()


def test_out_of_range_pages_fails_before_any_work(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    with pytest.raises(TranslationError) as excinfo:
        translate_pdf(
            src, _FakeTranslator(), layout="zh", ocr_enabled=False, pages="6"
        )
    assert "这份 PDF 只有 5 页" in str(excinfo.value)


def test_blank_selected_pages_say_which_range_was_used(tmp_path: Path) -> None:
    src = _make_source(tmp_path, pages=3, blank_last=True)
    with pytest.raises(TranslationError) as excinfo:
        translate_pdf(
            src, _FakeTranslator(), layout="zh", ocr_enabled=False, pages="3"
        )
    message = str(excinfo.value)
    assert "没有找到需要翻译的外文内容" in message
    assert "页范围「3」只选中了 1 页" in message, message


def test_empty_pages_spec_means_whole_document(tmp_path: Path) -> None:
    src = _make_source(tmp_path)
    logs: list[str] = []
    out = Path(
        translate_pdf(
            src, _FakeTranslator(), log=logs.append, layout="zh", ocr_enabled=False, pages="  "
        )
    )
    assert len(_texts(out)) == 5
    assert not any("页范围" in line for line in logs), logs


# --------------------------------------------------------------------------
# 命令行：--pages= 的识别与校验
# --------------------------------------------------------------------------

def _run(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PYTHON), "-X", "utf8", "app.py", *arguments],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


def test_cli_pages_accepts_valid_range_and_rejects_bad_one() -> None:
    ok = _run("--pages=1-3,5", "--version")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "文档翻译工具 v" in ok.stdout

    # 认不出的写法必须硬失败退 4，而不是静默按整份文档跑——
    # 用户等着拿「只有前 3 页」的成品，拿到全本比报错难查得多。
    bad = _run("--pages=5-3", "--version")
    assert bad.returncode == 4, bad.stdout + bad.stderr
    assert "起始页 5 大于结束页 3" in bad.stdout
    assert "--pages=" in _run("--page=1-3", "--version").stdout


_GUI_SCRIPT = """
import sys, threading
sys.argv = ["app"]
import pymupdf
import app

SRC = r"@SRC@"
CHAPTER = "Quarterly revenue rose 12 percent to 4,200 million dollars."

class FakeTranslator:
    stop_event = None
    def translate_texts(self, texts, progress=None):
        return [f"译文：{t}" for t in texts]
    def summary(self):
        return "fake"

doc = pymupdf.open()
for i in range(5):
    page = doc.new_page(width=595, height=842)
    page.insert_text(pymupdf.Point(72, 96), ["alpha","bravo","charlie","delta","echo"][i], fontsize=11)
    page.insert_text(pymupdf.Point(72, 120), CHAPTER, fontsize=11)
doc.save(SRC)
doc.close()

application = app.TranslatorApp()
try:
    assert application.pages_var.get() == "", application.pages_var.get()
    assert application.pdf_pages == ""

    application.pages_var.set("2-3")
    application._on_pages_change()
    assert application.pdf_pages == "2-3", application.pdf_pages
    log = application.log_text.get("1.0", "end")
    assert "文档范围：2-3" in log, log
    assert "PDF 按页、PPT 按张、EPUB 按章" in log, log

    application.pages_var.set("5-3")
    application._on_pages_change()
    log = application.log_text.get("1.0", "end")
    assert "文档范围写错了" in log, log
    assert application.pdf_pages == "5-3", application.pdf_pages

    application.pdf_pages = "2-3"
    out = application._translate_one(SRC, FakeTranslator(), False, threading.Event())
    written = pymupdf.open(out)
    try:
        assert written.page_count == 2, written.page_count
    finally:
        written.close()
    print("GUI_PAGES_OK")
finally:
    application.root.destroy()
"""


def test_gui_passes_the_page_range_into_pdf_translation(tmp_path: Path) -> None:
    """界面上的输入框 → self.pdf_pages → translate_pdf 这条接线必须真通。

    工作线程读不到 tk 变量（跨线程取值是崩溃级问题），所以选项在界面线程
    快照成普通属性再交给后台——属性名写错、忘了绑 FocusOut 都会让这一环
    静默失效：用户填了页范围，成品仍是全本，一点报错都没有。
    """
    script = _GUI_SCRIPT.replace("@SRC@", str(tmp_path / "gui.pdf"))
    child = subprocess.run(
        [str(PYTHON), "-X", "utf8", "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
    )
    assert child.returncode == 0, child.stdout + child.stderr[-3000:]
    assert "GUI_PAGES_OK" in child.stdout, child.stdout


#: PPT/EPUB 的分发点是各自一条 `pages=self.pdf_pages or None`。translators 的单元
#: 测试证明「拿到 pages 会照办」，这里证明「界面上那一栏真的传到了它们手里」——
#: 少传一个形参不会报错，只会安静地翻全本，是最难发现的一类接线断裂。
_GUI_CONTAINER_SCRIPT = """
import sys, threading, zipfile
sys.argv = ["app"]
import app
from pptx import Presentation

DECK = r"@DECK@"
BOOK = r"@BOOK@"

class FakeTranslator:
    stop_event = None
    def translate_texts(self, texts, progress=None):
        return [f"译文：{t}" for t in texts]
    def summary(self):
        return "fake"

application = app.TranslatorApp()
try:
    application.pdf_pages = "2"
    ppt_out = application._translate_one(DECK, FakeTranslator(), False, threading.Event())
    written = Presentation(ppt_out)
    per_slide = [
        "\\n".join(
            para.text
            for shape in slide.shapes
            if shape.has_text_frame
            for para in shape.text_frame.paragraphs
        )
        for slide in written.slides
    ]
    assert len(per_slide) == 3, per_slide
    assert "译文：" in per_slide[1], per_slide
    assert "译文：" not in per_slide[0] and "译文：" not in per_slide[2], per_slide

    application.pdf_pages = "2"
    epub_out = application._translate_one(BOOK, FakeTranslator(), False, threading.Event())
    with zipfile.ZipFile(epub_out) as archive:
        ch1 = archive.read("OEBPS/ch1.xhtml").decode("utf-8")
        ch2 = archive.read("OEBPS/ch2.xhtml").decode("utf-8")
    assert "译文：" not in ch1, ch1
    assert "译文：" in ch2, ch2
    print("GUI_CONTAINER_RANGE_OK")
finally:
    application.root.destroy()
"""


def _make_deck(path: Path, lines: list[str]) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    for line in lines:
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
        box.text_frame.text = line
    prs.save(str(path))


def _make_book(path: Path, chapters: list[str]) -> None:
    """两章的最小 EPUB3。夹具建在父进程里：内嵌脚本再套一层 XML 字面量只会让转义出错。"""
    head = '<?xml version="1.0" encoding="utf-8"?>\n'
    page = '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>{}</p></body></html>'
    opf = (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="b">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="b">urn:uuid:test</dc:identifier><dc:language>en</dc:language>'
        "</metadata><manifest>"
        '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>'
        "</manifest><spine><itemref idref=\"c1\"/><itemref idref=\"c2\"/></spine></package>"
    )
    container = (
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, b"application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        for index, chapter in enumerate(chapters, 1):
            archive.writestr(f"OEBPS/ch{index}.xhtml", head + page.format(chapter))


def test_gui_passes_the_range_into_ppt_and_epub(tmp_path: Path) -> None:
    deck = tmp_path / "gui.pptx"
    book = tmp_path / "gui.epub"
    _make_deck(deck, ["Slide one", "Slide two", "Slide three"])
    _make_book(book, ["Chapter one body.", "Chapter two body."])

    script = _GUI_CONTAINER_SCRIPT.replace("@DECK@", str(deck)).replace("@BOOK@", str(book))
    child = subprocess.run(
        [str(PYTHON), "-X", "utf8", "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
    )
    assert child.returncode == 0, child.stdout + child.stderr[-3000:]
    assert "GUI_CONTAINER_RANGE_OK" in child.stdout, child.stdout

