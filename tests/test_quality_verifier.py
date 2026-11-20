"""校验器测试：规则层的确定性与 LLM 层的防御性。"""

from __future__ import annotations

import pytest

from core.quality.characters import CharacterSheet, CharacterStore
from core.quality.verifier import (
    LLMVerifier,
    RuleVerifier,
    _find_placeholders,
)
from core.state import StateStore
from core.termbase import Termbase


@pytest.fixture()
def termbase(tmp_path) -> Termbase:
    termbase = Termbase(StateStore(str(tmp_path / "state.sqlite")))
    termbase.adopt("魔導書", "魔导书")
    return termbase


@pytest.fixture()
def sheets(tmp_path) -> CharacterStore:
    store = StateStore(str(tmp_path / "state.sqlite"))
    sheets = CharacterStore(store)
    sheets.save(
        CharacterSheet(
            name="桜", first_person="人家", gender="female", forbidden=("喂喂",)
        )
    )
    sheets.save(CharacterSheet(name="太郎", gender="male"))
    return sheets


# ---------------------------------------------------------------- 占位符


def test_placeholder_pattern_is_specific() -> None:
    """Zxq0Z 要认出来，X1 / A4 不能误认——误报 Critical 会毁了报告的可信度。"""
    assert _find_placeholders("这是 Zxq0Z 占位") == ["Zxq0Z"]
    assert _find_placeholders("Ztq12Z 也要认") == ["Ztq12Z"]
    assert _find_placeholders("型号 X1 与 A4 纸") == []


def test_placeholder_loss_is_critical(termbase: Termbase) -> None:
    verifier = RuleVerifier(termbase=termbase)
    issues = verifier.verify("s1", "请看 Zxq0Z 的内容", "请看的内容")
    assert len(issues) == 1
    assert issues[0].severity == "critical"
    assert issues[0].dimension == "format"


def test_placeholder_preserved_passes(termbase: Termbase) -> None:
    verifier = RuleVerifier(termbase=termbase)
    assert verifier.verify("s1", "请看 Zxq0Z", "请看 Zxq0Z") == []


# ---------------------------------------------------------------- 术语


def test_missing_term_translation_is_major(termbase: Termbase) -> None:
    verifier = RuleVerifier(termbase=termbase)
    issues = verifier.verify("s1", "他翻开魔導書。", "他翻开了那本书。")
    terminology = [issue for issue in issues if issue.dimension == "terminology"]
    assert len(terminology) == 1
    assert terminology[0].severity == "major"
    assert "魔导书" in terminology[0].detail


def test_consistent_term_translation_passes(termbase: Termbase) -> None:
    verifier = RuleVerifier(termbase=termbase)
    assert verifier.verify("s1", "他翻开魔導書。", "他翻开魔导书。") == []


def test_term_hit_ignores_a_substring(tmp_path) -> None:
    """命中判定不能用纯子串：note 在 denote 里命中会报出一堆假问题。"""
    base = Termbase(StateStore(str(tmp_path / "tb.sqlite")))
    base.adopt("note", "注释")
    verifier = RuleVerifier(termbase=base)
    assert verifier.check_terms("s1", "Please denote the value.", "请标明数值。") == []
    assert len(verifier.check_terms("s1", "Note the value.", "请标明数值。")) == 1


def test_target_hit_survives_polish_spacing(tmp_path) -> None:
    """润色在中英边界插的空格是排版结果，不是术语丢失。"""
    base = Termbase(StateStore(str(tmp_path / "tb.sqlite")))
    base.adopt("AI技术", "AI技术")
    verifier = RuleVerifier(termbase=base)
    assert verifier.check_terms("s1", "AI技术已成熟", "AI 技术已成熟。") == []


# ---------------------------------------------------------------- 行数


def test_line_count_mismatch_is_critical(termbase: Termbase) -> None:
    verifier = RuleVerifier(termbase=termbase, check_line_count=True)
    issues = verifier.verify("s1", "第一行\n第二行", "两行合成一行")
    assert any(
        issue.severity == "critical" and issue.dimension == "format"
        for issue in issues
    )


def test_line_count_off_by_default(termbase: Termbase) -> None:
    """文档模式不强行对齐行数——默认关闭，只有脚本模式打开。"""
    verifier = RuleVerifier(termbase=termbase)
    assert verifier.verify("s1", "第一行\n第二行", "一行") == []


# ---------------------------------------------------------------- 人称


def test_single_male_character_with_her_pronoun(sheets: CharacterStore) -> None:
    verifier = RuleVerifier(character_store=sheets)
    issues = verifier.verify("s1", "太郎打開了门。", "她打开了门。")
    assert any(issue.dimension == "agent_patient" for issue in issues)


def test_multi_character_scene_defers_to_llm(sheets: CharacterStore) -> None:
    """多角色同段时分不清人称归属，规则层不猜——留给 LLM 层。"""
    verifier = RuleVerifier(character_store=sheets)
    assert verifier.verify("s1", "太郎和桜都在。", "她和他都在。") == []


def test_female_character_not_flagged(sheets: CharacterStore) -> None:
    verifier = RuleVerifier(character_store=sheets)
    assert verifier.verify("s1", "桜微笑着说。", "她微笑着说。") == []


# ---------------------------------------------------------------- 禁用表达


def test_forbidden_expression_is_persona_issue(sheets: CharacterStore) -> None:
    verifier = RuleVerifier(character_store=sheets)
    issues = verifier.verify("s1", "桜哼了一声。", "喂喂，别误会了。", speakers=["桜"])
    persona = [issue for issue in issues if issue.dimension == "persona"]
    assert len(persona) == 1
    assert "禁用表达" in persona[0].detail


def test_forbidden_not_checked_without_speakers(sheets: CharacterStore) -> None:
    verifier = RuleVerifier(character_store=sheets)
    assert verifier.verify("s1", "桜哼了一声。", "喂喂，别误会了。") == []


# ---------------------------------------------------------------- LLM 层


class _FakeEngine:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def translate(self, text: str, ctx=None) -> str:
        self.prompts.append(text)
        return self.reply


def test_llm_parses_plain_json(sheets: CharacterStore) -> None:
    engine = _FakeEngine(
        '{"issues": [{"type": "persona", "severity": "major", '
        '"detail": "语气过于温柔"}]}'
    )
    verifier = LLMVerifier(engine=engine, character_store=sheets)
    issues = verifier.verify("s1", "桜：哼。", "桜：好的呀。", speakers=["桜"])
    assert len(issues) == 1
    assert issues[0].dimension == "persona"
    assert issues[0].severity == "major"


def test_llm_parses_fenced_json(sheets: CharacterStore) -> None:
    engine = _FakeEngine(
        '```json\n{"issues": [{"type": "agent_patient", "severity": '
        '"critical", "detail": "主宾颠倒"}]}\n```'
    )
    verifier = LLMVerifier(engine=engine)
    issues = verifier.verify("s1", "a", "b")
    assert len(issues) == 1
    assert issues[0].is_blocking()


def test_llm_garbage_output_yields_no_issues() -> None:
    """模型不按格式回答时不能抛异常，也不能猜测——没有结论就是没有结论。"""
    verifier = LLMVerifier(engine=_FakeEngine("我觉得这段译得很好。"))
    assert verifier.verify("s1", "a", "b") == []


def test_llm_engine_failure_is_silent() -> None:
    class _Boom:
        def translate(self, text, ctx=None):
            raise RuntimeError("服务挂了")

    assert LLMVerifier(engine=_Boom()).verify("s1", "a", "b") == []


def test_llm_empty_issues_json() -> None:
    verifier = LLMVerifier(engine=_FakeEngine('{"issues": []}'))
    assert verifier.verify("s1", "a", "b") == []


def test_llm_prompt_includes_rule_issues(sheets: CharacterStore) -> None:
    """规则层发现的问题要带进 LLM 复核——不重复劳动。"""
    from core.quality.verifier import Issue

    engine = _FakeEngine('{"issues": []}')
    verifier = LLMVerifier(engine=engine, character_store=sheets)
    rule_issues = [Issue("major", "terminology", "s1", "术语未采用")]
    verifier.verify("s1", "src", "dst", speakers=["桜"], rule_issues=rule_issues)
    prompt = engine.prompts[0]
    assert "术语未采用" in prompt
    assert "自称用「人家」" in prompt
