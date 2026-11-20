"""命令行输出的编码分流（v1.9.0，欠了四轮的老债：脚本侧要「先试 UTF-8、解不动退 GBK」）。

`app.py` 头部那条注释写了口径，本文件只管一件事：**两条路各自都走对了，而且互相不抢**。

- **连着控制台**（`isatty()` 真）保持本机代码页，只把 `errors` 降成 `backslashreplace`。
   cmd 下眼看中文的用户照旧看中文；GBK 装不下的那几个字符降级成 `\\uXXXX` 写法。
- **被重定向或走管道**（`isatty()` 假）整条换成 UTF-8，脚本侧拿到的是确定编码。
- **用户显式点了别的编码时一个字都不抢**（v1.9.2 起看 `PYTHONIOENCODING` 的**值**而不是「有没有设」）：
  点了 `gbk` 就保持 `gbk`，显式意图没有道理被程序盖掉，第 6 项真机场正是靠它钉住
  「GBK 控制台不崩」这条 v1.8.0 的修复；点的本来就是 UTF 系时**照样**换成 UTF-8——
  免安装版的解释器根本不吃这个 env（v1.9.1 封版实测：同条件源树给 UTF-8、包给 GBK），
  弃权等于把用户要的 UTF-8 换成他的本机代码页。

两条真机场用例（7、8）刻意**不加 `-X utf8`**、也不设 `PYTHONIOENCODING`：
加了就等于自己把被测的那条分支喂成期望值，断言恒假。中文一律从文件里读，
不从命令行传——Windows 上 argv 走本机代码页，`›` 这类字符传进去会变成代理对。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from core import segment_rules

ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = str(ROOT / ".venv" / "Scripts" / "python.exe")


class _FakeStream:
    """假流：只记录 reconfigure 收到了什么，不真碰任何文件描述符。"""

    def __init__(self, tty: bool, *, isatty_raises: bool = False,
                 reconfigure_raises: type[Exception] | None = None) -> None:
        self._tty = tty
        self._isatty_raises = isatty_raises
        self._reconfigure_raises = reconfigure_raises
        self.calls: list[dict] = []

    def isatty(self) -> bool:
        if self._isatty_raises:
            raise OSError("流已经关了")
        return self._tty

    def reconfigure(self, **kwargs) -> None:
        if self._reconfigure_raises is not None:
            raise self._reconfigure_raises("这条流不接受")
        self.calls.append(kwargs)


class _NoReconfigure:
    """老式包装流（pytest 捕获层就是这种）：没有 reconfigure 这个方法。"""


# ---------------------------------------------------------------- 单元：分流规则

def test_console_keeps_codepage_and_only_downgrades_errors(monkeypatch):
    from app import _reconfigure_streams

    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    stream = _FakeStream(tty=True)
    _reconfigure_streams((stream,))
    assert stream.calls == [{"errors": "backslashreplace"}]


def test_redirected_stream_becomes_utf8(monkeypatch):
    from app import _reconfigure_streams

    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    stream = _FakeStream(tty=False)
    _reconfigure_streams((stream,))
    assert stream.calls == [{"encoding": "utf-8", "errors": "backslashreplace"}]


def test_explicit_pythonioencoding_beats_the_pipeline_rule(monkeypatch):
    """用户显式点了**别的**编码时，就算在管道里也不许改他的 encoding（v1.9.2 起看值不看有无）。"""
    from app import _reconfigure_streams

    monkeypatch.setenv("PYTHONIOENCODING", "gbk")
    stream = _FakeStream(tty=False)
    _reconfigure_streams((stream,))
    assert stream.calls == [{"errors": "backslashreplace"}]


@pytest.mark.parametrize("value", [
    "utf-8", "UTF-8", "utf8", "UTF_8", "u8", "cp65001", "utf-8:backslashreplace", "  utf-8  ",
])
def test_utf_family_values_recognized(monkeypatch, value):
    """认 UTF 系只认**值**：这几写法的用户要的就是 UTF-8，程序设 UTF-8 不算抢他的意图。"""
    from app import _env_wants_utf8

    monkeypatch.setenv("PYTHONIOENCODING", value)
    assert _env_wants_utf8() is True


@pytest.mark.parametrize("value", ["gbk", "cp936", "GB2312", "latin-1", "big5", "utf-16", ":", "??"])
def test_non_utf_family_values_not_mistaken(monkeypatch, value):
    """别的编码（含坏写法）一律算「用户点了非 UTF」，弃权；`utf-16` 也不许当成 UTF-8。"""
    from app import _env_wants_utf8

    monkeypatch.setenv("PYTHONIOENCODING", value)
    assert _env_wants_utf8() is False


@pytest.mark.parametrize("value", [None, ""])
def test_absent_or_empty_env_is_not_a_request(monkeypatch, value):
    """没设与设成空串都算「没表态」，走管道那条换 UTF-8。"""
    from app import _env_wants_utf8

    if value is None:
        monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    else:
        monkeypatch.setenv("PYTHONIOENCODING", value)
    assert _env_wants_utf8() is False


def test_utf8_env_in_a_pipeline_still_becomes_utf8(monkeypatch):
    """v1.9.2 的正题：管道里 `PYTHONIOENCODING=utf-8` 必须**照样**换成 UTF-8。

    弃权分支原本看的是「有没有设」，于是这份意图在免安装版里落空——冻结包的解释器根本不吃
    这个 env（v1.9.1 封版实测：同条件源树给 UTF-8、包给 GBK），弃权把用户要的 UTF-8
    换成了他的本机代码页。
    """
    from app import _reconfigure_streams

    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    stream = _FakeStream(tty=False)
    _reconfigure_streams((stream,))
    assert stream.calls == [{"encoding": "utf-8", "errors": "backslashreplace"}]


def test_utf8_env_on_a_console_still_keeps_the_codepage(monkeypatch):
    """连着控制台时 `=utf-8` 也不许换编码：第 ① 条的判据只有 `isatty()`，与值无关。"""
    from app import _reconfigure_streams

    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    stream = _FakeStream(tty=True)
    _reconfigure_streams((stream,))
    assert stream.calls == [{"errors": "backslashreplace"}]


def test_unusable_streams_are_skipped_never_raised(monkeypatch):
    from app import _reconfigure_streams

    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    _reconfigure_streams((None, _NoReconfigure(), _FakeStream(tty=True, isatty_raises=True),
                          _FakeStream(tty=False, reconfigure_raises=ValueError),
                          _FakeStream(tty=False, reconfigure_raises=OSError)))


def test_one_bad_stream_does_not_cost_the_other(monkeypatch):
    """坏流不能把同伴一起带走：stderr 问不出 tty 时 stdout 还是要换 UTF-8。"""
    from app import _reconfigure_streams

    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    bad = _FakeStream(tty=False, isatty_raises=True)
    good = _FakeStream(tty=False)
    _reconfigure_streams((bad, good))
    assert good.calls == [{"encoding": "utf-8", "errors": "backslashreplace"}]


# ---------------------------------------------------------------- 真机场：子进程

def _run(args: list[str], extra_env: dict[str, str] | None = None,
         timeout: int = 300) -> subprocess.CompletedProcess:
    """起一个「管道两端」的子进程：不传 `-X utf8`、清掉 `PYTHONIOENCODING`。"""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    env.update(extra_env or {})
    return subprocess.run([VENV_PYTHON, "app.py", *args], cwd=str(ROOT),
                          capture_output=True, env=env, timeout=timeout)


def _rangle_rules(tmp_path) -> Path:
    """写一份把 `›`（U+203A，GBK 里没有这个字）当句末标点的规则表。"""
    rules = tmp_path / segment_rules.RULES_FILE_NAME
    rules.write_text(json.dumps({segment_rules.KEY_LATIN: ["›"]}, ensure_ascii=False),
                     encoding="utf-8")
    return rules


def _placeholder(tmp_path) -> Path:
    sample = tmp_path / "占位.srt"
    sample.write_text("1\n00:00:01,000 --> 00:00:03,000\nAlpha › Beta › Gamma\n\n",
                      encoding="utf-8")
    return sample


@pytest.mark.slow
def test_pipeline_output_is_strict_utf8_and_keeps_the_rangle(tmp_path):
    """重定向出去的字节必须是 UTF-8，`›` 原样在场、不再写成 \\u203a。

    判据用「严格 UTF-8 解码」当闸门：同一份中文回显在 GBK 下解不动，会直接抛
    `UnicodeDecodeError`——所以这条断言不是恒假的。
    """
    rules, sample = _rangle_rules(tmp_path), _placeholder(tmp_path)
    child = _run(["--offline", f"--segment-rules={rules}", "--convert", str(sample)])
    assert child.returncode == 0, child.stderr.decode("utf-8", "replace")[-800:]
    out = child.stdout.decode("utf-8")  # 解不动就是这条没做到
    assert "分段规则：须跟空白的标点 1 个" in out
    assert "›" in out
    assert "\\u203a" not in out


@pytest.mark.slow
def test_explicit_gbk_still_wins_and_only_degrades_that_one_char(tmp_path):
    """用户点名 GBK 时保持 GBK：`›` 降级成 \\u203a 写法，但成品照常、退码照常 0。"""
    rules, sample = _rangle_rules(tmp_path), _placeholder(tmp_path)
    child = _run(["--offline", f"--segment-rules={rules}", "--convert", str(sample)],
                 {"PYTHONIOENCODING": "gbk"})
    assert child.returncode == 0, child.stderr.decode("gbk", "replace")[-800:]
    out = child.stdout.decode("gbk", "replace")
    assert "UnicodeEncodeError" not in out
    assert "分段规则：须跟空白的标点 1 个" in out
    assert "\\u203a" in out


def test_explicit_utf8_env_yields_utf8_bytes_under_a_pipeline(tmp_path):
    """真子进程：管道里 `PYTHONIOENCODING=utf-8` 拿到的必须是**严格 UTF-8**（v1.9.2 的正题）。

    v1.9.1 封版实测：同一条命令、同样重定向，源树给 UTF-8 而冻结包给 GBK——弃权分支原本只看
    「有没有设这个 env」，而打包后的解释器并没有把这个值落到 stdio 上。判据只用
    「严格 UTF-8 解得动」这一道闸门（同一份中文在 GBK 下解不动会直接抛），不盯文案措辞。
    """
    import app

    child = _run(["--version"], {"PYTHONIOENCODING": "utf-8"})
    assert child.returncode == 0, child.stderr.decode("utf-8", "replace")[-800:]
    text = child.stdout.decode("utf-8")
    assert f"v{app.VERSION}" in text


def test_stderr_follows_the_same_rule_as_stdout(tmp_path):
    """两条流一起换：只改 stdout 的话，traceback 里的中文照样砸在脚本脸上。"""
    script = tmp_path / "probe.py"
    script.write_text(
        "import sys\n"
        "import app\n"
        "print('\u5bf9\u7167 \\u203a')\n"
        "print('\u9519\u8bef\u6d41 \\u203a', file=sys.stderr)\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    env["PYTHONPATH"] = str(ROOT)  # 探针脚本在 tmp 里，`app` 要从仓库根目录找
    child = subprocess.run([VENV_PYTHON, str(script)], cwd=str(ROOT),
                           capture_output=True, env=env, timeout=300)
    assert child.returncode == 0, child.stderr.decode("utf-8", "replace")[-800:]
    assert "对照 ›" in child.stdout.decode("utf-8")
    assert "错误流 ›" in child.stderr.decode("utf-8")


def test_version_under_a_pipeline_is_utf8_and_exits_zero():
    """换编码不许动退码，也不动内容：--version 这条最干净的通路要先立住。"""
    import app

    child = _run(["--version"])
    assert child.returncode == 0
    assert f"v{app.VERSION}" in child.stdout.decode("utf-8")


def test_error_report_reaches_the_pipeline_in_utf8_too():
    """退码 4 的报错清单也要是 UTF-8：脚本侧读报错说明，不该再猜编码。

    判据只盯「有一整段中文」这一件事，不盯具体措辞——措辞是 v1.6.7 那批用例的责任，
    这里再钉一遍会变成两处同时改的耦合。
    """
    child = _run(["--segment-rule=x.json"])
    assert child.returncode == 4, child.stdout.decode("utf-8", "replace")[-600:]
    out = child.stdout.decode("utf-8")  # 解不动就是这条没做到
    assert "--segment-rules=" in out
    assert re.search(r"[\u4e00-\u9fff]{2,}", out), out[:400]


def test_import_side_effect_reaches_the_real_streams():
    """模块导入期就分流：真 stdout 在管道里时，`app` 一被导入它就已是 UTF-8。

    这条守的是「调用点在模块顶层」这件事——写进函数里没人调用，等于没修。
    """
    code = ("import sys\n"
            "import app\n"
            "print(sys.stdout.encoding, sys.stdout.errors, sys.stdout.isatty(), "
            "sys.stderr.encoding, sys.stderr.errors)\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    child = subprocess.run([VENV_PYTHON, "-c", code], cwd=str(ROOT),
                           capture_output=True, env=env, timeout=300)
    assert child.returncode == 0, child.stderr.decode("utf-8", "replace")[-800:]
    out = child.stdout.decode("utf-8").strip()
    stdout_enc, stdout_err, tty, stderr_enc, stderr_err = out.split()
    assert tty == "False"
    assert (stdout_enc, stdout_err) == ("utf-8", "backslashreplace")
    assert (stderr_enc, stderr_err) == ("utf-8", "backslashreplace")
