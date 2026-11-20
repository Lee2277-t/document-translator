"""命令行开关的识别口径（v1.6.7）。

拼错的开关过去会被静默当成文件路径塞进 ``rest``：``--custom=地址``（正确写法是
``--custom-url=地址``）会让整道自托管闸门跳过，``--offlin`` 少一个字母更会以
**联网模式**起界面——用户以为自己在保密模式，实际文档可能出机器。现在认不出的
``--xx`` 一律硬失败（退出码 4），并把可用开关原样列出来。

必须在子进程里验：这里盯的是 ``main()`` 的完整分发顺序（``--to=`` 的语言码校验、
免翻区规则校验都排在 ``--version`` 之前，而闸门排在之后）。

跑法： ./.venv\\Scripts\\python.exe -X utf8 -m pytest tests -q
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

HEADER = (
    "import io, contextlib, sys\n"
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

#: 带 = 的开关要喂一个**合法**值，否则会在 ``--version`` 之前就被别的校验拦下：
#: ``--to=`` 认语言码、``--no-translate=`` 认规则名，其余随便给个占位值。
VALID_VALUES = {
    "--to=": "zh",
    "--no-translate=": "off",
    "--jobs=": "2",
    "--layout=": "side",
    "--segment-rules=": "分段规则.json",
    "--style=": "standard",
    "--tm-threshold=": "0.95",
}


def _child(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PYTHON), "-X", "utf8", "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


def test_every_listed_flag_is_recognized():
    """清单里的每个开关都必须真的能被参数循环认出来。

    ``app._CLI_FLAGS`` 同时是报错时打印给用户的可用清单：表里写了而参数循环不认，
    等于给用户一个「照抄也会被拒」的写法，所以这张表必须和分发链一致。
    """
    import app

    argv_items = [
        f"{flag}{VALID_VALUES.get(flag, '1')}" if flag.endswith("=") else flag
        for flag in app._CLI_FLAGS
    ]
    # `--help` 在这一串里会**提前结束整条命令**（打帮助、退 0），所以它这一行证明的是
    # 「不被守卫拒」，不证明「参数循环走得完」——后者在 tests/test_cli_help.py 里钉。
    # --dump-segment-rules 会**真的写文件**，清单里含它就必须把落点挪进临时目录：
    # 不然测试会在真实工具目录留下一个 分段规则.json，之后每条用例都自动读到它。
    script = (
        HEADER
        + "import pathlib, tempfile\n"
        + "_dir = tempfile.mkdtemp()\n"
        + "app.segment_rules.rules_file = lambda: pathlib.Path(_dir) / app.segment_rules.RULES_FILE_NAME\n"
        + "import json\n"
        + f"items = {argv_items!r}\n"
        + "rows = {}\n"
        + "for item in items:\n"
        + "    code, out = probe(['app', item, '--version'])\n"
        + "    rows[item] = [code, '未识别的参数' in out]\n"
        + "print(json.dumps(rows, ensure_ascii=False))\n"
    )
    child = _child(script)
    assert child.returncode == 0, child.stderr[-2000:]
    rows = json.loads(child.stdout.strip().splitlines()[-1])
    assert rows, "子进程一条都没跑"
    rejected = {k: v for k, v in rows.items() if v[0] != 0 or v[1]}
    assert not rejected, f"清单里的开关被自己的参数循环拒了：{rejected}"


def test_paths_are_not_mistaken_for_flags():
    """普通路径（不以 -- 开头）照常进 rest，不该被当成拼错的开关。"""
    script = (
        HEADER
        + "code, out = probe(['app', r'D:\\tmp\\这个文件不存在.docx', '--version'])\n"
        + "print(code, '未识别的参数' in out)\n"
    )
    child = _child(script)
    assert child.returncode == 0, child.stderr[-2000:]
    assert child.stdout.strip().endswith("0 False"), child.stdout


def test_misspelled_flags_hard_fail():
    """拼错 / 形态不对的开关退 4，并给出一句「最接近的写法」。"""
    cases = [
        ["app", "--offlin", "--version"],
        ["app", "--offline", "--custom=http://127.0.0.1:9999/v1"],
        ["app", "--conver", "D:\\tmp\\a.docx"],
        ["app", "--paid", "deepl"],
        ["app", "--to", "en"],
    ]
    script = (
        HEADER
        + "import json\n"
        + f"cases = {cases!r}\n"
        + "rows = []\n"
        + "for argv in cases:\n"
        + "    code, out = probe(argv)\n"
        + "    rows.append([argv[1] if argv[1] != '--offline' else argv[2], code, out])\n"
        + "print(json.dumps(rows, ensure_ascii=False))\n"
    )
    child = _child(script)
    assert child.returncode == 0, child.stderr[-2000:]
    rows = json.loads(child.stdout.strip().splitlines()[-1])
    by_flag = {row[0]: (row[1], row[2]) for row in rows}

    for flag in ("--offlin", "--custom=http://127.0.0.1:9999/v1", "--conver", "--paid", "--to"):
        code, out = by_flag[flag]
        assert code == 4, f"{flag} 退 {code}，应该硬失败退 4：{out}"
        assert "未识别的参数" in out, f"{flag} 没出声：{out}"
        assert flag in out, f"{flag} 的报错没点名自己：{out}"

    # 提示要真能把人带到正确写法上，这是这条改动的全部价值所在
    assert "--offline" in by_flag["--offlin"][1]
    assert "--convert" in by_flag["--conver"][1]
    assert "不带值" in by_flag["--custom=http://127.0.0.1:9999/v1"][1]
    assert "--paid=" in by_flag["--paid"][1]
    # 报错里列出可用开关，用户不用去翻文档
    assert "--no-translate=" in by_flag["--offlin"][1]


def test_offline_typo_does_not_open_the_cloud_window():
    """最危险的一条：--offlin 不能悄悄起界面。

    参数循环之后就是 ``TranslatorApp()``，子进程里没有 --version 兜底时，过去会卡在
    主循环（v1.6.6 封版验收时真卡了 6 分钟）。现在应当立刻退 4 且根本没走到界面。
    """
    script = (
        HEADER
        + "code, out = probe(['app', '--offlin'])\n"
        + "print(code, '未识别的参数' in out)\n"
    )
    child = _child(script)
    assert child.returncode == 0, child.stderr[-2000:]
    assert child.stdout.strip().endswith("4 True"), child.stdout


def test_tm_threshold_clamp_speaks_on_the_command_line():
    """``--tm-threshold=0.8`` 被夹回 0.92，而且必须当场念出来。

    静默夹取最坏：用户以为放宽成功了，实际一条都没放宽，日后排查只会怀疑
    「记忆是不是坏了」。
    """
    script = (
        HEADER
        + "code, out = probe(['app', '--tm-threshold=0.8', '--version'])\n"
        + "print(code, '不能低于 0.92' in out)\n"
    )
    child = _child(script)
    assert child.returncode == 0, child.stderr[-2000:]
    assert child.stdout.strip().endswith("0 True"), child.stdout
