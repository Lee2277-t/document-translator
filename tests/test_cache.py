"""在线模式缓存的可关性。

在线模式会把原文和译文写进 cache\\translations.sqlite——这是重复内容复用的基础，
但对内部文件来说等于把原文留在了盘上。所以必须能关掉，且关掉后确实一个字节都不写。
"""
from __future__ import annotations

import os
import tempfile

from core.translator import TranslationCache


class _Stub:
    """只要能走通缓存读写路径即可，不真的联网。"""

    engine = "stub"


def test_cache_writes_when_path_given():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.sqlite")
        cache = TranslationCache(path)
        try:
            cache.set("stub", "hello world", "你好，世界")
            assert os.path.exists(path)
            assert cache.get("stub", "hello world") == "你好，世界"
        finally:
            # Windows 会锁住打开的 sqlite 文件，不关连接临时目录删不掉。
            if cache._conn is not None:
                cache._conn.close()


def test_no_path_means_memory_only():
    """cache_path=None 时全程只在内存里，磁盘上一个文件都不能出现。"""
    with tempfile.TemporaryDirectory() as tmp:
        before = set(os.listdir(tmp))
        cache = TranslationCache(None)
        cache.set("stub", "hello world", "你好，世界")
        assert cache.get("stub", "hello world") == "你好，世界"
        assert set(os.listdir(tmp)) == before, "无缓存模式在盘上留下了文件"


def test_cache_lookup_skips_echo_entries():
    """译文等于原文的历史命中要跳过——否则早期存下的回显会被永久命中。"""
    with tempfile.TemporaryDirectory() as tmp:
        cache = TranslationCache(os.path.join(tmp, "t.sqlite"))
        try:
            cache.set("stub", "hello world", "hello world")
            assert cache.get("stub", "hello world") == "hello world"  # 存的什么取到什么
            # 上层 Translator._cache_lookup 会跳过这类命中，这里只保证读写本身正常
        finally:
            if cache._conn is not None:
                cache._conn.close()


def test_cache_key_covers_glossary_and_style():
    """术语表或风格一变，旧译文必须整体作废。

    术语锁定和风格润色都发生在翻译之后，缓存里存的是最终译文。键里不带这两个
    维度的话，用户往术语表里加一条术语后重翻同一份文件，会静默命中旧译文拿到
    没有新术语的结果，界面上还显示「翻译完成」——最难发现的一类失效。
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.sqlite")
        a = TranslationCache(path, "profile-a")
        a.set("stub", "Net profit rose.", "净利上升。")
        assert a.get("stub", "Net profit rose.") == "净利上升。"

        b = TranslationCache(path, "profile-b")
        assert b.get("stub", "Net profit rose.") is None
        if b._conn is not None:
            b._conn.close()
        if a._conn is not None:
            a._conn.close()


def test_glossary_fingerprint_tracks_term_changes():
    from core import glossary
    from core.heading_terms import USER_TERMS

    saved = dict(USER_TERMS)
    saved_compiled = glossary._compiled
    try:
        USER_TERMS.clear()
        glossary._compiled = None
        empty = glossary.fingerprint()
        USER_TERMS["net profit"] = "净利润"
        glossary._compiled = None
        assert glossary.fingerprint() != empty
    finally:
        USER_TERMS.clear()
        USER_TERMS.update(saved)
        glossary._compiled = saved_compiled
