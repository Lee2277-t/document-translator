"""DeepL 付费档（C3-a）的行为钉子。全部 mock，不真发 HTTP。

盯住的东西按优先级：
1. **key 的四种失败都必须安静地当没配**（文件不在 / 坏 JSON / 无条目 / 空 key），
   不能因为是付费档就让整个程序起不来；
2. **403 / 456 / 网络错必须说人话**——配额和 key 是付费档最高频失败，
   技术错误码对用户不可行动；
3. **分批规则**：单请求 ≤50 段且 ≤100KB（128KiB 是 DeepL 硬限），
   超长段先按句拆开；
4. **429 指数退避重试后成功、重试用尽才保留原文**；单批失败不拖垮整轮；
5. **术语锁定 / 标题词表 / 风格润色 / 缓存 / term_hits 与在线 Translator
   同口径**——付费档不是另一套翻译逻辑，只是换了引擎；
6. **双口径记账**：billed_characters 进 jsonl，month_usage 汇总正确，
   预算超限只警告不拦截；
7. **CLI 三道闸门**：与保密模式硬互斥、没 key 给引导、未确认涉密警示
   要求 --yes-i-know。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from core import glossary
from core.api_keys import (
    is_acknowledged,
    keys_file,
    load_key,
    looks_like_key,
    month_usage,
    record_usage,
    save_acknowledgement,
)
from core.common import TranslationError
from core.deepl import DeepLError, DeepLTranslator
from core.heading_terms import HEADINGS, USER_TERMS
from core.report import CheckReport, build_report, paid_usage_of
from core.translator import split_for_api

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

#: 一个形态合法的 Free key（:fx 结尾），测试统一用它。
FAKE_KEY = "123e4567-e89b-12d3-a456-426614174000:fx"


@pytest.fixture(autouse=True)
def _reset_glossary():
    """术语表是全局状态，测试改完必须还原，否则污染其他用例。"""
    saved_user = dict(USER_TERMS)
    saved_headings = dict(HEADINGS)
    saved_compiled = glossary._compiled
    yield
    USER_TERMS.clear()
    USER_TERMS.update(saved_user)
    HEADINGS.clear()
    HEADINGS.update(saved_headings)
    glossary._compiled = saved_compiled


def _set_terms(**pairs: str) -> None:
    """照 test_term_hits 的套路：用户词条同时进 HEADINGS，lookup 才查得到。"""
    USER_TERMS.clear()
    USER_TERMS.update({key.lower(): value for key, value in pairs.items()})
    for key, value in USER_TERMS.items():
        HEADINGS[key] = value
    glossary._compiled = None


class _Response:
    """假 HTTP 响应：只有 status_code 与 json() 两个被用到的面。"""

    def __init__(self, status: int, payload) -> None:
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def _ok(texts: list[str]) -> _Response:
    """200 响应：逐段给译文与 billed_characters。"""
    return _Response(
        200,
        {
            "translations": [
                {"detected_source_language": "EN", "text": text, "billed_characters": len(text)}
                for text in texts
            ]
        },
    )


def _render(marker: str = "译："):
    """按请求内容现造译文：每段返回 marker + 原文前 8 字符。

    billed_characters 按**原文**长度返回（真实 DeepL 就是按源文计费），
    这样记账测试盯的才是源文口径。
    """

    def render(path: str, payload: dict) -> _Response:
        return _Response(
            200,
            {
                "translations": [
                    {
                        "detected_source_language": "EN",
                        "text": f"{marker}{text[:8]}",
                        "billed_characters": len(text),
                    }
                    for text in payload["text"]
                ]
            },
        )

    return render


def _echo(keep: bool = True, mangle: bool = False):
    """术语占位符的命运：keep=完好穿过 / mangle=被改写成 ZxqOZ / 否则整个丢掉。"""

    def render(path: str, payload: dict) -> _Response:
        rows = []
        for text in payload["text"]:
            if keep:
                out = f"{text}。"
            elif mangle:
                out = re.sub(r"(?i)zxq\d+z", "ZxqOZ", text) + "。"
            else:
                out = "已译。"
            rows.append({"text": out, "billed_characters": len(text)})
        return _Response(200, {"translations": rows})

    return render


class _FakePost:
    """按脚本依次返回响应，记录每次调用的 payload。

    脚本项是 ``(item, times)``：item 可以是 ``_Response``（静态）、
    ``Exception``（抛出）或 ``callable(path, payload) -> _Response``
    （按请求内容现造）；times 次之后切到下一项，最后一项用尽后永远重复
    （方便写「一直 429」这类剧本）。
    """

    def __init__(self, script: list) -> None:
        self.script = [[item, times] for item, times in script]
        self.calls: list[dict] = []

    def __call__(self, path: str, payload: dict):
        self.calls.append({"path": path, "payload": payload})
        item, _times = self.script[0]
        self.script[0][1] -= 1
        if self.script[0][1] <= 0 and len(self.script) > 1:
            self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(path, payload)
        return item


#: 永远重复的默认次数（测试里当成「后续全部」用）。
FOREVER = 99


def _make_translator(post, tmp_path: Path, logs: list[str] | None = None,
                     budget: int = 0, style: str = "standard") -> DeepLTranslator:
    """拼一个不真发 HTTP 的 DeepLTranslator（缓存进临时目录）。"""
    key_config = {"key": FAKE_KEY, "monthly_char_budget": budget}
    translator = DeepLTranslator(
        key_config,
        style=style,
        log=logs.append if logs is not None else (lambda message: None),
        cache=str(tmp_path / "cache.sqlite"),
        post=post,
        get=lambda path: _Response(200, {"character_count": 42, "character_limit": 500000}),
    )
    # 测试里不让重试真睡。
    translator._rate_delays = (0, 0, 0)
    translator._pause = 0
    return translator


def _write_keys(tmp_path: Path, key: str = FAKE_KEY, budget=None, acknowledged=None) -> Path:
    entry: dict = {"key": key}
    if budget is not None:
        entry["monthly_char_budget"] = budget
    if acknowledged is not None:
        entry["acknowledged"] = acknowledged
    path = tmp_path / "api_keys.local.json"
    path.write_text(json.dumps({"deepl": entry}, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# key 加载（core/api_keys.py）
# ---------------------------------------------------------------------------


def test_load_key_returns_none_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr("core.api_keys.KEYS_FILE", tmp_path / "none.json")
    assert load_key("deepl") is None


def test_load_key_returns_none_on_broken_json(monkeypatch, tmp_path):
    path = tmp_path / "api_keys.local.json"
    path.write_text("这不是 json", encoding="utf-8")
    monkeypatch.setattr("core.api_keys.KEYS_FILE", path)
    assert load_key("deepl") is None


def test_load_key_returns_none_without_provider_or_empty_key(monkeypatch, tmp_path):
    path = tmp_path / "api_keys.local.json"
    path.write_text(json.dumps({"openai": {"key": "sk-xxx"}}), encoding="utf-8")
    monkeypatch.setattr("core.api_keys.KEYS_FILE", path)
    assert load_key("deepl") is None
    path.write_text(json.dumps({"deepl": {"key": "   "}}), encoding="utf-8")
    assert load_key("deepl") is None


def test_load_key_reads_key_and_budget(monkeypatch, tmp_path):
    path = _write_keys(tmp_path, budget=500000)
    monkeypatch.setattr("core.api_keys.KEYS_FILE", path)
    assert load_key("deepl") == {"key": FAKE_KEY, "monthly_char_budget": 500000}
    # budget 缺省 0 = 不设本地上限
    path.write_text(json.dumps({"deepl": {"key": FAKE_KEY}}), encoding="utf-8")
    assert load_key("deepl") == {"key": FAKE_KEY, "monthly_char_budget": 0}


def test_key_shape_validation():
    """:fx 结尾（Free）或裸 UUID（Pro）都算合法；空 key 与乱串当没配。"""
    assert looks_like_key(FAKE_KEY) is True
    assert looks_like_key("123e4567-e89b-12d3-a456-426614174000") is True
    assert looks_like_key("  " + FAKE_KEY + " ") is True
    assert looks_like_key("sk-不是deepl的key") is False
    assert looks_like_key("") is False
    assert looks_like_key("123e4567-e89b-12d3-a456-426614174000:fx ".strip() + "x") is False


def test_acknowledgement_roundtrip(monkeypatch, tmp_path):
    path = _write_keys(tmp_path)
    monkeypatch.setattr("core.api_keys.KEYS_FILE", path)
    assert is_acknowledged("deepl") is False
    save_acknowledgement("deepl")
    assert is_acknowledged("deepl") is True
    # 确认标记不能把 key 冲掉
    assert load_key("deepl")["key"] == FAKE_KEY


# ---------------------------------------------------------------------------
# 探测失败的人话错误
# ---------------------------------------------------------------------------


def test_probe_403_gives_human_error(tmp_path):
    post = _FakePost([(_Response(403, {}), FOREVER)])
    translator = _make_translator(post, tmp_path)
    with pytest.raises(TranslationError) as excinfo:
        translator.translate_texts(["Hello world."])
    message = str(excinfo.value)
    assert "无效或已过期" in message
    assert "api_keys.local.json" in message
    assert post.calls[0]["payload"]["text"] == ["Hello"]  # 探测就是最小请求


def test_probe_456_gives_human_error(tmp_path):
    post = _FakePost([(_Response(456, {}), FOREVER)])
    translator = _make_translator(post, tmp_path)
    with pytest.raises(TranslationError) as excinfo:
        translator.translate_texts(["Hello world."])
    message = str(excinfo.value)
    assert "免费额度已用完" in message
    assert "50 万字符" in message


def test_probe_network_error_gives_human_error(tmp_path):
    post = _FakePost([(ConnectionError("boom"), FOREVER)])
    translator = _make_translator(post, tmp_path)
    with pytest.raises(TranslationError) as excinfo:
        translator.translate_texts(["Hello world."])
    assert "无法连接 DeepL" in str(excinfo.value)


def test_no_key_configured_raises_guidance(tmp_path):
    translator = DeepLTranslator(
        {"key": ""}, log=lambda message: None, cache=False,
        post=_FakePost([(_render(), FOREVER)]),
    )
    with pytest.raises(TranslationError) as excinfo:
        translator.translate_texts(["Hello world."])
    assert "api_keys.local.json" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 分批规则
# ---------------------------------------------------------------------------


def test_batching_respects_item_and_byte_limits(tmp_path):
    """60 段 + 一段超长：每次请求都必须 ≤50 段且 ≤100KB，超长段按句拆开。"""
    short = [f"Segment {index} with a few English words in it." for index in range(60)]
    long_text = "word " * 30000  # 约 150KB，必然触发预拆分
    texts = short + [long_text]
    post = _FakePost([(_render(), FOREVER)])
    translator = _make_translator(post, tmp_path)
    results = translator.translate_texts(texts)

    requests = [call["payload"] for call in post.calls if call["path"] == "/v2/translate"]
    assert len(requests) >= 3  # 60 段 + 5 个拆分片，一个请求装不下
    for payload in requests:
        assert len(payload["text"]) <= 50
        assert sum(len(text.encode("utf-8")) for text in payload["text"]) <= 100 * 1024
    # 第一次调用是密钥探测（最小请求）
    assert post.calls[0]["payload"]["text"] == ["Hello"]
    # 超长段的译文 = 各拆分片译文按序拼回，整段都翻到了
    pieces = split_for_api(long_text, 30000)
    assert len(pieces) >= 5
    assert results[-1].count("译：") == len(pieces)
    assert results[-1] != long_text


def test_probe_happens_only_once(tmp_path):
    """探测一次，全程可用性确定；第二次 translate_texts 不再探。"""
    post = _FakePost([(_render(), FOREVER)])
    translator = _make_translator(post, tmp_path)
    translator.translate_texts(["Hello world."])
    assert len(post.calls) == 2  # 探测 + 1 批
    translator.translate_texts(["Another English sentence here."])
    assert len(post.calls) == 3  # 只多 1 批，没有第二次探测


# ---------------------------------------------------------------------------
# 正常翻译：译文 / 缓存 / term_hits / 风格
# ---------------------------------------------------------------------------


def test_translation_writes_cache_and_reuses_it(tmp_path):
    post = _FakePost([(_render(), FOREVER)])
    translator = _make_translator(post, tmp_path)
    texts = ["Hello world.", "The report is ready."]
    first = translator.translate_texts(texts)
    assert first == ["译：Hello wo", "译：The repo"]
    assert translator.translated_count == 2
    assert translator.cache_hits == 0

    second = translator.translate_texts(texts)
    assert second == first
    assert translator.cache_hits == 2
    assert len(post.calls) == 2  # 没有任何新请求（探测 + 首批）


def test_skipped_segments_stay_untouched(tmp_path):
    post = _FakePost([(_render(), FOREVER)])
    translator = _make_translator(post, tmp_path)
    results = translator.translate_texts(["", "12345", "你好世界"])
    assert results == ["", "12345", "你好世界"]
    assert translator.skipped_count == 3
    # 没有待译段，连探测都不该发
    assert post.calls == []


def test_term_hits_recorded_like_online_translator(tmp_path):
    """句内命中 + 整段精确命中都记；缓存命中的段不记（v1.4.1 口径）。

    记录顺序与在线 Translator 一致：预扫描阶段的整段精确命中先记
    （「Net profit」是第 2 段），批量阶段的句内命中后记（第 1 段）——
    在线档同样是「先预扫描全文、再跑批量」，两档口径保持逐字一致。
    """
    _set_terms(**{"net profit": "净利润"})
    post = _FakePost([(_echo(keep=True), FOREVER)])
    translator = _make_translator(post, tmp_path)
    results = translator.translate_texts(["The Net profit rose.", "Net profit"])
    assert "净利润" in results[0]
    assert results[1] == "净利润"
    assert translator.term_hits == [
        {"paragraph": 2, "source": "Net profit", "target": "净利润", "ok": True},
        {"paragraph": 1, "source": "Net profit", "target": "净利润", "ok": True},
    ]
    assert "术语命中 2 处，0 处未生效" in translator.summary()

    translator.translate_texts(["The Net profit rose.", "Net profit"])
    # 「Net profit」整段就是术语，走词表直译、根本不碰缓存；
    # 只有句内命中那段算缓存命中（与在线 Translator 同口径）。
    assert translator.cache_hits == 1
    # 缓存命中段不补记；词表直译段不走缓存，每次运行都记一条——
    # 与在线 Translator 逐字同口径（它的 lookup 分支同样不查缓存）。
    assert len(translator.term_hits) == 3


def test_dropped_placeholder_is_flagged_not_effective(tmp_path):
    """占位符被模型整个丢掉：指定译法没进译文 → 未生效。"""
    _set_terms(**{"net profit": "净利润"})
    post = _FakePost([(_echo(keep=False), FOREVER)])
    translator = _make_translator(post, tmp_path)
    results = translator.translate_texts(["The Net profit rose."])
    assert "净利润" not in results[0]
    assert translator.term_hits == [
        {"paragraph": 1, "source": "Net profit", "target": "净利润", "ok": False}
    ]


def test_mangled_placeholder_records_leftover(tmp_path):
    """占位符被改写成 ZxqOZ：unlock 换不回去，note 要念出残留标记。"""
    _set_terms(**{"net profit": "净利润"})
    post = _FakePost([(_echo(keep=False, mangle=True), FOREVER)])
    translator = _make_translator(post, tmp_path)
    translator.translate_texts(["The Net profit rose."])
    row = translator.term_hits[0]
    assert row["ok"] is False
    assert row["leftover"] == ["ZxqOZ"]


def test_style_polish_applied(tmp_path):
    """formal 档：你好→您好、谢谢→感谢（润色发生在译文管线上）。"""
    post = _FakePost([(lambda path, payload: _ok(["你好，谢谢你的帮助。"]), FOREVER)])
    translator = _make_translator(post, tmp_path, style="formal")
    results = translator.translate_texts(["Hello, thank you for your help."])
    assert results == ["您好，感谢贵方的帮助。"]


def test_context_summary_goes_into_payload(tmp_path):
    """ctx.summary 作为 DeepL context 参数带入（不计费、不影响翻译本身）。"""
    from core.ctx import TranslationContext

    post = _FakePost([(_render(), FOREVER)])
    translator = _make_translator(post, tmp_path)
    ctx = TranslationContext(summary="前文讲的是季度财报。")
    translator.translate_texts(["Hello world."], ctx=ctx)
    batch = post.calls[-1]["payload"]
    assert batch.get("context") == "前文讲的是季度财报。"


# ---------------------------------------------------------------------------
# 429 / 5xx / 456 的重试与救回
# ---------------------------------------------------------------------------


def test_429_retries_then_succeeds(tmp_path):
    """429 指数退避重试后成功：重试不计失败，译文照常产出。"""
    post = _FakePost([(_render(), 1), (_Response(429, {}), 2), (_render(), FOREVER)])
    translator = _make_translator(post, tmp_path)
    results = translator.translate_texts(["Hello world."])
    assert results == ["译：Hello wo"]
    assert translator.failed_count == 0
    # 探测 1 次 + 429 两次 + 成功 1 次
    assert len(post.calls) == 4


def test_429_exhausted_keeps_original(tmp_path):
    """429 重试用尽（最多 3 次）：该批保留原文 + log 警告，不抛异常。"""
    post = _FakePost([(_render(), 1), (_Response(429, {}), FOREVER)])
    logs: list[str] = []
    translator = _make_translator(post, tmp_path, logs=logs)
    results = translator.translate_texts(["Hello world."])
    assert results == ["Hello world."]
    assert translator.failed_count == 1
    assert any("429" in message for message in logs)
    assert any("保留原文" in message for message in logs)
    # 探测 1 次 + 首批 4 次尝试（1 + 3 次退避）
    assert len(post.calls) == 5


def test_5xx_retries_once_then_keeps_original(tmp_path):
    post = _FakePost([(_render(), 1), (_Response(500, {}), FOREVER)])
    logs: list[str] = []
    translator = _make_translator(post, tmp_path, logs=logs)
    results = translator.translate_texts(["Hello world."])
    assert results == ["Hello world."]
    assert translator.failed_count == 1
    assert len(post.calls) == 3  # 探测 + 首批 2 次尝试


def test_456_mid_translation_aborts_with_human_message(tmp_path):
    """探测通过后中途 456（额度用完）：整轮中止，人话消息。"""
    post = _FakePost([(_render(), 1), (_Response(456, {}), FOREVER)])
    translator = _make_translator(post, tmp_path)
    with pytest.raises(TranslationError) as excinfo:
        translator.translate_texts(["Hello world.", "Another sentence."])
    assert "免费额度已用完" in str(excinfo.value)


def test_one_batch_failure_does_not_sink_the_rest(tmp_path):
    """两批里第一批 429 到尽、第二批正常：失败段保留原文，其余照常。"""
    texts = [f"Sentence number {index} is plain English." for index in range(60)]
    post = _FakePost(
        [
            (_render(), 1),  # 探测
            (_Response(429, {}), 4),  # 第一批（50 段）重试用尽
            (_render(), FOREVER),  # 第二批（10 段）正常
        ]
    )
    translator = _make_translator(post, tmp_path)
    results = translator.translate_texts(texts)
    assert results[:50] == texts[:50]  # 失败批保留原文
    assert results[50:] == ["译：Sentence" for _ in range(10)]
    assert translator.failed_count == 50
    assert translator.translated_count == 10


# ---------------------------------------------------------------------------
# 用量记账与预算
# ---------------------------------------------------------------------------


def test_usage_recorded_in_jsonl_and_month_usage_aggregates(monkeypatch, tmp_path):
    monkeypatch.setattr("core.api_keys.USAGE_FILE", tmp_path / "paid_usage.jsonl")
    post = _FakePost([(_render(), FOREVER)])
    translator = _make_translator(post, tmp_path)
    # 第一次翻译时还不知道文件名：探测行记「（密钥探测）」，批量行 file 为空
    translator.translate_texts(["Hello world."])
    translator.current_file = "demo.docx"
    translator.translate_texts(["Another English sentence."])

    rows = [
        json.loads(line)
        for line in (tmp_path / "paid_usage.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    # 探测一行 + 两次批量各一行
    assert len(rows) == 3
    probe_row, first_batch, second_batch = rows
    assert probe_row["engine"] == "deepl"
    assert probe_row["file"] == "（密钥探测）"
    assert probe_row["chars"] == 5  # len("Hello")
    assert first_batch["file"] == ""
    assert first_batch["billed"] == len("Hello world.")
    assert second_batch["file"] == "demo.docx"
    assert re.match(r"\d{4}-\d{2}", second_batch["month"])

    month = second_batch["month"]
    usage = month_usage("deepl", month)
    assert usage["calls"] == 3
    assert usage["chars"] == 5 + len("Hello world.") + len("Another English sentence.")
    # 别的月份/引擎不计入
    assert month_usage("deepl", "1999-01") == {"chars": 0, "billed": 0, "calls": 0}
    assert month_usage("edge", month) == {"chars": 0, "billed": 0, "calls": 0}


def test_budget_warning_triggers_but_translation_continues(monkeypatch, tmp_path):
    monkeypatch.setattr("core.api_keys.USAGE_FILE", tmp_path / "paid_usage.jsonl")
    month = __import__("datetime").datetime.now().strftime("%Y-%m")
    record_usage("deepl", 90, 90, "old.docx")  # 本月已用 90
    post = _FakePost([(_render(), FOREVER)])
    logs: list[str] = []
    translator = _make_translator(post, tmp_path, logs=logs, budget=100)
    results = translator.translate_texts(["Hello world."])
    # 90 + 12 > 100：醒目警告，但翻译照常继续（不硬拦）
    assert any("预算提醒" in message for message in logs)
    assert results == ["译：Hello wo"]


def test_no_budget_means_no_warning(monkeypatch, tmp_path):
    monkeypatch.setattr("core.api_keys.USAGE_FILE", tmp_path / "paid_usage.jsonl")
    record_usage("deepl", 999999, 999999, "old.docx")
    post = _FakePost([(_render(), FOREVER)])
    logs: list[str] = []
    translator = _make_translator(post, tmp_path, logs=logs, budget=0)
    translator.translate_texts(["Hello world."])
    assert not any("预算提醒" in message for message in logs)


def test_summary_reports_session_and_month_usage(monkeypatch, tmp_path):
    monkeypatch.setattr("core.api_keys.USAGE_FILE", tmp_path / "paid_usage.jsonl")
    post = _FakePost([(_render(), FOREVER)])
    translator = _make_translator(post, tmp_path)
    translator.translate_texts(["Hello world."])
    summary = translator.summary()
    assert "DeepL 翻译 1 段" in summary
    assert "本次消耗" in summary
    assert "本月累计" in summary


def test_usage_payload_includes_official_numbers(tmp_path):
    """官方口径（GET /v2/usage）进 usage_payload，失败只降级不报错。"""
    translator = _make_translator(
        _FakePost([(_render(), FOREVER)]),
        tmp_path,
    )
    payload = translator.usage_payload()
    assert payload["engine"] == "deepl"
    assert payload["official"] == {"character_count": 42, "character_limit": 500000}

    broken = _make_translator(_FakePost([(_render(), FOREVER)]), tmp_path)
    broken.get = lambda path: _Response(403, {})
    assert broken.usage_payload()["official"] is None


# ---------------------------------------------------------------------------
# 自检报告用量段
# ---------------------------------------------------------------------------


def test_report_renders_paid_usage_section(tmp_path):
    output = tmp_path / "out.docx"
    output.write_text("x", encoding="utf-8")
    usage = {
        "engine": "deepl",
        "session_chars": 1200,
        "session_billed": 1180,
        "month_chars": 5000,
        "month_billed": 4900,
        "budget": 500000,
        "official": {"character_count": 4900, "character_limit": 500000},
    }
    path = build_report(str(output), CheckReport(), paid_usage=usage)
    assert path is not None
    text = Path(path).read_text(encoding="utf-8")
    assert "── 付费引擎用量 ──" in text
    assert "送检 1200 字符" in text
    assert "DeepL 计费 1180 字符" in text
    assert "本月本地累计：送检 5000 字符，计费 4900 字符" in text
    assert "DeepL 账户页显示：4900 / 500000 字符（官方口径）" in text
    assert "本地预算上限：500000 字符/月" in text


def test_report_without_paid_usage_has_no_section(tmp_path):
    output = tmp_path / "out.docx"
    output.write_text("x", encoding="utf-8")
    path = build_report(str(output), CheckReport())
    assert path is not None
    text = Path(path).read_text(encoding="utf-8")
    assert "付费引擎用量" not in text


def test_report_paid_usage_without_official_or_budget(tmp_path):
    """官方口径没拿到 / 没设预算时，两行都不出现，不占版面。"""
    output = tmp_path / "out.docx"
    output.write_text("x", encoding="utf-8")
    usage = {
        "engine": "deepl",
        "session_chars": 10,
        "session_billed": 10,
        "month_chars": 10,
        "month_billed": 10,
        "budget": 0,
        "official": None,
    }
    path = build_report(str(output), CheckReport(), paid_usage=usage)
    text = Path(path).read_text(encoding="utf-8")
    assert "── 付费引擎用量 ──" in text
    assert "账户页显示" not in text
    assert "预算上限" not in text


def test_paid_usage_of_plain_object():
    class _Plain:
        pass

    assert paid_usage_of(_Plain()) is None


def test_paid_usage_of_reads_translator_attribute():
    class _Rich:
        def usage_payload(self):
            return {"engine": "deepl"}

    assert paid_usage_of(_Rich()) == {"engine": "deepl"}


# ---------------------------------------------------------------------------
# CLI 三道闸门（app.py）
# ---------------------------------------------------------------------------


def test_gate_blocks_offline_combination(monkeypatch, capsys):
    """保密模式与付费档硬互斥：文档出境是红线，没有「提醒后放行」。"""
    import app

    monkeypatch.setattr(app, "CLI_PAID", "deepl", raising=False)
    monkeypatch.setattr(app, "OFFLINE", True, raising=False)
    assert app._paid_gate() == 2
    assert "保密模式不能用付费 API" in capsys.readouterr().out


def test_gate_guides_when_key_missing(monkeypatch, capsys, tmp_path):
    import app

    monkeypatch.setattr(app, "CLI_PAID", "deepl", raising=False)
    monkeypatch.setattr(app, "OFFLINE", False, raising=False)
    monkeypatch.setattr("core.api_keys.KEYS_FILE", tmp_path / "none.json")
    assert app._paid_gate() == 3
    out = capsys.readouterr().out
    assert "deepl.com/pro-api" in out
    # 引导文案要点名 key 文件的位置（测试里指向临时目录）
    assert str(tmp_path / "none.json") in out
    assert keys_file() == str(tmp_path / "none.json")


def test_gate_guides_when_key_malformed(monkeypatch, capsys, tmp_path):
    import app

    monkeypatch.setattr(app, "CLI_PAID", "deepl", raising=False)
    monkeypatch.setattr(app, "OFFLINE", False, raising=False)
    path = _write_keys(tmp_path, key="sk-不是deepl的key")
    monkeypatch.setattr("core.api_keys.KEYS_FILE", path)
    assert app._paid_gate() == 3
    assert "申请" in capsys.readouterr().out


def test_gate_requires_explicit_acknowledgement(monkeypatch, capsys, tmp_path):
    """未确认涉密警示 → 要求 --yes-i-know；给了就落标记，下次不再问。"""
    import app

    monkeypatch.setattr(app, "CLI_PAID", "deepl", raising=False)
    monkeypatch.setattr(app, "OFFLINE", False, raising=False)
    path = _write_keys(tmp_path)
    monkeypatch.setattr("core.api_keys.KEYS_FILE", path)

    monkeypatch.setattr(app, "CLI_YES_I_KNOW", False, raising=False)
    assert app._paid_gate() == 4
    out = capsys.readouterr().out
    assert "涉密警示" in out
    assert "境外服务器" in out
    assert "--yes-i-know" in out
    assert is_acknowledged("deepl") is False

    monkeypatch.setattr(app, "CLI_YES_I_KNOW", True, raising=False)
    assert app._paid_gate() is None
    assert is_acknowledged("deepl") is True


def test_gate_rejects_unknown_paid_engine(monkeypatch, capsys):
    import app

    monkeypatch.setattr(app, "CLI_PAID", "gpt", raising=False)
    assert app._paid_gate() == 5
    assert "deepl" in capsys.readouterr().out


def test_cli_rejects_paid_with_offline_end_to_end():
    """子进程里真跑一次 main()：--paid=deepl --offline 必须非零退出并说人话。"""
    script = (
        "import sys\n"
        'sys.argv = ["app", "--offline", "--paid=deepl", "--convert"]\n'
        "import app\n"
        "app.main()\n"
    )
    child = subprocess.run(
        [str(PYTHON), "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert child.returncode == 2, child.stdout + child.stderr
    assert "保密模式不能用付费 API" in child.stdout


def test_convert_builds_deepl_translator(monkeypatch, tmp_path):
    """--paid=deepl --convert 走 DeepLTranslator（key/缓存/风格照 CLI 开关传）。"""
    import app

    created: dict = {}

    class _FakeDeepL:
        def __init__(self, key_config, **kwargs) -> None:
            created["key_config"] = key_config
            created["kwargs"] = kwargs
            self.stop_event = None
            self.current_file = ""

        def translate_texts(self, texts, progress=None, ctx=None):
            return [f"译：{text}" for text in texts]

        def summary(self) -> str:
            return "fake"

    monkeypatch.setattr("core.deepl.DeepLTranslator", _FakeDeepL)
    monkeypatch.setattr(app, "CLI_PAID", "deepl", raising=False)
    monkeypatch.setattr(app, "CLI_NO_CACHE", False, raising=False)
    path = _write_keys(tmp_path)
    monkeypatch.setattr("core.api_keys.KEYS_FILE", path)

    sample = ROOT / "samples" / "示例-英文报告.docx"
    if not sample.exists():
        pytest.skip("样例缺失，跳过")
    work = tmp_path / sample.name
    work.write_bytes(sample.read_bytes())

    code = app._convert([str(work)])
    assert code == 0
    assert created["key_config"]["key"] == FAKE_KEY
    assert created["kwargs"]["style"] == app.CLI_STYLE
    assert created["kwargs"]["cache"].endswith("translations.sqlite")
    assert (tmp_path / "示例-英文报告_双语.docx").exists()
