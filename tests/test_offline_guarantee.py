"""保密模式的两条硬保证：断网 + 零落盘。

这两条是交接稿第 1 节的铁律 1 与铁律 2。它们必须在**子进程**里验证——
断网是 import app 时的模块级副作用，pytest 主进程里没法安全地开关。

跑法： ./.venv\\Scripts\\python.exe -m pytest tests -q
（这两条会真跑一次离线翻译，约 10 秒；加 -m "not slow" 可跳过）
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def _snapshot(*paths: Path) -> dict[str, tuple[int, int, str]]:
    state: dict[str, tuple[int, int, str]] = {}
    for path in paths:
        if not path.exists():
            state[str(path)] = (-1, -1, "")
            continue
        stat = path.stat()
        state[str(path)] = (
            stat.st_size,
            int(stat.st_mtime),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return state


def _run_child(script: str) -> subprocess.CompletedProcess:
    # 本文件盯的是**真实** cache/ 与 logs/：把测试专用的数据目录重定向摘掉，
    # 子进程走与用户机器完全一致的路径，才不会把「零落盘」测成「写到别处去了」。
    env = dict(os.environ)
    env.pop("TRANSLATOR_DATA_DIR", None)
    return subprocess.run(
        [str(PYTHON), "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        env=env,
    )


NETWORK_PROBE = r"""
import socket, sys, time
sys.argv = [sys.argv[0], "--offline"]   # 必须在 import app 之前就位
import app

app._enable_dpi_awareness()

failures = []
for host, port in (("huggingface.co", 443), ("cn.bing.com", 443), ("edge.microsoft.com", 443)):
    started = time.perf_counter()
    try:
        socket.create_connection((host, port), timeout=5)
        failures.append(f"{host}: 连上了（不该发生）")
    except OSError:
        elapsed = time.perf_counter() - started
        if elapsed > 1.0:
            failures.append(f"{host}: 耗时 {elapsed:.2f}s，像在等超时而不是立刻拒绝")
print("OK" if not failures else " | ".join(failures))
"""


@pytest.mark.slow
def test_network_is_hard_blocked():
    child = _run_child(NETWORK_PROBE)
    assert child.returncode == 0, child.stderr[-2000:]
    assert child.stdout.strip() == "OK", child.stdout


@pytest.mark.slow
def test_offline_translation_writes_nothing():
    """保密模式跑完整条链路后，logs/ 与 cache/ 的 size+mtime+sha256 必须一字不变。"""
    sample = ROOT / "samples" / "保密模式样例" / "示例-英文报告.docx"
    if not sample.exists():
        pytest.skip("样例缺失，跳过")

    watched = [ROOT / "logs" / "app.log", ROOT / "cache" / "translations.sqlite"]
    before = _snapshot(*watched)

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        shutil.copy2(sample, workdir / sample.name)
        script = (
            "import sys\n"
            f'sys.argv = [sys.argv[0], "--offline", "--convert", r"{workdir / sample.name}"]\n'
            "import app\n"
            "app.main()\n"
        )
        child = _run_child(script)
        assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-2000:]
        assert "已生成" in child.stdout, child.stdout

        produced = {p.name for p in workdir.glob("*")}
        # 成品 + 自检报告（自检报告是成品的一部分，见设计决策 23-②）
        assert "示例-英文报告_双语.docx" in produced

    assert _snapshot(*watched) == before, "保密模式在 logs/ 或 cache/ 留下了痕迹"


@pytest.mark.slow
def test_offline_metadata_log_holds_no_document_text():
    """按天的元信息日志可以写，但只能写元信息。

    `core/file_log.py` 的设计是「保密模式也留一份能复盘的日志，但只记时间/阶段/
    段号/异常类型」，而 FileLog 本身不做内容审查（它照抄调用方给的话）。所以这条
    界线得由测试盯住：真跑一次保密翻译，检查当天的 `logs/翻译日志_*.txt` 里
    既没有原文句子，也没有临时目录的完整路径。
    """
    from datetime import date

    from docx import Document

    sample = ROOT / "samples" / "保密模式样例" / "示例-英文报告.docx"
    if not sample.exists():
        pytest.skip("样例缺失，跳过")
    source_lines = [
        p.text.strip() for p in Document(str(sample)).paragraphs if len(p.text.strip()) > 20
    ]
    assert source_lines, "样例里没有长句，这条测试没有意义"

    log_dir = ROOT / "logs"
    log_path = log_dir / f"翻译日志_{date.today():%Y-%m-%d}.txt"
    before = log_path.read_text(encoding="utf-8") if log_path.exists() else ""

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        shutil.copy2(sample, workdir / sample.name)
        script = (
            "import sys\n"
            f'sys.argv = [sys.argv[0], "--offline", "--convert", r"{workdir / sample.name}"]\n'
            "import app\n"
            "app.main()\n"
        )
        child = _run_child(script)
        assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-2000:]

    appended = ""
    if log_path.exists():
        appended = log_path.read_text(encoding="utf-8")[len(before) :]
    # 保密模式确实会写元信息日志（这是 FileLog 存在的理由）；顺带钉住这条行为没被悄悄改掉
    assert appended.strip(), "保密模式没写元信息日志，与 core/file_log.py 的设计不符"
    for line in source_lines:
        assert line[:24] not in appended, f"元信息日志里出现了原文句子：{line[:40]}"
    assert str(workdir) not in appended, "元信息日志里写了文档的完整路径"
