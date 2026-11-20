"""GUI 无人值守端到端验收（v1.9.4）。

界面上那条链此前**一条测试都没跑过**：``start_translation`` 起后台线程 → 线程把事件塞进
``self.events`` → ``_pump_events`` 用 ``root.after`` 泵出来 → ``_on_done``/``_on_error``/
``_on_cancelled`` 收尾。已有的 GUI 用例要么直接调 ``_translate_one``（tests/test_page_range.py），
要么点某个按钮的分支，中间这三跳是空白。这类链的坏法都不报错，只是界面永远停在「忙碌」：

* 事件名写错 → ``_pump_events`` 的 ``elif`` 全落空，翻译早就翻完了，进度条一直转；
* 收尾分支漏复位 ``busy`` → 「开始翻译」按钮永久 disabled，只能重开程序；
* 弹窗（``messagebox``）在无人值守里会**真的卡住**整个进程——所以驱动脚本先把它换成记录器，
  顺带把「该不该弹窗」变成可断言的事实（失败必弹、批量有失败弹警告、取消不弹）。

第二条是这一轮真正的收获：把 ``log_text`` 整段抄出来看，保密模式在「已生成」那一行确实只念
文件名，但成品旁边的《自检报告》《译后编辑清单》两行是各翻译器自己拼的**绝对路径**，
Word/PDF/PPT/EPUB/脚本五条管线一路原样送到界面上——《保密模式说明.txt》四·4 那句
「界面日志只显示文件名，不显示完整路径」在这里是假的。涉密文档放在哪个盘哪个文件夹，
本来就是截图找人帮忙时不该一起交出去的东西。现在统一走 ``core.common.shown_path``，
``app.py`` 启动时按 ``--offline`` 置 ``common.CONFIDENTIAL``。联网档照旧念完整路径
（那边没有承诺，且用户要靠这行找到成品），所以两个方向各有一条断言，防止收口收过头。

跑法： ./.venv\\Scripts\\python.exe -X utf8 -m pytest tests/test_gui_pipeline.py -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from docx import Document

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

#: 公共骨架：argv 先就位（``OFFLINE`` 是 import 期的模块级副作用，晚一拍改不动），
#: ``TRANSLATOR_DATA_DIR`` 另指临时目录（``cache/``、``logs/`` 都挂在 ``data_root()`` 下，
#: 才谈得上取证），弹窗换成记录器，引擎构造整体换成假翻译器——不加载模型、不联网，
#: 但翻出来的东西照样走完真实的写盘、自检、清单与事件泵。
_CHILD_HEAD = """
import os, sys, time
from pathlib import Path

HOME = Path(r"@HOME@")
SRC = HOME / "src"
SRC.mkdir(parents=True, exist_ok=True)
os.environ["TRANSLATOR_DATA_DIR"] = str(HOME)
sys.argv = ["app"@FLAGS@]

import app
from core.postedit import edit_list_path
from docx import Document

problems = []


class Recorder:
    def __init__(self):
        self.calls = []

    def of(self, kind):
        return lambda *a, **k: self.calls.append((kind,) + a[:2])

    def names(self):
        return [c[0] for c in self.calls]

    def clear(self):
        self.calls = []


DIALOGS = Recorder()
app.messagebox.showinfo = DIALOGS.of("info")
app.messagebox.showwarning = DIALOGS.of("warning")
app.messagebox.showerror = DIALOGS.of("error")
app.messagebox.askyesno = lambda *a, **k: (DIALOGS.calls.append(("ask",)), True)[1]

FIRST = "Quarterly revenue rose 12 percent to 4,200 million dollars."
SECOND = "The board approved the merger unanimously."
THIRD = "Cloud infrastructure spending grew sharply this year."
TRANSLATED = {
    FIRST: "本季度营收增长 12%，达 42 亿美元。",
    SECOND: "董事会一致通过了这项并购。",
    THIRD: "今年云基础设施支出大幅增长。",
}


def make_doc(name, texts):
    path = SRC / name
    doc = Document()
    for text in texts:
        doc.add_paragraph(text)
    doc.save(str(path))
    return str(path)


A = make_doc("甲试验.docx", [FIRST, SECOND])
B = make_doc("乙试验.docx", [THIRD])
C = make_doc("丙试验.docx", [FIRST])


def report_name(output):
    return os.path.basename(Path(output).stem + " 自检报告.txt")


def edit_name(output):
    return os.path.basename(edit_list_path(output))


STATE = {"mode": "ok"}
BUILT = []


class Fake:
    #: 带 current_file 属性 → app._translate_one 会把当前文件名写进来，批量失败场景靠它挑文件。
    stop_event = None
    current_file = None

    def translate_texts(self, texts, progress=None):
        from core.common import CancelledError, TranslationError

        mode = STATE["mode"]
        if mode == "cancel":
            if self.stop_event is not None and self.stop_event.wait(10):
                raise CancelledError()
            problems.append("取消场景没等到 stop_event")
        if mode == "boom":
            raise TranslationError("引擎拒绝：这一段翻不了。")
        if mode == "boom_second" and self.current_file == os.path.basename(B):
            raise TranslationError("引擎拒绝：这个文件翻不了。")
        if mode == "fail_then_cancel" and self.current_file == os.path.basename(A):
            raise TranslationError("引擎拒绝：第一个文件翻不了。")
        if mode in ("cancel_second", "fail_then_cancel") and self.current_file == os.path.basename(B):
            STATE["waiting"] = True
            if self.stop_event is not None and self.stop_event.wait(10):
                raise CancelledError()
            problems.append("批量取消场景没等到 stop_event")
        return [TRANSLATED[text] for text in texts]

    def summary(self):
        return "假译 2 段"


FAKE = Fake()


def fake_build(key, params):
    BUILT.append(key)
    FAKE.stop_event = params.stop_event
    return FAKE


app.engine_registry.build = fake_build

app._enable_dpi_awareness()
application = app.TranslatorApp()


def run_until_quiet(timeout=60):
    \"\"\"泵到后台线程收工、事件队列清空；_on_done 里的 busy 复位与文案更新在同一次泵里。\"\"\"
    deadline = time.time() + timeout
    while time.time() < deadline:
        application.root.update()
        if not application.busy and application.events.empty():
            return True
        time.sleep(0.02)
    problems.append("60 秒内没回到空闲（进度条一直在转）")
    return False


def log_text():
    return application.log_text.get("1.0", "end")


def status_text():
    return str(application.status_label.cget("text"))


def btn(name):
    return str(getattr(application, name).cget("state"))
"""

#: 保密模式（``--offline``）四条链：成功 / 单文件失败 / 批量有失败 / 取消。
_CHILD_OFFLINE = """
assert app.OFFLINE is True, "--offline 没生效"
assert app.common.CONFIDENTIAL is True, "保密口径没传给翻译器"

# ── 1. 成功：成品真长出来了，界面只念文件名 ─────────────────────────────
STATE["mode"] = "ok"
application.set_files([A])
application.start_translation()
run_until_quiet()
if application.busy:
    problems.append("成功后 busy 没复位")
if application.stop_event is not None:
    problems.append("成功后 stop_event 没清空")
output = application.output_path
if not output or not os.path.isfile(output):
    problems.append(f"成品文件不存在：{output}")
    output = ""
product = os.path.basename(output)
if status_text() != f"翻译完成：{product}":
    problems.append(f"状态栏={status_text()!r}")
if application.percent_label.cget("text") != "100%":
    problems.append(f"百分比={application.percent_label.cget('text')!r}")
if (btn("start_btn"), btn("cancel_btn"), btn("open_btn")) != (
    "normal", "disabled", "normal"
):
    problems.append(f"按钮没复位={btn('start_btn')},{btn('cancel_btn')},{btn('open_btn')}")
if BUILT != [app.engine_registry.OFFLINE]:
    problems.append(f"保密模式构造了别的档位：{BUILT}")
if application.results != [(A, output)]:
    problems.append(f"results={application.results}")
if DIALOGS.names():
    problems.append(f"成功却弹了窗：{DIALOGS.names()}")
paragraphs = [p.text for p in Document(output).paragraphs]
if paragraphs != [FIRST, TRANSLATED[FIRST], SECOND, TRANSLATED[SECOND]]:
    problems.append(f"成品正文不对：{paragraphs}")
log = log_text()
for needle in (
    f"开始翻译：{os.path.basename(A)}",
    f"已生成：{product}（",
    f"自检报告：{report_name(output)}（",
    f"译后编辑清单：{edit_name(output)}",
):
    if needle not in log:
        problems.append(f"日志缺行：{needle!r}")
if str(SRC) in log or str(HOME) in log:
    problems.append(f"保密模式日志里出现了目录：{log}")

# 缓存与应用日志一个都不该落（离线仍写按天元信息日志，那份不含原文也不含路径）
if (HOME / "cache").exists():
    problems.append(f"保密模式落了缓存：{sorted(p.name for p in (HOME / 'cache').rglob('*'))}")
if (HOME / "logs" / "app.log").exists():
    problems.append("保密模式写了应用日志 app.log")

# ── 2. 单文件失败：_on_error 复位界面 + 必弹错误框 ───────────────────────
application.clear_log()
DIALOGS.clear()
BUILT.clear()
STATE["mode"] = "boom"
application.set_files([B])
application.start_translation()
run_until_quiet()
if application.busy:
    problems.append("失败后 busy 没复位")
if application.stop_event is not None:
    problems.append("失败后 stop_event 没清空")
if application.output_path is not None:
    problems.append(f"失败后 output_path 残留：{application.output_path}")
if status_text() != "翻译失败：引擎拒绝：这一段翻不了。":
    problems.append(f"失败状态栏={status_text()!r}")
if "翻译失败：引擎拒绝：这一段翻不了。" not in log_text():
    problems.append("失败原因没进界面日志")
if DIALOGS.names() != ["error"]:
    problems.append(f"失败弹窗={DIALOGS.calls}")
if (btn("start_btn"), btn("open_btn")) != ("normal", "disabled"):
    problems.append(f"失败后按钮={btn('start_btn')},{btn('open_btn')}")

# ── 3. 批量有失败：_on_done 的批量分支逐条点名 + 警告框 ──────────────────
application.clear_log()
DIALOGS.clear()
STATE["mode"] = "boom_second"
application.set_files([A, B])
application.start_translation()
run_until_quiet()
log = log_text()
if status_text() != "批量完成：成功 1 个，失败 1 个":
    problems.append(f"批量状态栏={status_text()!r}")
for needle in (
    "开始批量翻译 2 个文件…",
    f"[1/2] {os.path.basename(A)}",
    f"[2/2] {os.path.basename(B)}",
    "  跳过：引擎拒绝：这个文件翻不了。",
    "批量完成：成功 1 个，失败 1 个。",
    f"  未生成：{os.path.basename(B)}",
):
    if needle not in log:
        problems.append(f"批量日志缺行：{needle!r}")
if DIALOGS.names() != ["warning"]:
    problems.append(f"批量警告框={DIALOGS.calls}")
if [r[1] is not None for r in application.results] != [True, False]:
    problems.append(f"批量 results={application.results}")
if str(SRC) in log or str(HOME) in log:
    problems.append("批量日志里出现了目录")

# ── 4. 取消：_on_cancelled 收尾，不出成品、不弹窗 ────────────────────────
application.clear_log()
DIALOGS.clear()
STATE["mode"] = "cancel"
application.set_files([C])
application.start_translation()
application.cancel_translation()
run_until_quiet()
if application.busy:
    problems.append("取消后 busy 没复位")
if application.stop_event is not None:
    problems.append("取消后 stop_event 没清空")
if status_text() != "已取消翻译":
    problems.append(f"取消状态栏={status_text()!r}")
log = log_text()
if "已请求取消，将在当前段落或页面结束后停止。" not in log:
    problems.append("取消请求没出声")
if "已取消，未生成译文文件。" not in log:
    problems.append("取消收尾文案没进日志")
if DIALOGS.names():
    problems.append(f"取消却弹了窗：{DIALOGS.names()}")
if any("丙试验" in p.name and "双语" in p.name for p in SRC.iterdir()):
    problems.append("取消后仍写出了成品")

if problems:
    print("FAIL:" + ";".join(problems))
    sys.exit(1)
print("GUI_PIPELINE_OK")
"""

#: 联网档：口径反过来验一遍——完整成品路径必须照念，收口不许收过头。
_CHILD_ONLINE = """
assert app.OFFLINE is False, "这一格不该带 --offline"
assert app.common.CONFIDENTIAL is False, "联网档被当成保密模式了"
STATE["mode"] = "ok"
application.set_files([A])
application.start_translation()
run_until_quiet()
output = application.output_path or ""
log = log_text()
if not output:
    problems.append(f"联网模式没出成品：状态={status_text()!r} 日志={log}")
else:
    if f"已生成：{output}（" not in log:
        problems.append(f"联网模式没念完整成品路径：{log}")
    full_report = str(Path(output).with_name(Path(output).stem + " 自检报告.txt"))
    if f"自检报告：{full_report}" not in log:
        problems.append(f"联网模式自检报告那行没念完整路径：{log}")
    if status_text() != f"翻译完成：{os.path.basename(output)}":
        problems.append(f"联网模式状态栏={status_text()!r}")

if problems:
    print("FAIL:" + ";".join(problems))
    sys.exit(1)
print("GUI_ONLINE_OK")
"""


def _run_child(home: Path, flags: str, body: str, marker: str) -> None:
    script = _CHILD_HEAD.replace("@HOME@", str(home)).replace("@FLAGS@", flags) + body
    result = subprocess.run(
        [str(PYTHON), "-X", "utf8", "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=420,
    )
    assert marker in result.stdout, (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr[-4000:]}"
    )


_CHILD_BATCH_CANCEL = """
STATE["mode"] = "@MODE@"
STATE["waiting"] = False
application.set_files([A, B, C])
application.start_btn.invoke()


def cancel_when_waiting():
    if STATE["waiting"]:
        application.cancel_btn.invoke()
    elif application.busy:
        application.root.after(20, cancel_when_waiting)


application.root.after(20, cancel_when_waiting)
run_until_quiet()
assert STATE["waiting"], "没有开始翻译第二个文件"
assert not application.busy and application.stop_event is None
assert application.batch_index == 0 and application.batch_total == 0
assert not DIALOGS.names(), DIALOGS.calls
assert (btn("start_btn"), btn("cancel_btn")) == ("normal", "disabled")
assert "未生成译文文件" not in log_text()
assert not any("乙试验_双语" in p.name or "丙试验_双语" in p.name for p in SRC.iterdir())
if STATE["mode"] == "cancel_second":
    assert len(application.results) == 1
    output = application.results[0][1]
    assert output and Path(output).is_file()
    assert application.output_path == output
    assert btn("open_btn") == "normal"
    assert btn("postedit_btn") == "normal"
    assert status_text() == "已取消翻译，已保留 1 个成品"
    assert "已完成的 1 个译文文件已保留" in log_text()
    assert os.path.basename(output) in log_text()
    assert [p.text for p in Document(output).paragraphs] == [
        FIRST, TRANSLATED[FIRST], SECOND, TRANSLATED[SECOND]
    ]
else:
    assert application.results == [(A, None)]
    assert application.output_path is None
    assert btn("open_btn") == "disabled"
    assert status_text() == "已取消翻译"
    assert "本次没有已完成的译文文件" in log_text()
if app.OFFLINE:
    assert str(SRC) not in log_text() and str(HOME) not in log_text()
    assert not (HOME / "cache").exists()
    assert not (HOME / "logs" / "app.log").exists()

application.clear_log()
STATE["mode"] = "ok"
application.set_files([C])
application.start_btn.invoke()
run_until_quiet()
assert status_text().startswith("翻译完成：")
assert len(application.results) == 1 and application.results[0][0] == C
assert Path(application.output_path).is_file()
assert btn("open_btn") == "normal"
application.root.destroy()
print("GUI_BATCH_CANCEL_OK")
"""


@pytest.mark.parametrize("offline", [False, True])
@pytest.mark.parametrize("mode", ["cancel_second", "fail_then_cancel"])
def test_gui_batch_cancel_keeps_completed_outputs(tmp_path: Path, offline, mode) -> None:
    flags = ', "--offline"' if offline else ""
    body = _CHILD_BATCH_CANCEL.replace("@MODE@", mode)
    _run_child(tmp_path / "batch", flags, body, "GUI_BATCH_CANCEL_OK")


def test_gui_async_pipeline_offline(tmp_path: Path) -> None:
    """保密模式四条链一次跑穿：成品、界面文案、按钮复位、弹窗时机、不落缓存。"""
    _run_child(tmp_path / "off", ', "--offline"', _CHILD_OFFLINE, "GUI_PIPELINE_OK")


def test_gui_async_pipeline_online_keeps_full_paths(tmp_path: Path) -> None:
    """联网档反过来验：只收保密模式那一半，用户找成品要用的完整路径还在。"""
    _run_child(tmp_path / "on", "", _CHILD_ONLINE, "GUI_ONLINE_OK")


# ---------------------------------------------------------------------------
# 收口点本身（进程内）：口径长在 core.common，界面只是搬运工
# ---------------------------------------------------------------------------

_SENTENCE = "Quarterly revenue rose 12 percent to 4,200 million dollars."


class _Fake:
    stop_event = None

    def translate_texts(self, texts, progress=None):
        return ["本季度营收增长 12%，达 42 亿美元。"] * len(texts)

    def summary(self):
        return "假译 1 段"


def test_shown_path_shrinks_only_in_confidential_mode(monkeypatch, tmp_path: Path) -> None:
    from core import common

    deep = tmp_path / "涉密目录" / "季度报告_双语.docx"
    monkeypatch.setattr(common, "CONFIDENTIAL", False)
    assert common.shown_path(deep) == str(deep)
    assert common.shown_path(str(deep)) == str(deep)
    monkeypatch.setattr(common, "CONFIDENTIAL", True)
    assert common.shown_path(deep) == "季度报告_双语.docx"
    assert common.shown_path(str(deep)) == "季度报告_双语.docx"


def test_word_pipeline_lines_hold_no_directory_when_confidential(
    monkeypatch, tmp_path: Path
) -> None:
    """漏点本来在翻译器自己拼的那两行：保密口径要在这一层就生效，轮不到界面补救。"""
    from core import common
    from core.word_translator import translate_word

    src = tmp_path / "季度报告.docx"
    doc = Document()
    doc.add_paragraph(_SENTENCE)
    doc.save(str(src))

    lines: list[str] = []
    monkeypatch.setattr(common, "CONFIDENTIAL", True)
    translate_word(str(src), _Fake(), log=lines.append)
    text = "\n".join(lines)
    assert "自检报告：" in text and "译后编辑清单：" in text
    assert str(tmp_path) not in text, text

    # 反过来：联网模式仍然念完整路径，否则用户不知道成品去哪找。
    lines.clear()
    monkeypatch.setattr(common, "CONFIDENTIAL", False)
    translate_word(str(src), _Fake(), log=lines.append)
    text = "\n".join(lines)
    assert str(tmp_path) in text, text

