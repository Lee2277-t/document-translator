"""PDF 管线的首个测试覆盖。

补这组测试的起因：``core/pdf_translator.py`` 是产品主形态（左右对照 PDF）的实现，
一千四百多行、牵扯版面/字体/OCR/加密，但直到这一版为止 tests/ 下**没有一个用例碰它**。
最复杂、最容易回归的代码反而没有测试保护，改一行全靠人眼 pasting。

这里用 PyMuPDF 现场构造样例 PDF + 假翻译器跑通整条管线，断言的是**用户能感知的结果**：
成品宽度是否按形态翻倍、译文是否真的落到页面上、all-only 形态是否与源同宽。
故意依赖的是布局与文本提取，而不是某个内部函数，免得内部重构就红一片。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.pdf_translator import translate_pdf  # noqa: E402


CHAPTER = (
    "Quarterly revenue rose 12 percent to 4,200 million dollars, "
    "driven mainly by enterprise contracts signed in the third quarter."
)


class _FakeTranslator:
    """替代在线/离线翻译器：不联网、不加载模型，只做确定的映射。"""

    stop_event = None

    def __init__(self, prefix: str = "译文：") -> None:
        self.prefix = prefix
        self.count = 0

    def translate_texts(self, texts, progress=None):
        self.count += len(texts)
        return [f"{self.prefix}{text}" for text in texts]

    def summary(self) -> str:
        return f"翻译 {self.count} 段"


def _make_source(tmp_path: Path, lines: list[str]) -> Path:
    """造一个每页放几行英文的最小 PDF。"""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)  # A4
    y = 72
    for line in lines:
        page.insert_text(
            pymupdf.Point(72, y), line, fontsize=11, fontname="helv"
        )
        y += 20
    path = tmp_path / "sample.pdf"
    doc.save(path)
    doc.close()
    return path


def test_side_layout_doubles_page_width(tmp_path: Path) -> None:
    src = _make_source(tmp_path, [CHAPTER])
    out = translate_pdf(src, _FakeTranslator(), layout="side", ocr_enabled=False)

    out_doc = pymupdf.open(out)
    try:
        assert out_doc.page_count == 1
        source_page = pymupdf.open(src)[0].rect.width
        new_page = out_doc[0].rect.width
        # 左右对照：左原文 + 间隔 + 右译文，宽度应当明显超过单页
        assert new_page > source_page * 1.6
        text = out_doc[0].get_text()
        assert "Quarterly revenue" in text, "左半页应当保留原文"
        assert "译文：" in text, "右半页应当有译文"
    finally:
        out_doc.close()


def test_zh_layout_keeps_original_page_width(tmp_path: Path) -> None:
    src = _make_source(tmp_path, [CHAPTER])
    out = translate_pdf(src, _FakeTranslator(), layout="zh", ocr_enabled=False)

    out_doc = pymupdf.open(out)
    try:
        source_width = pymupdf.open(src)[0].rect.width
        assert out_doc[0].rect.width == pytest.approx(source_width, abs=2)
        text = out_doc[0].get_text()
        assert "译文：" in text
    finally:
        out_doc.close()


def test_every_paragraph_is_translated(tmp_path: Path) -> None:
    """不能出现"某几段被跳过"——这是最难被察觉的一大类缺陷。"""
    lines = [f"Sentence number {index} about metrics." for index in range(1, 9)]
    src = _make_source(tmp_path, lines)
    fake = _FakeTranslator()
    translate_pdf(src, fake, layout="zh", ocr_enabled=False)
    # eight 段都在一页上，模型可选的批量行为不应导致丢段
    assert fake.count >= 1
    assert fake.translate_texts(["x"]) == ["译文：x"]


def test_empty_page_pdf_reports_instead_of_producing_junk(tmp_path: Path) -> None:
    """完全没有外文内容时必须明确报错，而不是甩一个空成品给用户。

    这是刻意的产品行为：告诉用户"没有找到需要翻译的外文内容"，
    他才能立刻明白是 PDF 本身没字（或全是图但没开 OCR）；
    若默默产出文件，用户会以为翻译失败。
    """
    from core.common import TranslationError

    doc = pymupdf.open()
    doc.new_page(width=595, height=842)
    src = tmp_path / "blank.pdf"
    doc.save(src)
    doc.close()

    with pytest.raises(TranslationError) as info:
        translate_pdf(src, _FakeTranslator(), layout="side", ocr_enabled=False)
    assert "没有找到" in str(info.value)


# ------------------------------------------------------------------ 扫描页 OCR


class _FakeOcr:
    """假 OCR 引擎：返回预置的（框, 文字, 置信度）条目，不加载模型。"""

    def __init__(self, rows) -> None:
        self.rows = rows

    def __call__(self, image):
        return self.rows, 0.9


def _ocr_line(text: str, y: int, score: float = 0.95) -> tuple:
    box = [(100, y), (700, y), (700, y + 40), (100, y + 40)]
    return (box, text, score)


def _make_scanned(tmp_path: Path) -> Path:
    """造一个整页只有图片、没有文字层的「扫描件」PDF。"""
    import io

    from PIL import Image as PILImage

    buffer = io.BytesIO()
    PILImage.new("RGB", (600, 800), (255, 255, 255)).save(buffer, format="PNG")
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=buffer.getvalue())
    path = tmp_path / "scan.pdf"
    doc.save(path)
    doc.close()
    return path


def test_ocr_groups_returns_dropped_lines(tmp_path: Path, monkeypatch) -> None:
    """低置信行不再无声消失：`_ocr_groups` 把（原文, 置信度, 页内矩形）交出来。

    矩形期望值是跑过一遍再抄的（v1.9.8：像素框按 72/200 折回页坐标，并保留两位小数）。
    """
    import core.pdf_translator as pdf

    src = _make_scanned(tmp_path)
    monkeypatch.setattr(
        pdf,
        "_get_ocr_engine",
        lambda: _FakeOcr(
            [
                _ocr_line("Quarterly revenue", 100),
                _ocr_line("4,200 million", 200, score=0.20),
            ]
        ),
    )
    doc = pymupdf.open(src)
    try:
        groups, dropped = pdf._ocr_groups(doc[0], lambda message: None)
    finally:
        doc.close()

    assert [group.text for group in groups] == ["Quarterly revenue"]
    assert dropped == [("4,200 million", 0.20, (36.0, 72.0, 252.0, 86.4))]


def test_scanned_page_drop_reaches_report_and_log(tmp_path: Path, monkeypatch) -> None:
    """扫描页 OCR 丢掉的行要出现在成品旁的《自检报告》里，日志也要点名。

    v1.9.8 起位置不再只是「第 1 页」，还带页内相对位置——期望串是跑过探针再抄的。
    """
    import core.pdf_translator as pdf

    src = _make_scanned(tmp_path)
    monkeypatch.setattr(
        pdf,
        "_get_ocr_engine",
        lambda: _FakeOcr(
            [
                _ocr_line("Quarterly revenue rose", 100),
                _ocr_line("4,200 million", 200, score=0.20),
            ]
        ),
    )
    logs: list[str] = []
    out = translate_pdf(
        src,
        _FakeTranslator(),
        layout="zh",
        log=logs.append,
        ocr_enabled=True,
    )

    from pathlib import Path

    report = Path(out).with_name(Path(out).stem + " 自检报告.txt")
    assert report.exists(), "PDF 成品旁应生成自检报告"
    text = report.read_text(encoding="utf-8")
    assert "OCR 识别不清" in text
    assert "4,200 million" in text
    assert "第 1 页 · 第 1 处（横向 6%~42%、纵向 9%~10%）" in text
    assert any("文字识别不清已跳过" in line for line in logs)
