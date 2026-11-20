"""角色卡测试：结构化字段、渲染、禁用表达、持久化。"""

from __future__ import annotations

from core.quality.characters import CharacterSheet, CharacterStore
from core.state import StateStore


def test_round_trip_through_dict() -> None:
    sheet = CharacterSheet(
        name="桜",
        first_person="人家",
        second_person="你",
        tone="傲娇，嘴上不饶人",
        forbidden=("人家", "人家才不是"),
        gender="female",
        notes="二年级，班长",
    )
    restored = CharacterSheet.from_dict(sheet.to_dict())
    assert restored == sheet


def test_forbidden_accepts_string() -> None:
    sheet = CharacterSheet.from_dict({"name": "x", "forbidden": "人家，人家才不是"})
    assert sheet.forbidden == ("人家", "人家才不是")


def test_prompt_skips_empty_fields() -> None:
    sheet = CharacterSheet(name="桜", first_person="人家")
    assert sheet.to_prompt() == "自称用「人家」"
    assert CharacterSheet(name="空").to_prompt() == ""


def test_violations() -> None:
    sheet = CharacterSheet(name="先生", forbidden=("人家", "啦啦啦"))
    assert sheet.violations("人家才不去呢") == ["人家"]
    assert sheet.violations("我明白了") == []
    assert sheet.violations("") == []


def test_character_store_persists(tmp_path) -> None:
    store = StateStore(str(tmp_path / "state.sqlite"))
    sheets = CharacterStore(store)
    sheets.save(CharacterSheet(name="桜", first_person="人家", gender="female"))
    sheets.save(CharacterSheet(name="空"))  # 空卡也要能存，读取时过滤

    loaded = sheets.get("桜")
    assert loaded is not None
    assert loaded.first_person == "人家"

    present = sheets.present_sheets(["桜", "空", "不存在"])
    assert present == [("桜", "自称用「人家」")]
    assert set(sheets.all()) == {"桜", "空"}
