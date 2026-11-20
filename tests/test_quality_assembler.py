"""上下文装配测试：预算分配、命中术语、摘要取舍。"""

from __future__ import annotations

from core.quality.assembler import ContextAssembler
from core.quality.characters import CharacterSheet, CharacterStore
from core.state import StateStore
from core.termbase import Termbase


def _termbase(tmp_path) -> Termbase:
    store = StateStore(str(tmp_path / "state.sqlite"))
    termbase = Termbase(store)
    termbase.adopt("魔導書", "魔导书")
    termbase.adopt("冒険者協会", "冒险者协会")
    termbase.adopt(" unrelated", "无关词")
    return termbase


def test_only_matched_terms_are_injected(tmp_path) -> None:
    """术语库 1000 条，单批只注入真正命中的那几条。"""
    assembler = ContextAssembler(termbase=_termbase(tmp_path))
    result = assembler.assemble(["他打开了魔導書。"])
    assert result.context.terms == (("魔導書", "魔导书"),)
    assert "魔導書 → 魔导书" in result.block
    assert "冒険者協会" not in result.block
    assert result.stats["terms"] == 1


def test_budget_priority_terms_over_summary(tmp_path) -> None:
    """预算不够时砍摘要，不砍术语——术语违反是错译，摘要只是连贯性。"""
    store = StateStore(str(tmp_path / "state.sqlite"))
    termbase = Termbase(store)
    for index in range(60):  # 远超 max_terms 的截断
        termbase.adopt(f"术语{index}", f"译名{index}")
    assembler = ContextAssembler(termbase=termbase, budget=200)
    sources = [f"这里有术语{index}。" for index in range(60)]
    result = assembler.assemble(sources, summary="前文说了很多很多很多事情" * 20)
    assert result.stats["terms"] <= 40
    assert result.context.summary == "", "预算被术语吃光时摘要必须让位"


def test_characters_only_when_present(tmp_path) -> None:
    store = StateStore(str(tmp_path / "state.sqlite"))
    sheets = CharacterStore(store)
    sheets.save(
        CharacterSheet(name="桜", first_person="人家", gender="female")
    )
    sheets.save(CharacterSheet(name="博士", tone="古风"))
    assembler = ContextAssembler(character_store=sheets, budget=800)

    result = assembler.assemble(["桜突然出现了。"], speakers=["桜"])
    assert result.context.characters == ("桜",)
    assert result.context.character_sheets == (("桜", "自称用「人家」"),)
    assert "博士" not in result.block, "不在场的角色不应注入"


def test_mentioned_character_joins_scene(tmp_path) -> None:
    store = StateStore(str(tmp_path / "state.sqlite"))
    sheets = CharacterStore(store)
    sheets.save(CharacterSheet(name="博士", tone="古风"))
    assembler = ContextAssembler(character_store=sheets)
    result = assembler.assemble(["博士点了点头。"])
    assert result.context.characters == ("博士",)


def test_summary_enters_context_but_not_cache_profile(tmp_path) -> None:
    """摘要进上下文、不进缓存键——否则同一句换个场次就永远 miss。"""
    assembler = ContextAssembler(termbase=_termbase(tmp_path))
    result = assembler.assemble(
        ["他打开了魔導書。"], summary="上一场他们在城堡里。"
    )
    assert result.context.summary == "上一场他们在城堡里。"
    assert "前文摘要" in result.block
    profile_with = result.context.cache_profile()

    without = assembler.assemble(["他打开了魔導書。"])
    assert profile_with == without.context.cache_profile(), (
        "场景摘要绝不能进缓存键"
    )


def test_rolling_summary_is_tail_of_previous_scene() -> None:
    """摘要是上一场景拼接后的尾部——零推理开销的直接衔接。"""
    assembler = ContextAssembler(tail_chars=10)
    summary = assembler.rolling_summary(["第一段内容", "第二段内容"])
    assert summary == ("第一段内容\n第二段内容")[-10:]
    assert summary.endswith("第二段内容")


def test_empty_assembler_is_inert() -> None:
    """没有术语库也没有角色库时，不注入任何东西。"""
    result = ContextAssembler().assemble(["anything"])
    assert result.block == ""
    assert result.context.terms == ()
    assert result.context.characters == ()
    assert result.stats == {
        "terms": 0,
        "characters": 0,
        "sheets": 0,
        "summary": 0,
        "budget_used": 0,
    }
