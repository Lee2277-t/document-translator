"""混排段落切分与补译（F3，v1.5.9）。

要守住的三件事：
1. **只切「本来会被整段跳过」的段落**：会正常翻译的段落不许被切碎，
   功能上线不能改变大多数文档的既有译文；
2. **只送少数语种子句**：URL、邮箱、公式、3 个词的短语都不许送；
3. **失败的补译不能拖垮整份文档**：通道挂了就保持原文。
"""

from __future__ import annotations

import pytest

from core import direction, mixed
from core.translator import Translator, should_translate

MIXED = (
    "本合同适用中华人民共和国法律，双方确认并同意参考 "
    "the laws of the People's Republic of China 之相关规定。"
)
SPAN = "the laws of the People's Republic of China"


def _translations(spans: list[str], mapping: dict[str, str] | None = None) -> list[str]:
    mapping = mapping or {}
    return [mapping.get(span, f"译:{span}") for span in spans]


# ---------------------------------------------------------------------------
# 切分
# ---------------------------------------------------------------------------


def test_mixed_paragraph_is_skipped_by_whole_paragraph_rule():
    """前提校验：没有这条，下面的测试就测了个寂寞。"""
    assert not should_translate(MIXED)


def test_plan_splits_minority_clause_and_keeps_the_rest():
    segments = mixed.plan(MIXED)
    assert segments is not None
    assert [segment.translate for segment in segments] == [False, True, False]
    assert segments[1].text == SPAN
    assert segments[0].text.startswith("本合同适用")
    assert segments[2].text.startswith(" 之相关规定")


def test_plan_leaves_normally_translatable_paragraph_alone():
    """以英文为主的段落由引擎整段翻，切碎只会伤连贯性。"""
    text = "本合同适用 the laws of the People's Republic of China 之相关规定。"
    assert should_translate(text)
    assert mixed.plan(text) is None


def test_plan_ignores_short_latin_phrases():
    assert mixed.plan("请参考 United Nations 的相关决议。") is None


def test_plan_ignores_urls_and_mail_addresses():
    assert mixed.plan("详见 www.example.com 的说明，或联系 admin@example.com 获取。") is None
    assert mixed.plan("访问 https://example.com/docs 查看文档，有问题请联系我们。") is None


def test_plan_ignores_formula_paragraph():
    """中文夹一行公式——公式不是要翻的外文。"""
    assert mixed.plan("计算公式如下：E = mc² 请核对后签字。") is None


def test_plan_handles_multiple_clauses():
    text = (
        "甲方应根据合同约定 deliver the goods on time 并且乙方应在约定期限内 "
        "pay within thirty days 完成交易并交付全部单据。"
    )
    segments = mixed.plan(text)
    assert segments is not None
    assert [segment.text for segment in segments if segment.translate] == [
        "deliver the goods on time",
        "pay within thirty days",
    ]


def test_plan_translates_chinese_parenthetical_when_target_is_english():
    direction.set_target("en")
    text = "This Agreement（以下简称“本协议”）shall be effective upon signature."
    segments = mixed.plan(text)
    assert segments is not None
    assert [segment.text for segment in segments if segment.translate] == ["（以下简称“本协议”）"]


def test_plan_leaves_english_paragraph_alone_when_target_is_english():
    direction.set_target("en")
    assert mixed.plan("This is a pure English paragraph with no Chinese at all.") is None


# ---------------------------------------------------------------------------
# 拼回
# ---------------------------------------------------------------------------


def test_join_replaces_only_translated_spans():
    segments = mixed.plan(MIXED)
    assert segments is not None
    joined = mixed.join(segments, ["中华人民共和国法律"])
    assert "the laws of the People's Republic" not in joined
    assert "中华人民共和国法律" in joined
    assert joined.startswith("本合同适用中华人民共和国法律")
    assert joined.endswith("之相关规定。")


def test_join_keeps_source_when_translation_is_empty_or_echo():
    segments = mixed.plan(MIXED)
    assert segments is not None
    assert mixed.join(segments, [""]) == MIXED
    assert mixed.join(segments, [SPAN]) == MIXED


# ---------------------------------------------------------------------------
# 批量补译
# ---------------------------------------------------------------------------


def test_fill_translates_spans_in_one_batch_and_leaves_other_paragraphs():
    texts = [MIXED, "本协议自双方签字之日起生效。", "This entire paragraph is in English."]
    results: list[str | None] = list(texts)
    calls: list[list[str]] = []

    def translate(spans: list[str]) -> list[str]:
        calls.append(list(spans))
        return ["中华人民共和国法律"]

    assert mixed.fill(texts, results, translate) == 1
    assert calls == [[SPAN]]
    assert results[0] == "本合同适用中华人民共和国法律，双方确认并同意参考 中华人民共和国法律 之相关规定。"
    assert results[1] == texts[1]
    assert results[2] == texts[2]


def test_fill_leaves_paragraph_alone_when_engine_fails():
    texts = [MIXED]
    results: list[str | None] = list(texts)

    def translate(_spans: list[str]) -> list[str]:
        raise RuntimeError("通道不可用")

    assert mixed.fill(texts, results, translate) == 0
    assert results[0] == MIXED


def test_fill_skips_already_translated_paragraphs():
    texts = [MIXED]
    results: list[str | None] = ["已经处理过的译文"]

    def translate(_spans: list[str]) -> list[str]:
        raise AssertionError("已翻译的段落不该再进引擎")

    assert mixed.fill(texts, results, translate) == 0
    assert results[0] == "已经处理过的译文"


class _Engine:
    def __init__(self) -> None:
        self.translated_count = 0
        self.skipped_count = 1
        self.cache_hits = 0
        self.term_hits: list[dict] = []


def test_fill_for_engine_moves_counts_and_drops_span_level_term_hits():
    engine = _Engine()
    texts = [MIXED]
    results: list[str | None] = list(texts)

    def translate(spans: list[str]) -> list[str]:
        # 模拟嵌套调用顺带记了一条段落号没有意义的术语命中
        engine.term_hits.append({"段落": 1})
        engine.translated_count += 1
        return _translations(spans)

    assert mixed.fill_for_engine(engine, texts, results, translate) == 1
    # 子句级命中不许留在报表里
    assert engine.term_hits == []
    # 整段从「跳过」挪到「翻译」，且子句内部的计数不重复累加
    assert engine.translated_count == 1
    assert engine.skipped_count == 0


def test_fill_for_engine_does_not_recurse():
    engine = _Engine()
    texts = [MIXED]
    results: list[str | None] = list(texts)

    def translate(spans: list[str]) -> list[str]:
        engine._mixed_depth = True  # 模拟嵌套那一层正在跑
        try:
            assert mixed.fill_for_engine(engine, texts, results, lambda _spans: ["x"]) == 0
        finally:
            engine._mixed_depth = False
        return _translations(spans)

    assert mixed.fill_for_engine(engine, texts, results, translate) == 1


def test_offline_local_translator_fills_skipped_mixed_paragraph():
    """保密模式的离线档也要补：整段判成中文跳过时，子句仍进本机模型。"""
    from core.local_translator import LocalTranslator

    translator = LocalTranslator(log=lambda _message: None)

    class _FakeModel:
        def load(self) -> None:
            pass

        def close(self) -> None:
            pass

        def available(self) -> bool:
            return True

        def translate(self, texts, _language):
            return [
                ("中华人民共和国法律" if "laws of the People" in text else "译文", 0.0)
                for text in texts
            ]

    translator._english = _FakeModel()
    translator._multilingual = _FakeModel()

    out = translator.translate_texts([MIXED])

    assert "中华人民共和国法律" in out[0]
    assert "本合同适用" in out[0]
    assert translator.translated_count == 1
    assert translator.skipped_count == 0


def test_cloud_translator_fills_skipped_mixed_paragraph(tmp_path):
    """在线档端到端：整段被跳过，但子句仍然进了通道。"""
    translator = Translator(cache_path=str(tmp_path / "cache.sqlite"))
    translator.preflight = lambda: None  # 不联网
    seen: list[list[str]] = []

    def fake_batch(pending, texts, results, *_args, **_kwargs):
        seen.append([texts[index] for index in pending])
        for index in pending:
            results[index] = f"译:{texts[index]}"
        return [], 100

    translator._batch_pass = fake_batch
    out = translator.translate_texts([MIXED])

    assert seen == [[SPAN]]
    assert out[0] == (
        "本合同适用中华人民共和国法律，双方确认并同意参考 译:the laws of the "
        "People's Republic of China 之相关规定。"
    )
    assert translator.translated_count == 1
    assert translator.skipped_count == 0
