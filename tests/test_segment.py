"""core/segment.py 的分段切句规则测试（v1.6.4，G1）。

钉住三件事：中文长段能按全角标点切开、拉丁原文的既有口径不回退、
兜底硬切不会劈开单词与数字串。最后再验 split_for_api 的两条硬约束
（每片不超预算、内容不丢）。
"""

from __future__ import annotations

from core.segment import safe_cut, split_sentences
from core.translator import split_for_api


def test_cjk_sentences_split_on_fullwidth_punctuation():
    text = "人工智能正在改变学习方式。它也改变了工作方式！这是否意味着传统岗位会消失？我们拭目以待。"
    parts = split_sentences(text)
    assert parts == [
        "人工智能正在改变学习方式。",
        "它也改变了工作方式！",
        "这是否意味着传统岗位会消失？",
        "我们拭目以待。",
    ]


def test_cjk_close_punctuation_stays_with_its_sentence():
    parts = split_sentences("报告称「明年增长」。随后结束了。")
    assert parts == ["报告称「明年增长」。", "随后结束了。"]


def test_decimal_never_splits_but_punctuation_followed_by_space_does():
    """小数点两侧都是数字，不切；「U.S.」这种缩写会被切——与改造前的
    ``re.split(r"(?<=[.!?;:])\\s+")`` 同口径，不是本轮引入的回归。"""
    parts = split_sentences("The U.S. economy. It grew 3.14 percent.")
    assert parts == ["The U.S.", "economy.", "It grew 3.14 percent."]


def test_latin_colon_and_semicolon_split():
    parts = split_sentences("First: one item; then another. Done.")
    assert parts == ["First:", "one item;", "then another.", "Done."]


def test_newlines_always_break():
    assert split_sentences("line one\nline two\n\nline three") == [
        "line one",
        "line two",
        "line three",
    ]


def test_safe_cut_prefers_sentence_boundary_inside_limit():
    text = "第一句话很长很长很长很长。第二句话也被排在后面。第三句收尾。"
    head, tail = safe_cut(text, 20)
    assert head.endswith("。")
    assert len(head) <= 20
    assert tail.startswith("第二句话")
    assert head + tail == text.replace(" ", "")


def test_safe_cut_moves_ascii_run_whole_into_next_piece():
    """劈口正好落在版本号中间时，整串挪到后一片，前一片短两个字符也认。"""
    text = "中" * 20 + "v1.6.4" + "后" * 10
    head, tail = safe_cut(text, 22)
    assert head == "中" * 20
    assert tail.startswith("v1.6.4")


def test_safe_cut_keeps_token_intact_when_boundary_is_clean():
    text = "中文说明文字中文说明文字版本v1.6.4后续中文中文中文中文中文"
    head, tail = safe_cut(text, 22)
    assert "版本v1.6.4" in head
    assert not head.endswith("v1.")
    assert not tail.startswith("6.4")


def test_safe_cut_falls_back_to_space_for_latin_text():
    text = "alpha beta gamma delta epsilon zeta eta theta"
    head, tail = safe_cut(text, 20)
    assert head == "alpha beta gamma"
    assert tail == "delta epsilon zeta eta theta"


def test_safe_cut_short_text_returns_whole():
    assert safe_cut("短句子。", 50) == ("短句子。", "")


def test_long_cjk_paragraph_splits_within_budget():
    """保密离线档的 MAX_CHUNK_CHARS=1200：中文长段必须按「。」切成多片，每片不越预算。"""
    sentence = "人工智能正在改变我们学习与工作的方式，这需要更多可核验的证据支撑。"
    text = sentence * 60  # 33 字 × 60 = 1980 字，超 1200 一截
    chunks = split_for_api(text, 1200)
    assert len(chunks) > 1
    assert all(len(chunk) <= 1200 for chunk in chunks)
    assert "".join(chunk.replace(" ", "") for chunk in chunks) == text
    assert all(chunk.endswith("。") for chunk in chunks[:-1])


def test_split_for_api_keeps_latin_behaviour():
    text = " ".join(f"Sentence number {index} is here." for index in range(60))
    chunks = split_for_api(text, 300)
    assert all(len(chunk) <= 300 for chunk in chunks)
    assert " ".join(chunks).split() == text.split()


def test_split_for_api_empty_and_short():
    assert split_for_api("", 100) == []
    assert split_for_api("很短的一句。", 100) == ["很短的一句。"]


def test_split_for_api_no_infinite_loop_on_runaway_token():
    """超长无标点且夹一个比 limit 还长的 token 时，必须硬切而不是卡死。"""
    text = "啊" * 50 + "x" * 500 + "啊" * 50
    chunks = split_for_api(text, 100)
    assert chunks
    assert sum(len(chunk) for chunk in chunks) <= len(text)
