"""第二模型兜底：只在主模型判死时才换模型重试。

这里钉住两条最容易被写错的规则：
1. 不做双模型投票——两个模型的自评分不在一个量级，投票会挑错。
2. 兜底必须更严格：分数要更高、数字要齐全、不能回显，否则一律不换。
"""

from __future__ import annotations

from core.local_translator import LOW_CONFIDENCE, LocalTranslator


class _Stub:
    def __init__(self, rows: list[tuple[str, float]]) -> None:
        self.rows = rows
        self.calls: list[list[str]] = []

    def translate(self, texts, language=""):
        self.calls.append(list(texts))
        # 行数对齐：真实引擎保证一一对应
        return self.rows[: len(texts)]

    def available(self) -> bool:
        return True


def _make(rows, alt_rows):
    translator = LocalTranslator.__new__(LocalTranslator)
    translator.rescued_count = 0
    translator._rescue_warned = False
    translator._multilingual = _Stub(alt_rows)
    return translator


def test_healthy_rows_are_never_touched():
    """主模型结果都可用时不换模型，也不加载第二模型。"""
    primary = [("董事会批准了报告。", -0.4), ("营收增长了 12%。", -0.5)]
    translator = _make(primary, [])
    out = translator._rescue_rows("en", ["The board approved it.", "Revenue grew 12%."], list(primary))
    assert out == primary
    assert translator._multilingual.calls == []
    assert translator.rescued_count == 0


def test_weak_row_is_replaced_when_alt_is_better():
    primary = [("董事会批准批准批准批准批准", -1.9)]
    alt = [("董事会批准了该报告。", -0.6)]
    translator = _make(primary, alt)
    out = translator._rescue_rows("en", ["The board approved the report."], list(primary))
    assert out[0][0] == "董事会批准了该报告。"
    assert translator.rescued_count == 1


def test_alt_must_score_higher():
    """兜底分数不比主模型高就不换。"""
    primary = [("原结果。", -0.8)]
    alt = [("另一个结果。", -1.3)]
    translator = _make(primary, alt)
    out = translator._rescue_rows("en", ["Something."], list(primary))
    assert out[0] == primary[0]
    assert translator.rescued_count == 0


def test_alt_below_confidence_floor_is_rejected():
    primary = [("原结果。", -1.6)]
    alt = [("勉强的结果。", LOW_CONFIDENCE - 0.01)]
    translator = _make(primary, alt)
    out = translator._rescue_rows("en", ["Something."], list(primary))
    assert out[0] == primary[0]
    assert translator.rescued_count == 0


def test_alt_that_drops_numbers_is_rejected():
    """丢了原文数字的一律不换，哪怕分数更高。数字错比句子糙严重得多。"""
    primary = [("原结果。", -1.4)]
    alt = [("营收增长了。", -0.5)]
    translator = _make(primary, alt)
    out = translator._rescue_rows("en", ["Revenue grew 12.4 percent."], list(primary))
    assert out[0] == primary[0]
    assert translator.rescued_count == 0


def test_alt_that_echoes_source_is_rejected():
    primary = [("原结果。", -1.4)]
    alt = [("Revenue grew 12 percent.", -0.5)]
    translator = _make(primary, alt)
    source = "Revenue grew 12 percent."
    out = translator._rescue_rows("en", [source], list(primary))
    assert out[0] == primary[0]
    assert translator.rescued_count == 0


def test_candidate_ok_basics():
    translator = _make([], [])
    assert translator._candidate_ok("Hello world", "你好，世界")
    assert not translator._candidate_ok("Hello world", "")
    assert not translator._candidate_ok("Hello world", "Hello world")
    assert not translator._candidate_ok("Revenue 12.4", "营收增长。")
