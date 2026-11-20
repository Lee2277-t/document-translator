"""术语锁定：用户术语表现在要在正文长句里也强制生效。

之前术语表只用于「整段就是一条术语」的情况，正文里的专业词管不到，长文档里
同一个词被译成三四种译法也没人发现。这里钉住掩蔽→翻译→还原这条链。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from core import glossary
from core.heading_terms import USER_TERMS

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _reset_glossary():
    saved = dict(USER_TERMS)
    saved_compiled = glossary._compiled
    saved_inflection = glossary.inflection_on()
    yield
    USER_TERMS.clear()
    USER_TERMS.update(saved)
    glossary._compiled = saved_compiled
    glossary.set_inflection(saved_inflection)


def _set_terms(**pairs: str) -> None:
    USER_TERMS.clear()
    USER_TERMS.update({key.lower(): value for key, value in pairs.items()})
    glossary._compiled = None


def test_placeholder_is_engine_safe():
    """占位符必须是不含标点、不含 Unicode 符号的字母数字串。

    实测 ⟦T1⟧ 这类形状会被 CTranslate2 的分词器整个吃掉，只剩纯字母数字能穿过。
    """
    assert re.fullmatch(r"[A-Za-z0-9]+", "Zxq0Z")


def test_lock_masks_term_inside_a_sentence():
    _set_terms(**{"net profit": "净利润"})
    masked, mapping = glossary.lock(
        "Net profit attributable to shareholders decreased by 12.4 percent."
    )
    assert "net profit" not in masked.lower()
    assert mapping == {"Zxq0Z": "净利润"}


def test_unlock_restores_official_term():
    _set_terms(**{"net profit": "净利润"})
    masked, mapping = glossary.lock("The net profit fell sharply this quarter.")
    translated = "Zxq0Z本季度大幅下降。"
    assert glossary.unlock(translated, mapping) == "净利润本季度大幅下降。"


def test_unlock_cleans_spaces_the_model_adds():
    _set_terms(**{"net profit": "净利润"})
    masked, mapping = glossary.lock("The net profit fell.")
    assert glossary.unlock("Zxq0Z 大幅下降 。", mapping) == "净利润大幅下降。"


def test_unlock_handles_lowercased_placeholder():
    _set_terms(**{"net profit": "净利润"})
    _, mapping = glossary.lock("The net profit fell.")
    assert glossary.unlock("zxq0z下降。", mapping) == "净利润下降。"


def test_unlock_is_a_noop_without_mapping():
    assert glossary.unlock("这句话没有术语。", {}) == "这句话没有术语。"
    assert glossary.unlock("", {"Zxq0Z": "净利润"}) == ""


def test_longest_term_wins():
    _set_terms(**{"profit": "利润", "net profit": "净利润"})
    masked, mapping = glossary.lock("Net profit rose again.")
    assert mapping == {"Zxq0Z": "净利润"}
    assert "profit" not in masked.lower().replace("zxq0z", "")


def test_word_boundary_prevents_partial_hits():
    """拉丁术语要带词边界，note 不该命中 denote，AI 不该命中 said。"""
    _set_terms(**{"note": "票据"})
    masked, _ = glossary.lock("Please denote the amount on the invoice.")
    assert masked == "Please denote the amount on the invoice."

    _set_terms(**{"ai": "人工智能"})
    masked, _ = glossary.lock("He said the plan is ready.")
    assert masked == "He said the plan is ready."


def test_cjk_term_matches_without_boundary():
    _set_terms(**{"目次": "目录"})
    masked, mapping = glossary.lock("本文的目次见第2页。")
    assert mapping == {"Zxq0Z": "目录"}
    assert "目次" not in masked


def test_multiple_terms_in_one_sentence():
    _set_terms(**{"net profit": "净利润", "fiscal year": "会计年度"})
    masked, mapping = glossary.lock(
        "Net profit for the fiscal year exceeded expectations."
    )
    assert len(mapping) == 2
    # 长术语先占位，编号顺序不保证，按值断言而不是按编号断言
    first, second = list(mapping.values())
    restored = glossary.unlock("Zxq0Z和Zxq1Z都在增长。", mapping)
    assert restored == f"{first}和{second}都在增长。"
    assert set(mapping.values()) == {"净利润", "会计年度"}


def test_no_terms_leaves_text_untouched():
    _set_terms()
    glossary._compiled = None
    text = "Nothing to lock here."
    assert glossary.lock(text) == (text, {})
    assert not glossary.enabled()


def test_symbol_only_entries_are_ignored():
    _set_terms(**{"---": "破折号"})
    masked, mapping = glossary.lock("a --- b")
    assert mapping == {}


def test_reload_picks_up_new_terms():
    _set_terms(**{"alpha": "阿尔法"})
    assert glossary.enabled()
    USER_TERMS.clear()
    glossary.reload()
    assert not glossary.enabled()


def test_common_word_terms_are_not_locked_into_sentences():
    """`note = 注释` 不能把 "Please note that…" 译成「请注释」。

    整条术语每个词都是通用词时不参与句内替换，只在整段恰好就是它时才生效。
    宁可少锁一条，也不能整篇文档到处插错词。
    """
    _set_terms(**{"note": "注释", "in case": "万一", "please note": "请注意"})
    assert glossary.lock("Please note that the report is ready.") == (
        "Please note that the report is ready.",
        {},
    )
    assert glossary.skipped()


def test_real_terms_are_still_locked():
    """只要有一个词不是通用词就照常锁定，不能因为加了防护就整体失效。"""
    _set_terms(**{"net profit": "净利润", "EBITDA": "息税折旧摊销前利润"})
    masked, mapping = glossary.lock("Net profit rose and EBITDA improved.")
    assert mapping
    assert glossary.unlock(masked, mapping).count("净利润") == 1
    assert "EBITDA" not in glossary.skipped()


def test_intro_mentions_skipped_terms():
    _set_terms(**{"note": "注释", "net profit": "净利润"})
    text = glossary.intro()
    assert text and "净利润" not in text and "note" in text


def test_long_single_word_term_also_locks_its_inflected_forms():
    """术语表只写原形，正文里的复数也得锁住——以前 transformers 漏网，
    模型把它译成「变换器」，用户加了术语表却看不出为什么没生效。"""
    _set_terms(**{"transformer": "变压器"})
    masked, mapping = glossary.lock("Two transformers failed during the test run.")
    assert mapping == {"Zxq0Z": "变压器"}
    assert "transformers" not in masked.lower()
    assert glossary.unlock(masked, mapping) == "Two 变压器 failed during the test run."


def test_inflection_never_grabs_a_derived_word():
    """-er/-est 明确不做：reporter 是另一个人，不是 report 的变形。"""
    assert glossary.in_source("report", "the report is ready")
    assert not glossary.in_source("report", "the reporter wrote it")


def test_short_words_keep_their_exact_form_only():
    """短词的 -s/-ing 形态在通用语里到处都是，一律锁住比漏锁更疼。"""
    assert not glossary.in_source("port", "the ports are open")
    assert glossary.in_source("port", "the port is closed")


def test_in_source_does_not_match_inside_another_word():
    """校验侧以前用纯子串，note 会在 denote 里命中、报出一堆假问题。"""
    assert glossary.in_source("note", "please note that")
    assert not glossary.in_source("note", "please denote that")


def test_multi_word_terms_are_not_inflected():
    """多词术语照旧逐词拼，只认表里的写法。"""
    assert glossary.in_source("net profit", "the NET PROFIT fell")
    assert not glossary.in_source("net profit", "the net profits fell")


def test_switch_off_falls_back_to_base_form_only():
    _set_terms(**{"transformer": "变压器"})
    glossary.set_inflection(False)
    masked, mapping = glossary.lock("Two transformers failed here.")
    assert mapping == {}
    assert masked == "Two transformers failed here."
    text = glossary.intro()
    assert text and "词形变化" not in text


def test_intro_mentions_inflected_terms():
    _set_terms(**{"transformer": "变压器", "container": "集装箱"})
    text = glossary.intro()
    assert text and "词形变化" in text and "transformer" in text
    assert "--no-term-inflection" in text


def test_fingerprint_follows_the_inflection_switch():
    """缓存键里有指纹：开关一翻，同一份文件必须重翻，不能命中旧译文。"""
    _set_terms(**{"transformer": "变压器"})
    on = glossary.fingerprint()
    glossary.set_inflection(False)
    assert glossary.fingerprint() != on


def test_target_present_survives_polish_spacing():
    """润色会在中英边界插空格，判定必须认这是同一个译法在场。"""
    assert glossary.target_present("AI技术", "这是 AI 技术。")
    assert not glossary.target_present("AI技术", "这是人工智能技术。")
    assert not glossary.target_present("", "任意译文")
    assert not glossary.target_present("净利润", "")


def test_hit_rows_uses_the_shared_match():
    rows = glossary.hit_rows(
        1, [("Zxq0Z", "transformer", "变压器")], "两台 变压器 已跳闸。"
    )
    assert rows and rows[0]["ok"] is True
    assert rows[0]["source"] == "transformer"
    assert "leftover" not in rows[0]
    bad = glossary.hit_rows(1, [("Zxq0Z", "transformer", "变压器")], "两台 ZxqOZ 已跳闸。")
    assert bad and bad[0]["ok"] is False
    assert bad[0]["leftover"] == ["ZxqOZ"]


def test_cli_switch_turns_inflection_off():
    """``--no-term-inflection`` 在参数解析阶段就落到 glossary 上，不等翻译开始。"""
    script = (
        "import sys\n"
        "sys.argv = ['app.py', '--no-term-inflection', '--version']\n"
        "import app\n"
        "from core import glossary\n"
        "try:\n"
        "    app.main()\n"
        "except SystemExit:\n"
        "    pass\n"
        "print('INFLECTION=' + str(glossary.inflection_on()))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-800:]
    assert "INFLECTION=False" in result.stdout
