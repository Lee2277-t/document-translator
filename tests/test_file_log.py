"""离线模式落盘日志的测试。

最关键的一条界线要在这里说清楚：**FileLog 本身并不做内容审查**，
它收到什么就写什么。"不落盘涉密内容"的保证来自**调用方**——
只有元信息（时间、页数、异常类型）被传进来，原文/译文一律不进日志。
这条线一旦被后来的修改跨过去，保密模式就不成立了，所以写在测试里，
而不是写在某个容易被忽略的地方。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.file_log import FileLog, tee_log  # noqa: E402


def test_log_file_is_created_with_meta_only(tmp_path: Path) -> None:
    file_log = FileLog(tmp_path)
    file_log.write("加载模型：nllb-200-zh-ct2")
    file_log.write("第 3 页：2 段因识别失败被跳过")

    target = file_log.path
    assert target is not None and target.exists()
    content = target.read_text(encoding="utf-8")
    assert "加载模型" in content
    assert "本次启动" in content


def test_startup_header_written_once_per_process(tmp_path: Path) -> None:
    file_log = FileLog(tmp_path)
    file_log.write("第一条")
    file_log.write("第二条")
    content = file_log.path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert content.count("本次启动") == 1
    assert "第一条" in content and "第二条" in content


def test_tee_log_reaches_both_sinks(tmp_path: Path) -> None:
    seen: list[str] = []
    emit = tee_log(seen.append, FileLog(tmp_path))
    emit("进度：1/3")

    assert seen == ["进度：1/3"]
    content = (FileLog(tmp_path).path).read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "1/3" in content


def test_broken_ui_callback_does_not_sink_the_file_log(tmp_path: Path) -> None:
    """界面回调炸了不能连累日志——反之亦然，两者互相隔离。"""

    def explode(_message: str) -> None:
        raise RuntimeError("界面已经关了")

    emit = tee_log(explode, FileLog(tmp_path))
    emit("仍然要写下去")
    content = (FileLog(tmp_path).path).read_text(encoding="utf-8")  # type: ignore[union-attr]
    assert "仍然要写下去" in content


def test_unwritable_dir_silently_degrades(tmp_path: Path) -> None:
    """写盘失败要静默：日志是辅助手段，不能反过来中断翻译。"""
    blocker = tmp_path / "not-a-dir-marker"
    blocker.write_text("占位", encoding="utf-8")
    file_log = FileLog(blocker)  # 父路径是个文件，建不了目录
    file_log.write("这条写不进去")  # 不应抛异常
