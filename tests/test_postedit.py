"""译后编辑最小闭环（v1.5.4，E2）的行为钉子。

盯住的东西按优先级：

1. **翻译成功即落清单**：六条管线（Word/PPT/PDF/SRT/JSON/T++）都在自检报告
   旁边落一份《原名 译后编辑清单.json》，每个译成段一条（位置 + 原文 + 译文
   + 疑点）。疑点按位置/原文片段挂到段上——「疑点定位」靠的就是这个；
2. **回写不坏版式**：改一段译后文字，只动这一个段落的文字 run，格式（粗斜体/
   中文字体/字号）、源文件、其他段落原样不动；时间轴/序号/单元格/非译文字段
   同样不碰；
3. **定位漂移不盲写**：成品被人动过、按文字找不到对应段时跳过并出声，
   而不是改错地方；
4. **PDF 只定位不回写**：清单照给（疑点定位靠页码），apply 明确告知不支持；
5. **CLI 闭环**：``--postedit=成品 --list`` 打印清单；手改 JSON 后
   ``--postedit=成品`` 写回；``--retranslate=段号`` 认不出的段号报错不瞎跑；
6. **GUI 闭环**：译后编辑窗口默认只看疑点，选段→看原文/疑点→改或重译→
   写回，成品与清单同步更新。

GUI 用例走子进程：本机 venv 的 ``_tkinter`` 走系统 Python 的 tcl 目录，
同进程反复创建 Tk 根有 ``tcl_findLibrary`` 抖动（仓库既有 GUI 测试一律
子进程的原因，见 tests/test_paid_gui.py 头注）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from docx import Document
from openpyxl import Workbook, load_workbook
from pptx import Presentation

import pymupdf  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from core.postedit import (  # noqa: E402
    EditItem,
    apply_edits,
    attach_issues,
    dump_edit_list,
    edit_list_path,
    format_edit_list,
    items_from_pairs,
    load_edit_list,
    retranslate_segment,
)
from core.ppt_translator import translate_ppt  # noqa: E402
from core.script_json import translate_script_json  # noqa: E402
from core.script_srt import translate_srt  # noqa: E402
from core.script_tpp import translate_tpp  # noqa: E402
from core.word_translator import translate_word  # noqa: E402
from core.pdf_translator import translate_pdf  # noqa: E402

PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

FIRST = "Revenue grew by 15.3 percent last quarter."
SECOND = "The board approved the merger unanimously."


class FakeTranslator:
    """不联网、不加载模型。默认给确定译文；可用 mapping 精确控制某段。"""

    stop_event = None

    def __init__(self, mapping: dict[str, str] | None = None, prefix: str = "译文："):
        self.mapping = mapping or {}
        self.prefix = prefix
        self.count = 0

    def translate_texts(self, texts, progress=None):
        self.count += len(texts)
        return [self.mapping.get(text, f"{self.prefix}{text[:8]}") for text in texts]

    def summary(self) -> str:
        return f"假译{self.count}段"


def _make_word_source(tmp_path: Path) -> Path:
    doc = Document()
    doc.add_paragraph(FIRST)
    doc.add_paragraph(SECOND)
    bold = doc.add_paragraph()
    run = bold.add_run("Confidential draft")
    run.bold = True
    doc.save(str(tmp_path / "sample.docx"))
    return tmp_path / "sample.docx"


def _translate_word(tmp_path: Path, mapping: dict[str, str] | None = None):
    src = _make_word_source(tmp_path)
    return src, translate_word(str(src), FakeTranslator(mapping), log=lambda m: None)


# ---------------------------------------------------------------------------
# 一、清单结构与疑点附着
# ---------------------------------------------------------------------------


def test_edit_list_path_next_to_output() -> None:
    assert edit_list_path(r"D:\x\报告_双语.docx") == r"D:\x\报告_双语 译后编辑清单.json"


def test_items_from_pairs_and_pending() -> None:
    items = items_from_pairs(
        [("hello", "你好")], ["段落 1"], [{}], "docx", "w"
    )
    assert len(items) == 1
    item = items[0]
    assert item.id == "w1" and item.location == "段落 1"
    assert item.applied == item.translation and not item.pending
    item.translation = "改过的"
    assert item.pending


def test_attach_issues_by_location_and_globals() -> None:
    items = items_from_pairs([("hello", "你好")], ["段落 1"], [{}], "docx", "w")

    class _Issue:
        kind = "数字疑点"
        location = "段落 1"
        source = "hello 15.3"
        note = "数字缺失"

        def level(self) -> str:
            return "高"

    class _GlobalIssue:
        kind = "译名不一致"
        location = "全文"
        source = "bank"
        note = "两处译法不同"

        def level(self) -> str:
            return "中"

    globals_ = attach_issues(items, [_Issue(), _GlobalIssue()])
    assert items[0].issues == ["[高] 数字疑点：数字缺失"]
    assert globals_ == ["[中] 译名不一致：两处译法不同"]


def test_attach_issues_falls_back_to_source_prefix() -> None:
    """疑点的位置串与清单对不上时，按原文片段反查也要挂上。

    v1.7.0 起 PPT 的疑点位置已带张号（与 EPUB 带章号同口径），这条测的是那条兜底分支本身：
    位置写不认识的形态（这里是光秃秃的「幻灯片」）不能把疑点丢掉。
    """
    long_source = "Revenue grew by 15.3 percent last quarter across regions."
    items = items_from_pairs([(long_source, "译文")], ["第 1 张"], [{}], "pptx", "p")

    class _Issue:
        kind = "数字疑点"
        location = "幻灯片"
        source = long_source[:40]  # 报告里是截断版
        note = "数字缺失"

        def level(self) -> str:
            return "高"

    globals_ = attach_issues(items, [_Issue()])
    assert globals_ == []
    assert items[0].issues == ["[高] 数字疑点：数字缺失"]


def test_format_edit_list_orders_issues_first_and_marks_pending() -> None:
    clean = items_from_pairs([("a", "甲")], ["段落 1"], [{}], "docx", "w")[0]
    dirty = items_from_pairs([("b", "乙")], ["段落 2"], [{}], "docx", "w")[0]
    dirty.issues.append("[高] 数字疑点：数字缺失")
    dirty.translation = "改过的乙"
    text = format_edit_list([clean, dirty])
    assert text.index("段落 2") < text.index("段落 1")
    assert "⚠" in text and "*待写回*" in text


def test_load_edit_list_rejects_bad_json(tmp_path: Path) -> None:
    from core.common import TranslationError

    bad = tmp_path / "bad.json"
    bad.write_text("{不是 json", encoding="utf-8")
    with pytest.raises(TranslationError):
        load_edit_list(str(bad))


@pytest.mark.parametrize("stage", ["write", "sync", "replace"])
@pytest.mark.parametrize("existing", [False, True])
def test_edit_list_failed_save_preserves_previous_file(tmp_path, monkeypatch, stage, existing):
    from core import postedit

    output = str(tmp_path / "报告_双语.docx")
    items = items_from_pairs([("Hello", "你好")], ["段落 1"], [{}], "docx", "w")
    path = Path(edit_list_path(output))
    files_before = set(tmp_path.iterdir())
    if existing:
        dump_edit_list(output, items)
    original = path.read_bytes() if existing else None
    items[0].translation = "修改后的译文"

    def fail(*args, **kwargs):
        if stage == "write":
            args[1].write('{"items":')
        raise OSError("模拟保存失败")

    if stage == "write":
        monkeypatch.setattr(postedit.json, "dump", fail)
    elif stage == "sync":
        monkeypatch.setattr(postedit.os, "fsync", fail)
    else:
        monkeypatch.setattr(postedit.os, "replace", fail)
    with pytest.raises(OSError, match="模拟保存失败"):
        dump_edit_list(output, items)
    if existing:
        assert path.read_bytes() == original
        assert load_edit_list(str(path))[0].translation == "你好"
    else:
        assert not path.exists()
    assert set(tmp_path.iterdir()) == files_before | ({path} if existing else set())


def test_edit_list_atomic_save_roundtrip(tmp_path):
    files_before = set(tmp_path.iterdir())
    output = str(tmp_path / "报告_双语.docx")
    items = items_from_pairs([("Hello", "你好")], ["段落 1"], [{}], "docx", "w")
    path = Path(dump_edit_list(output, items))
    items[0].translation = "修改后的译文"
    items[0].candidates = ["你好！"]
    assert dump_edit_list(output, items) == str(path)
    assert [item.to_dict() for item in load_edit_list(str(path))] == [items[0].to_dict()]
    assert json.loads(path.read_text(encoding="utf-8"))["output"] == "报告_双语.docx"
    assert "修改后的译文" in path.read_text(encoding="utf-8")
    assert set(tmp_path.iterdir()) == files_before | {path}


# ---------------------------------------------------------------------------
# 二、Word 回写（主形态）
# ---------------------------------------------------------------------------


def test_word_roundtrip_translate_edit_writeback(tmp_path: Path) -> None:
    src, out = _translate_word(
        tmp_path, mapping={SECOND: "董事会一致批准了合并。"}
    )
    assert Path(out).name.endswith("_双语.docx")
    items = load_edit_list(edit_list_path(out))
    assert len(items) == 3
    assert items[0].location == "段落 1"
    assert all(item.applied == item.translation for item in items)
    # 第一段译文丢了数字 → 数字疑点挂上去
    assert any("数字疑点" in issue for issue in items[0].issues)

    # 改第二段（普通）与第三段（粗体）
    items[1].translation = "董事会一致批准了合并案。"
    items[2].translation = "机密草案。"
    result = apply_edits(out, [items[1], items[2]], log=lambda m: None)
    assert result.applied == ["w2", "w3"]
    assert result.skipped == []

    doc = Document(out)
    texts = [p.text for p in doc.paragraphs]
    assert "董事会一致批准了合并案。" in texts
    assert "机密草案。" in texts
    assert FIRST in texts and SECOND in texts  # 原文段不动
    assert ("译文：" + FIRST[:8]).strip() in texts  # 其他译文段不动
    for para in doc.paragraphs:
        if para.text == "机密草案。":
            assert para.runs and para.runs[0].bold is True, "粗体格式丢了"
        if para.text == "董事会一致批准了合并案。":
            assert para.runs and para.runs[0].font.name, "中文字体丢了"
    # 源文件自始至终不被碰
    source_text = "\n".join(p.text for p in Document(str(src)).paragraphs)
    assert FIRST in source_text and SECOND in source_text


def test_word_apply_skips_when_paragraph_drifted(tmp_path: Path) -> None:
    _src, out = _translate_word(tmp_path)
    items = load_edit_list(edit_list_path(out))
    items[0].translation = "全新的译文"
    items[0].applied = "根本不存在的旧译文"
    before = Path(out).read_bytes()
    result = apply_edits(out, [items[0]], log=lambda m: None)
    assert result.applied == []
    assert result.skipped == ["w1"]
    assert "找不到对应的译文段" in result.notes[0]
    assert Path(out).read_bytes() == before, "漂移时一个字都不能动"


def test_word_duplicate_translations_need_locator(tmp_path: Path) -> None:
    """两段译文相同且没有索引时宁可不写——盲写可能改错段。"""
    _src, out = _translate_word(
        tmp_path, mapping={FIRST: "相同译文", SECOND: "相同译文"}
    )
    items = load_edit_list(edit_list_path(out))
    for item in items:
        item.locator = {}  # 抹掉索引，只剩文本匹配
    items[0].translation = "改了的译文"
    result = apply_edits(out, [items[0]], log=lambda m: None)
    assert result.applied == [] and result.skipped == ["w1"]
    # 有索引就能精确定位
    items2 = load_edit_list(edit_list_path(out))
    assert items2[0].locator.get("index") is not None
    items2[0].translation = "改了的译文"
    result2 = apply_edits(out, [items2[0]], log=lambda m: None)
    assert result2.applied == ["w1"]


# ---------------------------------------------------------------------------
# 三、其余格式 roundtrip
# ---------------------------------------------------------------------------


def test_srt_roundtrip_keeps_timing_and_index(tmp_path: Path) -> None:
    src = tmp_path / "a.srt"
    src.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nHello world\n\n"
        "2\n00:00:04,000 --> 00:00:06,000\nGoodbye world\n",
        encoding="utf-8",
    )
    out = translate_srt(str(src), FakeTranslator(), log=lambda m: None)
    items = load_edit_list(edit_list_path(out))
    assert len(items) == 2
    assert items[0].location.startswith("块 #1（00:00:01,000")

    items[0].translation = "你好，世界。"
    result = apply_edits(out, [items[0]], log=lambda m: None)
    assert result.applied == ["b1"]
    text = Path(out).read_text(encoding="utf-8")
    assert "00:00:01,000 --> 00:00:03,000" in text  # 时间轴不动
    assert "你好，世界。" in text
    assert "译文：Goodbye" in text  # 另一块不动


def test_srt_line_count_mismatch_still_writes_with_note(tmp_path: Path) -> None:
    src = tmp_path / "a.srt"
    src.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nHello world\n", encoding="utf-8"
    )
    # 假译文刻意不带冒号：成品重解析时「xx：yy」会被说话人正则拆成
    # speaker=xx，render 再加回前缀，与回写值对不上（真实译文无此前缀）。
    out = translate_srt(
        str(src), FakeTranslator(mapping={"Hello world": "你好世界"}), log=lambda m: None
    )
    items = load_edit_list(edit_list_path(out))
    items[0].translation = "第一行\n第二行"  # 原文 1 行，改写成 2 行
    result = apply_edits(out, [items[0]], log=lambda m: None)
    assert result.applied == ["b1"]
    assert any("行数不一致" in note for note in result.notes)
    text = Path(out).read_text(encoding="utf-8")
    assert "第一行\n第二行" in text


def test_json_roundtrip_only_touches_translation_key(tmp_path: Path) -> None:
    src = tmp_path / "a.json"
    src.write_text(
        json.dumps(
            [{"message": "Hello", "name": "Alice"}, {"message": "World", "name": "Bob"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    out = translate_script_json(str(src), FakeTranslator(), log=lambda m: None)
    items = load_edit_list(edit_list_path(out))
    assert len(items) == 2 and items[0].location == "第 1 行"
    items[1].translation = "世界！"
    result = apply_edits(out, [items[1]], log=lambda m: None)
    assert result.applied == ["j2"]
    data = json.loads(Path(out).read_text(encoding="utf-8"))
    assert data[1]["译文"] == "世界！"
    assert data[1]["message"] == "World" and data[1]["name"] == "Bob"
    assert data[0]["译文"] == items[0].applied  # 另一行不动


@pytest.mark.parametrize("container_key", ["lines", "data", "items", "scripts"])
def test_json_writeback_preserves_wrapper_metadata(tmp_path: Path, container_key) -> None:
    data = {
        "version": 7,
        "metadata": {"title": "章节一", "flags": [1, None, False]},
        container_key: [
            {"message": "Hello", "译文": "你好", "id": 12},
            {"message": "World", "译文": "世界", "name": "Bob"},
        ],
    }
    path = tmp_path / "成品.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    item = EditItem(
        id="j1", location="第 1 行", source="Hello", translation="你好！",
        kind="json", locator={"index": 0}, applied="你好",
    )
    result = apply_edits(str(path), [item])
    data[container_key][0]["译文"] = "你好！"
    assert result.applied == ["j1"]
    assert json.loads(path.read_text(encoding="utf-8")) == data


def test_tpp_roundtrip_only_touches_translation_cell(tmp_path: Path) -> None:
    src = tmp_path / "a.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["说话人", "原文", "译文"])
    sheet.append(["Alice", "Hello there", ""])
    sheet.append(["Bob", "General Kenobi", ""])
    workbook.save(str(src))
    out = translate_tpp(str(src), FakeTranslator(), log=lambda m: None)
    items = load_edit_list(edit_list_path(out))
    assert len(items) == 2
    assert items[0].location == "第 2 行"
    items[0].translation = "你好。"
    result = apply_edits(out, [items[0]], log=lambda m: None)
    assert result.applied == ["x1"]
    sheet2 = load_workbook(out).active
    assert sheet2.cell(row=2, column=3).value == "你好。"
    assert sheet2.cell(row=3, column=3).value == "译文：General"
    assert sheet2.cell(row=2, column=2).value == "Hello there"  # 原文列不动


def test_ppt_roundtrip_updates_translation_paragraph(tmp_path: Path) -> None:
    src = tmp_path / "a.pptx"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(45720, 45720, 4572000, 4572000)
    box.text_frame.text = FIRST
    slide2 = prs.slides.add_slide(prs.slide_layouts[6])
    box2 = slide2.shapes.add_textbox(45720, 45720, 4572000, 4572000)
    box2.text_frame.text = SECOND
    prs.save(str(src))
    out = translate_ppt(str(src), FakeTranslator(), log=lambda m: None)
    items = load_edit_list(edit_list_path(out))
    assert len(items) == 2 and items[0].location == "第 1 张"
    items[0].translation = "收入增长了15.3%。"
    result = apply_edits(out, [items[0]], log=lambda m: None)
    assert result.applied == ["p1"]
    prs2 = Presentation(out)
    frames = [
        shape.text_frame.text
        for slide in prs2.slides
        for shape in slide.shapes
        if shape.has_text_frame
    ]
    joined = "\n".join(frames)
    assert "收入增长了15.3%。" in joined  # 译文段已改
    assert FIRST in joined  # 原文段不动
    assert "译文：" + SECOND[:8] in joined  # 另一页不动


# ---------------------------------------------------------------------------
# 四、PDF：只定位不回写
# ---------------------------------------------------------------------------


def test_pdf_list_is_reference_only(tmp_path: Path) -> None:
    src = tmp_path / "a.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text(pymupdf.Point(72, 72), FIRST, fontsize=11, fontname="helv")
    doc.save(str(src))
    doc.close()

    out = translate_pdf(
        str(src), FakeTranslator(), log=lambda m: None, ocr_enabled=False
    )
    items = load_edit_list(edit_list_path(out))
    assert items and all(item.kind == "pdf" for item in items)
    assert items[0].location == "第 1 页"
    before = Path(out).read_bytes()
    result = apply_edits(out, items, log=lambda m: None)
    assert result.applied == []
    assert len(result.skipped) == len(items)
    assert any("PDF" in note and "绘制文本" in note for note in result.notes)
    assert Path(out).read_bytes() == before


# ---------------------------------------------------------------------------
# 五、单段重译与清单持久化
# ---------------------------------------------------------------------------


def test_retranslate_segment_uses_same_engine() -> None:
    class _Engine:
        def translate_texts(self, texts, progress=None):
            assert texts == ["hello"]
            return ["重摇的译文"]

    assert retranslate_segment(_Engine(), "hello") == "重摇的译文"


def test_retranslate_segment_empty_result_raises() -> None:
    from core.common import TranslationError

    class _Engine:
        def translate_texts(self, texts, progress=None):
            return ["   "]

    with pytest.raises(TranslationError, match="引擎没有返回"):
        retranslate_segment(_Engine(), "hello")


def test_dump_and_load_roundtrip(tmp_path: Path) -> None:
    items = items_from_pairs([("hello", "你好")], ["段落 1"], [{}], "docx", "w")
    path = dump_edit_list(str(tmp_path / "x_双语.docx"), items)
    assert Path(path).name == "x_双语 译后编辑清单.json"
    back = load_edit_list(path)
    assert back[0].to_dict() == items[0].to_dict()


# ---------------------------------------------------------------------------
# 六、CLI 闭环（进程内直调 _run_postedit，不起子进程避免 GBK 管道）
# ---------------------------------------------------------------------------


def _prepare_output(tmp_path: Path) -> str:
    _src, out = _translate_word(tmp_path)
    return out


def test_cli_postedit_list_prints_inventory(tmp_path, monkeypatch, capsys) -> None:
    out = _prepare_output(tmp_path)
    monkeypatch.setattr(app, "CLI_POSTEDIT_LIST", True)
    monkeypatch.setattr(app, "CLI_RETRANSLATE", "")
    code = app._run_postedit(out)
    assert code == 0
    printed = capsys.readouterr().out
    assert "译后编辑清单" in printed
    assert "段落 1" in printed and "数字疑点" in printed


def test_cli_postedit_applies_manual_json_edits(tmp_path, monkeypatch, capsys) -> None:
    out = _prepare_output(tmp_path)
    list_path = edit_list_path(out)
    payload = json.loads(Path(list_path).read_text(encoding="utf-8"))
    payload["items"][0]["translation"] = "手改的第一段。"
    Path(list_path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    monkeypatch.setattr(app, "CLI_POSTEDIT_LIST", False)
    monkeypatch.setattr(app, "CLI_RETRANSLATE", "")
    code = app._run_postedit(out)
    assert code == 0
    printed = capsys.readouterr().out
    assert "已写回 1 段" in printed
    doc = Document(out)
    assert any(p.text == "手改的第一段。" for p in doc.paragraphs)
    # 清单同步：applied 跟上
    items = load_edit_list(list_path)
    assert not any(item.pending for item in items)


def test_cli_postedit_without_list_says_so(tmp_path, capsys) -> None:
    stray = tmp_path / "stray.docx"
    stray.write_text("不是成品也没有清单", encoding="utf-8")
    code = app._run_postedit(str(stray))
    assert code == 1
    assert "没有找到译后编辑清单" in capsys.readouterr().out


def test_cli_postedit_retranslate_unknown_id_errors(tmp_path, monkeypatch, capsys) -> None:
    out = _prepare_output(tmp_path)
    monkeypatch.setattr(app, "CLI_POSTEDIT_LIST", False)
    monkeypatch.setattr(app, "CLI_RETRANSLATE", "zzz")
    code = app._run_postedit(out)
    assert code == 1
    assert "清单里没有这些段" in capsys.readouterr().out


def test_cli_postedit_nothing_pending(tmp_path, monkeypatch, capsys) -> None:
    out = _prepare_output(tmp_path)
    monkeypatch.setattr(app, "CLI_POSTEDIT_LIST", False)
    monkeypatch.setattr(app, "CLI_RETRANSLATE", "")
    code = app._run_postedit(out)
    assert code == 0
    assert "没有待写回的修改" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 七、GUI 窗口（子进程：一个子进程只构造一次 Tk 根）
# ---------------------------------------------------------------------------

_CHILD_GUI = """\
import json, sys, tempfile
from pathlib import Path
from unittest import mock

sys.argv = ["app.py"]

import tkinter as tk
import app
from docx import Document
from core.postedit import edit_list_path, load_edit_list
from core.word_translator import translate_word

FIRST = "Revenue grew by 15.3 percent last quarter."
SECOND = "The board approved the merger unanimously."


class Fake:
    stop_event = None

    def translate_texts(self, texts, progress=None):
        return ["译文甲", "译文乙"]

    def summary(self):
        return "假译2段"


work = Path(tempfile.mkdtemp())
src = work / "s.docx"
doc = Document()
doc.add_paragraph(FIRST)
doc.add_paragraph(SECOND)
doc.save(str(src))
out = translate_word(str(src), Fake(), log=lambda m: None)

# 弹窗一律吞掉（headless 没人点确定）
for name in ("showinfo", "showwarning", "showerror"):
    setattr(app.messagebox, name, lambda *a, **k: None)

app._enable_dpi_awareness()
application = app.TranslatorApp()
application.output_path = out
window = app.PostEditWindow(application)
window.window.update()
problems = []

# 1. 默认只看疑点：第一段译文丢了数字，应恰好一条可见
if not window.only_issues.get():
    problems.append("only_issues 默认没开")
if len(window.visible) != 1:
    problems.append(f"疑点可见数={len(window.visible)}")
if not window.visible or not window.visible[0].issues:
    problems.append("可见项没有疑点")

# 2. 放开过滤：两段都在
window.only_issues.set(False)
window._refresh_list()
window.window.update()
if len(window.visible) != 2:
    problems.append(f"全部可见数={len(window.visible)}")

# 3. 选中第一段：原文/译文/位置填充
window.listbox.selection_set(0)
window._on_select()
window.window.update()
if window.current is None:
    problems.append("没有选中项")
elif FIRST not in window.fields[0].get("1.0", "end"):
    problems.append("原文框没填充")
elif "译文甲" not in window.trans_text.get("1.0", "end"):
    problems.append("译文框没填充")
elif not window.fields[1].get("1.0", "end").strip():
    problems.append("疑点框是空的")

# 4. 改译文 → 保存写回 → 成品与清单同步
window.trans_text.delete("1.0", "end")
window.trans_text.insert("1.0", "界面改过的译文。")
window._save()
window.window.update()
doc2 = Document(out)
texts = [p.text for p in doc2.paragraphs]
if "界面改过的译文。" not in texts:
    problems.append("写回没生效")
if SECOND not in texts:
    problems.append("原文段被动了")
items = load_edit_list(edit_list_path(out))
if not any(i.applied == "界面改过的译文。" for i in items):
    problems.append("清单 applied 没更新")

# 5. 恢复初译按钮：把编辑框还原到上次写回值
window._restore()
window.window.update()
if "界面改过的译文。" not in window.trans_text.get("1.0", "end"):
    problems.append("恢复初译没还原")

if problems:
    print("FAIL:" + ";".join(problems))
    sys.exit(1)
print("POSTEDIT_GUI_OK")
"""


def test_gui_postedit_window_roundtrip() -> None:
    result = subprocess.run(
        [str(PYTHON), "-c", _CHILD_GUI],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    assert "POSTEDIT_GUI_OK" in result.stdout, (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
