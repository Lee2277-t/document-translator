"""图片翻译测试：OCR mock + 擦除/叠字管线。

不加载真 RapidOCR（几百 MB）：mock 掉引擎，验证管线行为——
行聚类、低置信度丢弃、译文与原文相同不覆盖、输出文件生成。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

import core.image_translator as it
from core.image_translator import OcrLine, translate_image


class _FakeOcr:
    def __init__(self, rows) -> None:
        self.rows = rows  # [[(box, text, score), ...], ...]

    def __call__(self, image):
        return self.rows, 0.9


class _FakeTranslator:
    stop_event = None

    def __init__(self, replies=None) -> None:
        self.replies = replies or {}
        self.seen: list[str] = []

    def translate_texts(self, texts, progress=None):
        self.seen.extend(texts)
        return [self.replies.get(text, f"译:{text}") for text in texts]


@pytest.fixture()
def fake_font(monkeypatch):
    """用 PIL 内置默认字体顶替中文字体——不依赖测试机装了什么字体。"""
    from PIL import ImageFont

    monkeypatch.setattr(it, "_find_font", lambda size: ImageFont.load_default(size=size))


def _line(text: str, x0: float, y0: float, score: float = 0.95) -> tuple:
    box = [(x0, y0), (x0 + 60, y0), (x0 + 60, y0 + 20), (x0, y0 + 20)]
    return (box, text, score)


def _install_ocr(monkeypatch, rows) -> None:
    monkeypatch.setattr(it, "_get_ocr_engine", lambda: _FakeOcr(rows))


def _make_image(tmp_path: Path, name: str = "shot.png") -> str:
    image = Image.new("RGB", (400, 200), (255, 255, 255))
    path = tmp_path / name
    image.save(path)
    return str(path)


# ------------------------------------------------------------------ 聚类


def test_cluster_lines_merges_same_row() -> None:
    lines = [
        OcrLine(box=[(0, 0), (40, 0), (40, 20), (0, 20)], text="Hello", score=0.9),
        OcrLine(box=[(45, 1), (90, 1), (90, 21), (45, 21)], text="world", score=0.9),
        OcrLine(box=[(0, 40), (60, 40), (60, 60), (0, 60)], text="second", score=0.9),
    ]
    rows = it._cluster_lines(lines)
    assert len(rows) == 2
    assert [line.text for line in rows[0]] == ["Hello", "world"]


def test_merge_row_takes_union_box() -> None:
    lines = [
        OcrLine(box=[(0, 0), (10, 0), (10, 5), (0, 5)], text="a", score=0.9),
        OcrLine(box=[(20, 2), (40, 2), (40, 8), (20, 8)], text="b", score=0.8),
    ]
    merged = it._merge_row(lines)
    assert merged.x0 == 0 and merged.x1 == 40
    assert merged.y0 == 0 and merged.y1 == 8
    assert merged.text == "a b"
    assert merged.score == 0.8


# ------------------------------------------------------------------ 管线


def test_translate_image_end_to_end(tmp_path, monkeypatch, fake_font) -> None:
    src = _make_image(tmp_path)
    # 注意：RapidOCR 返回的是扁平 item 列表，行聚类由模块自己做
    _install_ocr(monkeypatch, [_line("Hello", 10, 10), _line("World", 10, 50)])
    out = translate_image(src, _FakeTranslator())
    assert Path(out).exists()
    assert Path(out).name.endswith("_中文.png")
    # 输出图尺寸与输入一致（只换内容，不裁切）
    assert Image.open(out).size == (400, 200)


def test_low_score_lines_discarded(tmp_path, monkeypatch, fake_font) -> None:
    src = _make_image(tmp_path)
    _install_ocr(monkeypatch, [[_line("garbage", 10, 10, score=0.2)]])
    with pytest.raises(Exception):
        translate_image(src, _FakeTranslator())


def test_empty_ocr_raises(tmp_path, monkeypatch, fake_font) -> None:
    src = _make_image(tmp_path)
    _install_ocr(monkeypatch, [])
    with pytest.raises(Exception):
        translate_image(src, _FakeTranslator())


def test_same_as_source_not_overwritten(tmp_path, monkeypatch, fake_font) -> None:
    """引擎原样返回时不覆盖原字——否则等于把原文擦掉换成同样的字。"""
    src = _make_image(tmp_path)
    _install_ocr(monkeypatch, [[_line("Hello", 10, 10)]])
    translator = _FakeTranslator(replies={"Hello": "Hello"})
    with pytest.raises(Exception):
        translate_image(src, translator)


def test_rejects_non_image(tmp_path, fake_font) -> None:
    src = tmp_path / "doc.txt"
    src.write_text("x", encoding="utf-8")
    with pytest.raises(Exception):
        translate_image(str(src), _FakeTranslator())


def test_is_image_file() -> None:
    assert it.is_image_file("a.png")
    assert it.is_image_file("a.JPG")
    assert not it.is_image_file("a.docx")


# ------------------------------------------------------------------ 自检报告


def test_report_surfaces_dropped_low_score_lines(tmp_path, monkeypatch, fake_font) -> None:
    """低置信行不再是无声丢弃：成品旁的报告要点名原文片段与最低置信度。"""
    src = _make_image(tmp_path)
    _install_ocr(
        monkeypatch,
        [_line("Hello", 10, 10), _line("4,200", 10, 60, score=0.30)],
    )
    out = translate_image(src, _FakeTranslator())

    report = Path(out).with_name(Path(out).stem + " 自检报告.txt")
    assert report.exists(), "图片成品旁应生成自检报告"
    text = report.read_text(encoding="utf-8")
    assert "OCR 识别不清" in text
    assert "4,200" in text
    assert "0.30" in text


def test_report_flags_number_change_in_image(tmp_path, monkeypatch, fake_font) -> None:
    src = _make_image(tmp_path)
    _install_ocr(monkeypatch, [_line("Invoice 1234", 10, 10)])
    translator = _FakeTranslator(replies={"Invoice 1234": "发票 4321"})
    out = translate_image(src, translator)

    report = Path(out).with_name(Path(out).stem + " 自检报告.txt")
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    assert "数字疑点" in text


def test_report_gives_each_dropped_line_a_relative_position(
    tmp_path, monkeypatch, fake_font
) -> None:
    """截图上没有行号可念，只有相对位置——丢掉的行必须能照百分比找回去。

    期望值先跑了真实输出再抄（probe 见 Round 22 交付记录），没有按 400×200 手算百分比。
    """
    src = _make_image(tmp_path)
    _install_ocr(
        monkeypatch,
        [
            _line("Hello", 10, 10),
            _line("4,200", 100, 60, score=0.30),
            _line("garbled", 10, 160, score=0.20),
            _line("tiny", 300, 100, score=0.40),
        ],
    )
    out = translate_image(src, _FakeTranslator())
    text = Path(out).with_name(Path(out).stem + " 自检报告.txt").read_text(encoding="utf-8")

    # 按阅读顺序编号（自上而下、自左而右），不是 OCR 引擎返回的顺序
    assert "第 1 处（横向 25%~40%、纵向 30%~40%） | 4,200" in text
    assert "第 2 处（横向 75%~90%、纵向 50%~60%） | tiny" in text
    assert "第 3 处（横向 2%~18%、纵向 80%~90%） | garbled" in text


def test_report_merges_drops_beyond_the_cap(tmp_path, monkeypatch, fake_font) -> None:
    """丢掉的行再多也不许把报告淹掉：前 8 条各带位置，其余合成一条。"""
    src = _make_image(tmp_path)
    rows = [_line("Hello", 10, 10)]
    rows += [_line(f"noise{i}", 0, 20 * i, score=0.10) for i in range(1, 11)]
    _install_ocr(monkeypatch, rows)
    out = translate_image(src, _FakeTranslator())
    text = Path(out).with_name(Path(out).stem + " 自检报告.txt").read_text(encoding="utf-8")

    assert text.count("OCR 识别不清 · 第 ") == 8
    assert "其余位置" in text
    assert "另有 2 处" in text


def test_box_rect_tolerates_a_broken_box() -> None:
    """OCR 引擎偶尔给出认不出的框；位置算不出来就退回「其余位置」，不许炸整张图。"""
    from core.report import box_rect

    assert box_rect([(1, 2), (5, 2), (5, 9), (1, 9)]) == (1.0, 2.0, 5.0, 9.0)
    assert box_rect("nonsense") is None
    assert box_rect([]) is None
