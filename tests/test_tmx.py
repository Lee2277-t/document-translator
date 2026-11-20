"""v1.5.6 TMX 翻译记忆交换（TMX 1.4 最小骨架）的行为钉子。

盯住的东西按优先级：

1. **去重规则**：导入**绝不覆盖**库内既有译文（与 TBX 同口径，全项目两个
   交换模块必须一致），冲突逐条出声并计数；
2. **不配对要看见**：没有中文译文的 tu 既不能被静默丢弃，也不能假装成功——
   它们单独计进 ``unpaired`` 并逐条出声；
3. **roundtrip 自洽**：generate→parse→generate 逐字节稳定（导出文件要能进
   git diff），source/target/src_lang/note 原样回来；
4. **三条判定口径**：方向锁定（zh tuv 恒当译文）、header ``srclang="*all*"``、
   内联标签（``<bpt>/<ept>/<ph>/<ut>`` 子树不收进 seg）；
5. **保密闸门**：TMX 导出会把原文句子写成明文 XML，**独立**开关
   ``--yes-i-know-tmx``（不复用 ``--yes-i-know``），保密模式直接拒绝；
6. **CLI 闭环**：进程内直调（避免 GBK 管道），失败给人话错误 + 非零退出码；
7. **GUI 闭环**（子进程：一个子进程只构造一次 Tk 根，原因见
   test_postedit.py 头注）。
"""

from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from core.state import StateStore  # noqa: E402
from core.tm import TranslationMemory  # noqa: E402
from core.tmx import (  # noqa: E402
    export_segments,
    generate_tmx,
    import_segments,
    parse_tmx,
)

PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

#: ElementTree 里 xml:lang 的完整键名，测试断言导出形状时用。
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

#: 每条的四个字段与 parse_tmx 的输出形状严格一致。
_SEGMENTS = [
    {
        "source": "Please confirm the following terms.",
        "target": "请确认以下条款。",
        "src_lang": "en",
        "note": "来自合同模板",
    },
    {
        "source": "The board approved the merger.",
        "target": "董事会批准了并购。",
        "src_lang": "en",
        "note": "",
    },
    {
        "source": "別添資料を確認してください。",
        "target": "请确认另附资料。",
        "src_lang": "ja",
        "note": "",
    },
]


def _memory(tmp_path: Path) -> TranslationMemory:
    """临时 SQLite 上的记忆对象，不碰项目真库。"""
    return TranslationMemory(StateStore(str(tmp_path / "state.sqlite")))


@pytest.fixture(autouse=True)
def _reset_cli_globals(monkeypatch):
    """每条用例前把 TMX 相关的全局开关复位。

    为什么必须：``app.main()`` 会用 ``global`` 真改这些模块变量（测试
    ``test_main_*`` 因此会污染进程），而本文件里有多条用例依赖"没确认过"
    这个初始状态。``monkeypatch`` 在用例结束后自动还原，不会污染别的文件。
    """
    monkeypatch.setattr(app, "CLI_TMX_ACK", False)
    monkeypatch.setattr(app, "CLI_TMX_IMPORT", None)
    monkeypatch.setattr(app, "CLI_TMX_EXPORT", None)
    monkeypatch.setattr(app, "OFFLINE", False)


def _seed_memory(tmp_path: Path) -> StateStore:
    """在临时库里预置两条记忆（一条引擎产出、一条人工确认），返回库本身。"""
    store = StateStore(str(tmp_path / "state.sqlite"))
    memory = TranslationMemory(store)
    memory.add("Please confirm the following terms.", "请确认以下条款。", src_lang="en")
    memory.confirm("The board approved the merger.", "董事会通过了并购案。")
    return store


def _write_tmx(path: Path, segments: list[dict[str, str]]) -> Path:
    path.write_text(generate_tmx(segments), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 一、roundtrip 与生成形状
# ---------------------------------------------------------------------------


def test_parse_generated_tmx_roundtrips_segments() -> None:
    assert parse_tmx(generate_tmx(_SEGMENTS)) == _SEGMENTS


def test_roundtrip_generate_parse_generate_is_byte_stable() -> None:
    """同一份记忆两次生成必须逐字节一致——头部不带时间戳、TU 按顺序编号。

    变异检验：给 header 加 ``creationdate``/时间戳 → 这条立刻变红。
    """
    first = generate_tmx(_SEGMENTS)
    second = generate_tmx(parse_tmx(first))
    assert second == first


def test_generate_header_has_no_volatile_fields() -> None:
    """头部不许带时间戳这类"每次都变"的属性。

    为什么不只靠上面那条 roundtrip：**同一毫秒内**调两次生成的时间戳可能
    一模一样（Windows 的时钟分辨率约 15ms），纯 roundtrip 断言在这种机器上
    会漏过去。属性名单直接盯源头，与时钟无关，变异检验才可靠。
    """
    root = ET.fromstring(generate_tmx(_SEGMENTS))
    attributes = [key.lower() for key in root.attrib]
    header = root.find("header")
    assert header is not None
    attributes += [key.lower() for key in header.attrib]
    assert not [key for key in attributes if "date" in key or "time" in key]


def test_parse_tmx_from_file_path_str_and_bytes(tmp_path: Path) -> None:
    xml = generate_tmx(_SEGMENTS)
    path = tmp_path / "memory.tmx"
    path.write_text(xml, encoding="utf-8")
    assert parse_tmx(path) == _SEGMENTS  # Path
    assert parse_tmx(str(path)) == _SEGMENTS  # str 路径
    assert parse_tmx(xml.encode("utf-8")) == _SEGMENTS  # 字节


def test_generate_tmx_shape_is_valid_xml() -> None:
    root = ET.fromstring(generate_tmx(_SEGMENTS))
    assert root.tag == "tmx" and root.get("version") == "1.4"
    header = root.find("header")
    assert header is not None
    # header 的 srclang 恒为 *all*：我方记忆库混合源语言，逐个 tu 标真实语言
    assert header.get("srclang") == "*all*"
    tus = root.findall("body/tu")
    assert len(tus) == len(_SEGMENTS)
    # 每个 tu 自己标真实 srclang（不是一律 en：第三条原文是日文）
    assert [tu.get("srclang") for tu in tus] == ["en", "en", "ja"]
    langs = [tuv.get(_XML_LANG) for tu in tus for tuv in tu.findall("tuv")]
    assert langs == ["en", "zh-Hans", "en", "zh-Hans", "ja", "zh-Hans"]


def test_generate_tmx_accepts_plain_sequences() -> None:
    xml = generate_tmx([("Agreement", "协议", "en"), ("Revenue", "收入", "en", "财务")])
    assert parse_tmx(xml) == [
        {"source": "Agreement", "target": "协议", "src_lang": "en", "note": ""},
        {"source": "Revenue", "target": "收入", "src_lang": "en", "note": "财务"},
    ]


def test_generate_tmx_honours_target_language_argument() -> None:
    xml = generate_tmx([_SEGMENTS[0]], tgt_lang="zh-Hant")
    assert parse_tmx(xml)[0] == {**_SEGMENTS[0], "target": "请确认以下条款。"}
    assert "zh-Hant" in xml


# ---------------------------------------------------------------------------
# 二、容错：不许崩，也不要静默吞掉明显不是 TMX 的文件
# ---------------------------------------------------------------------------


def test_parse_tolerates_missing_header_empty_tu_and_comments() -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- 某 CAT 工具导出的记忆 -->\n"
        '<tmx version="1.4">\n'  # 没有 header
        "  <body>\n"
        "    <tu></tu>\n"  # 空 TU
        '    <tu srclang="en">'
        '<tuv xml:lang="en"><seg>Agreement</seg></tuv>'
        '<tuv xml:lang="zh-Hans"><seg>协议</seg></tuv>'
        "</tu>\n"
        "  </body>\n"
        "</tmx>"
    )
    assert parse_tmx(xml) == [
        {"source": "Agreement", "target": "协议", "src_lang": "en", "note": ""}
    ]


def test_parse_reads_note_and_skips_incomplete_body() -> None:
    xml = (
        '<tmx version="1.4">'
        '<tu srclang="en">'
        '<tuv xml:lang="en"><seg>Agreement</seg></tuv>'
        '<tuv xml:lang="zh-Hans"><seg>协议</seg></tuv>'
        "<note>来自法务部术语例会</note>"
        "</tu>"
        "</tmx>"
    )
    assert parse_tmx(xml) == [
        {"source": "Agreement", "target": "协议", "src_lang": "en", "note": "来自法务部术语例会"}
    ]


def test_parse_multiple_tuvs_takes_zh_as_target() -> None:
    """方向锁定：不管 zh 排在第几位，它都是译文（与 TBX 同口径）。"""
    xml = (
        '<tmx version="1.4"><body><tu srclang="en">'
        '<tuv xml:lang="en"><seg>Agreement</seg></tuv>'
        '<tuv xml:lang="ja"><seg>契約</seg></tuv>'
        '<tuv xml:lang="zh-Hans"><seg>协议</seg></tuv>'
        "</tu></body></tmx>"
    )
    assert parse_tmx(xml) == [
        {"source": "Agreement", "target": "协议", "src_lang": "en", "note": ""}
    ]


def test_parse_reverse_direction_is_interpreted_as_zh_target() -> None:
    """反向文件（zh 在前、英文在后）也按「zh 恒为译文」解读——已知局限。

    中译英方向留给路线图 v1.5.6（届时 TBX / TMX 一起扩展语言角色判定）。
    这条测试防止将来有人「顺手支持反向」而悄悄改变现有语义。
    """
    reverse = (
        '<tmx version="1.4"><body><tu srclang="zh-Hans">'
        '<tuv xml:lang="zh-Hans"><seg>协议</seg></tuv>'
        '<tuv xml:lang="en"><seg>Agreement</seg></tuv>'
        "</tu></body></tmx>"
    )
    assert parse_tmx(reverse) == [
        {"source": "Agreement", "target": "协议", "src_lang": "zh-Hans", "note": ""}
    ]


def test_parse_keeps_tu_without_chinese_tuv_as_unpaired(tmp_path: Path) -> None:
    """没有中文 tuv 的 tu **不能被静默丢弃**——它的身份要能算进 unpaired。

    变异检验：改成跳过这类 tu（不返回 target="" 的行）→ 这条与
    ``test_import_counts_unpaired_as_not_added`` 同时变红。
    """
    xml = (
        '<tmx version="1.4"><body>'
        '<tu srclang="en"><tuv xml:lang="en"><seg>Only source here.</seg></tuv></tu>'
        '<tu srclang="en"><tuv xml:lang="en"><seg>Agreement</seg></tuv>'
        '<tuv xml:lang="zh-Hans"><seg>协议</seg></tuv></tu>'
        "</body></tmx>"
    )
    assert parse_tmx(xml) == [
        {"source": "Only source here.", "target": "", "src_lang": "en", "note": ""},
        {"source": "Agreement", "target": "协议", "src_lang": "en", "note": ""},
    ]


def test_parse_reads_src_lang_all_as_unknown() -> None:
    """``*all*``（混合语言/未知）读成空串——"不知道"就是不知道，不猜。

    tu 级 srclang 也是 ``*all*`` 但 tuv 上标了具体语言时，取具体的那个：
    它比通配符信息量大，没有理由丢掉。
    """
    both_unknown = (
        '<tmx version="1.4"><body><tu srclang="*all*">'
        '<tuv xml:lang="*all*"><seg>Agreement</seg></tuv>'
        '<tuv xml:lang="zh-Hans"><seg>协议</seg></tuv>'
        "</tu></body></tmx>"
    )
    assert parse_tmx(both_unknown)[0]["src_lang"] == ""
    declared_but_tuv_knows = (
        '<tmx version="1.4"><body><tu srclang="*all*">'
        '<tuv xml:lang="en"><seg>Agreement</seg></tuv>'
        '<tuv xml:lang="zh-Hans"><seg>协议</seg></tuv>'
        "</tu></body></tmx>"
    )
    assert parse_tmx(declared_but_tuv_knows)[0]["src_lang"] == "en"


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("broken xml", "<不是 XML <><>"),
        ("html root", "<html><body>网页</body></html>"),
    ],
)
def test_parse_rejects_non_tmx_with_human_error(label: str, payload: str) -> None:
    with pytest.raises(ValueError, match="不是有效的 TMX"):
        parse_tmx(payload)


def test_parse_missing_file_says_so(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="不存在或不可读"):
        parse_tmx(str(tmp_path / "nope.tmx"))


def test_parse_gbk_encoded_file(tmp_path: Path) -> None:
    """国内 Windows 手工保存的 TMX 常是 GBK——编码问题不该判成格式问题。"""
    gbk = (
        '<?xml version="1.0" encoding="GBK"?><tmx version="1.4"><body><tu srclang="en">'
        '<tuv xml:lang="en"><seg>Agreement</seg></tuv>'
        '<tuv xml:lang="zh-Hans"><seg>协议</seg></tuv>'
        "</tu></body></tmx>"
    ).encode("gbk")
    path = tmp_path / "gbk.tmx"
    path.write_bytes(gbk)
    assert parse_tmx(path) == [
        {"source": "Agreement", "target": "协议", "src_lang": "en", "note": ""}
    ]


# ---------------------------------------------------------------------------
# 三、内联标签口径（第三号判定口径）
# ---------------------------------------------------------------------------


def test_parse_excludes_marker_tags_from_seg(tmp_path: Path) -> None:
    """``<bpt>/<ept>/<ph>/<ut>`` 承载的是标记本身的文字，不能进记忆。

    变异检验：把这四个子树也算进 seg（改用 itertext）→ 这条变红。
    """
    xml = (
        '<tmx version="1.4"><body><tu srclang="en">'
        '<tuv xml:lang="en"><seg>Press <bpt i="1" type="bold">&lt;b&gt;</bpt>OK'
        '<ept i="1">&lt;/b&gt;</ept> to continue.'
        '<ph id="2">&lt;br/&gt;</ph></seg></tuv>'
        '<tuv xml:lang="zh-Hans"><seg>点击确定继续。</seg></tuv>'
        "</tu></body></tmx>"
    )
    parsed = parse_tmx(xml)
    assert parsed[0]["source"] == "Press OK to continue."
    assert "<b>" not in parsed[0]["source"]
    assert "<br/>" not in parsed[0]["source"]


def test_parse_keeps_highlight_style_tags_in_seg() -> None:
    """``<hi>/<it>`` 包的是真正的文本，要与纯文本一样收进来。"""
    xml = (
        '<tmx version="1.4"><body><tu srclang="en">'
        '<tuv xml:lang="en"><seg>Read the <hi>terms</hi> first.</seg></tuv>'
        '<tuv xml:lang="zh-Hans"><seg>请先阅读条款。</seg></tuv>'
        "</tu></body></tmx>"
    )
    assert parse_tmx(xml)[0]["source"] == "Read the terms first."


# ---------------------------------------------------------------------------
# 四、导入去重：用户既有资产不容外部文件覆盖
# ---------------------------------------------------------------------------


def test_import_adds_new_segments_as_tmx_origin(tmp_path: Path) -> None:
    store = StateStore(str(tmp_path / "state.sqlite"))
    memory = TranslationMemory(store)
    result = import_segments(memory, _SEGMENTS)
    assert result == {
        "added": 3,
        "kept": 0,
        "skipped": 0,
        "conflicts": [],
        "unpaired": 0,
    }
    rows = store.all_tm_full()
    assert [row["origin"] for row in rows] == ["tmx", "tmx", "tmx"]
    assert all(row["confirmed"] is False for row in rows)
    assert memory.lookup("Please confirm the following terms.") == "请确认以下条款。"


def test_import_keeps_in_library_value_on_conflict(tmp_path: Path) -> None:
    """变异检验（本轮最重要的一颗钉子）：改成「覆盖」立刻变红。"""
    store = StateStore(str(tmp_path / "state.sqlite"))
    memory = TranslationMemory(store)
    memory.add("Please confirm the following terms.", "我先前定好的译法")
    memory.confirm("The board approved the merger.", "董事会通过了并购案。")
    logs: list[str] = []
    result = import_segments(
        memory,
        [
            {"source": "Please confirm the following terms.", "target": "外部译法一"},
            {"source": "The board approved the merger.", "target": "外部译法二"},
        ],
        log=logs.append,
    )
    assert result["added"] == 0
    assert result["kept"] == 2
    # 库内原值一动不动（写回之后还要再验一次）
    assert store.get_tm("Please confirm the following terms.") == "我先前定好的译法"
    assert store.get_tm("The board approved the merger.") == "董事会通过了并购案。"
    assert sorted(result["conflicts"]) == [
        ("Please confirm the following terms.", "我先前定好的译法", "外部译法一"),
        ("The board approved the merger.", "董事会通过了并购案。", "外部译法二"),
    ]
    # 冲突逐条出声，绝不静默
    assert len(logs) == 2
    assert "我先前定好的译法" in logs[0]


def test_import_counts_unpaired_as_not_added(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    logs: list[str] = []
    result = import_segments(
        memory,
        [{"source": "Only source here.", "target": "", "src_lang": "en"}],
        log=logs.append,
    )
    assert result["unpaired"] == 1
    assert result["added"] == 0
    assert logs and "没有中文译文" in logs[0]
    assert memory.store.count_tm() == 0


def test_import_skips_entries_without_source(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    result = import_segments(
        memory,
        [{"source": "", "target": "孤儿译文"}, {"source": "  ", "target": "量子"}],
    )
    assert result == {
        "added": 0,
        "kept": 0,
        "skipped": 2,
        "conflicts": [],
        "unpaired": 0,
    }
    assert memory.store.count_tm() == 0


def test_import_treats_target_equal_to_source_as_uninformative(tmp_path: Path) -> None:
    """译文与原文逐字相同：没有记忆价值，不能混进 added 里充数。"""
    memory = _memory(tmp_path)
    result = import_segments(
        memory, [{"source": "OK", "target": "OK", "src_lang": "en"}]
    )
    assert result["unpaired"] == 1 and result["added"] == 0
    assert memory.store.count_tm() == 0


def test_import_accepts_plain_sequences(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    result = import_segments(memory, [("Agreement", "协议", "en", "备注")])
    assert result["added"] == 1
    row = memory.store.all_tm_full()[0]
    assert row["src_lang"] == "en"


# ---------------------------------------------------------------------------
# 五、导出口径（与 TBX 故意不同：默认全导）
# ---------------------------------------------------------------------------


def test_export_segments_exports_everything_by_default(tmp_path: Path) -> None:
    """记忆的入库门槛本来就是「零问题遗留」，所以默认全导——含引擎产出。"""
    _seed_memory(tmp_path)
    store = StateStore(str(tmp_path / "state.sqlite"))
    exported = export_segments(store)
    assert [item["source"] for item in exported] == [
        "Please confirm the following terms.",
        "The board approved the merger.",
    ]  # 按 source 排序：导出文件稳定可 diff
    assert [item["target"] for item in exported] == [
        "请确认以下条款。",
        "董事会通过了并购案。",
    ]
    assert all(item["note"] == "" for item in exported)


def test_export_segments_only_confirmed_filters_the_rest(tmp_path: Path) -> None:
    _seed_memory(tmp_path)
    store = StateStore(str(tmp_path / "state.sqlite"))
    exported = export_segments(store, only_confirmed=True)
    assert [item["source"] for item in exported] == ["The board approved the merger."]


def test_export_segments_roundtrip_through_tmx(tmp_path: Path) -> None:
    _seed_memory(tmp_path)
    store = StateStore(str(tmp_path / "state.sqlite"))
    exported = export_segments(store)
    assert parse_tmx(generate_tmx(exported)) == exported


def test_export_segments_skips_uninformative_rows(tmp_path: Path) -> None:
    store = StateStore(str(tmp_path / "state.sqlite"))
    store.upsert_tm("配好对的原文", "有译文")
    store.upsert_tm("缺原文的", "")
    exported = export_segments(store)
    assert [item["source"] for item in exported] == ["配好对的原文"]


# ---------------------------------------------------------------------------
# 六、CLI 闭环（进程内直调，CACHE_DIR 指到临时库）
# ---------------------------------------------------------------------------


def _seed_cache(tmp_path: Path) -> Path:
    """临时 cache 目录 + 名为 translations.sqlite 的记忆库（与 CLI 同一路径）。"""
    cache = tmp_path / "cache"
    memory = TranslationMemory(StateStore(str(cache / "translations.sqlite")))
    memory.add("Please confirm the following terms.", "请确认以下条款。", src_lang="en")
    memory.confirm("The board approved the merger.", "董事会通过了并购案。")
    return cache


def test_cli_tmx_import_adds_and_reports_conflict(tmp_path, monkeypatch, capsys) -> None:
    cache = _seed_cache(tmp_path)
    monkeypatch.setattr(app, "CACHE_DIR", cache)
    tmx = _write_tmx(
        tmp_path / "in.tmx",
        [
            {
                "source": "Please confirm the following terms.",
                "target": "外部译法",
                "src_lang": "en",
                "note": "",
            },
            {
                "source": "New sentence from outside.",
                "target": "来自外部的译文。",
                "src_lang": "en",
                "note": "",
            },
        ],
    )
    code = app._run_tmx_import(str(tmx))
    assert code == 0
    printed = capsys.readouterr().out
    assert "新增 1 条" in printed
    assert "冲突保留 1 条" in printed
    assert "请确认以下条款。" in printed  # 保留值看得见

    store = StateStore(str(cache / "translations.sqlite"))
    assert store.get_tm("New sentence from outside.") == "来自外部的译文。"
    assert store.get_tm("Please confirm the following terms.") == "请确认以下条款。"
    store.close()


def test_cli_tmx_import_reports_unpaired(tmp_path, monkeypatch, capsys) -> None:
    cache = _seed_cache(tmp_path)
    monkeypatch.setattr(app, "CACHE_DIR", cache)
    tmx = _write_tmx(
        tmp_path / "in.tmx",
        [{"source": "No chinese target.", "target": "", "src_lang": "en", "note": ""}],
    )
    assert app._run_tmx_import(str(tmx)) == 0
    assert "无中文译文 1 条" in capsys.readouterr().out


def test_cli_tmx_import_missing_file(tmp_path, capsys) -> None:
    assert app._run_tmx_import(str(tmp_path / "nope.tmx")) == 1
    assert "文件不存在" in capsys.readouterr().out


def test_cli_tmx_import_garbage_file(tmp_path, capsys) -> None:
    bad = tmp_path / "bad.tmx"
    bad.write_text("<html>不是翻译记忆</html>", encoding="utf-8")
    assert app._run_tmx_import(str(bad)) == 1
    assert "不是有效的 TMX" in capsys.readouterr().out


def test_cli_tmx_export_requires_its_own_ack(tmp_path, monkeypatch, capsys) -> None:
    """``--yes-i-know`` 是「内容出境给 DeepL」，本项是「内容落盘」——不能共用。

    变异检验：把闸门换成读 CLI_YES_I_KNOW → 这条变红。
    """
    cache = _seed_cache(tmp_path)
    monkeypatch.setattr(app, "CACHE_DIR", cache)
    monkeypatch.setattr(app, "CLI_YES_I_KNOW", True)  # 只为付费档点过，不顶用
    out = tmp_path / "out.tmx"
    assert app._run_tmx_export(str(out)) == 2
    assert "yes-i-know-tmx" in capsys.readouterr().out
    assert not out.exists()


def test_cli_tmx_export_writes_file_after_ack(tmp_path, monkeypatch, capsys) -> None:
    cache = _seed_cache(tmp_path)
    monkeypatch.setattr(app, "CACHE_DIR", cache)
    monkeypatch.setattr(app, "CLI_TMX_ACK", True)
    out = tmp_path / "out" / "memory.tmx"
    assert app._run_tmx_export(str(out)) == 0
    printed = capsys.readouterr().out
    assert "TMX 导出完成" in printed
    assert out.is_file()
    parsed = parse_tmx(out)
    assert [item["source"] for item in parsed] == [
        "Please confirm the following terms.",
        "The board approved the merger.",
    ]


def test_cli_tmx_export_refused_in_offline_mode(tmp_path, monkeypatch, capsys) -> None:
    """保密模式零落盘：TMX 导出把原文写成明文 XML，一律不放行。

    变异检验：去掉这道闸门 → 这条变红（保密模式就会出现敏感内容明文落盘）。
    """
    cache = _seed_cache(tmp_path)
    monkeypatch.setattr(app, "CACHE_DIR", cache)
    monkeypatch.setattr(app, "OFFLINE", True)
    monkeypatch.setattr(app, "CLI_TMX_ACK", True)  # 确认了也不行
    out = tmp_path / "offline.tmx"
    assert app._run_tmx_export(str(out)) == 2
    printed = capsys.readouterr().out
    assert "保密模式不导出翻译记忆" in printed
    assert not out.exists()


def test_cli_tmx_export_empty_library(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(app, "CACHE_DIR", tmp_path / "empty-cache")
    monkeypatch.setattr(app, "CLI_TMX_ACK", True)
    out = tmp_path / "empty.tmx"
    assert app._run_tmx_export(str(out)) == 0
    assert "还是空的" in capsys.readouterr().out
    assert parse_tmx(out) == []  # 空库也写一个结构合法的文件


def test_main_dispatches_tmx_import(tmp_path, monkeypatch) -> None:
    cache = _seed_cache(tmp_path)
    monkeypatch.setattr(app, "CACHE_DIR", cache)
    monkeypatch.setattr(app, "CLI_TMX_IMPORT", None)
    tmx = _write_tmx(
        tmp_path / "in.tmx",
        [{"source": "Quantum tunneling.", "target": "量子隧穿。", "src_lang": "en", "note": ""}],
    )
    monkeypatch.setattr(sys, "argv", ["app.py", f"--tmx-import={tmx}"])
    with pytest.raises(SystemExit) as excinfo:
        app.main()
    assert excinfo.value.code == 0
    store = StateStore(str(cache / "translations.sqlite"))
    assert store.get_tm("Quantum tunneling.") == "量子隧穿。"
    store.close()


def test_main_dispatches_tmx_export(tmp_path, monkeypatch) -> None:
    cache = _seed_cache(tmp_path)
    monkeypatch.setattr(app, "CACHE_DIR", cache)
    monkeypatch.setattr(app, "CLI_TMX_EXPORT", None)
    monkeypatch.setattr(app, "CLI_TMX_ACK", True)
    out = tmp_path / "out.tmx"
    monkeypatch.setattr(sys, "argv", ["app.py", f"--tmx-export={out}"])
    with pytest.raises(SystemExit) as excinfo:
        app.main()
    assert excinfo.value.code == 0
    assert out.is_file()
    assert len(parse_tmx(out)) == 2


# ---------------------------------------------------------------------------
# 七、GUI 窗口（子进程：一个子进程只构造一次 Tk 根）
# ---------------------------------------------------------------------------

_CHILD_GUI = """\
import sys, tempfile
from pathlib import Path

sys.argv = ["app.py"]

import app
from core.state import StateStore
from core.tm import TranslationMemory
from core.tmx import generate_tmx, parse_tmx

work = Path(tempfile.mkdtemp())
store = StateStore(str(work / "state.sqlite"))
memory = TranslationMemory(store)
memory.add("Please confirm the following terms.", "请确认以下条款。", src_lang="en")

# 弹窗一律吞掉（headless 没人点确定）
for name in ("showinfo", "showwarning", "showerror"):
    setattr(app.messagebox, name, lambda *a, **k: None)

app._enable_dpi_awareness()
application = app.TranslatorApp()
window = app.TermbaseWindow(application, store=store)
window.window.update()
problems = []

# 1. 翻译记忆分区在：两个按钮 + 条数
if not hasattr(window, "memory_label"):
    problems.append("没有 memory_label")
if str(window.memory_label.cget("text")).find("1 条") < 0:
    problems.append(f"条数不对：{window.memory_label.cget('text')!r}")

# 2. 导入 TMX：新条目进库、冲突保留库内原值
in_path = work / "in.tmx"
in_path.write_text(generate_tmx([
    {"source": "Please confirm the following terms.", "target": "外部译法", "src_lang": "en", "note": ""},
    {"source": "New line from TMX.", "target": "来自 TMX 的新行。", "src_lang": "en", "note": ""},
]), encoding="utf-8")
app.filedialog.askopenfilename = lambda **k: str(in_path)
window._import_tmx()
window.window.update()
if store.get_tm("New line from TMX.") != "来自 TMX 的新行。":
    problems.append("TMX 新条目没进记忆库")
if store.get_tm("Please confirm the following terms.") != "请确认以下条款。":
    problems.append("导入覆盖了库内资产")
if str(window.memory_label.cget("text")).find("2 条") < 0:
    problems.append("导入后条数没刷新")

# 3. 导出 TMX：二次确认后写盘，能解析回来
out_path = work / "out.tmx"
app.filedialog.asksaveasfilename = lambda **k: str(out_path)
app.messagebox.askyesno = lambda *a, **k: True
window._export_tmx()
window.window.update()
if not out_path.is_file():
    problems.append("导出文件没写")
entries = parse_tmx(out_path)
if {e["source"] for e in entries} != {
    "Please confirm the following terms.", "New line from TMX."
}:
    problems.append(f"导出条目不对：{[e['source'] for e in entries]}")

# 4. 保密模式：导出被拒（连 file dialog 都不该出现）
app.OFFLINE = True
called = []
app.filedialog.asksaveasfilename = lambda **k: (called.append(1), str(work / "bad.tmx"))[1]
window._export_tmx()
window.window.update()
if called:
    problems.append("保密模式还弹了保存对话框")
if not (work / "bad.tmx").exists():
    pass  # 期望不写盘：文件不存在是对的
else:
    problems.append("保密模式竟然写出了文件")
app.OFFLINE = False

if problems:
    print("FAIL:" + ";".join(problems))
    sys.exit(1)
print("TMX_GUI_OK")
"""


def test_gui_tmx_import_export_in_termbase_window() -> None:
    result = subprocess.run(
        [str(PYTHON), "-c", _CHILD_GUI],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    assert "TMX_GUI_OK" in result.stdout, (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
