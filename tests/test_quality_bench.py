"""v1.5.2 质量量化基准的测试：锚定集 / 打分 / 报告 / judge / CLI 闸门。

全部用假翻译器与假 HTTP——不真发请求、不加载 ctranslate2 模型，
几秒内跑完。真机三引擎对比由人工 ``--bench`` 验收（沙箱跑不了 exe，
但 ``app.py --bench`` 本身可在 venv 里直跑）。
"""

from __future__ import annotations

import re

import pytest

import core.bench as bench
from core.bench import (
    Anchor,
    BenchRun,
    _expect_numbers_present,
    _num_key,
    _squash,
    build_translators,
    load_anchors,
    make_ollama_judge,
    render_report,
    run_anchor,
    run_judge,
)


class FakeTranslator:
    """假翻译器：fn(anchor, source) -> 译文，同签名 translate_texts。

    run_anchor 只把原文文本传进 translate_texts，所以按 source 反查
    anchor——helper 需要 expect_terms / expect_numbers 才能构造针对性译文。
    """

    def __init__(self, fn, anchors=None):
        self.fn = fn
        self.by_source = {a.source: a for a in (anchors or [])}

    def translate_texts(self, texts, progress=None, ctx=None):
        return [self.fn(self.by_source.get(text), text) for text in texts]


def _perfect(anchor, source: str) -> str:
    """构造一条必过质检的译文：术语指定译法 + 原文（保住数字）+ 中文补位。"""
    parts = ["基准测量译文", "中文长度补位内容", "中文长度补位内容"]
    for term in (anchor.expect_terms if anchor else []):
        parts.append(term["dst"])
    parts.append(source)
    return "".join(parts)


def _drop_terms(anchor, source: str) -> str:
    return "中文长度补位内容" * 3 + source


def _drop_numbers(anchor, source: str) -> str:
    # 按正则删光所有阿拉伯数字串——不能按 expect 书写形态替换：
    # 源文是 "3,000" 而期望值写 "3000"、源文 "3.000,00" 期望值 "3000.00"，
    # 按字符串 replace 会删不掉，测试就失去了意义。
    stripped = re.sub(r"\d[\d.,]*", "", source)
    return "中文长度补位内容" * 3 + stripped


@pytest.fixture(scope="module")
def anchors() -> list[Anchor]:
    return load_anchors()


# ---------------------------------------------------------------- 锚定集


def test_anchor_set_structure(anchors):
    assert len(anchors) >= 40, "锚定集规模不足以当基准"
    assert all(anchor.source for anchor in anchors)
    assert all(anchor.min_chars >= 0 for anchor in anchors)
    # 每个考察类别都要有锚点，否则维度覆盖是假的
    categories = {focus for anchor in anchors for focus in anchor.focus}
    assert {"terminology", "numbers", "structure", "multilingual"} <= categories


def test_anchor_set_ids_unique():
    ids = [anchor.id for anchor in load_anchors()]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------- 判定函数


def test_squash_ignores_spaces():
    # v1.4.1 教训钉子：_tidy 插空格不能造成术语误报
    assert _squash("机器 学习") == _squash("机器学习")
    assert _squash("AI 技术") == "AI技术"


def test_num_key_forms():
    assert _num_key("3000.00") == _num_key("3000")
    assert _num_key("3.50") == _num_key("3.5")
    assert _num_key("2.1.0") == "2.1.0"  # 非数字原样
    # 日期分隔符与负号归一口径
    assert _num_key("-03") == _num_key("3")
    assert _num_key("-5") == _num_key("5")


def test_expect_numbers_present_forms():
    assert _expect_numbers_present(["3000.00"], "价格是 3,000.00 欧元")
    assert _expect_numbers_present(["2024-03-15"], "发布于 2024 年 3 月 15 日")
    assert _expect_numbers_present(["3.000,00"], "价格 3,000.00 欧元")
    assert not _expect_numbers_present(["42"], "没有数字的译文")


# ---------------------------------------------------------------- 单锚点评分


def test_perfect_translation_passes_all(anchors):
    translator = FakeTranslator(_perfect, anchors)
    for anchor in anchors:
        result = run_anchor(translator, anchor)
        assert result.ok, f"{anchor.id} 不该失败：{result.failures}"


def test_missing_term_fails_with_reason(anchors):
    translator = FakeTranslator(_drop_terms, anchors)
    term_anchors = [a for a in anchors if a.expect_terms]
    assert term_anchors, "锚定集应含术语锚点"
    for anchor in term_anchors:
        result = run_anchor(translator, anchor)
        assert result.checks["terminology"] is False
        assert any("术语未生效" in line for line in result.failures)


def test_spaced_term_still_counts(anchors):
    # 译文把「机器学习」写成「机器 学习」——空白归一度必须算命中
    anchor = next(a for a in anchors if a.expect_terms)
    term = anchor.expect_terms[0]
    spaced = "机器 学习" if term["dst"] == "机器学习" else term["dst"]
    translator = FakeTranslator(
        lambda a, src: f"中文补位内容{spaced}" + src, anchors
    )
    result = run_anchor(translator, anchor)
    assert result.checks["terminology"] is True


def test_missing_number_fails(anchors):
    translator = FakeTranslator(_drop_numbers, anchors)
    number_anchors = [a for a in anchors if a.expect_numbers]
    assert number_anchors
    for anchor in number_anchors:
        result = run_anchor(translator, anchor)
        assert result.checks["numbers"] is False


def test_euro_notation_number_counts(anchors):
    # 欧陆写法 3.000,00 与 3,000.00 是同一个数（N09 的口径）
    anchor = next(a for a in anchors if a.id == "N09")
    translator = FakeTranslator(
        lambda a, src: "中文补位内容" + src.replace("3.000,00", "3,000.00"), anchors
    )
    result = run_anchor(translator, anchor)
    assert result.checks["numbers"] is True


def test_placeholder_leftover_fails_format(anchors):
    translator = FakeTranslator(
        lambda a, src: f"中文补位内容若干{src}Ztq0Z", anchors
    )
    result = run_anchor(translator, anchors[0])
    assert result.checks["format"] is False
    assert any("占位符残留" in line for line in result.failures)


def test_too_short_translation_fails_format(anchors):
    anchor = next(a for a in anchors if a.min_chars > 1)
    translator = FakeTranslator(lambda a, src: "好", anchors)
    result = run_anchor(translator, anchor)
    assert result.checks["format"] is False
    assert any("译文过短" in line for line in result.failures)


def test_echo_translation_fails_format(anchors):
    translator = FakeTranslator(lambda a, src: src, anchors)
    result = run_anchor(translator, anchors[0])
    assert result.checks["format"] is False
    assert any("引擎未翻译" in line for line in result.failures)


def test_empty_translation_fails_format(anchors):
    translator = FakeTranslator(lambda a, src: "", anchors)
    result = run_anchor(translator, anchors[0])
    assert result.checks["format"] is False
    assert any("译文为空" in line for line in result.failures)


def test_engine_exception_recorded_not_raised(anchors):
    class Broken(FakeTranslator):
        def translate_texts(self, texts, progress=None, ctx=None):
            raise RuntimeError("连接失败")

    result = run_anchor(Broken(None), anchors[0])
    assert not result.ok
    assert any("引擎调用失败" in line for line in result.failures)


# ---------------------------------------------------------------- 汇总


def test_score_engine_excludes_not_applicable(anchors):
    results = [run_anchor(FakeTranslator(_perfect, anchors), a) for a in anchors]
    score = bench.score_engine("x", "测试引擎", results)
    # 无 expect_terms 的锚点不计术语分母
    term_anchors = sum(1 for a in anchors if a.expect_terms)
    assert score.applicable["terminology"] == term_anchors
    assert score.applicable["format"] == len(anchors)
    assert score.overall == pytest.approx(1.0)


def test_score_engine_counts_failures(anchors):
    results = [run_anchor(FakeTranslator(_drop_terms, anchors), a) for a in anchors]
    score = bench.score_engine("x", "测试引擎", results)
    term_anchors = sum(1 for a in anchors if a.expect_terms)
    assert score.rate("terminology") == 0.0
    assert score.applicable["terminology"] == term_anchors
    assert score.overall < 1.0


# ---------------------------------------------------------------- 报告


def _fake_run(anchors) -> BenchRun:
    run = BenchRun()
    results = [run_anchor(FakeTranslator(_perfect, anchors), a) for a in anchors]
    run.scores["offline"] = bench.score_engine("offline", "离线小模型", results)
    run.details["offline"] = results
    # 造一个差一点的引擎：术语全丢
    weak = [run_anchor(FakeTranslator(_drop_terms, anchors), a) for a in anchors]
    run.scores["cloud"] = bench.score_engine("cloud", "免费云端", weak)
    run.details["cloud"] = weak
    return run


def test_report_with_baseline_shows_gap(anchors):
    run = _fake_run(anchors)
    # 把 offline 当作基准线渲染
    report = render_report(run, anchors, baseline="offline")
    assert "离线小模型" in report
    assert "免费云端" in report
    assert "与 离线小模型 基准差距" in report
    assert "百分点" in report


def test_report_without_baseline_shows_guidance(anchors):
    run = _fake_run(anchors)
    report = render_report(run, anchors, baseline="deepl")
    assert "对照基准缺失" in report
    assert "deepl.com/pro-api" in report


def test_report_lists_failure_details(anchors):
    run = _fake_run(anchors)
    report = render_report(run, anchors)
    assert "失败明细" in report
    assert "术语未生效" in report


# ---------------------------------------------------------------- judge


def _fake_post_factory(payload: dict):
    def post(url, body, timeout):
        return payload

    return post


def test_judge_parses_structured_output(monkeypatch):
    monkeypatch.setattr(
        "core.local_llm._http_post_json",
        _fake_post_factory(
            {"choices": [{"message": {"content": '{"fluency": 4, "fidelity": 5}'}}]}
        ),
    )
    judge = make_ollama_judge("qwen3:1.7b")
    verdict = judge("hello", "你好")
    assert verdict == {"fluency": 4, "fidelity": 5}


def test_judge_survives_garbage(monkeypatch):
    monkeypatch.setattr(
        "core.local_llm._http_post_json",
        _fake_post_factory(
            {"choices": [{"message": {"content": "我觉得挺好的，就打 4 分吧"}}]}
        ),
    )
    judge = make_ollama_judge("qwen3:1.7b")
    assert judge("hello", "你好") is None  # 无 JSON → 不计分


def test_judge_rejects_out_of_range(monkeypatch):
    monkeypatch.setattr(
        "core.local_llm._http_post_json",
        _fake_post_factory(
            {"choices": [{"message": {"content": '{"fluency": 9, "fidelity": 1}'}}]}
        ),
    )
    judge = make_ollama_judge("qwen3:1.7b")
    assert judge("hello", "你好") is None


def test_judge_strips_code_fence(monkeypatch):
    monkeypatch.setattr(
        "core.local_llm._http_post_json",
        _fake_post_factory(
            {
                "choices": [
                    {
                        "message": {
                            "content": '```json\n{"fluency": 3, "fidelity": 3}\n```'
                        }
                    }
                ]
            }
        ),
    )
    judge = make_ollama_judge("qwen3:1.7b")
    assert judge("hello", "你好") == {"fluency": 3, "fidelity": 3}


def test_run_judge_averages_and_skips_failures(anchors):
    run = _fake_run(anchors)
    subset = run.details["offline"][:4]
    run.details = {"offline": subset}
    run.scores = {"offline": run.scores["offline"]}
    calls = {"n": 0}

    def judge(source, translation):
        calls["n"] += 1
        if calls["n"] == 2:
            return None  # 第二条解析失败，不进均分
        return {"fluency": 4, "fidelity": 2}

    judged = run_judge(judge, run, anchors[:4])
    assert judged["offline"]["scored"] == 3
    assert judged["offline"]["fluency"] == 4.0
    assert judged["offline"]["fidelity"] == 2.0


# ---------------------------------------------------------------- 装配与 CLI


def test_build_translators_unknown_engine_noted():
    engines, notes = build_translators(["bogus"])
    assert engines == []
    assert any("未知引擎" in note for note in notes)


def test_build_translators_deepl_without_key_noted():
    # 本机未配 DeepL key：不静默跳过，notes 要说清原因与申请地址
    from core.api_keys import load_key

    if load_key("deepl"):
        pytest.skip("本机已配置 DeepL key，跳过无 key 分支")
    engines, notes = build_translators(["deepl"])
    assert engines == []
    assert any("deepl" in note.lower() and "key" in note for note in notes)


def test_cli_bench_deepl_requires_acknowledgement(monkeypatch, capsys):
    # 涉密闸门：跑 DeepL 对照前必须确认过（--yes-i-know），与 --paid 同口径
    monkeypatch.setattr("core.api_keys.is_acknowledged", lambda provider="deepl": False)
    import app as app_module

    code = app_module._run_bench(["--engine=deepl"])
    assert code == 4
    assert "涉密警示" in capsys.readouterr().out


def test_cli_bench_no_engine_returns_one(capsys):
    import app as app_module

    code = app_module._run_bench(["--engine=bogus"])
    assert code == 1
    assert "没有可用的引擎" in capsys.readouterr().out
