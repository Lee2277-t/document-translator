"""Galgame 脚本前端测试：SRT / JSON / T++ 表格 + 统一入口。

全部用假翻译器，不联网、不拉模型。重点验证三条产品铁律：
① 序号与时间轴一个字符都不能动；② 行数不一致时保留原文而不是错位；
③ 说话人拆分后能进质量引擎、译文里按原样拼回。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.script_json import translate_script_json
from core.script_srt import parse_srt, render_srt, translate_srt
from core.script_translator import is_script_file, translate_script
from core.script_tpp import translate_tpp


class _FakeTranslator:
    """假翻译器：英文转「译：」前缀，并模拟一次行数错误。"""

    stop_event = None

    def __init__(self, break_lines_on: str | None = None) -> None:
        self.seen: list[str] = []
        self.break_lines_on = break_lines_on

    def translate_texts(self, texts, progress=None):
        self.seen.extend(texts)
        out = []
        for text in texts:
            if self.break_lines_on and self.break_lines_on in text:
                out.append("合并成一行")  # 行数对不上 → 调用方应保留原文
            else:
                rows = text.split("\n")
                out.append("\n".join(f"译:{row}" for row in rows))
        return out


class _QualityTranslator(_FakeTranslator):
    """带质量管线的假翻译器：管线返回什么就用什么。"""

    def __init__(self) -> None:
        super().__init__()
        self.pipeline_calls: list[list] = []

        class _Pipeline:
            calls = self.pipeline_calls

            def run(self, segments, progress=None):
                self.calls.append(list(segments))
                return type(
                    "Result",
                    (),
                    {
                        "translations": [f"管线译:{s.source}" for s in segments],
                        "issues": [],
                    },
                )()

        self.quality_pipeline = _Pipeline()


# ------------------------------------------------------------------ SRT


def _write(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


SAMPLE_SRT = """1
00:00:01,000 --> 00:00:04,000
Hello there

2
00:00:05,000 --> 00:00:08,500
How are you?
I am fine

3
00:00:09,000 --> 00:00:12,000
桜：元気？
"""


def test_parse_srt_blocks_and_speaker(tmp_path: Path) -> None:
    blocks = parse_srt(SAMPLE_SRT)
    assert len(blocks) == 3
    assert blocks[0].timing == "00:00:01,000 --> 00:00:04,000"
    assert blocks[0].text_lines == ["Hello there"]
    assert blocks[1].text_lines == ["How are you?", "I am fine"]
    assert blocks[2].speaker == "桜"
    assert blocks[2].text_lines == ["元気？"]


def test_translate_srt_preserves_timing_and_structure(tmp_path: Path) -> None:
    src = _write(tmp_path, "a.srt", SAMPLE_SRT)
    out = translate_srt(src, _FakeTranslator())
    text = Path(out).read_text(encoding="utf-8")

    assert "00:00:01,000 --> 00:00:04,000" in text
    assert "00:00:05,000 --> 00:00:08,500" in text
    assert "00:00:09,000 --> 00:00:12,000" in text
    # 块数与原文一致
    assert len(parse_srt(text)) == 3
    # 说话人行按原样拼回
    assert "桜：管线" not in text
    assert "桜：译:元気？" in text
    # 多行结构保留
    assert "译:How are you?\n译:I am fine" in text
    assert Path(out).name.endswith("_双语.srt")


def test_translate_srt_line_count_mismatch_keeps_source(tmp_path: Path) -> None:
    """行数对不上时整块保留原文——错位比不翻更糟。"""
    src = _write(tmp_path, "b.srt", SAMPLE_SRT)
    out = translate_srt(src, _FakeTranslator(break_lines_on="How are you?"))
    text = Path(out).read_text(encoding="utf-8")
    assert "How are you?\nI am fine" in text
    assert len(parse_srt(text)) == 3


def test_translate_srt_uses_quality_pipeline_with_speakers(tmp_path: Path) -> None:
    translator = _QualityTranslator()
    src = _write(tmp_path, "c.srt", SAMPLE_SRT)
    translate_srt(src, translator)
    # 说话人被传进管线（角色卡才能用上）
    speakers = [seg.speaker for seg in translator.pipeline_calls[0]]
    assert speakers == ["", "", "桜"]


def test_translate_srt_rejects_garbage(tmp_path: Path) -> None:
    src = _write(tmp_path, "d.srt", "这不是字幕文件")
    with pytest.raises(Exception):
        translate_srt(src, _FakeTranslator())


def test_parse_srt_handles_vtt_header() -> None:
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n"
    blocks = parse_srt(vtt)
    assert len(blocks) == 1
    assert blocks[0].text_lines == ["Hi"]


# ------------------------------------------------------------------ JSON


def test_translate_script_json_mtool_shape(tmp_path: Path) -> None:
    data = [
        {"names": ["桜"], "message": "你好啊"},
        {"names": ["太郎"], "message": "怎么了"},
        {"names": [], "message": ""},
    ]
    src = tmp_path / "script.json"
    src.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    out = translate_script_json(str(src), _FakeTranslator())
    result = json.loads(Path(out).read_text(encoding="utf-8"))
    assert result[0]["译文"] == "译:你好啊"
    assert result[1]["译文"] == "译:怎么了"
    # 原字段一个都不能少
    assert result[0]["names"] == ["桜"]
    assert result[0]["message"] == "你好啊"
    assert "译文" not in result[2]


def test_translate_script_json_wrapped_container(tmp_path: Path) -> None:
    data = {"lines": [{"name": "桜", "text": "你好"}]}
    src = tmp_path / "wrapped.json"
    src.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    out = translate_script_json(str(src), _FakeTranslator())
    result = json.loads(Path(out).read_text(encoding="utf-8"))
    assert result["lines"][0]["译文"] == "译:你好"


@pytest.mark.parametrize("container_key", ["lines", "data", "items", "scripts"])
def test_translate_script_json_preserves_wrapper_metadata(tmp_path: Path, container_key) -> None:
    data = {
        "version": 7,
        "metadata": {"title": "章节一", "tags": ["draft", "保留"]},
        "enabled": False,
        "other_rows": [{"text": "Do not translate metadata"}],
        container_key: [
            {"message": "Hello", "name": "Alice", "id": 12},
            {"message": "", "extra": [1, 2]},
        ],
    }
    src = tmp_path / "带附加信息.json"
    src.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    original = src.read_bytes()

    translator = _FakeTranslator()
    out = translate_script_json(str(src), translator)
    result = json.loads(Path(out).read_text(encoding="utf-8"))
    expected = json.loads(original)
    expected[container_key][0]["译文"] = "译:Hello"
    assert result == expected
    assert translator.seen == ["Hello"]
    assert src.read_bytes() == original


def test_translate_script_json_rejects_non_script(tmp_path: Path) -> None:
    src = tmp_path / "bad.json"
    src.write_text('{"foo": 1}', encoding="utf-8")
    with pytest.raises(Exception):
        translate_script_json(str(src), _FakeTranslator())


# ------------------------------------------------------------------ T++


def _make_tpp(tmp_path: Path, with_header: bool = True) -> str:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    if with_header:
        sheet.append(["名前", "原文", "备注"])
        sheet.append(["桜", "你好啊", "第一句"])
        sheet.append(["太郎", "怎么了", ""])
    else:
        sheet.append(["桜", "你好啊"])
        sheet.append(["太郎", "怎么了"])
    path = tmp_path / "script.xlsx"
    workbook.save(path)
    return str(path)


def test_translate_tpp_with_header(tmp_path: Path) -> None:
    from openpyxl import load_workbook

    src = _make_tpp(tmp_path, with_header=True)
    out = translate_tpp(src, _FakeTranslator())
    sheet = load_workbook(out).active
    rows = list(sheet.iter_rows(values_only=True))
    assert rows[0][:3] == ("名前", "原文", "备注")
    assert rows[0][3] == "译文"
    assert rows[1][3] == "译:你好啊"
    assert rows[2][3] == "译:怎么了"
    # 原有列不动
    assert rows[1][0] == "桜"
    assert rows[1][2] == "第一句"


def test_translate_tpp_without_header(tmp_path: Path) -> None:
    from openpyxl import load_workbook

    src = _make_tpp(tmp_path, with_header=False)
    out = translate_tpp(src, _FakeTranslator())
    sheet = load_workbook(out).active
    rows = list(sheet.iter_rows(values_only=True))
    # 无表头时不插入标题行：数据整体不错位
    assert rows[0][2] == "译:你好啊"
    assert rows[1][2] == "译:怎么了"


# ------------------------------------------------------------------ 入口


def test_is_script_file() -> None:
    assert is_script_file("a.srt")
    assert is_script_file("a.vtt")
    assert is_script_file("a.json")
    assert is_script_file("a.xlsx")
    assert not is_script_file("a.docx")
    assert not is_script_file("a.pdf")


def test_translate_script_dispatches(tmp_path: Path) -> None:
    src = _write(tmp_path, "e.srt", SAMPLE_SRT)
    out = translate_script(src, _FakeTranslator())
    assert Path(out).exists()


def test_translate_script_rejects_unknown(tmp_path: Path) -> None:
    src = _write(tmp_path, "f.txt", "hello")
    with pytest.raises(Exception):
        translate_script(src, _FakeTranslator())


# ------------------------------------------------------------------ 自检报告


class _NumberMangler(_FakeTranslator):
    """把 1234 改成 4321：模拟模型把金额/编号译错。"""

    def translate_texts(self, texts, progress=None):
        return [
            f"译:{text}".replace("1234", "4321") for text in texts
        ]


def _report_text(output: str) -> str:
    report = Path(output).with_name(Path(output).stem + " 自检报告.txt")
    assert report.exists(), f"脚本成品旁应生成自检报告：{report}"
    return report.read_text(encoding="utf-8")


def test_srt_writes_report_without_duplicating_block_fallback(tmp_path: Path) -> None:
    """行数不一致的块只报一次「保留原文」，不再叠一条「回显原文」。"""
    src = _write(tmp_path, "g.srt", SAMPLE_SRT)
    out = translate_srt(src, _FakeTranslator(break_lines_on="How are you?"))
    text = _report_text(out)
    assert "译文行数与原文不一致，已保留原文" in text
    assert text.count("块 #2（00:00:05,000 --> 00:00:08,500）") == 1


def test_srt_report_flags_number_change(tmp_path: Path) -> None:
    src = _write(
        tmp_path,
        "h.srt",
        "1\n00:00:01,000 --> 00:00:02,000\nInvoice 1234 issued\n",
    )
    out = translate_srt(src, _NumberMangler())
    text = _report_text(out)
    assert "[高] 数字疑点" in text
    assert "块 #1（00:00:01,000 --> 00:00:02,000）" in text


def test_srt_report_uses_the_files_own_index_not_the_parse_order(tmp_path: Path) -> None:
    """序号行跳号/不从 1 起时，报告里的「块 #N」必须是文件里那个号，否则会把人带到错块。"""
    src = _write(
        tmp_path,
        "gapped.srt",
        "5\n00:00:01,000 --> 00:00:03,000\nInvoice 1234 issued\n\n"
        "9\n00:00:05,000 --> 00:00:08,500\nHow are you?\nI am fine.\n",
    )
    out = translate_srt(src, _FakeTranslator(break_lines_on="How are you?"))
    report = _report_text(out)
    assert "块 #9（00:00:05,000 --> 00:00:08,500）" in report
    assert "块 #2（" not in report, "解析顺序不是文件里的序号，报告不许念错"

    edit = Path(out).with_name(Path(out).stem + " 译后编辑清单.json")
    listing = json.loads(edit.read_text(encoding="utf-8"))
    locations = [item["location"] for item in listing["items"]]
    assert locations == [
        "块 #5（00:00:01,000 --> 00:00:03,000）",
        "块 #9（00:00:05,000 --> 00:00:08,500）",
    ], listing


def test_script_json_report_uses_line_numbers(tmp_path: Path) -> None:
    src = tmp_path / "script2.json"
    src.write_text(
        json.dumps([{"message": "订单 1234 号"}], ensure_ascii=False),
        encoding="utf-8",
    )
    out = translate_script_json(str(src), _NumberMangler())
    text = _report_text(out)
    assert "[高] 数字疑点" in text
    assert "第 1 行" in text


def test_tpp_report_uses_excel_row_numbers(tmp_path: Path) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["名前", "原文"])
    sheet.append(["桜", "编号 1234 已发出"])
    src = tmp_path / "numbered.xlsx"
    workbook.save(src)

    out = translate_tpp(str(src), _NumberMangler())
    text = _report_text(out)
    assert "[高] 数字疑点" in text
    # 表格行号从 1 起、表头占第 1 行 → 第一条数据是第 2 行
    assert "第 2 行" in text


def test_tpp_report_written_even_without_issues(tmp_path: Path) -> None:
    src = _make_tpp(tmp_path, with_header=True)
    out = translate_tpp(src, _FakeTranslator())
    text = _report_text(out)
    # 没疑点也写：用户才知道核对跑过、结果如何
    assert "未发现需要复核的疑点" in text
