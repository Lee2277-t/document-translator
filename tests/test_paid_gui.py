"""付费 API（DeepL）GUI 接线（C3-b）的行为钉子。

盯住的东西按优先级：

1. **保密模式看不到任何付费入口**——engine_frame 整个只在非保密模式下
   渲染。子进程里 ``--offline`` 导入 app 后，既没有 ``engine_var``，
   也没有任何带「付费 / DeepL」字样的控件。这是涉密防线的直接证据
   （付费档与保密模式硬互斥，没有「提醒后放行」）；
2. **联网模式渲染出四个档位**（免费云端 / 本地大模型 / 付费 API / 自托管服务）；
3. **三道闸门照搬 CLI ``_paid_gate``**：没 key → 引导弹窗 + 弹回云端；
   没确认 → 涉密警示对话框，「是」落确认标记并保持付费、「否」弹回
   云端；已确认 → 不弹窗、保持付费档；
4. **``_run_translation`` 的 paid 分支**正确构造 ``DeepLTranslator``
   （key/style/stop_event/log/cache 透传；cache_path 为 None 时
   cache=False），key 缺失时走统一错误通道；
5. **``main()`` 把 ``--paid=deepl`` 同步到界面档位**，命令行用户也
   看得见当前用的是哪档。

全部 mock，不真发 HTTP。

**为什么每条用例都起子进程**：本机 venv 的 ``_tkinter`` 走系统 Python
的 tcl 目录，同一进程里反复创建 Tk 根会间歇性触发
``tcl_findLibrary`` / ``init.tcl`` 抖动（实测约 1/6 概率），所以沿用
仓库既有套路（tests/test_engine_tier_gui.py）：**一个子进程只构造一次
界面**，控件级方法驱动（``radio.invoke()`` 模拟点单选），断言结果用
ASCII 标记回传。``OFFLINE`` 是 import 期的模块级副作用，保密模式那条
更必须在子进程里现导。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

#: 形态合法的 Free key（:fx 结尾），与 tests/test_deepl.py 统一。
FAKE_KEY = "123e4567-e89b-12d3-a456-426614174000:fx"

#: 子进程全部成功的标记；失败时子进程打印 "FAIL: ..." 并非零退出。
_OK_MARKER = "PAID_GUI_OK"

#: 子进程公共头部：先设 argv（OFFLINE 是 import 期的模块级副作用，必须
#: 在 import app 之前就位），再导入 app、构造一次界面、准备记录器。
_CHILD_PREFIX = """\
import json, sys, tempfile, threading
from pathlib import Path
from unittest import mock

sys.argv = {argv!r}

import tkinter as tk
import app
import core.api_keys as api_keys
import core.deepl as deepl_module

FAKE_KEY = "{fake_key}"

app._enable_dpi_awareness()
application = app.TranslatorApp()
problems = []


def _engine_radios():
    stack = [application.root]
    radios = []
    while stack:
        widget = stack.pop()
        for child in widget.winfo_children():
            stack.append(child)
            if (
                isinstance(child, tk.Radiobutton)
                and str(child.cget("variable")) == str(application.engine_var)
            ):
                radios.append(child)
    return radios


def _paid_radio():
    for radio in _engine_radios():
        if radio.cget("value") == "paid":
            return radio
    raise AssertionError("联网模式必须渲染出「付费 API」单选")


def _write_keys(acknowledged):
    entry = {{"key": FAKE_KEY}}
    if acknowledged:
        entry["acknowledged"] = True
    path = Path(tempfile.mkdtemp()) / "api_keys.local.json"
    path.write_text(
        json.dumps({{"deepl": entry}}, ensure_ascii=False), encoding="utf-8"
    )
    api_keys.KEYS_FILE = path


def _finish():
    try:
        application.root.destroy()
    except Exception:
        pass
    if problems:
        print("FAIL: " + " | ".join(problems))
        sys.exit(1)
    print("{marker}")
"""


def _run_child(argv: list[str], body: str) -> subprocess.CompletedProcess:
    """在子进程里构造一次界面并执行 body（body 是已缩进的 Python 源码）。"""
    script = _CHILD_PREFIX.format(
        argv=argv, fake_key=FAKE_KEY, marker=_OK_MARKER
    )
    script += "try:\n" + body.strip("\n") + "\n"
    script += "finally:\n    _finish()\n"
    return subprocess.run(
        [str(PYTHON), "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


def _assert_ok(child: subprocess.CompletedProcess) -> None:
    assert child.returncode == 0, (
        f"stdout: {child.stdout}\nstderr: {child.stderr}"
    )
    assert _OK_MARKER in child.stdout, (
        f"stdout: {child.stdout}\nstderr: {child.stderr}"
    )


# ---------------------------------------------------------------------------
# 1. 档位渲染与界面自述
# ---------------------------------------------------------------------------

_RENDER_BODY = """\
    # 四个档位必须都渲染出来（顺序无关，按 value 对齐）：档位清单的唯一真相是
    # core.engine_registry，界面只是把它渲染成单选，所以这张表就是断言的对照物。
    labels = {radio.cget("value"): radio.cget("text") for radio in _engine_radios()}
    expected = {
        "cloud": "免费云端",
        "local-llm": "本地大模型（更强）",
        "paid": "付费 API",
        "custom": "自托管服务",
    }
    if labels != expected:
        problems.append("引擎单选与登记表对不上: %r" % (labels,))
    if list(app.engine_registry.radio_choices(False)) != [
        (key, value) for key, value in expected.items()
    ]:
        problems.append("登记表的档位顺序与界面不一致")
    # 「关于」对话框的引擎清单要包含付费档（界面自述与实际一致）
    with mock.patch.object(app.messagebox, "showinfo") as showinfo:
        application.show_about()
    if not showinfo.called:
        problems.append("show_about 没弹窗")
    else:
        message = showinfo.call_args[0][1]
        if "付费 API" not in message or "DeepL" not in message:
            problems.append("关于对话框没列出付费档")
"""


def test_engine_radio_renders_four_tiers() -> None:
    _assert_ok(_run_child(["app"], _RENDER_BODY))


# ---------------------------------------------------------------------------
# 2. 保密模式硬保证：看不到任何付费入口
# ---------------------------------------------------------------------------

_OFFLINE_BODY = """\
    import socket

    if hasattr(application, "engine_var"):
        problems.append("保密模式出现了 engine_var")
    stack = [application.root]
    radio_values = []
    texts = []
    while stack:
        widget = stack.pop()
        for child in widget.winfo_children():
            stack.append(child)
            if isinstance(child, tk.Radiobutton):
                radio_values.append(str(child.cget("value")))
            try:
                text = str(child.cget("text"))
            except Exception:
                text = ""
            if text:
                texts.append(text)
    if "paid" in radio_values:
        problems.append("出现了付费档单选")
    joined = "\\n".join(texts)
    for needle in ("DeepL", "deepl", "付费"):
        if needle in joined:
            problems.append("界面出现付费/DeepL 字样: " + needle)
    # 顺带证明保密硬互斥的另一半：本进程真的联不上网
    try:
        socket.create_connection(("cn.bing.com", 443), timeout=5)
        problems.append("保密模式居然能联网")
    except OSError:
        pass
"""


def test_offline_mode_has_no_paid_entry() -> None:
    """涉密防线直接证据：--offline 子进程里没有 engine_var、没有任何
    付费/DeepL 入口，且进程真的断网——付费档与保密模式硬互斥。"""
    _assert_ok(_run_child(["app", "--offline"], _OFFLINE_BODY))


# ---------------------------------------------------------------------------
# 3. 三道闸门（照搬 CLI _paid_gate 的判断口径）
# ---------------------------------------------------------------------------

_NO_KEY_BODY = """\
    with mock.patch.object(app.messagebox, "showinfo") as showinfo, \\
            mock.patch.object(api_keys, "load_key", return_value=None):
        _paid_radio().invoke()
    if application.engine_var.get() != "cloud":
        problems.append("没 key 却没弹回云端: %r" % application.engine_var.get())
    if not showinfo.called:
        problems.append("没 key 却没弹引导窗")
    else:
        message = showinfo.call_args[0][1]
        for needle in (
            "deepl.com/pro-api",
            "api_keys.local.json",
            "50 万字符",
            ":fx",
        ):
            if needle not in message:
                problems.append("引导窗缺关键信息: %s" % needle)
    if application.mode_label.cget("text") != "免费翻译 · 无需 API Key":
        problems.append("弹回云端后 mode_label 没恢复")
    if application.quality_switch.winfo_manager() != "":
        problems.append("弹回云端后质量引擎开关应隐藏")
"""


def test_paid_without_key_shows_guidance_and_bounces_back() -> None:
    """没配 key → 弹引导窗（申请地址 / 填哪个文件 / json 样例），弹回云端。"""
    _assert_ok(_run_child(["app"], _NO_KEY_BODY))


_FIRST_USE_BODY = """\
    _write_keys(acknowledged=False)
    with mock.patch.object(app.messagebox, "askyesno", return_value=True) as ask, \\
            mock.patch.object(api_keys, "save_acknowledgement") as save_ack:
        _paid_radio().invoke()
    if application.engine_var.get() != "paid":
        problems.append("确认后档位应保持 paid: %r" % application.engine_var.get())
    if not ask.called:
        problems.append("首次选付费档必须弹涉密警示")
    else:
        message = ask.call_args[0][1]
        for needle in ("境外服务器", "保密模式"):
            if needle not in message:
                problems.append("涉密警示缺关键信息: %s" % needle)
    if not save_ack.called or save_ack.call_args[0] != ("deepl",):
        problems.append("确认后必须落 save_acknowledgement 标记")
"""


def test_paid_first_use_asks_and_saves_acknowledgement() -> None:
    """配了 key 未确认 → 弹涉密警示；选「是」落确认标记，档位保持 paid。"""
    _assert_ok(_run_child(["app"], _FIRST_USE_BODY))


_DECLINED_BODY = """\
    _write_keys(acknowledged=False)
    with mock.patch.object(app.messagebox, "askyesno", return_value=False) as ask, \\
            mock.patch.object(api_keys, "save_acknowledgement") as save_ack:
        _paid_radio().invoke()
    if application.engine_var.get() != "cloud":
        problems.append("警示选否应弹回云端: %r" % application.engine_var.get())
    if not ask.called:
        problems.append("未确认状态必须先弹警示")
    if save_ack.called:
        problems.append("选否不应落确认标记")
    if application.mode_label.cget("text") != "免费翻译 · 无需 API Key":
        problems.append("弹回云端后 mode_label 没恢复")
"""


def test_paid_acknowledgement_declined_bounces_back() -> None:
    """涉密警示选「否」→ 弹回免费云端，不落确认标记。"""
    _assert_ok(_run_child(["app"], _DECLINED_BODY))


_ACKNOWLEDGED_BODY = """\
    _write_keys(acknowledged=True)
    with mock.patch.object(app.messagebox, "showinfo") as showinfo, \\
            mock.patch.object(app.messagebox, "askyesno") as askyesno:
        _paid_radio().invoke()
    if application.engine_var.get() != "paid":
        problems.append("已确认状态选 paid 应保持: %r" % application.engine_var.get())
    if showinfo.called or askyesno.called:
        problems.append("已确认状态不应再弹任何窗")
    if application.mode_label.cget("text") != "付费 API · 原文发送至 DeepL":
        problems.append("paid 档 mode_label 不对: %r" % application.mode_label.cget("text"))
    log_text = application.log_text.get("1.0", "end-1c")
    for needle in (
        "付费 API（DeepL）",
        "按量计费",
        "自备 key",
        "境外服务器",
        "保密模式",
    ):
        if needle not in log_text:
            problems.append("paid 档日志缺关键信息: %s" % needle)
    # 质量引擎只挂本地大模型档：paid 隐藏；local-llm 仍显示（回归钉子）
    if application.quality_switch.winfo_manager() != "":
        problems.append("paid 档应隐藏质量引擎开关")
    application.engine_var.set("local-llm")
    application._on_engine_change()
    if application.quality_switch.winfo_manager() != "grid":
        problems.append("本地大模型档应显示质量引擎开关")
"""


def test_paid_acknowledged_never_asks_again() -> None:
    """已确认状态选 paid → 不弹任何窗，档位保持，日志念全名。"""
    _assert_ok(_run_child(["app"], _ACKNOWLEDGED_BODY))


# ---------------------------------------------------------------------------
# 4. _run_translation 的 paid 分支 + current_file
# ---------------------------------------------------------------------------

_RUN_TRANSLATION_BODY = """\
    _write_keys(acknowledged=True)
    created = {}

    class _FakeDeepL:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.current_file = ""

        def summary(self):
            return "fake"

    deepl_module.DeepLTranslator = _FakeDeepL

    # 场景一：默认写缓存，参数透传
    application.engine_var.set("paid")
    stop_event = threading.Event()
    application._run_translation([], False, stop_event, application._ui_options())
    if created.get("key_config", {}).get("key") != FAKE_KEY:
        problems.append("key 没透传: %r" % (created.get("key_config"),))
    if created.get("style") != application.style:
        problems.append("style 没透传")
    if created.get("stop_event") is not stop_event:
        problems.append("stop_event 没透传")
    if created.get("log") != application.emit_log:
        problems.append("log 没透传")
    if not str(created.get("cache", "")).endswith("translations.sqlite"):
        problems.append("默认 cache 应是 sqlite 路径: %r" % (created.get("cache"),))

    # 场景二：「不保存翻译缓存」→ cache_path 为 None → cache=False
    created.clear()
    application.cache_switch._on_click(None)
    if application.cache_switch.get() is not True:
        problems.append("点一下开关应置为「不保存缓存」")
    application._run_translation([], False, threading.Event(), application._ui_options())
    if created.get("cache") is not False:
        problems.append("cache_path 为 None 时 cache 应为 False: %r" % (created.get("cache"),))

    # 场景三：key 缺失的双保险走统一错误通道（不裸抛卡死界面）
    application.cache_switch._on_click(None)  # 恢复默认（写缓存）
    while not application.events.empty():  # 清掉前两个场景遗留的事件
        application.events.get_nowait()
    with mock.patch.object(api_keys, "load_key", return_value=None):
        application._run_translation([], False, threading.Event(), application._ui_options())
    kind, message = application.events.get_nowait()
    if kind != "error":
        problems.append("key 缺失应报 error 事件: %r" % (kind,))
    if "api_keys.local.json" not in message or "deepl.com/pro-api" not in message:
        problems.append("key 缺失的错误不说人话: %r" % (message,))
"""


def test_run_translation_paid_branch() -> None:
    """paid 档构造 DeepLTranslator：参数透传 / 无缓存 cache=False /
    key 缺失走统一错误通道。"""
    _assert_ok(_run_child(["app"], _RUN_TRANSLATION_BODY))


_CURRENT_FILE_BODY = """\
    class _Paid:
        def __init__(self):
            self.current_file = ""

        def translate_texts(self, texts, progress=None, ctx=None):
            return list(texts)

    class _Plain:
        def translate_texts(self, texts, progress=None, ctx=None):
            return list(texts)

    app.translate_word = lambda *args, **kwargs: "out.docx"

    paid = _Paid()
    output = application._translate_one(
        r"D:\\docs\\示例-英文报告.docx", paid, False, threading.Event()
    )
    if output != "out.docx":
        problems.append("dispatch 结果不对: %r" % (output,))
    if paid.current_file != "示例-英文报告.docx":
        problems.append("current_file 没按 basename 赋值: %r" % (paid.current_file,))

    plain = _Plain()
    application._translate_one(r"D:\\docs\\a.docx", plain, False, threading.Event())
    if hasattr(plain, "current_file"):
        problems.append("没有 current_file 属性的引擎不该被写")
"""


def test_translate_one_sets_current_file() -> None:
    """GUI 路径补齐 CLI _convert 的 current_file 赋值（用量流水 file 字段），
    没有该属性的引擎不受影响（hasattr 守卫）。"""
    _assert_ok(_run_child(["app"], _CURRENT_FILE_BODY))


# ---------------------------------------------------------------------------
# 5. main() 的界面同步（独立子进程：main() 自己构造界面）
# ---------------------------------------------------------------------------

_MAIN_SCRIPT = """\
import json, sys, tempfile
from pathlib import Path
from unittest import mock

sys.argv = ["app", "--paid=deepl"]
import app
import core.api_keys as api_keys

entry = {{"key": "{fake_key}", "acknowledged": True}}
path = Path(tempfile.mkdtemp()) / "api_keys.local.json"
path.write_text(json.dumps({{"deepl": entry}}, ensure_ascii=False), encoding="utf-8")
api_keys.KEYS_FILE = path

created = []
app.TranslatorApp.run = lambda self: created.append(self)
problems = []
try:
    app.main()
    if not created:
        problems.append("main() 没构造界面")
    else:
        gui = created[0]
        if gui.engine_var.get() != "paid":
            problems.append("main() 没把界面拨到 paid: %r" % gui.engine_var.get())
        if gui.mode_label.cget("text") != "付费 API · 原文发送至 DeepL":
            problems.append("mode_label 不对: %r" % gui.mode_label.cget("text"))
except SystemExit as exc:
    problems.append("main() 不该退出: %r" % (exc.code,))
except Exception as exc:
    problems.append("异常 %s: %s" % (type(exc).__name__, exc))
finally:
    for gui in created:
        try:
            gui.root.destroy()
        except Exception:
            pass
if problems:
    print("FAIL: " + " | ".join(problems))
    sys.exit(1)
print("{marker}")
""".format(fake_key=FAKE_KEY, marker=_OK_MARKER)


def test_main_syncs_paid_engine_to_gui() -> None:
    """--paid=deepl（三道闸门已过）把界面 engine_var 拨到 paid，命令行用户
    也看得见当前档位。不依赖真实网络：key 已配且已确认，不弹窗。"""
    child = subprocess.run(
        [str(PYTHON), "-c", _MAIN_SCRIPT],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    _assert_ok(child)
