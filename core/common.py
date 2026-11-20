from __future__ import annotations

import os
import sys
from pathlib import Path


def app_root() -> Path:
    """工具根目录。打包成 exe 后 __file__ 指向临时解包目录，术语表、缓存、成品
    必须落在 exe 同级目录，用户才看得见、改得了。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_root() -> Path:
    """可变数据的根目录（cache/ 与 logs/ 都挂在它下面）。

    默认就是 ``app_root()``；环境变量 ``TRANSLATOR_DATA_DIR`` 可以整体改到别处，
    两个用途：① 测试进程把用户资产（付费流水、翻译记忆、离线日志）隔离到临时目录，
    绝不污染真实 cache/ 与 logs/；② 程序目录只读或要求数据集中存放的部署。

    **模型、术语表、样例仍走 ``app_root()``**：它们是程序自带资产，不随数据搬家。
    """
    override = os.environ.get("TRANSLATOR_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return app_root()


#: 保密模式口径。``app.py`` 在启动时按 ``--offline`` 置 True（与 ``app.OFFLINE``
#: 同性质：一次运行一个事实，翻译器模块不各自猜）。
CONFIDENTIAL = False


def shown_path(path) -> str:
    """回显给人的文件路径：保密模式只念文件名。

    《保密模式说明.txt》四·4 承诺「界面日志只显示文件名，不显示完整路径」，
    但成品旁边的《自检报告》《译后编辑清单》这两行一直直接拼绝对路径（v1.9.4
    的 GUI 无人值守验收实测到）。涉密文档放在哪个盘哪个文件夹本身就是要收住的
    信息——截图找人帮忙时，不该连目录一起交出去。
    """
    return os.path.basename(str(path)) if CONFIDENTIAL else str(path)


class CancelledError(Exception):
    pass


class TranslationError(Exception):
    pass


def unique_output_path(src: str, suffix: str, ext: str | None = None) -> str:
    stem, src_ext = os.path.splitext(src)
    if ext is None:
        ext = src_ext
    candidate = f"{stem}_{suffix}{ext}"
    index = 2
    while os.path.exists(candidate):
        candidate = f"{stem}_{suffix}({index}){ext}"
        index += 1
    return candidate


def check_cancel(stop_event) -> None:
    if stop_event is not None and stop_event.is_set():
        raise CancelledError()


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
