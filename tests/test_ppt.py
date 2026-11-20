"""PPT 双语翻译的单元测试。

不依赖网络或离线模型：用一个假 translator 把每段原文前面拼上「译：」，
验证 ppt_translator 确实在原文段落之后插入了中文段落，且正确覆盖文本框、
表格单元格与组合形状；并验证「没有可翻译内容」时报明确错误。
"""
from __future__ import annotations

from pathlib import Path

from pptx import Presentation
from pptx.util import Inches, Pt

from core.ppt_translator import translate_ppt


class _FakeTranslator:
    """把每段原文前面拼上「译：」并返回，模拟一次成功翻译。"""

    stop_event = None

    def translate_texts(self, texts, progress=None):
        return [f"译：{text}" for text in texts]


def _texts_after(path: str) -> list[list[str]]:
    """读出成品里每个文本帧的段落文本列表（按幻灯片、文本帧顺序）。"""
    prs = Presentation(path)
    frames: list[list[str]] = []
    for slide in prs.slides:
        for shape in slide.shapes:
            _collect(shape, frames)
    return frames


def _collect(shape, frames: list[list[str]]) -> None:
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        for child in shape.shapes:
            _collect(child, frames)
        return
    if shape.has_table:
        for row in shape.table.rows:
            for cell in row.cells:
                frames.append([p.text for p in cell.text_frame.paragraphs])
        return
    if shape.has_text_frame:
        frames.append([p.text for p in shape.text_frame.paragraphs])


def _make_sample(tmp_path: Path) -> str:
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])

    # 普通文本框
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    box.text_frame.text = "Hello world"

    # 表格
    table = slide.shapes.add_table(1, 1, Inches(1), Inches(3), Inches(3), Inches(1)).table
    table.cell(0, 0).text = "Good morning"

    # 组合形状内的文本框
    group = slide.shapes.add_group_shape()
    gbox = group.shapes.add_textbox(Inches(1), Inches(5), Inches(3), Inches(1))
    gbox.text_frame.text = "Thank you"
    # 让文本框字号非默认，验证 rPr 被克隆
    gbox.text_frame.paragraphs[0].runs[0].font.size = Pt(24)

    src = str(tmp_path / "sample.pptx")
    prs.save(src)
    return src


def test_ppt_inserts_chinese_after_each_paragraph(tmp_path: Path) -> None:
    src = _make_sample(tmp_path)
    out = translate_ppt(src, _FakeTranslator())

    frames = _texts_after(out)
    # 文本框 Hello world → [原文, 译：Hello world]
    assert ["Hello world", "译：Hello world"] in frames
    # 表格单元格
    assert ["Good morning", "译：Good morning"] in frames
    # 组合形状内的文本框（字号 24 应被克隆到译文段）
    assert ["Thank you", "译：Thank you"] in frames


def test_ppt_preserves_font_size_on_translation(tmp_path: Path) -> None:
    src = _make_sample(tmp_path)
    out = translate_ppt(src, _FakeTranslator())

    prs = Presentation(out)
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    # 只检查组合形状里那个设了 24 号字的文本框：其译文段落应克隆出相同字号。
    found = False
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.shape_type != MSO_SHAPE_TYPE.GROUP:
                continue
            for sh in shape.shapes:
                if not sh.has_text_frame:
                    continue
                for para in sh.text_frame.paragraphs:
                    if para.text.startswith("译：") and para.runs:
                        assert para.runs[0].font.size == Pt(24)
                        found = True
    assert found, "没有在组合形状里找到译文段落"


def test_ppt_no_text_raises(tmp_path: Path) -> None:
    from core.common import TranslationError

    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[5])  # 空幻灯片，无任何文字
    src = str(tmp_path / "empty.pptx")
    prs.save(src)
    try:
        translate_ppt(src, _FakeTranslator())
    except TranslationError as exc:
        assert "没有可翻译" in str(exc)
    else:
        raise AssertionError("空 PPT 应当报「没有可翻译内容」")


def test_ppt_writes_selfcheck_report(tmp_path: Path) -> None:
    """PPT 也必须出自检报告——此前只有 PDF/Word 有，PPT 用户看不到疑点清单。"""
    src = _make_sample(tmp_path)
    out = translate_ppt(src, _FakeTranslator())
    report = Path(out).with_name(
        Path(out).stem + " 自检报告.txt"
    )
    assert report.exists(), "PPT 成品旁应生成自检报告"
    text = report.read_text(encoding="utf-8")
    assert "自检报告" in text
    # 假翻译器没有质量属性，报告里不应出现 MQM 段
    assert "MQM" not in text


def test_ppt_report_lists_memory_fuzzy_reuse(tmp_path: Path) -> None:
    """近似复用（v1.9.9）：PPT 报告也要照着段落找得到，不能只在日志里念段数。"""

    class _FuzzyTranslator(_FakeTranslator):
        tm_fuzzy_hits = [
            {"paragraph": 1, "source": "Please confirm the following terms?", "ratio": 0.93}
        ]

    src = _make_sample(tmp_path)
    out = translate_ppt(src, _FuzzyTranslator())
    text = Path(out).with_name(
        Path(out).stem + " 自检报告.txt"
    ).read_text(encoding="utf-8")
    assert "[中] 记忆近似复用 · 段落 1" in text
    assert "相似度 93%" in text


# --------------------------------------------------------------------------
# 幻灯片范围（v1.7.0）：--pages= 从「只对 PDF 生效」扩展到 PPT 按张挑
# --------------------------------------------------------------------------


def _make_deck(tmp_path: Path, texts: list[str]) -> str:
    """每张幻灯片一个文本框装一句话——范围测试要的是「张」的边界，不是版式。"""
    prs = Presentation()
    for text in texts:
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        if text:
            box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
            box.text_frame.text = text
    src = str(tmp_path / "deck.pptx")
    prs.save(src)
    return src


class _RecordingTranslator(_FakeTranslator):
    """顺手记下送进引擎的原文，用来断言未选中的张**根本没被送翻**。"""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def translate_texts(self, texts, progress=None):
        self.seen.extend(texts)
        return [f"译：{text}" for text in texts]


def _slide_texts(path: str) -> list[str]:
    """每张幻灯片的全部文字（各文本框段落拼起来），用于按张判断翻没翻。"""
    prs = Presentation(path)
    return [
        "\n".join(
            para.text
            for shape in slide.shapes
            if shape.has_text_frame
            for para in shape.text_frame.paragraphs
        )
        for slide in prs.slides
    ]


def test_slide_range_sends_only_selected_slides(tmp_path: Path) -> None:
    src = _make_deck(tmp_path, ["First slide", "Second slide", "Third slide"])
    engine = _RecordingTranslator()
    out = translate_ppt(src, engine, pages="1,3")

    assert engine.seen == ["First slide", "Third slide"], (
        "未选中的张不该送进引擎——省的是真金白银的付费配额与离线耗时"
    )
    assert len(Presentation(out).slides) == 3, "PPT 不重建幻灯片：成品张数必须等于源张数"

    per_slide = _slide_texts(out)
    assert len(per_slide) == 3
    assert "译：First slide" in per_slide[0] and "译：Third slide" in per_slide[2]
    assert "译：" not in per_slide[1], "第二张不在范围内，一个字都不该多出来"
    assert "Second slide" in per_slide[1], "未选中的张要原样留在成品里，不能变成空张"


def test_slide_range_says_it_does_not_rebuild_slides(tmp_path: Path) -> None:
    """「成品还是 3 张」这句必须出声：不说的话，用户看到全本会以为范围没生效。"""
    src = _make_deck(tmp_path, ["Alpha", "Beta", "Gamma"])
    logs: list[str] = []
    translate_ppt(src, _RecordingTranslator(), log=logs.append, pages="2")

    text = "\n".join(logs)
    assert "幻灯片范围：第 2 张" in text, logs
    assert "这份 PPT 共 3 张，选中 1 张" in text, logs
    assert "不重建幻灯片" in text and "成品还是 3 张" in text, logs


def test_slide_range_out_of_range_names_slide_counter(tmp_path: Path) -> None:
    from core.common import TranslationError

    src = _make_deck(tmp_path, ["Alpha", "Beta"])
    try:
        translate_ppt(src, _RecordingTranslator(), pages="3")
    except TranslationError as exc:
        assert "幻灯片范围「3」超出文档张数" in str(exc), str(exc)
        assert "这份 PPT 只有 2 张，你写到第 3 张" in str(exc), str(exc)
    else:
        raise AssertionError("超出张数应当报错，而不是静默夹到最后一张")


def test_slide_range_error_never_calls_slides_pages(tmp_path: Path) -> None:
    """量词是这一轮的全部意义：对 PPT 念「页」会让用户拿错的单位去核对成品。"""
    from core.common import TranslationError

    src = _make_deck(tmp_path, ["Alpha", "Beta"])
    for bad in ("9", "2-9", "abc"):
        try:
            translate_ppt(src, _RecordingTranslator(), pages=bad)
        except TranslationError as exc:
            assert "页" not in str(exc), f"PPT 的报错里出现了「页」：{exc}"


def test_slide_range_on_blank_slide_points_at_the_box(tmp_path: Path) -> None:
    """选中了空张不能报「这份 PPT 没有文字」——那是假话，文字在别的张上。"""
    from core.common import TranslationError

    src = _make_deck(tmp_path, ["Alpha", "", "Gamma"])
    try:
        translate_ppt(src, _RecordingTranslator(), pages="2")
    except TranslationError as exc:
        assert "选中的这 1 张里没有可翻译的文字内容" in str(exc), str(exc)
        assert "「范围」那一栏" in str(exc), str(exc)
    else:
        raise AssertionError("选中张全为空应当报错并指出范围")


def test_no_slide_range_translates_every_slide(tmp_path: Path) -> None:
    """范围只填了空格 = 没填 = 整份翻，且日志不该念「幻灯片范围」（免得看起来像被裁过）。

    第一版在这里抓到过真 bug：`--pages="  "` 被当成范围去解析，当场报「幻灯片范围是空的」，
    而 PDF 的既有口径是留空即整份——空白口径本轮统一到 `(pages or "").strip()`。
    """
    src = _make_deck(tmp_path, ["Alpha", "Beta", "Gamma"])
    engine = _RecordingTranslator()
    logs: list[str] = []
    out = translate_ppt(src, engine, log=logs.append, pages="  ")

    assert engine.seen == ["Alpha", "Beta", "Gamma"]
    assert not any("幻灯片范围" in line for line in logs), logs
    assert all("译：" in text for text in _slide_texts(out))



def test_number_issue_report_names_the_slide_number(tmp_path: Path) -> None:
    """自检报告里的数字疑点要带张号：只写「幻灯片」等于让用户自己挨张找。"""
    src = _make_deck(tmp_path, ["Alpha", "Revenue fell 15.3 percent."])

    class _NoDigits(_FakeTranslator):
        def translate_texts(self, texts, progress=None):
            return ["译文里数字丢了" for _ in texts]

    out = translate_ppt(src, _NoDigits(), log=lambda m: None)
    report = Path(out).with_name(Path(out).stem + " 自检报告.txt")
    text = report.read_text(encoding="utf-8")

    assert "数字疑点 · 第 2 张" in text, text
    assert "数字疑点 · 幻灯片 |" not in text, text
