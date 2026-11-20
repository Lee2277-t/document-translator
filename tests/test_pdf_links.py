"""重建版式 PDF 的链接保留（Round 5 F1）。

起因：`show_pdf_page` 只搬页面**外观**，链接标注一条都跟不过来——左右对照与
仅中文两种版式做出来的成品，原文里的超链接与「跳到第 N 页」全部点不动
（实测成品页 `get_links()` 为 0）。原位重排没这个问题，因为页面根本没重建。

这里两头都测：端到端测「成品文件里真的能读回这条链接、热区落在对的那半页」，
单元测 `_map_link` 的映射规则（同页跳转要改写成显式页号、`to` 用目标页的落点、
搬不了的种类如实跳过）。断言一律读**落盘后的文件**：新建文档的内存页对象在
`insert_link` 之后读不回刚插的链接（实测），拿 `get_links()` 断言内存态会假阴。
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
    RIGHT_GAP,
    _map_link,
    _placement_matrix,
    translate_pdf,
)

CHAPTER = (
    "Quarterly revenue rose 12 percent to 4,200 million dollars, "
    "driven mainly by enterprise contracts signed in the third quarter."
)
URI = "https://example.com/annual-report"
W, H = 595.0, 842.0
GAP = RIGHT_GAP


class _FakeTranslator:
    stop_event = None

    def __init__(self) -> None:
        self.count = 0

    def translate_texts(self, texts, progress=None):
        self.count += len(texts)
        return [f"译文：{text}" for text in texts]

    def summary(self) -> str:
        return f"翻译 {self.count} 段"


def _make_linked_source(tmp_path: Path, extra_links: list[dict] | None = None) -> Path:
    """造一个两页、第一页带网址链接与跨页跳转的 PDF。"""
    doc = pymupdf.open()
    for index in range(2):
        page = doc.new_page(width=W, height=H)
        page.insert_text(pymupdf.Point(72, 120), f"{CHAPTER} page {index}", fontsize=11)
    doc[0].insert_link(
        {"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(70, 110, 200, 130), "uri": URI}
    )
    doc[0].insert_link(
        {
            "kind": pymupdf.LINK_GOTO,
            "from": pymupdf.Rect(70, 200, 160, 215),
            "page": 1,
            "to": pymupdf.Point(80, 140),
            "zoom": 0.0,
        }
    )
    for link in extra_links or []:
        doc[0].insert_link(dict(link))
    path = tmp_path / "linked.pdf"
    doc.save(path, garbage=3, deflate=True)
    doc.close()
    return path


def _links_of(path: Path, page_number: int) -> list[dict]:
    doc = pymupdf.open(path)
    try:
        return doc[page_number].get_links()
    finally:
        doc.close()


def test_side_layout_carries_uri_link(tmp_path: Path) -> None:
    src = _make_linked_source(tmp_path)
    out = Path(translate_pdf(src, _FakeTranslator(), layout="side", ocr_enabled=False))

    links = _links_of(out, 0)
    uri_links = [link for link in links if link["kind"] == pymupdf.LINK_URI]
    assert len(uri_links) == 1, "对照版成品页上应当有一条能点的网址链接"
    assert uri_links[0]["uri"] == URI
    # 左半页就是源页，热区坐标不该被挪走
    assert uri_links[0]["from"] == pymupdf.Rect(70, 110, 200, 130)


def test_zh_layout_carries_links(tmp_path: Path) -> None:
    src = _make_linked_source(tmp_path)
    out = Path(translate_pdf(src, _FakeTranslator(), layout="zh", ocr_enabled=False))

    links = _links_of(out, 0)
    assert len(links) == 2, "仅中文版两种链接都该搬过去"
    assert {link["kind"] for link in links} == {pymupdf.LINK_URI, pymupdf.LINK_GOTO}


def test_goto_keeps_target_page_and_destination(tmp_path: Path) -> None:
    src = _make_linked_source(tmp_path)
    out = Path(translate_pdf(src, _FakeTranslator(), layout="side", ocr_enabled=False))

    goto = [
        link
        for link in _links_of(out, 0)
        if link["kind"] == pymupdf.LINK_GOTO
    ][0]
    # 一页对一页，目标页号不变；跳落点仍在目标页正文那一行附近
    assert goto["page"] == 1
    assert (goto["to"].x, goto["to"].y) == pytest.approx((80, 140))


def test_carried_link_count_is_reported(tmp_path: Path) -> None:
    src = _make_linked_source(tmp_path)
    messages: list[str] = []
    translate_pdf(
        src,
        _FakeTranslator(),
        log=messages.append,
        layout="side",
        ocr_enabled=False,
    )

    assert any("2 个链接一并保留" in line for line in messages), messages


def test_unsupported_kinds_are_skipped_out_loud(tmp_path: Path) -> None:
    """命名目标 / 启动程序这类链接在成品里没有可指向的东西，跳过但要出声。"""
    src = _make_linked_source(
        tmp_path,
        extra_links=[
            {"kind": pymupdf.LINK_NAMED, "from": pymupdf.Rect(70, 400, 160, 415),
             "nameddest": "somewhere"},
            {"kind": pymupdf.LINK_LAUNCH, "from": pymupdf.Rect(70, 500, 160, 515),
             "file": "calc.exe"},
        ],
    )
    messages: list[str] = []
    out = Path(
        translate_pdf(
            src, _FakeTranslator(), log=messages.append, layout="side", ocr_enabled=False
        )
    )

    assert len(_links_of(out, 0)) == 2, "搬不了的种类不该硬塞进成品"
    assert any("没有可指向的目标" in line and "2 个" in line for line in messages), messages
    assert any("2 个链接一并保留" in line for line in messages), messages


def test_source_without_links_carries_nothing(tmp_path: Path) -> None:
    """没有链接的常规文档不该多出任何空话或链接。"""
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    page.insert_text(pymupdf.Point(72, 120), CHAPTER, fontsize=11)
    src = tmp_path / "plain.pdf"
    doc.save(src)
    doc.close()

    messages: list[str] = []
    out = Path(
        translate_pdf(
            src, _FakeTranslator(), log=messages.append, layout="side", ocr_enabled=False
        )
    )

    assert _links_of(out, 0) == []
    assert not any("链接" in line for line in messages), messages


# --- _map_link 的映射规则：端到端造不出来的形态在这里钉住 -------------------

SRC_RECT = pymupdf.Rect(0, 0, W, H)
LEFT_BOX = pymupdf.Rect(0, 0, W, H)
RIGHT_BOX = pymupdf.Rect(W + GAP, 0, 2 * W + GAP, H)
BOXES = {
    # 表是人为摆的：两种重建版式里每个源页都贴在成品页的左半边，页号也一一相同；
    # 这里故意让成品页号与源页号不相等、目标页落在右半边，才能证明映射真的查了表。
    0: (5, SRC_RECT, LEFT_BOX),
    1: (7, SRC_RECT, RIGHT_BOX),
}


def test_same_page_goto_becomes_explicit_output_page() -> None:
    """`page=-1`（跳本页）必须改写成显式成品页号。

    实测 `insert_link` 收到 page=-1 直接抛 TypeError，留着不改写等于这条链接
    必丢——这是本轮最容易踩空的一处，单独钉住。
    """
    mapped = _map_link(
        {
            "kind": pymupdf.LINK_GOTO,
            "from": pymupdf.Rect(70, 300, 160, 315),
            "page": -1,
            "to": pymupdf.Point(80, 300),
            "zoom": 0.0,
            "id": "fitz-L2",
            "xref": 41,
        },
        SRC_RECT,
        LEFT_BOX,
        BOXES,
        out_index=5,
    )

    assert mapped is not None
    assert mapped["page"] == 5
    assert "id" not in mapped and "xref" not in mapped, "源文档的对象号不该带进成品"


def test_goto_uses_target_page_box_for_destination() -> None:
    """跳落点 `to` 在**目标页**的坐标系里，要按目标页的贴入框映射。"""
    mapped = _map_link(
        {
            "kind": pymupdf.LINK_GOTO,
            "from": pymupdf.Rect(70, 200, 160, 215),
            "page": 1,
            "to": pymupdf.Point(80, 140),
            "zoom": 2.0,
        },
        SRC_RECT,
        LEFT_BOX,
        BOXES,
        out_index=5,
    )

    assert mapped is not None
    assert mapped["page"] == 7, "目标页号要换成成品页号"
    assert (mapped["to"].x, mapped["to"].y) == pytest.approx((80 + W + GAP, 140))
    assert mapped["zoom"] == 2.0
    # 热区本身仍在源页那一侧（左半页），不跟着目标页平移
    assert list(mapped["from"]) == pytest.approx([70, 200, 160, 215])


def test_missing_target_page_is_skipped() -> None:
    assert _map_link(
        {"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(0, 0, 10, 10), "page": 99},
        SRC_RECT,
        LEFT_BOX,
        BOXES,
        out_index=5,
    ) is None


def test_remote_goto_is_skipped() -> None:
    """指向另一个文件的跳转：成品里没有那个文件，点开了也是死链。"""
    assert _map_link(
        {"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(0, 0, 10, 10),
         "remote": 1, "file": "other.pdf", "page": 0},
        SRC_RECT,
        LEFT_BOX,
        BOXES,
        out_index=5,
    ) is None


@pytest.mark.parametrize(
    "kind",
    # 5 = 文档跳转（pymupdf 没有给出这个名字的常量，实测读回是 5）
    [pymupdf.LINK_NONE, pymupdf.LINK_NAMED, pymupdf.LINK_LAUNCH, 5],
)
def test_other_kinds_are_skipped(kind: int) -> None:
    assert _map_link(
        {"kind": kind, "from": pymupdf.Rect(0, 0, 10, 10)},
        SRC_RECT,
        LEFT_BOX,
        BOXES,
        out_index=5,
    ) is None


def test_offset_source_rect_shifts_hot_zone_back() -> None:
    """CropBox 起点不在 (0, 0) 的源页：热区要跟着挪回来。

    源页坐标整体带 (+7, +3) 偏移，贴入框原点仍是 0，直接照搬会让热区偏离文字。
    """
    offset = pymupdf.Rect(7, 3, 7 + W, 3 + H)
    mapped = _map_link(
        {"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(77, 113, 207, 133), "uri": URI},
        offset,
        LEFT_BOX,
        {0: (0, offset, LEFT_BOX)},
        out_index=0,
    )

    assert mapped is not None
    assert list(mapped["from"]) == pytest.approx([70, 110, 200, 130])


def test_placement_matrix_scales_shrunk_box() -> None:
    """版式若把源页缩着贴，热区按同比例缩——通用矩阵而不是硬编码平移的理由。"""
    matrix = _placement_matrix(SRC_RECT, pymupdf.Rect(0, 0, W / 2, H / 2))

    assert list(pymupdf.Rect(70, 110, 200, 130).transform(matrix)) == pytest.approx(
        [35, 55, 100, 65]
    )
    assert (
        pymupdf.Point(80, 140).transform(matrix).x,
        pymupdf.Point(80, 140).transform(matrix).y,
    ) == pytest.approx((40, 70))
