"""原文回显检测回归。交接稿第 5 节设计决策 14 / 20。

微软网页接口的哨兵批量偶尔把原文单独占一行、译文写在旁边——段数校验发现不了
（数量对、内容错）。这一层是唯一能抓住它的闸门。
"""
from __future__ import annotations

from core.translator import _echoes_source


def test_clean_translation_is_not_echo():
    assert not _echoes_source("Hello world", "你好，世界")


def test_source_then_translation_is_echo():
    assert _echoes_source("Hello world", "Hello world\n你好，世界")


def test_translation_then_source_is_echo():
    assert _echoes_source("Hello world", "你好，世界\nHello world")


def test_echo_check_is_case_insensitive():
    assert _echoes_source("Hello world", "hello world\n你好，世界")


def test_single_line_repeat_is_not_echo():
    """整段只有原文、没有别的内容 —— 那是「引擎吐回原文」，由另一层处理，
    不算批量框架泄漏，不该整批作废。"""
    assert not _echoes_source("Hello world", "Hello world")
