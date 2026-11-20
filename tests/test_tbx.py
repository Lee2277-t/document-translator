"""v1.5.5 TBX 术语库交换（ISO 30042 最小骨架）的行为钉子。

盯住的东西按优先级：

1. **roundtrip 自洽**：generate→parse→generate 逐字节稳定，条目
   （source/target/note）原样回来——导出的文件再导回来不能变味；
2. **容错不崩**：缺 descrip、缺 martifHeader、多 langSet、空 termEntry、
   XML 声明/注释、GBK 编码，全部正常处理或安静跳过；明显不是 TBX 的
   文件抛 ValueError 说人话，由调用方（GUI 弹窗 / CLI 打印）负责出声；
3. **导入不覆盖用户裁决**：已存在条目（含 rejected / disputed）一律保留
   库内原值，冲突逐条出声并计数——外部 TBX 说啥都不算数；
4. **导出只带已采用**：disputed / candidate 是未决状态，不随导出推给下游；
5. **CLI 闭环**：--tbx-import / --tbx-export 进 main() 分发，失败给人话
   错误 + 非零退出码（进程内直调，不起子进程避免 GBK 管道，同
   test_postedit.py 的 CLI 用例姿势）；
6. **GUI 闭环**（子进程：一个子进程只构造一次 Tk 根，原因见
   test_postedit.py 头注）：未决优先排序、字段填充、采用/改名/拒绝落库、
   TBX 导入导出走 filedialog 桩。
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
from core.tbx import export_entries, generate_tbx, import_entries, parse_tbx  # noqa: E402
from core.termbase import Termbase  # noqa: E402

PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

#: ElementTree 里 xml:lang 的完整键名，测试断言导出形状时用。
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

_ENTRIES = [
    {"source": "bank", "target": "银行", "note": "金融机构"},
    {"source": "quarter", "target": "季度", "note": ""},
    {"source": "AI", "target": "", "note": "待定"},
]


def _store(tmp_path: Path) -> StateStore:
    """临时 SQLite 状态库，不碰项目真库。"""
    return StateStore(str(tmp_path / "state.sqlite"))


# ---------------------------------------------------------------------------
# 一、roundtrip 与生成形状
# ---------------------------------------------------------------------------


def test_parse_generated_tbx_roundtrips_entries() -> None:
    xml = generate_tbx(_ENTRIES)
    assert parse_tbx(xml) == _ENTRIES


def test_roundtrip_generate_parse_generate_is_byte_stable() -> None:
    """同一份条目两次生成必须逐字节一致——头部不带时间戳、id 按序编号。"""
    first = generate_tbx(_ENTRIES)
    second = generate_tbx(parse_tbx(first))
    assert second == first


def test_parse_tbx_from_file_path_and_text(tmp_path: Path) -> None:
    xml = generate_tbx(_ENTRIES)
    path = tmp_path / "terms.tbx"
    path.write_text(xml, encoding="utf-8")
    assert parse_tbx(path) == _ENTRIES  # Path
    assert parse_tbx(str(path)) == _ENTRIES  # str 路径
    assert parse_tbx(xml.encode("utf-8")) == _ENTRIES  # 字节


def test_generate_tbx_shape_is_valid_xml() -> None:
    root = ET.fromstring(generate_tbx(_ENTRIES))
    assert root.tag == "martif" and root.get("type") == "TBX"
    assert root.find("martifHeader") is not None  # 头部不缺席
    langs = [lang_set.get(_XML_LANG) for lang_set in root.iter("langSet")]
    assert langs == ["en", "zh"] * len(_ENTRIES)  # 先源后译，条目级成对


def test_generate_tbx_accepts_plain_tuples() -> None:
    xml = generate_tbx([("bank", "银行"), ("quarter", "季度", "财务")])
    assert parse_tbx(xml) == [
        {"source": "bank", "target": "银行", "note": ""},
        {"source": "quarter", "target": "季度", "note": "财务"},
    ]


# ---------------------------------------------------------------------------
# 二、容错：不许崩，也不要静默吞掉明显不是 TBX 的文件
# ---------------------------------------------------------------------------


def test_parse_tolerates_missing_descrip_header_and_comments() -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- 导出工具留下的注释 -->\n"
        '<martif type="TBX">\n'
        "  <body>\n"  # 没有 text 层、没有 martifHeader
        '    <termEntry>\n'
        '      <langSet xml:lang="en"><tig><term>bank</term></tig></langSet>\n'
        '      <langSet xml:lang="zh"><tig><term>银行</term></tig></langSet>\n'
        "    </termEntry>\n"
        "  </body>\n"
        "</martif>"
    )
    assert parse_tbx(xml) == [{"source": "bank", "target": "银行", "note": ""}]


def test_parse_skips_empty_term_entry() -> None:
    xml = (
        '<martif type="TBX"><body>'
        "<termEntry></termEntry>"
        '<termEntry><langSet xml:lang="en"><tig></tig></langSet></termEntry>'
        '<termEntry><langSet xml:lang="en"><tig><term>bank</term></tig></langSet>'
        '<langSet xml:lang="zh"><tig><term>银行</term></tig></langSet></termEntry>'
        "</body></martif>"
    )
    assert parse_tbx(xml) == [{"source": "bank", "target": "银行", "note": ""}]


def test_parse_multiple_langsets_uses_zh_as_target() -> None:
    xml = (
        '<martif type="TBX"><body><termEntry>'
        '<langSet xml:lang="en"><tig><term>bank</term></tig></langSet>'
        '<langSet xml:lang="ja"><tig><term>銀行</term></tig></langSet>'
        '<langSet xml:lang="zh"><tig><term>银行</term></tig></langSet>'
        "</termEntry></body></martif>"
    )
    assert parse_tbx(xml) == [{"source": "bank", "target": "银行", "note": ""}]


def test_parse_without_zh_falls_back_to_position() -> None:
    xml = (
        '<martif type="TBX"><body><termEntry>'
        '<langSet xml:lang="en"><tig><term>bank</term></tig></langSet>'
        '<langSet xml:lang="fr"><tig><term>banque</term></tig></langSet>'
        "</termEntry></body></martif>"
    )
    assert parse_tbx(xml) == [{"source": "bank", "target": "banque", "note": ""}]


def test_parse_single_langset_has_empty_target() -> None:
    xml = (
        '<martif type="TBX"><body><termEntry>'
        '<langSet xml:lang="en"><tig><term>bank</term>'
        '<descrip type="note">只有源词</descrip></tig></langSet>'
        "</termEntry></body></martif>"
    )
    assert parse_tbx(xml) == [{"source": "bank", "target": "", "note": "只有源词"}]


def test_parse_reverse_language_pair_is_interpreted_as_zh_target() -> None:
    """反向 TBX（zh 放源词、en 放译名）按「zh 恒为译名」解读——当前设计语义。

    术语库语义恒为「译入中文」，parse 不猜语言角色：zh 语言集一律当译名。
    所以这张表 parse 回来源词是 en 侧的 "bank"、译名是 zh 侧的「银行」，
    而不是按标签以为的「银行 → bank」。这是**已知局限**（中译英方向留给
    路线图 v1.5.6 扩展语言角色判定，见 core/tbx.py 模块 docstring 的
    「方向锁定」段），不是 bug——这条测试把它钉住，防止将来「顺手支持
    反向」时悄悄改变现有语义。
    """
    reverse = (
        '<martif type="TBX"><body><termEntry>'
        '<langSet xml:lang="zh"><tig><term>银行</term></tig></langSet>'
        '<langSet xml:lang="en"><tig><term>bank</term></tig></langSet>'
        "</termEntry></body></martif>"
    )
    assert parse_tbx(reverse) == [{"source": "bank", "target": "银行", "note": ""}]


def test_parse_reads_term_note_variant() -> None:
    """备注除了 descrip，也认 tig 下的 termNote（两种导出工具都见过）。"""
    xml = (
        '<martif type="TBX"><body><termEntry>'
        '<langSet xml:lang="en"><tig><term>bank</term></tig></langSet>'
        '<langSet xml:lang="zh"><tig><term>银行</term>'
        '<termNote type="note">金融机构</termNote></tig></langSet>'
        "</termEntry></body></martif>"
    )
    assert parse_tbx(xml) == [{"source": "bank", "target": "银行", "note": "金融机构"}]


def test_parse_entry_level_descrip_is_note() -> None:
    xml = (
        '<martif type="TBX"><body><termEntry>'
        '<descrip type="note">条目级备注</descrip>'
        '<langSet xml:lang="en"><tig><term>bank</term></tig></langSet>'
        '<langSet xml:lang="zh"><tig><term>银行</term></tig></langSet>'
        "</termEntry></body></martif>"
    )
    assert parse_tbx(xml) == [{"source": "bank", "target": "银行", "note": "条目级备注"}]


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("broken xml", "<不是 XML <><>"),
        ("html root", "<html><body>网页</body></html>"),
    ],
)
def test_parse_rejects_non_tbx_with_human_error(label: str, payload: str) -> None:
    with pytest.raises(ValueError, match="不是有效的 TBX"):
        parse_tbx(payload)


def test_parse_wrong_root_element_names_martif() -> None:
    with pytest.raises(ValueError, match="根元素是 <html>"):
        parse_tbx("<html><body>网页</body></html>")


def test_parse_missing_file_says_so(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="不存在或不可读"):
        parse_tbx(str(tmp_path / "nope.tbx"))


def test_parse_gbk_encoded_file(tmp_path: Path) -> None:
    """国内 Windows 手工保存的 TBX 常是 GBK——编码问题不该判成格式问题。"""
    gbk = (
        '<?xml version="1.0" encoding="GBK"?><martif type="TBX"><body><termEntry>'
        '<langSet xml:lang="en"><tig><term>bank</term></tig></langSet>'
        '<langSet xml:lang="zh"><tig><term>银行</term></tig></langSet>'
        "</termEntry></body></martif>"
    ).encode("gbk")
    path = tmp_path / "gbk.tbx"
    path.write_bytes(gbk)
    assert parse_tbx(path) == [{"source": "bank", "target": "银行", "note": ""}]


# ---------------------------------------------------------------------------
# 三、导入去重：用户既有裁决不容外部文件覆盖
# ---------------------------------------------------------------------------


def test_import_adds_new_entries_and_candidates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    termbase = Termbase(store)
    result = import_entries(termbase, _ENTRIES)
    assert result["added"] == 3
    assert result["kept"] == 0
    assert result["skipped"] == 0
    assert result["conflicts"] == []
    assert termbase.lookup("bank") == "银行"
    # 没有译名的条目按候选进未决池，不硬编一个译名
    candidate = store.get_term("AI")
    assert candidate["state"] == "candidate"


def test_import_keeps_user_adjudication_on_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path)
    termbase = Termbase(store)
    termbase.adopt("bank", "河岸", note="我拍板的")
    termbase.add_candidate("quarter")  # 先成为候选，再人工拒绝
    termbase.reject("quarter", "确认不是术语")
    logs: list[str] = []
    result = import_entries(
        termbase,
        [
            {"source": "bank", "target": "银行", "note": "外部"},
            {"source": "quarter", "target": "季度", "note": "外部"},
        ],
        log=logs.append,
    )
    assert result["added"] == 0
    assert result["kept"] == 2
    assert result["skipped"] == 0
    assert result["conflicts"] == [
        ("bank", "河岸", "银行"),
        ("quarter", "（未定）", "季度"),
    ]
    # 库内原值一动不动：adopted 的译名、rejected 的状态都不被覆盖
    assert store.get_term("bank")["target"] == "河岸"
    assert store.get_term("quarter")["state"] == "rejected"
    assert store.get_term("quarter")["target"] == ""
    # 冲突逐条出声，不静默
    assert any("bank" in line and "河岸" in line for line in logs)


def test_import_skips_entries_without_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    termbase = Termbase(store)
    result = import_entries(
        termbase, [{"source": "", "target": "银行"}, {"source": "  ", "target": "量子"}]
    )
    assert result == {"added": 0, "kept": 0, "skipped": 2, "conflicts": []}
    assert store.all_terms() == []


# ---------------------------------------------------------------------------
# 四、导出范围：只带已采用
# ---------------------------------------------------------------------------


def test_export_entries_adopted_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    termbase = Termbase(store)
    termbase.adopt("bank", "河岸", note="我拍板的")
    termbase.dispute("quarter", "两种译法")
    termbase.add_candidate("AI")  # 候选、无译名
    termbase.reject("foo", "不是术语")
    exported = export_entries(store)
    assert exported == [{"source": "bank", "target": "河岸", "note": "我拍板的"}]


def test_export_entries_default_excludes_disputed_with_target(tmp_path: Path) -> None:
    """默认口径只导 adopted：**带译名的存疑条目**也不漏。

    上面那条的存疑/拒绝样例是「源词从未进过库」的空转（update_state 对
    不存在条目是 no-op），守不住默认 states——变异检验（把默认值改成
    ("adopted", "disputed")）那条全绿。存疑条目带译名是真实形态：模型
    给了两种译法、人工标疑后既定译名还在库里，必须能用默认口径挡住。
    """
    store = _store(tmp_path)
    termbase = Termbase(store)
    termbase.adopt("bank", "河岸", note="我拍板的")
    store.upsert_term("quarter", "季度", state="disputed", note="出现了两种译法")
    exported = export_entries(store)
    assert exported == [{"source": "bank", "target": "河岸", "note": "我拍板的"}]


def test_export_entries_roundtrip_through_tbx(tmp_path: Path) -> None:
    store = _store(tmp_path)
    termbase = Termbase(store)
    termbase.adopt("bank", "河岸", note="我拍板的")
    termbase.adopt("revenue", "收入", note="")
    exported = export_entries(store)
    assert parse_tbx(generate_tbx(exported)) == exported


def test_export_entries_explicit_states_include_pending(tmp_path: Path) -> None:
    store = _store(tmp_path)
    termbase = Termbase(store)
    termbase.adopt("bank", "河岸")
    termbase.adopt("quarter", "季度")
    termbase.dispute("quarter", "出现了两种译法")
    exported = export_entries(store, states=("adopted", "disputed"))
    assert {entry["source"] for entry in exported} == {"bank", "quarter"}


# ---------------------------------------------------------------------------
# 五、CLI 闭环（进程内直调，CACHE_DIR 指到临时库）
# ---------------------------------------------------------------------------


def _seed_cache(tmp_path: Path) -> Path:
    """在临时 cache 目录里预置术语库，返回数据库路径。"""
    cache = tmp_path / "cache"
    store = StateStore(str(cache / "translations.sqlite"))
    Termbase(store).adopt("bank", "河岸", note="我拍板的")
    store.close()
    return cache


def _write_tbx(path: Path, entries: list[dict[str, str]]) -> Path:
    path.write_text(generate_tbx(entries), encoding="utf-8")
    return path


def test_cli_tbx_import_adds_and_reports_conflict(tmp_path, monkeypatch, capsys) -> None:
    cache = _seed_cache(tmp_path)
    monkeypatch.setattr(app, "CACHE_DIR", cache)
    tbx = _write_tbx(
        tmp_path / "in.tbx",
        [
            {"source": "bank", "target": "银行", "note": "外部"},
            {"source": "quantum", "target": "量子", "note": "物理"},
        ],
    )
    code = app._run_tbx_import(str(tbx))
    assert code == 0
    printed = capsys.readouterr().out
    assert "新增 1 条" in printed
    assert "冲突保留 1 条" in printed
    assert "河岸" in printed  # 冲突逐条出声，保留值看得见

    store = StateStore(str(cache / "translations.sqlite"))
    assert store.get_term("quantum")["target"] == "量子"
    assert store.get_term("bank")["target"] == "河岸"  # 用户裁决未被覆盖
    store.close()


def test_cli_tbx_import_missing_file(tmp_path, capsys) -> None:
    code = app._run_tbx_import(str(tmp_path / "nope.tbx"))
    assert code == 1
    assert "文件不存在" in capsys.readouterr().out


def test_cli_tbx_import_garbage_file(tmp_path, capsys) -> None:
    bad = tmp_path / "bad.tbx"
    bad.write_text("<html>不是术语库</html>", encoding="utf-8")
    code = app._run_tbx_import(str(bad))
    assert code == 1
    assert "不是有效的 TBX" in capsys.readouterr().out


def test_cli_tbx_export_writes_adopted_terms(tmp_path, monkeypatch, capsys) -> None:
    cache = _seed_cache(tmp_path)
    monkeypatch.setattr(app, "CACHE_DIR", cache)
    out = tmp_path / "out" / "terms.tbx"
    code = app._run_tbx_export(str(out))
    assert code == 0
    printed = capsys.readouterr().out
    assert "TBX 导出完成" in printed
    assert out.is_file()
    assert parse_tbx(out) == [{"source": "bank", "target": "河岸", "note": "我拍板的"}]


def test_cli_tbx_export_empty_termbase(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(app, "CACHE_DIR", tmp_path / "empty-cache")
    out = tmp_path / "empty.tbx"
    code = app._run_tbx_export(str(out))
    assert code == 0
    assert "还没有已采用术语" in capsys.readouterr().out
    assert parse_tbx(out) == []  # 空库也写结构合法的文件，不让调用方猜


def test_main_dispatches_tbx_import(tmp_path, monkeypatch) -> None:
    cache = _seed_cache(tmp_path)
    monkeypatch.setattr(app, "CACHE_DIR", cache)
    monkeypatch.setattr(app, "CLI_TBX_IMPORT", None)
    tbx = _write_tbx(tmp_path / "in.tbx", [{"source": "quantum", "target": "量子"}])
    monkeypatch.setattr(sys, "argv", ["app.py", f"--tbx-import={tbx}"])
    with pytest.raises(SystemExit) as excinfo:
        app.main()
    assert excinfo.value.code == 0
    store = StateStore(str(cache / "translations.sqlite"))
    assert store.get_term("quantum")["target"] == "量子"
    store.close()


def test_main_dispatches_tbx_export(tmp_path, monkeypatch) -> None:
    cache = _seed_cache(tmp_path)
    monkeypatch.setattr(app, "CACHE_DIR", cache)
    monkeypatch.setattr(app, "CLI_TBX_EXPORT", None)
    out = tmp_path / "out.tbx"
    monkeypatch.setattr(sys, "argv", ["app.py", f"--tbx-export={out}"])
    with pytest.raises(SystemExit) as excinfo:
        app.main()
    assert excinfo.value.code == 0
    assert out.is_file()
    assert parse_tbx(out) == [{"source": "bank", "target": "河岸", "note": "我拍板的"}]


# ---------------------------------------------------------------------------
# 六、GUI 窗口（子进程：一个子进程只构造一次 Tk 根）
# ---------------------------------------------------------------------------

_CHILD_GUI = """\
import sys, tempfile
from pathlib import Path

sys.argv = ["app.py"]

import app
from core.state import StateStore
from core.termbase import Termbase
from core.tbx import generate_tbx, parse_tbx

work = Path(tempfile.mkdtemp())
store = StateStore(str(work / "state.sqlite"))
tb = Termbase(store)
tb.adopt("revenue", "收入", note="来自术语表")
tb.adopt("bank", "河岸", note="我拍板的")
tb.dispute("bank", "发现不同译法：已定「河岸」，本次出现「银行」")
tb.add_candidate("AI", note="预处理抽取")

# 弹窗一律吞掉（headless 没人点确定）
for name in ("showinfo", "showwarning", "showerror"):
    setattr(app.messagebox, name, lambda *a, **k: None)

app._enable_dpi_awareness()
application = app.TranslatorApp()
window = app.TermbaseWindow(application, store=store)
window.window.update()
problems = []

# 1. 未决优先：默认只看未决 → bank（存疑）在前、AI（候选）可见，revenue 折叠
if not window.only_pending.get():
    problems.append("only_pending 默认没开")
if len(window.visible) != 2:
    problems.append(f"未决可见数={len(window.visible)}")
if not window.visible or window.visible[0]["source"] != "bank":
    problems.append("存疑没排在最前")

# 2. 放开过滤：三条都在
window.only_pending.set(False)
window._refresh()
window.window.update()
if len(window.visible) != 3:
    problems.append(f"全部可见数={len(window.visible)}")

# 3. 选中存疑条 bank：源词/译名/备注填充
window.listbox.selection_set(0)
window._on_select()
window.window.update()
if window.current is None or window.current["source"] != "bank":
    problems.append("没选中 bank")
elif "bank" not in window.source_text.get("1.0", "end"):
    problems.append("源词框没填充")
elif "河岸" not in window.target_text.get("1.0", "end"):
    problems.append("译名框没填充")

# 4. 采用：把 bank 的译法改成「银行」
window.target_entry.delete(0, "end")
window.target_entry.insert(0, "银行")
window._adopt()
window.window.update()
row = store.get_term("bank")
if row["state"] != "adopted" or row["target"] != "银行":
    problems.append(f"采用没生效：{row}")
if not window.status_label.cget("text"):
    problems.append("采用后状态栏没出声")

# 5. 改名：bank 已采用，改名换掉译法（rename 会顺带置回 adopted）
window.target_entry.delete(0, "end")
window.target_entry.insert(0, "银行业")
window._rename()
window.window.update()
row = store.get_term("bank")
if row["state"] != "adopted" or row["target"] != "银行业":
    problems.append(f"改名没生效：{row}")

# 6. 候选 AI 采用为「人工智能」
window.only_pending.set(True)
window._refresh()
window.window.update()
for index, item in enumerate(window.visible):
    if item["source"] == "AI":
        window.listbox.selection_set(index)
        window._on_select()
        break
window.target_entry.delete(0, "end")
window.target_entry.insert(0, "人工智能")
window._adopt()
window.window.update()
row = store.get_term("AI")
if row["state"] != "adopted" or row["target"] != "人工智能":
    problems.append(f"候选采用没生效：{row}")

# 7. 拒绝：把 revenue 拒掉（放开过滤选中它）
window.only_pending.set(False)
window._refresh()
window.window.update()
for index, item in enumerate(window.visible):
    if item["source"] == "revenue":
        window.listbox.selection_set(index)
        window._on_select()
        break
window.target_entry.delete(0, "end")
window.target_entry.insert(0, "确认不是术语")
window._reject()
window.window.update()
row = store.get_term("revenue")
if row["state"] != "rejected":
    problems.append(f"拒绝没生效：{row}")

# 8. TBX 导入：新条目进库，冲突保留用户裁决（bank 停在「银行业」）
tbx_path = work / "in.tbx"
tbx_path.write_text(generate_tbx([
    {"source": "bank", "target": "银行", "note": "外部"},
    {"source": "quantum", "target": "量子", "note": "物理"},
]), encoding="utf-8")
app.filedialog.askopenfilename = lambda **k: str(tbx_path)
window._import_tbx()
window.window.update()
if store.get_term("quantum") is None or store.get_term("quantum")["state"] != "adopted":
    problems.append("TBX 新条目没进库")
if store.get_term("bank")["target"] != "银行业":
    problems.append("冲突覆盖了用户裁决")

# 9. TBX 导出：写盘并能解析回来（revenue 已拒绝，不在导出里）
out_path = work / "out.tbx"
app.filedialog.asksaveasfilename = lambda **k: str(out_path)
window._export_tbx()
window.window.update()
if not out_path.is_file():
    problems.append("导出文件没写")
entries = parse_tbx(out_path)
sources = {e["source"] for e in entries}
if sources != {"bank", "AI", "quantum"}:
    problems.append(f"导出条目不对：{sources}")

if problems:
    print("FAIL:" + ";".join(problems))
    sys.exit(1)
print("TERMBASE_GUI_OK")
"""


def test_gui_termbase_window_roundtrip() -> None:
    result = subprocess.run(
        [str(PYTHON), "-c", _CHILD_GUI],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    assert "TERMBASE_GUI_OK" in result.stdout, (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
