"""测试卫生的元测试：测试进程不许往项目目录写用户资产。

背景（交接稿 §11.5.1）：v1.5.0 起 ``tests/test_deepl.py`` 直接写真实的
``cache/paid_usage.jsonl``（每次跑测试多十几条「（密钥探测）」假账），
其中一条还把 ``confirmed=1`` 夹具写进**用户的翻译记忆**——这条假记忆会让
同一句原文之后 100% 直通到一个测试用的译文。根因是 `tests/conftest.py`
缺少重定向夹具，不是被测代码的错。

这里钉住的是夹具本身：只要有人删掉 `_isolate_user_data` 或把它改成
import 时绑定，本文件立刻变红。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_usage_ledger_goes_to_tmp():
    from core import api_keys

    api_keys.record_usage("deepl", chars=5, billed=5, file="（元测试）")

    assert ROOT not in api_keys.USAGE_FILE.parents, api_keys.USAGE_FILE
    assert "（元测试）" in api_keys.USAGE_FILE.read_text(encoding="utf-8")


def test_state_store_goes_to_tmp():
    from core import state

    store = state.StateStore()
    try:
        assert store.available
        store.upsert_tm("meta probe", "元测试", origin="auto")
        assert store.get_tm("meta probe") is not None
    finally:
        store.close()

    assert ROOT not in Path(state.DEFAULT_DB).parents, state.DEFAULT_DB
    with sqlite3.connect(state.DEFAULT_DB) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tm").fetchone()[0] == 1


def test_app_cache_dir_is_redirected_when_app_loaded():
    import app

    assert ROOT not in Path(app.CACHE_DIR).parents, app.CACHE_DIR
    assert ROOT not in Path(app.LOG_DIR).parents, app.LOG_DIR


def test_data_root_override_only_moves_cache_and_logs(tmp_path):
    """TRANSLATOR_DATA_DIR 只搬数据，模型/术语表/样例仍跟着程序目录。"""
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["TRANSLATOR_DATA_DIR"] = str(tmp_path)
    script = (
        "import app\n"
        "from core.common import app_root, data_root\n"
        "print(app_root())\n"
        "print(data_root())\n"
        "print(app.CACHE_DIR)\n"
        "print(app.LOG_DIR)\n"
    )
    child = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
    )
    assert child.returncode == 0, child.stdout + child.stderr
    root_line, data_line, cache_line, log_line = child.stdout.strip().splitlines()
    assert Path(root_line) == ROOT
    assert Path(data_line) == tmp_path.resolve()
    assert Path(cache_line) == tmp_path.resolve() / "cache"
    assert Path(log_line) == tmp_path.resolve() / "logs"


def test_child_process_stdio_is_utf8():
    """子进程断言中文输出依赖它；Windows 默认代码页会让父进程解出乱码。"""
    import subprocess
    import sys

    child = subprocess.run(
        [sys.executable, "-c", "print('保密模式不能用付费 API')"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert "保密模式不能用付费 API" in child.stdout, child.stdout
