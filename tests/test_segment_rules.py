"""用户可编辑分段规则表（v1.8.0，差距清单 C5 剩下的「规则表可编辑」）。

覆盖五件事：① **空表逐字保持内置行为**（这条最要紧，切句一改就会动摇所有既有成品）；
② 表里的四种字段各自真的能改变下刀位置，且 ``safe_cut`` 的分片兜底同样吃规则；
③ 坏写法逐条出声并跳过，绝不因为一条正则写错就拦住整份翻译；
④ 模板生成与「已存在不覆盖」；⑤ CLI 两个新开关的接线。

**必须隔离的两处全局态**（都踩过）：``core.segment_rules._ACTIVE`` 与 ``app`` 的
``CLI_*`` 模块级变量——``main()`` 会把命令行值写进后者，不还原就会漏到下一条用例，
届时 ``--dump-segment-rules`` 会把模板真写进仓库根目录（v1.8.0 首轮真机复现，
连带把 ``test_tbx`` / ``test_tmx`` 四条打红）。两者现在都由 ``tests/conftest.py``
的 autouse 夹具兜住，本文件只剩一件自己的事：**把规则文件的落点换成 tmp**，
任何读真实工具目录的路径都不许出现在测试里。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from core import segment_rules
from core.segment import safe_cut, split_sentences
from core.translator import split_for_api

BUILTIN_TEXT = "First sentence here. Second one follows! 第三句。第四句？"
BUILTIN_PARTS = ["First sentence here.", "Second one follows!", "第三句。", "第四句？"]


@pytest.fixture(autouse=True)
def _rules_land_in_tmp(tmp_path, monkeypatch):
    """规则表只准读写本条用例自己的 tmp 目录。"""
    monkeypatch.setattr(segment_rules, "rules_file",
                        lambda: tmp_path / segment_rules.RULES_FILE_NAME)


def _load(data: object, notes: list[str] | None = None) -> segment_rules.Rules:
    """按表编译规则；传了 notes 就把出声收进这个列表。

    这里不能写 ``notes or []``——空列表本身是假值，传进来的 ``[]`` 会被换成
    一个用完即丢的新列表，于是所有「检查有没有出声」的断言恒假。
    """
    sink = [] if notes is None else notes
    return segment_rules.build(data, sink.append, Path("分段规则.json"))


# ---------------------------------------------------------------- ① 默认零改变

def test_default_table_keeps_builtin_behaviour_word_for_word():
    assert split_sentences(BUILTIN_TEXT) == BUILTIN_PARTS
    assert safe_cut("aaaa bbbb cccc dddd", 9) == ("aaaa", "bbbb cccc dddd")
    assert safe_cut("第一句。第二句。第三句。", 10) == ("第一句。第二句。", "第三句。")


def test_active_rules_default_is_empty():
    assert segment_rules.active().is_default
    assert segment_rules.Rules().summary() == ""


# ---------------------------------------------------------------- ② 四种字段生效

def test_extra_cjk_ender_splits_without_space():
    segment_rules.set_active(_load({segment_rules.KEY_CJK: ["…"]}))
    assert split_sentences("第一段…第二段") == ["第一段…", "第二段"]
    assert split_sentences(BUILTIN_TEXT) == BUILTIN_PARTS  # 内置那一套没被动


def test_extra_latin_ender_requires_space():
    segment_rules.set_active(_load({segment_rules.KEY_LATIN: ["›"]}))
    assert split_sentences("甲 › 乙") == ["甲 ›", "乙"]
    assert split_sentences("甲›乙") == ["甲›乙"]


def test_blocked_phrase_stops_splitting_after_abbreviation():
    text = "Use a tool e.g. this one. Done."
    assert split_sentences(text) == ["Use a tool e.g.", "this one.", "Done."]
    segment_rules.set_active(_load({segment_rules.KEY_PHRASES: ["e.g."]}))
    assert split_sentences(text) == ["Use a tool e.g. this one.", "Done."]


def test_blocked_regex_protects_numbering():
    text = "见 1.2.3. 系统概述。下一句。"
    assert split_sentences(text) == ["见 1.2.3.", "系统概述。", "下一句。"]
    segment_rules.set_active(_load({segment_rules.KEY_REGEX: [r"\d+(?:\.\d+)*\.$"]}))
    assert split_sentences(text) == ["见 1.2.3. 系统概述。", "下一句。"]


def test_safe_cut_also_honours_blockers():
    """超长段的兜底下刀点也不能落在被禁的位置：只在切句层生效的规则表是假的。"""
    text = "aaa bbb。ccc ddd eee fff"
    assert safe_cut(text, 12) == ("aaa bbb。", "ccc ddd eee fff")
    segment_rules.set_active(_load({segment_rules.KEY_PHRASES: ["bbb。"]}))
    assert safe_cut(text, 12) == ("aaa bbb。ccc", "ddd eee fff")


def test_rules_reach_api_chunking():
    """split_for_api 走的是同一套规则：改的是**送翻前**的断句，不只是界面显示。"""
    text = ("This clause matters e.g. here and then continues with many more words "
            "so that the chunk limit is exceeded again and again. " * 2)
    before = split_for_api(text, 120)
    segment_rules.set_active(_load({segment_rules.KEY_PHRASES: ["e.g."]}))
    after = split_for_api(text, 120)
    assert any(piece.endswith("e.g.") for piece in before)
    assert not any(piece.endswith("e.g.") for piece in after)


def test_added_ender_reaches_api_chunking():
    """内置不认的间隔号一旦进表，送翻切片就改在它后面收口。

    改之前 120 字连排没有句边界，只能硬切在 40 字——劈口正好落在「段落」中间；
    改之后每片（末片除外，原文本就收在「丁」）都在间隔号后收尾，不再劈词。
    """
    text = "段落甲・段落乙・段落丙・段落丁" * 8
    before = split_for_api(text, 40)
    assert len(before) == 3 and not before[0].endswith("・")
    segment_rules.set_active(_load({segment_rules.KEY_CJK: ["・"]}))
    after = split_for_api(text, 40)
    assert len(after) > len(before)
    assert all(piece.endswith("・") for piece in after[:-1])


def test_lookback_window_bounds_the_pattern_search():
    """规则只看候选切点之前 LOOKBACK 个字符：远处的无关文字不许影响这一刀。"""
    segment_rules.set_active(_load({segment_rules.KEY_PHRASES: ["e.g."]}))
    tail = "x" * (segment_rules.LOOKBACK + 10)
    assert split_sentences(f"e.g.{tail}. 后一句") == [f"e.g.{tail}.", "后一句"]
    assert split_sentences("e.g. 后一句") == ["e.g. 后一句"]


# ---------------------------------------------------------------- ③ 坏写法只丢那条

def test_broken_json_falls_back_to_builtin_with_one_notice(tmp_path):
    path = tmp_path / "分段规则.json"
    path.write_text("{这不是 JSON", encoding="utf-8")
    notes: list[str] = []
    rules = segment_rules.load(path, notes.append)
    assert rules.is_default
    assert any("不是合法 JSON" in note for note in notes)


def test_unreadable_file_is_named_in_the_notice(tmp_path):
    notes: list[str] = []
    rules = segment_rules.load(tmp_path / "不存在.json", notes.append)
    assert rules.is_default
    assert any("不存在.json" in note for note in notes)


def test_top_level_array_is_rejected_loudly():
    notes: list[str] = []
    rules = _load(["e.g."], notes)
    assert rules.is_default
    assert any("最外层不是" in note for note in notes)


def test_unknown_key_is_named_but_rest_still_applies():
    notes: list[str] = []
    rules = _load({"切句正则": ["(?=("], segment_rules.KEY_PHRASES: ["e.g."]}, notes)
    assert len(rules.blockers) == 1
    assert any("认不出的栏位" in note and "切句正则" in note for note in notes)
    assert any(segment_rules.KEY_NOTE in note for note in notes)


def test_multi_char_ender_is_redirected_to_phrases():
    notes: list[str] = []
    rules = _load({segment_rules.KEY_CJK: ["。。。", "。"]}, notes)
    assert rules.cjk_extra == "。"
    assert any("不是单个字符" in note and segment_rules.KEY_PHRASES in note for note in notes)


def test_bad_regex_drops_only_that_entry():
    notes: list[str] = []
    rules = _load({segment_rules.KEY_REGEX: ["(?=(坏", r"\d\.$"]}, notes)
    assert len(rules.blockers) == 1
    assert any("不是合法正则" in note for note in notes)


def test_overlong_regex_is_refused():
    notes: list[str] = []
    rules = _load({segment_rules.KEY_REGEX: [r"\d" * (segment_rules.MAX_PATTERN_CHARS + 1)]}, notes)
    assert rules.blockers == ()
    assert any(str(segment_rules.MAX_PATTERN_CHARS) in note for note in notes)


def test_entry_cap_keeps_first_n_and_says_so():
    notes: list[str] = []
    many = [f"词{i}" for i in range(segment_rules.MAX_ENTRIES + 5)]
    rules = _load({segment_rules.KEY_PHRASES: many}, notes)
    assert len(rules.blockers) == segment_rules.MAX_ENTRIES
    assert any("超过" in note for note in notes)


def test_non_list_field_is_ignored_with_notice():
    notes: list[str] = []
    rules = _load({segment_rules.KEY_PHRASES: "e.g."}, notes)
    assert rules.blockers == ()
    assert any("要一个列表" in note for note in notes)


def test_duplicates_collapse():
    assert _load({segment_rules.KEY_CJK: ["…", "…"]}).cjk_extra == "…"


# ---------------------------------------------------------------- ④ 文件与模板

def test_startup_is_silent_when_no_file():
    notes: list[str] = []
    rules, path = segment_rules.startup(notes.append)
    assert rules.is_default and path is None and notes == []


def test_startup_reads_the_file_next_to_the_exe(tmp_path):
    target = tmp_path / segment_rules.RULES_FILE_NAME
    target.write_text(json.dumps({segment_rules.KEY_PHRASES: ["e.g."]}, ensure_ascii=False),
                      encoding="utf-8")
    rules, path = segment_rules.load(target, lambda m: None), target
    assert path == target and len(rules.blockers) == 1


def test_template_roundtrips_and_is_editable():
    notes: list[str] = []
    created, path = segment_rules.write_template(notes.append)
    assert created and path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data[segment_rules.KEY_NOTE]
    rules = _load(data, notes)
    assert not rules.is_default
    assert len(rules.blockers) == len(data[segment_rules.KEY_PHRASES]) + 1
    assert "不许下刀的规则" in rules.summary()


def test_template_never_overwrites(tmp_path):
    target = tmp_path / segment_rules.RULES_FILE_NAME
    target.write_text('{"不切短语": ["用户自己写的"]}', encoding="utf-8")
    notes: list[str] = []
    created, _ = segment_rules.write_template(notes.append, target)
    assert created is False
    assert "没有动它" in notes[0]
    assert target.read_text(encoding="utf-8").startswith('{"不切短语"')


def test_load_accepts_a_bom(tmp_path):
    path = tmp_path / "带BOM.json"
    path.write_bytes(b"\xef\xbb\xbf" + '{"追加句末标点": ["…"]}'.encode("utf-8"))
    assert segment_rules.load(path, lambda m: None).cjk_extra == "…"


# ---------------------------------------------------------------- ⑤ CLI 接线

def test_apply_records_one_broadcast_line(tmp_path, monkeypatch):
    """命令行装载后要留下「生效了什么」那句话，且话里带得上条数与文件名。"""
    import app

    target = tmp_path / segment_rules.RULES_FILE_NAME
    target.write_text(json.dumps({segment_rules.KEY_PHRASES: ["e.g.", "i.e."]},
                                 ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(app, "CLI_SEGMENT_RULES", str(target))
    monkeypatch.setattr(app, "CLI_SEGMENT_NOTE", "上一轮的残留")
    app._apply_segment_rules()
    assert app.CLI_SEGMENT_NOTE.startswith("分段规则：不许下刀的规则 2 条（来自 ")
    assert len(segment_rules.active().blockers) == 2


def test_apply_clears_the_note_without_a_file(tmp_path, monkeypatch):
    """没有规则表时这句必须清空——否则界面会回显一份根本不存在于本次运行的规则。"""
    import app

    monkeypatch.setattr(app, "CLI_SEGMENT_RULES", "")
    monkeypatch.setattr(app, "CLI_SEGMENT_NOTE", "上一轮的残留")
    app._apply_segment_rules()
    assert app.CLI_SEGMENT_NOTE == ""


def test_dump_flag_writes_template_and_exits_zero(tmp_path, monkeypatch, capsys):
    import app

    monkeypatch.setattr(app.segment_rules, "rules_file",
                        lambda: tmp_path / segment_rules.RULES_FILE_NAME)
    monkeypatch.setattr(sys, "argv", ["app", "--dump-segment-rules"])
    with pytest.raises(SystemExit) as exc:
        app.main()
    assert exc.value.code == 0
    assert (tmp_path / segment_rules.RULES_FILE_NAME).exists()
    assert "已生成分段规则模板" in capsys.readouterr().out


def test_dump_flag_refuses_to_clobber(tmp_path, monkeypatch):
    """已有规则表时 --dump-segment-rules 不许覆盖用户写过的东西。"""
    import app

    target = tmp_path / segment_rules.RULES_FILE_NAME
    target.write_text('{"不切短语": ["用户自己写的"]}', encoding="utf-8")
    monkeypatch.setattr(app.segment_rules, "rules_file", lambda: target)
    monkeypatch.setattr(app, "CLI_DUMP_SEGMENT_RULES", False)
    monkeypatch.setattr(sys, "argv", ["app", "--dump-segment-rules", "--version"])
    with pytest.raises(SystemExit) as exc:
        app.main()
    assert exc.value.code == 0
    assert json.loads(target.read_text(encoding="utf-8")) == {"不切短语": ["用户自己写的"]}


def test_broken_rule_file_never_blocks_the_run(tmp_path, monkeypatch, capsys):
    """规则表坏透也要照常出版本号：切句规则写错不该挡住整份翻译。"""
    import app

    bad = tmp_path / "坏规则.json"
    bad.write_text("{这不是 JSON", encoding="utf-8")
    monkeypatch.setattr(app, "CLI_DUMP_SEGMENT_RULES", False)
    monkeypatch.setattr(sys, "argv", ["app", f"--segment-rules={bad}", "--version"])
    with pytest.raises(SystemExit) as exc:
        app.main()
    out = capsys.readouterr().out
    assert exc.value.code == 0
    assert "不是合法 JSON" in out
    assert f"v{app.VERSION}" in out
    assert segment_rules.active().is_default


def test_console_codepage_cannot_break_the_cli_report(tmp_path):
    """回显里带 GBK 装不下的字符时，报告照样打完、退码照样 0。

    v1.8.0 冻结包真机验收抓到的崩溃：规则表把 `›`（U+203A，GBK 没有这个字）加进句末标点，
    回显那一行就带上了它，`print(report)` 在 GBK 控制台上抛 UnicodeEncodeError——
    **成品已经生成好了，用户看到的却是一屏 traceback、退码非 0**。修法是启动时把
    stdout/stderr 的 errors 设成 backslashreplace（编码本身不动，见 `app.py` 头部注释）。
    """
    import os
    import subprocess

    root = Path(__file__).resolve().parent.parent
    rules = tmp_path / segment_rules.RULES_FILE_NAME
    rules.write_text(json.dumps({segment_rules.KEY_LATIN: ["›"]}, ensure_ascii=False),
                     encoding="utf-8")
    sample = tmp_path / "占位.srt"
    sample.write_text("1\n00:00:01,000 --> 00:00:03,000\nAlpha › Beta › Gamma\n\n", encoding="utf-8")
    child = subprocess.run(
        [str(root / ".venv" / "Scripts" / "python.exe"), "app.py", "--offline",
         f"--segment-rules={rules}", "--convert", str(sample)],
        cwd=str(root), capture_output=True, timeout=300,
        env={**os.environ, "PYTHONIOENCODING": "gbk"},
    )
    out = child.stdout.decode("gbk", "replace") + child.stderr.decode("gbk", "replace")
    assert child.returncode == 0, out[-800:]
    assert "UnicodeEncodeError" not in out
    assert "分段规则：须跟空白的标点 1 个" in out


def test_misspelled_segment_flag_still_hard_fails():
    """新开关也受 v1.6.7 的未知参数守卫管：少一个字母不许静默当成文件路径。"""
    import subprocess

    root = Path(__file__).resolve().parent.parent
    child = subprocess.run(
        [str(root / ".venv" / "Scripts" / "python.exe"), "-X", "utf8", "app.py",
         "--segment-rule=x.json"],
        cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )
    out = child.stdout + child.stderr
    assert child.returncode == 4, out[-600:]
    assert "--segment-rules=" in out
