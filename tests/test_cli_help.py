# -*- coding: utf-8 -*-
"""`--help` 这一屏（v1.9.1，目标模式 Round 11）。

为什么单独成一个文件：v1.6.7 的未知参数守卫把「可用开关」列成了一行扁平清单，
`--help` 过去也走那条路、以退码 4 结束——用户想查用法，收到的是一份写着「你写错了」
的帮助。现在这一屏是**每个开关唯一的人类可读说明**，所以它必须被钉住三件事：

1. **覆盖率**：`_CLI_FLAGS` 里每一项都要在这屏出现，且这屏不许讲一个不存在的开关
   （双向核对。加了开关忘了写说明 → 红；说明里手抖写错开关名 → 也红）。
2. **优先于校验**：`--help` 必须比 `--to=` 的语言码校验、比未知参数守卫都早生效。
   同一条命令里既有 `--help` 又有拼错的开关时，用户要的是这一屏，退码 0。
3. **不落盘**：帮助不许写任何文件（`--dump-segment-rules` 会真长出模板，这一条得对比着钉）。

必须在子进程里验的部分同 `test_cli_arguments.py`：盯的是 `main()` 的完整分发顺序。

跑法： ./.venv\\Scripts\\python.exe -X utf8 -m pytest tests -q
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

HEADER = (
    "import io, contextlib, sys, os, pathlib\n"
    'sys.argv = ["app"]\n'
    "import app\n"
    "def probe(argv):\n"
    "    sys.argv = list(argv)\n"
    "    buf = io.StringIO()\n"
    "    code = 0\n"
    "    try:\n"
    "        with contextlib.redirect_stdout(buf):\n"
    "            app.main()\n"
    "    except SystemExit as exc:\n"
    "        code = exc.code if isinstance(exc.code, int) else 1\n"
    "    return code, buf.getvalue()\n"
)


def _child(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(VENV_PYTHON), "-X", "utf8", "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env=env,
    )


def _help_rows(app) -> list[tuple[str, str]]:
    """把分组表摊平成 (开关写法, 说明) 的列表。"""
    return [pair for _, items in app._CLI_HELP_GROUPS for pair in items]


def _flag_name(written: str) -> str:
    """从帮助那一栏的写法回收开关名：`--jobs=N` → `--jobs=`，`--offline` → `--offline`。"""
    head = written.split(" ", 1)[0]
    return head.split("=", 1)[0] + "=" if "=" in head else head


def test_help_table_covers_every_listed_flag():
    """双向核对：清单里的每个开关都在这屏出现，这屏也不讲不存在的开关。"""
    import app

    rows = _help_rows(app)
    names = [_flag_name(row[0]) for row in rows]
    assert len(set(names)) == len(names), "帮助里有重复的开关"
    missing = sorted(set(app._CLI_FLAGS) - set(names))
    assert not missing, f"这些开关没写进 --help：{missing}"
    extra = sorted(set(names) - set(app._CLI_FLAGS))
    assert not extra, f"--help 讲了不存在的开关：{extra}"


def test_help_output_contains_version_and_group_titles(capsys):
    """这一屏长出来该有的三样：版本号、九组标题、指向《使用说明.txt》。"""
    import app

    app.print_help()
    out = capsys.readouterr().out
    assert f"v{app.VERSION}" in out
    for title, _ in app._CLI_HELP_GROUPS:
        assert title in out
    assert "《使用说明.txt》" in out
    assert "用法：" in out


def test_help_is_one_line_per_flag_and_hints_the_default(capsys):
    """每个开关一行、不许出现「同上/见前」这种要回头找的写法。

    这屏是命令行唯一的说明，写成两行或带指代就等于把一半用户挡在门外。
    """
    import app

    app.print_help()
    out = capsys.readouterr().out
    assert "同上" not in out and "见前" not in out
    for flag, desc in _help_rows(app):
        line = next(l for l in out.splitlines() if l.strip().startswith(flag))
        assert desc[:12] in line, f"{flag} 的说明没落在一行里"


def test_help_mentions_the_confidentiality_route(capsys):
    """保密口径必须留在第一屏：这是本项目最要紧的一条红线，不能只写在随包文档里。"""
    import app

    app.print_help()
    out = capsys.readouterr().out
    assert "保密模式" in out
    assert "内容不出本机" in out


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_exits_zero_with_the_same_screen(flag):
    """`--help` 与 `-h` 都退 0、打同一屏；`-h` 不以 -- 开头，过去会静默当成文件路径。"""
    script = (
        HEADER
        + "import json\n"
        + f"code, out = probe(['app', {flag!r}])\n"
        + "print(json.dumps([code, '用法：' in out, '模式与入口' in out, len(out)], ensure_ascii=False))\n"
    )
    child = _child(script)
    assert child.returncode == 0, child.stderr[-2000:]
    code, has_usage, has_group, length = json.loads(child.stdout.strip().splitlines()[-1])
    assert code == 0, "帮助不该带非 0 退码"
    assert has_usage and has_group
    assert length > 1500, f"这一屏只有 {length} 字符，说明没打全"


def test_help_wins_over_a_misspelled_companion():
    """同一条命令里既有 --help 又有拼错的开关：给帮助、退 0（与 argparse 同口径）。"""
    script = (
        HEADER
        + "import json\n"
        + "code, out = probe(['app', '--offlin', '--help'])\n"
        + "print(json.dumps([code, '未识别的参数' in out, '用法：' in out], ensure_ascii=False))\n"
    )
    child = _child(script)
    assert child.returncode == 0, child.stderr[-2000:]
    code, complained, has_usage = json.loads(child.stdout.strip().splitlines()[-1])
    assert [code, complained, has_usage] == [0, False, True]


def test_help_wins_over_a_bad_language_code():
    """帮助也不许被 `--to=` 的语言码校验挡住：查用法的人不该先被判「你参数写错了」。"""
    script = (
        HEADER
        + "import json\n"
        + "code, out = probe(['app', '--to=zzz', '--help'])\n"
        + "print(json.dumps([code, '用法：' in out], ensure_ascii=False))\n"
    )
    child = _child(script)
    assert child.returncode == 0, child.stderr[-2000:]
    code, has_usage = json.loads(child.stdout.strip().splitlines()[-1])
    assert [code, has_usage] == [0, True]


def test_unknown_flag_report_now_points_at_help():
    """守卫的报错里要指路：扁平清单只列名字，说清每个干什么得去看这一屏。"""
    script = (
        HEADER
        + "import json\n"
        + "code, out = probe(['app', '--segment-rule=x.json'])\n"
        + "print(json.dumps([code, '未识别的参数' in out, '每个开关干什么' in out], ensure_ascii=False))\n"
    )
    child = _child(script)
    assert child.returncode == 0, child.stderr[-2000:]
    code, complained, points = json.loads(child.stdout.strip().splitlines()[-1])
    assert code == 4, "拼错的开关仍然必须硬失败"
    assert complained and points


def test_help_writes_nothing_to_disk():
    """帮助是纯输出：不许像 `--dump-segment-rules` 那样在目录里长出文件。"""
    script = (
        HEADER
        + "import json, tempfile\n"
        + "_dir = tempfile.mkdtemp()\n"
        + "os.chdir(_dir)\n"
        + "before = sorted(os.listdir(_dir))\n"
        + "code, out = probe(['app', '--help'])\n"
        + "after = sorted(os.listdir(_dir))\n"
        + "print(json.dumps([code, before == after, after], ensure_ascii=False))\n"
    )
    child = _child(script)
    assert child.returncode == 0, child.stderr[-2000:]
    code, same, after = json.loads(child.stdout.strip().splitlines()[-1])
    assert [code, same, after] == [0, True, []]


def test_help_through_a_real_pipe_is_utf8_and_exits_zero():
    """真子进程 + 管道（v1.9.0 那一路）：严格 UTF-8 可解、退 0、中文原样。"""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    child = subprocess.run(
        [str(VENV_PYTHON), "-c", "import app, sys; sys.argv=['app','--help']; app.main()"],
        cwd=str(ROOT),
        capture_output=True,
        env=env,
        timeout=180,
    )
    assert child.returncode == 0, child.stderr.decode("utf-8", "replace")[-800:]
    out = child.stdout.decode("utf-8")  # 解不动就是这条没做到
    assert "用法：" in out and "断句规则" in out


def test_help_survives_a_gbk_console():
    """显式 GBK 控制台：这一屏全是中文与 ASCII，应当照常打全、不降级不崩。"""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    env["PYTHONIOENCODING"] = "gbk"
    child = subprocess.run(
        [str(VENV_PYTHON), "-c", "import app, sys; sys.argv=['app','--help']; app.main()"],
        cwd=str(ROOT),
        capture_output=True,
        env=env,
        timeout=180,
    )
    assert child.returncode == 0, child.stderr.decode("gbk", "replace")[-800:]
    out = child.stdout.decode("gbk")
    assert "用法：" in out
    assert "\\u" not in out, "帮助里没有 GBK 装不下的字符，出现降级写法就是异常"
