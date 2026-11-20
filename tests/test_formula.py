"""公式与统计量保护。借鉴 Zotero 生态 PDF2zh 的「公式识别」。

学术文献里 E = mc²、p < 0.01 这类片段送进翻译引擎必被拆成句子翻坏，
所以判定为公式的一律保留原文。判定必须保守：漏判只是没翻到，误判就是丢内容。
"""
from __future__ import annotations

from core.translator import is_translatable, looks_like_formula, should_translate


def test_classic_equation_is_formula():
    assert looks_like_formula("E = mc\u00b2")
    assert not should_translate("E = mc\u00b2")


def test_subscript_notation_is_formula():
    assert looks_like_formula("x\u1d62 + y\u2c7c = z\u2096")


def test_statistics_line_is_formula():
    assert looks_like_formula("R\u00b2 = 0.85, p < 0.01")
    assert looks_like_formula("\u03b1 = 0.05")


def test_chemical_formula_is_formula():
    assert looks_like_formula("H\u2082O + CO\u2082 \u2192 H\u2082CO\u2083")


def test_real_sentence_is_not_formula():
    """句子里有大把三字母以上的词，绝不能被当成公式。"""
    assert not looks_like_formula("The cost is $5 plus $3 per unit")
    assert should_translate("The cost is $5 plus $3 per unit")


def test_table_total_with_one_operator_is_not_formula():
    """只有一个算子还带实词 —— 放过去，交给引擎。宁可漏判也不误判。"""
    assert not looks_like_formula("Total = 1,500")
    assert should_translate("Total = 1,500")


def test_accounting_heading_is_not_formula():
    assert not looks_like_formula("IFRS 17 requires separate presentation")
    assert should_translate("IFRS 17 requires separate presentation")


def test_formula_is_not_translatable_even_via_term_table():
    assert not is_translatable("n\u2081 + n\u2082 = 42")
