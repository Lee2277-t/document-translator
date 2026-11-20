"""界面引擎档位接线的冒烟测试。

本地大模型档（``--local-llm`` / 界面单选）最终要落到
``LocalLLMTranslator`` 上；这条链路上任何一环接错，用户选了档位却
还在用旧引擎，而且没有任何报错。所以在子进程里真构造一次界面，
验证两个档位各自走对构造器。

保密模式的保证在 test_offline_guarantee.py 里验证，这里只测联网模式。

v1.6.3 起档位由 ``core.engine_registry`` 构造，类是在工厂函数体内 import 的，
所以假类要 patch 在 ``core.translator.Translator`` /
``core.local_llm.LocalLLMTranslator`` 上（patch ``app.Translator`` 已经不生效
——app 模块自己不再直接构造云端翻译器了）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

_SCRIPT = """
import sys, threading
sys.argv = ["app"]  # 明确不是 --offline，走联网模式分支
import app
import core.local_llm as llm_module
import core.translator as translator_module

created = {}

class FakeTranslator:
    def __init__(self, **kwargs):
        created["cloud"] = kwargs
    def summary(self):
        return "fake"

class FakeLocalLLM:
    def __init__(self, **kwargs):
        created["local_llm"] = kwargs
    def summary(self):
        return "fake"

app.Translator = FakeTranslator
translator_module.Translator = FakeTranslator
llm_module.LocalLLMTranslator = FakeLocalLLM

application = app.TranslatorApp()
try:
    assert application.engine_var.get() == "cloud", "默认档位应为免费云端"
    application._run_translation([], False, threading.Event(), application._ui_options())
    assert "cloud" in created and "local_llm" not in created, created

    application.engine_var.set("local-llm")
    application._run_translation([], False, threading.Event(), application._ui_options())
    assert "local_llm" in created, f"选了本地大模型却没构造 LocalLLMTranslator: {created}"
    assert created["local_llm"].get("fallback") == "local"
    print("GUI_WIRING_OK")
finally:
    application.root.destroy()
"""


def test_engine_tier_selects_right_translator() -> None:
    result = subprocess.run(
        [str(PYTHON), "-c", _SCRIPT],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert "GUI_WIRING_OK" in result.stdout, (
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
