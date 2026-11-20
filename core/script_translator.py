"""Galgame 脚本翻译统一入口：按扩展名分派到 SRT / JSON / T++ 表格。

这里只做"识别格式 + 转调"，不做任何翻译逻辑——三种格式的共同部分
（质量引擎接入、说话人传递、行数一致性）已经在各自模块里处理好。

**不做游戏解包/注入**：输入输出都是 GalTransl 生态已提取好的格式。
用户从 GalTransl/mtool 导出 → 拖进来 → 拿译文回去导入，链条闭合。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import formats
from .common import TranslationError

#: 支持的脚本格式（v1.9.11 起由登记表派生，不再各写一份）。
SUPPORTED_SUFFIXES = formats.script_suffixes()


def is_script_file(path: str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_SUFFIXES


def translate_script(
    src_path: str,
    translator,
    log: Callable[[str], None] | None = None,
    progress=None,
) -> str:
    """翻译一个脚本文件，返回成品路径。不认识的格式明确报错。"""
    suffix = Path(src_path).suffix.lower()
    if suffix in (".srt", ".vtt"):
        from .script_srt import translate_srt

        return translate_srt(src_path, translator, log, progress)
    if suffix == ".json":
        from .script_json import translate_script_json

        return translate_script_json(src_path, translator, log, progress)
    if suffix == ".xlsx":
        from .script_tpp import translate_tpp

        return translate_tpp(src_path, translator, log, progress)
    raise TranslationError(
        f"暂不支持的脚本格式 {suffix or '（无扩展名）'}；"
        f"目前支持：{'、'.join(SUPPORTED_SUFFIXES)}"
    )
