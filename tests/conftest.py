"""pytest 夹具：把项目根目录加进 sys.path，测试才能 import core.*。

跑测试的姿势（必须用项目自己的 venv，系统 Python 没有 rapidocr/wordninja）：
    ./.venv\\Scripts\\python.exe -m pytest tests -q

三条全局约定，都是踩过坑之后加的，别删：

1. **子进程 stdio 固定 UTF-8**。Windows 下被管道接走的子进程默认按控制台代码页
   （中文机器是 cp936）编码 stdout，而测试父进程按 UTF-8 解码，断言里的中文全成乱码。
   只在环境变量里声明 ``PYTHONIOENCODING``，**不启用 PYTHONUTF8**——后者会把文件写入的
   默认编码也改掉，掩盖真实的 GBK 机器行为。
2. **测试不碰真实的 cache/ 与 logs/**。付费档流水（``cache/paid_usage.jsonl``）、
   统一状态库（``cache/translations.sqlite``，含用户的术语库与翻译记忆）、离线日志
   都是用户资产：测试往里写一次，用户的对账数据就多一条假账、翻译记忆就多一条假记忆。
   本夹具把它们全部重定向到每个测试自己的 tmp 目录。

第三条是同一方向的补充：**每条测试结束后复位所有「跑过一次 main() 就会被写脏」的模块级
生效态**（``app.CLI_*`` 全局、翻译方向、分段规则）。它们脏了的表征从来不是本条用例失败，
而是**后面某条毫不相干的用例**多执行了一个上一轮的分支、或者在错误的规则下断句——
v1.8.0 就是 ``--dump-segment-rules`` 的残留把 ``test_tbx`` / ``test_tmx`` 四条打红的。
兜法一律用「出厂值」而不是「进来之前的值」：顺序怎么变都回到同一个起点。
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

#: 整轮测试的数据根目录（cache/ 与 logs/ 挂在这里）。必须在**任何**
#: ``import app`` / ``import core.*`` 之前设好：app.py 在模块级就把 CACHE_DIR /
#: LOG_DIR / logging 文件句柄算死了，事后 monkeypatch 管不到子进程，
#: 也管不到父进程已有的 FileHandler。模型与术语表仍走 app_root()，不受影响。
_SESSION_HOME = Path(tempfile.mkdtemp(prefix="translator-test-home-"))
os.environ["TRANSLATOR_DATA_DIR"] = str(_SESSION_HOME)
atexit.register(shutil.rmtree, _SESSION_HOME, True)


@pytest.fixture(autouse=True)
def _reset_direction():
    """每个测试结束后把翻译方向复位成中文。

    方向是**模块级全局**（与 ``app.OFFLINE`` 同性质），一条测试里 ``set_target("en")``
    若不复位，后面所有测试都会在英文方向下跑——表现是随机几条抽段断言失败，
    而且顺序一变就不失败。放在 autouse 里，测试自己不用记得收尾。
    """
    from core import direction

    yield
    direction.reset()


#: ``app`` 模块刚 import 完时的 CLI_* 快照。第一条测试跑之前取，取到的是纯出厂值。
_CLI_DEFAULTS: dict[str, object] | None = None


@pytest.fixture(autouse=True)
def _reset_cli_globals():
    """每条测试结束后把 ``app`` 的 ``CLI_*`` 全局复位成出厂值。

    ``main()`` 会把命令行值直接写进模块级全局（``CLI_PAGES`` / ``CLI_SEGMENT_RULES`` /
    ``CLI_DUMP_SEGMENT_RULES`` / ``CLI_SEGMENT_NOTE``…），而测试里的 monkeypatch 只盖得住
    它自己点过名的那几个；**漏掉一个开关，下一条调用 main() 的用例就会替上一条执行它的分支**。
    v1.8.0 实测：``--dump-segment-rules`` 的残留让 ``test_tbx`` / ``test_tmx`` 四条用例
    先把模板写进仓库根目录、再提前退出，TMX 导入导出根本没跑到。
    兜法用「出厂值」而不是「进来之前的值」：顺序怎么变都回到同一个起点。
    """
    global _CLI_DEFAULTS
    import app as app_module

    if _CLI_DEFAULTS is None:
        _CLI_DEFAULTS = {
            name: value
            for name, value in vars(app_module).items()
            if name.startswith("CLI_")
        }
    yield
    for name, value in _CLI_DEFAULTS.items():
        setattr(app_module, name, value)


@pytest.fixture(autouse=True)
def _reset_segment_rules():
    """每条测试前后都把生效中的分段规则清空（v1.8.0）。

    ``segment_rules._ACTIVE`` 与 ``main()`` 里的 ``_apply_segment_rules()`` 是一对：
    只要有一条测试真跑过 main()，生效态就可能带着上一份规则表留到下一条用例，
    表现是切句断言随机失败——与 ``_reset_direction`` 同一类病，所以同一个兜法。
    规则文件的**落点**不归这里管，由 ``tests/test_segment_rules.py`` 自己的夹具换成 tmp。
    """
    from core import segment_rules

    segment_rules.set_active(segment_rules.Rules())
    yield
    segment_rules.set_active(segment_rules.Rules())


@pytest.fixture(autouse=True)
def _isolate_user_data(tmp_path, monkeypatch):
    """把测试期间所有"落盘到用户目录"的入口重定向到 tmp_path。

    这些常量（``USAGE_FILE`` / ``DEFAULT_DB`` / ``app.CACHE_DIR``）都是**模块级常量、
    按调用时查全局**，所以 monkeypatch 有效；别把它们改成 import 时绑定的别名。
    """
    from core import api_keys, local_translator, state
    from core.file_log import FileLog

    tmp_cache = tmp_path / "cache"
    tmp_cache.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(api_keys, "USAGE_FILE", tmp_cache / "paid_usage.jsonl")
    monkeypatch.setattr(state, "DEFAULT_DB", tmp_cache / "translations.sqlite")
    # 离线日志：LocalTranslator 在 __init__ 里构造 FileLog()，默认写项目根 logs/。
    monkeypatch.setattr(
        local_translator, "FileLog", lambda *a, **kw: FileLog(tmp_path / "logs")
    )

    app = sys.modules.get("app")
    if app is not None:
        monkeypatch.setattr(app, "CACHE_DIR", tmp_cache, raising=False)
        monkeypatch.setattr(app, "LOG_DIR", tmp_path / "logs", raising=False)
    yield
