"""重建版式 PDF 的批注保留（Round 27，F1 残留半截）。

起因：`show_pdf_page` 只搬页面外观，批注不属于外观。左右对照与仅中文两种版式
做出来的成品里，审阅者画的高亮、便签、方框一条都没有——合同与审阅稿最值钱的
那部分被整份吞掉；原位重排没这个问题（页面没重建）。

判据全部读**落盘后的成品文件**，并在文档还开着的时候快照成纯数据：批注对象与
页对象是弱引用关系，页对象被回收后 `annot.rect` 会直接抛 ReferenceError（本轮
实测踩过，九条批注因此全被误判跳过），关掉文档再读同样读不到。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.pdf_translator import (  # noqa: E402
    _annot_name,
    _freetext_fontsize,
    _placement_matrix,
    _rebuild_annot,
    translate_pdf,
)

W, H = 595.0, 842.0
TEXT = "Alpha beta gamma delta, epsilon zeta eta theta."
NOTE = "请复核这一段"


class _FakeTranslator:
    stop_event = None

    def __init__(self) -> None:
        self.count = 0

    def translate_texts(self, texts, progress=None):
        self.count += len(texts)
        return [f"译文：{text}" for text in texts]

    def summary(self) -> str:
        return f"翻译 {self.count} 段"


def _new_doc(pages: int = 1) -> pymupdf.Document:
    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page(width=W, height=H)
        page.insert_text(pymupdf.Point(72, 120), f"{TEXT} page {index}", fontsize=11)
    return doc


def _add_common_annots(page: pymupdf.Page) -> None:
    note = page.add_text_annot(pymupdf.Point(72, 130), NOTE)
    note.set_info(title="Reviewer", subject="Note", content=NOTE)
    note.update()

    highlight = page.add_highlight_annot(pymupdf.Rect(72, 110, 220, 125))
    highlight.set_colors(stroke=(1, 1, 0))
    highlight.set_opacity(0.5)
    highlight.update()

    underline = page.add_underline_annot(pymupdf.Rect(72, 150, 210, 165))
    underline.set_colors(stroke=(0, 0, 1))
    underline.update()

    square = page.add_rect_annot(pymupdf.Rect(300, 100, 420, 180))
    square.set_colors(stroke=(1, 0, 0), fill=(1, 1, 0))
    square.update()

    line = page.add_line_annot(pymupdf.Point(300, 220), pymupdf.Point(420, 260))
    line.update()

    ink = page.add_ink_annot([[(100, 300), (140, 320), (180, 300)]])
    ink.update()

    freetext = page.add_freetext_annot(
        pymupdf.Rect(100, 400, 300, 460), "free text", fontsize=10
    )
    freetext.update()

    caret = page.add_caret_annot(pymupdf.Point(350, 400))
    caret.update()

    stamp = page.add_stamp_annot(pymupdf.Rect(350, 300, 450, 360))
    stamp.update()


def _add_widget(page: pymupdf.Page) -> None:
    widget = pymupdf.Widget()
    widget.rect = pymupdf.Rect(100, 500, 220, 520)
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    widget.field_name = "probe-field"
    page.add_widget(widget)


def _save(doc: pymupdf.Document, path: Path) -> Path:
    doc.save(path, garbage=3, deflate=True)
    doc.close()
    return path


def _point_tuple(point) -> tuple[float, float]:
    try:
        return (round(point.x, 1), round(point.y, 1))
    except AttributeError:
        return (round(point[0], 1), round(point[1], 1))


def _vertices_of(annot: pymupdf.Annot):
    raw = annot.vertices or []
    if not raw:
        return []
    first = raw[0]
    if hasattr(first, "x"):
        return [_point_tuple(point) for point in raw]
    if (
        isinstance(first, (tuple, list))
        and len(first) == 2
        and all(isinstance(value, (int, float)) for value in first)
    ):
        return [_point_tuple(point) for point in raw]
    return [[_point_tuple(point) for point in stroke] for stroke in raw]


def _snapshot(path: Path, page_number: int = 0) -> list[dict]:
    """文档开着时把批注读成纯数据；关掉文档或丢掉页对象后就读不到了。"""
    doc = pymupdf.open(path)
    try:
        page = doc[page_number]
        items = []
        for annot in page.annots():
            items.append(
                {
                    "type": annot.type[1],
                    "rect": tuple(round(value, 1) for value in annot.rect),
                    "vertices": _vertices_of(annot),
                    "colors": {key: list(value) for key, value in annot.colors.items()},
                    "opacity": annot.opacity,
                    "info": dict(annot.info),
                }
            )
        return items
    finally:
        doc.close()


def _widget_count(path: Path, page_number: int = 0) -> int:
    doc = pymupdf.open(path)
    try:
        return sum(1 for _ in doc[page_number].widgets())
    finally:
        doc.close()


COMMON_TYPES = {
    "Text",
    "Highlight",
    "Underline",
    "Square",
    "Line",
    "Ink",
    "FreeText",
    "Caret",
    "Stamp",
}
HIGHLIGHT_QUAD = [(72.0, 110.0), (220.0, 110.0), (72.0, 125.0), (220.0, 125.0)]


def test_side_layout_carries_common_annotations(tmp_path: Path) -> None:
    doc = _new_doc()
    _add_common_annots(doc[0])
    src = _save(doc, tmp_path / "annotated.pdf")

    out = Path(translate_pdf(src, _FakeTranslator(), layout="side", ocr_enabled=False))
    annots = _snapshot(out)

    assert {item["type"] for item in annots} == COMMON_TYPES
    by_type = {item["type"]: item for item in annots}
    # 左半页就是源页，透写坐标不该被挪动
    assert by_type["Highlight"]["vertices"] == HIGHLIGHT_QUAD
    assert by_type["Highlight"]["opacity"] == pytest.approx(0.5)
    assert by_type["Text"]["info"]["title"] == "Reviewer"
    assert by_type["Text"]["info"]["content"] == NOTE
    assert by_type["Square"]["colors"]["fill"] == pytest.approx([1.0, 1.0, 0.0])
    assert by_type["Line"]["vertices"] == [(300.0, 220.0), (420.0, 260.0)]
    # Ink 的 vertices 按「笔画」分组，一笔就是一层列表
    assert by_type["Ink"]["vertices"] == [
        [(100.0, 300.0), (140.0, 320.0), (180.0, 300.0)]
    ]
    # 所有批注仍落在左半页（源页宽度以内），不会漂到译文那半页
    assert all(item["rect"][2] <= W for item in annots)


def test_zh_layout_carries_annotations(tmp_path: Path) -> None:
    doc = _new_doc()
    page = doc[0]
    page.add_highlight_annot(pymupdf.Rect(72, 110, 220, 125)).update()
    src = _save(doc, tmp_path / "annotated.pdf")

    out = Path(translate_pdf(src, _FakeTranslator(), layout="zh", ocr_enabled=False))
    doc = pymupdf.open(out)
    try:
        assert doc[0].rect.width == pytest.approx(W)
    finally:
        doc.close()
    annots = _snapshot(out)
    assert [item["type"] for item in annots] == ["Highlight"]
    assert annots[0]["vertices"] == HIGHLIGHT_QUAD


def test_page_range_carries_only_selected_pages(tmp_path: Path) -> None:
    doc = _new_doc(pages=2)
    first, second = doc[0], doc[1]
    first.add_highlight_annot(pymupdf.Rect(72, 110, 220, 125)).update()
    second.add_highlight_annot(pymupdf.Rect(300, 200, 400, 215)).update()
    src = _save(doc, tmp_path / "twopage.pdf")

    out = Path(
        translate_pdf(src, _FakeTranslator(), layout="zh", ocr_enabled=False, pages="1")
    )
    reloaded = pymupdf.open(out)
    try:
        assert reloaded.page_count == 1
    finally:
        reloaded.close()
    annots = _snapshot(out)
    assert len(annots) == 1
    assert annots[0]["vertices"] == HIGHLIGHT_QUAD


def test_widget_is_reported_as_skipped(tmp_path: Path) -> None:
    doc = _new_doc()
    _add_common_annots(doc[0])
    _add_widget(doc[0])
    src = _save(doc, tmp_path / "with-form.pdf")

    log: list[str] = []
    out = Path(
        translate_pdf(src, _FakeTranslator(), layout="side", ocr_enabled=False, log=log.append)
    )
    joined = "\n".join(log)
    assert "Widget 1 条" in joined, "表单控件不进 annots()，必须单独清点并报数"
    assert "已生成对照 PDF" in joined and "没有对应物" in joined
    assert _widget_count(out) == 0


def test_inplace_keeps_original_annots(tmp_path: Path) -> None:
    doc = _new_doc()
    page = doc[0]
    page.add_highlight_annot(pymupdf.Rect(72, 110, 220, 125)).update()
    page.add_text_annot(pymupdf.Point(300, 300), NOTE).update()
    src = _save(doc, tmp_path / "annotated.pdf")

    log: list[str] = []
    out = Path(
        translate_pdf(src, _FakeTranslator(), layout="inplace", ocr_enabled=False, log=log.append)
    )
    annots = _snapshot(out)
    assert {item["type"] for item in annots} == {"Highlight", "Text"}
    assert not any("没有对应物" in line for line in log), "原位重排不该走搬运，也不该报跳过"
    assert any("批注原样保留" in line for line in log)


def test_annot_name_and_freetext_size_helpers(tmp_path: Path) -> None:
    doc = _new_doc()
    _add_common_annots(doc[0])
    src = _save(doc, tmp_path / "annotated.pdf")

    doc = pymupdf.open(src)
    try:
        page = doc[0]
        by_type = {annot.type[1]: annot for annot in page.annots()}
        assert _annot_name(doc, by_type["Text"]) == "Note"
        assert _annot_name(doc, by_type["Stamp"]) == "Approved"
        assert _freetext_fontsize(doc, by_type["FreeText"]) == pytest.approx(10.0)
        assert _annot_name(doc, by_type["Highlight"]) == ""
    finally:
        doc.close()


def test_rebuilt_annot_keeps_icon_and_stamp_name(tmp_path: Path) -> None:
    doc = _new_doc()
    _add_common_annots(doc[0])
    src = _save(doc, tmp_path / "annotated.pdf")

    source = pymupdf.open(src)
    out = pymupdf.open()
    built = tmp_path / "rebuilt.pdf"
    try:
        page = out.new_page(width=W, height=H)
        matrix = _placement_matrix(source[0].rect, pymupdf.Rect(0, 0, W, H))
        for annot in source[0].annots():
            _rebuild_annot(source, page, annot, matrix)
        out.save(built)
    finally:
        out.close()
        source.close()

    reloaded = pymupdf.open(built)
    try:
        page = reloaded[0]
        by_type = {annot.type[1]: annot for annot in page.annots()}
        assert reloaded.xref_get_key(by_type["Text"].xref, "Name")[1] == "/Note"
        assert reloaded.xref_get_key(by_type["Stamp"].xref, "Name")[1] == "/Approved"
        assert by_type["FreeText"].info["content"] == "free text"
    finally:
        reloaded.close()


def test_redact_annot_is_not_rebuilt(tmp_path: Path) -> None:
    doc = _new_doc()
    page = doc[0]
    page.add_redact_annot(pymupdf.Rect(72, 110, 220, 125))
    src = _save(doc, tmp_path / "redact-draft.pdf")

    source = pymupdf.open(src)
    out = pymupdf.open()
    try:
        source_page = source[0]
        page = out.new_page(width=W, height=H)
        matrix = _placement_matrix(source_page.rect, pymupdf.Rect(0, 0, W, H))
        annots = list(source_page.annots())
        assert any(annot.type[1] == "Redact" for annot in annots), "夹具应当带一条 Redact"
        for annot in annots:
            assert _rebuild_annot(source, page, annot, matrix) is None
    finally:
        out.close()
        source.close()
