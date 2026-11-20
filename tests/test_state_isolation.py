"""统一状态库测试：同库多表的界线——清缓存不能毁术语库。"""

from __future__ import annotations

from core.state import StateStore


def test_clear_translations_keeps_terms_and_tm(tmp_path) -> None:
    """「清除缓存」只清译文缓存：术语/角色/场景/TM 必须活下来。"""
    store = StateStore(str(tmp_path / "state.sqlite"))
    store.upsert_term("魔導書", "魔导书")
    store.upsert_tm("请确认条款", "请确认条款（译文）")
    store.upsert_character("桜", {"name": "桜", "first_person": "人家"})
    store.add_scene("sc0001", "前文摘要")

    # 造几条译文缓存（与线上 Translator 同一张表）
    store._conn.execute(
        "CREATE TABLE IF NOT EXISTS translations ("
        "key TEXT PRIMARY KEY, translated TEXT NOT NULL, created_at INTEGER NOT NULL)"
    )
    store._conn.execute(
        "INSERT OR REPLACE INTO translations VALUES ('k1', '旧译文', 1)"
    )
    store._conn.commit()

    store.clear_translations()

    assert store.get_term("魔導書") is not None
    assert store.get_tm("请确认条款") == "请确认条款（译文）"
    assert store.get_character("桜") is not None
    assert store.recent_scenes(1) == ["前文摘要"]
    # 缓存表真的空了
    row = store._conn.execute("SELECT COUNT(*) FROM translations").fetchone()
    assert row[0] == 0


def test_memory_mode_survives_clear(tmp_path) -> None:
    """:memory: 或坏路径下清缓存不能抛异常。"""
    store = StateStore(":memory:")
    store.clear_translations()  # 不抛异常即通过


def test_tm_round_trip(tmp_path) -> None:
    store = StateStore(str(tmp_path / "s.sqlite"))
    store.upsert_tm("句子甲", "译文甲")
    store.upsert_tm("句子甲", "译文甲改")  # 同名更新
    assert store.get_tm("句子甲") == "译文甲改"
    assert store.count_tm() == 1
    assert store.all_tm() == [("句子甲", "译文甲改")]
