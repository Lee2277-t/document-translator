"""译后风格档位与通用润色。

离线小模型不接受提示词，风格只能落在后处理上。这里钉住三条：排版润色、
正式商务语气、简洁化，以及「档位不认识时不许抛错、不许改坏」。
"""

from __future__ import annotations

from core.style import CONCISE, FORMAL, STANDARD, polish


def test_tidy_puts_space_between_cjk_and_latin():
    assert polish("采用AI技术") == "采用 AI 技术"
    assert polish("共12项") == "共 12 项"
    assert polish("the report报告") == "the report 报告"


def test_tidy_removes_space_before_punctuation():
    assert polish("增长很快 。") == "增长很快。"


def test_tidy_collapses_doubled_particles():
    assert polish("这是我们的的的方案") == "这是我们的方案"


def test_tidy_drops_empty_brackets():
    assert polish("本报告（）由财务部出具") == "本报告由财务部出具"


def test_formal_raises_register():
    assert polish("请查收附件", FORMAL) == "敬请查收附件"
    assert polish("我们已经收到你的文件。", FORMAL) == "我方已经收到贵方的文件。"


def test_formal_does_not_touch_application_word():
    """申请 里也有「请」，不能被换成 敬请。"""
    assert polish("请提交申请", FORMAL) == "敬请提交申请"


def test_concise_trims_redundancy():
    assert polish("对于小企业来说", CONCISE) == "对小企业"
    assert polish("由于天气的原因", CONCISE) == "由于天气"
    assert polish("在没有得到批准的情况下", CONCISE) == "在没有得到批准时"
    assert polish("需要注意的是，请复核。", CONCISE) == "请复核。"


def test_standard_leaves_wording_alone():
    text = "我们已经收到你的文件。"
    assert polish(text, STANDARD) == "我们已经收到你的文件。"


def test_unknown_style_falls_back_to_standard():
    assert polish("我们已经收到你的文件。", "不存在的档位") == "我们已经收到你的文件。"


def test_empty_input_is_safe():
    assert polish("", FORMAL) == ""
    assert polish("   ", CONCISE) == ""


def test_numbers_survive_polish():
    assert polish("营收为12.4%左右", STANDARD) == "营收为 12.4% 左右"
