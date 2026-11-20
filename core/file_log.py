"""离线（保密）模式的落盘日志。

为什么要有它
------------
保密模式以往只把日志回调给界面，屏幕上滚过去就没了。用户拿着"第 42 页好像没翻出来"
来问时，既没 Retained 现场、也不知道当时有没有出异常——无从复盘。

为什么不直接把原文写进去
------------------------
保密模式对用户的核心承诺就是**零落盘涉密内容**。所以这里只记录元信息：
时间、阶段、页数、失败次数、异常类型。任何一句原文、译文都不写。
一旦破了这条，保密模式就名不副实了。宁可日志粗一点，也不能写内容。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from datetime import datetime
from typing import Callable

from .common import data_root

#: 与大多数 Rust 项目的习惯不同，我们只按天切文件，不做轮转删除——
#: 用户想回看三个月前那次失败时，日志还在。文件很小，不用心疼。
_LOG_DIR_NAME = "logs"

_MAX_FILE_BYTES = 4 * 1024 * 1024


class FileLog:
    """按天追加写文件的日志。线程安全，写失败静默降级（不拖累翻译）。"""

    def __init__(self, base_dir: str | os.PathLike[str] | None = None) -> None:
        base = Path(base_dir) if base_dir else data_root()
        self.dir = base / _LOG_DIR_NAME
        self._lock = threading.Lock()
        #: 本次进程是否已经写过启动头。
        self._opened = False
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.dir = None  # type: ignore[assignment]

    @property
    def path(self) -> Path | None:
        if self.dir is None:
            return None
        stamp = datetime.now().strftime("%Y-%m-%d")
        return self.dir / f"翻译日志_{stamp}.txt"

    def write(self, message: str) -> None:
        """写一行。写盘失败一律静默——日志是辅助，不能反过来打断翻译。"""
        line = f"[{time.strftime('%H:%M:%S')}] {message}\n"
        with self._lock:
            if self.dir is None:
                return
            target = self.path
            if target is None:
                return
            try:
                needs_header = not self._opened
                with target.open("a", encoding="utf-8") as handle:
                    if needs_header:
                        handle.write(
                            f"===== 本次启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n"
                        )
                    handle.write(line)
                self._opened = True
            except Exception:
                pass


def tee_log(
    primary: Callable[[str], None] | None,
    file_log: FileLog,
) -> Callable[[str], None]:
    """把一个界面日志回调和文件日志合并成一个。

    用法：``translator = LocalTranslator(log=tee_log(lines.append, FileLog()))``。
    """

    def emit(message: str) -> None:
        if primary is not None:
            try:
                primary(message)
            except Exception:
                pass
        file_log.write(message)

    return emit
