"""统一重试 / 退避（core/retry.py）的行为钉子。

排序按「错了以后谁最倒霉」：

1. **分类**（什么该重试、什么不该）——分错了整套退避都是反作用：
   给 401/403 重试三遍，用户白等三倍时间还帮对方风控刷了流量；
2. **次数上限**——不可无限重试，这是硬保证；
3. **出声**——每次重试要说，耗尽也要说，绝不静默吞掉；
4. **取消**——stop_event 一到立刻放弃，不等退避睡完；
5. **抖动**——存在且不超过上限。

**禁止依赖时序/睡眠的断言**（上一轮 M3 变异检验的教训：Windows 时钟
分辨率约 15ms，纯 roundtrip 断言抓不到问题）。这里所有用例要么用注入的
``sleep`` / ``rand`` 直接观察，要么用 ``threading.Event`` 强制交错。
"""

from __future__ import annotations

import http.client
import logging
import socket
import urllib.error

import pytest

from core.common import CancelledError
from core.retry import (
    MAX_ATTEMPTS,
    BackoffPolicy,
    PermanentError,
    TransientError,
    http_error,
    is_retryable,
    retry,
    status_of,
)


# ---------------------------------------------------------------------------
# 1. 分类：可重试 vs 不可重试
# ---------------------------------------------------------------------------


def test_rate_limit_and_server_errors_are_retryable() -> None:
    """429 / 5xx 是「对方暂时不行」，值得再试。"""
    for status in (429, 500, 502, 503, 504):
        assert is_retryable(http_error(status, "boom")) is True


def test_key_and_parameter_errors_are_not_retryable() -> None:
    """401/403（key 错）与 400（参数错）重试一万次也是同一个结果。

    这条是本次改动的核心纠正项：改前的手写重试对**任何**异常都重试一次，
    包括 key 失效——用户因此要等满一轮才知道「其实是 key 错了」。
    """
    for status in (400, 401, 403, 404, 422):
        assert is_retryable(http_error(status, "boom")) is False


def test_network_errors_are_retryable() -> None:
    """超时 / 断连 / DNS / 握手：链路问题，等一会儿再来是理性的。"""
    assert is_retryable(TimeoutError("read timeout")) is True
    assert is_retryable(socket.gaierror("dns")) is True
    assert is_retryable(urllib.error.URLError("down")) is True
    assert is_retryable(http.client.HTTPException("bad")) is True


def test_unknown_errors_are_not_retryable() -> None:
    """认不出来的不重试：宁可让用户早点看到错误，也不要把未知错误当抖动
    反复撞墙（保守口径，理由写在 core/retry.py 模块说明里）。"""
    assert is_retryable(RuntimeError("something odd")) is False


def test_cancellation_is_never_retried() -> None:
    """取消不是失败——绝不能以「重试」的名义抵消用户的取消意图。"""
    assert is_retryable(CancelledError()) is False


def test_explicit_markers_win_over_guessing() -> None:
    """调用方显式标了类型就按类型判：这是「保守口径」的唯一逃生门。"""
    assert is_retryable(TransientError("自认为可重试")) is True
    assert is_retryable(PermanentError("自认为不可重试")) is False


def test_status_of_reads_different_attribute_names() -> None:
    """不同 HTTP 库把状态码放在不同字段上，分类全靠取得到它。"""
    assert status_of(http_error(429, "x")) == 429

    class Requests:  # 模拟 requests 的 HTTPError（码在 response 上）
        def __init__(self, code: int) -> None:
            self.response = type("R", (), {"status_code": code})()

    assert status_of(Requests(503)) == 503
    assert status_of(RuntimeError("没有状态码")) is None


def test_http_error_keeps_http_status_on_the_exception() -> None:
    """异常的 status 字段必须留着：上层靠它区分「限流」与「服务不可用」。

    靠文案匹配迟早会因为改一句提示就悄悄失效。
    """
    assert http_error(429, "限流").status == 429
    assert isinstance(http_error(429, "限流"), TransientError)
    assert isinstance(http_error(403, "key 错"), PermanentError)


# ---------------------------------------------------------------------------
# 2. 次数上限：不可无限重试
# ---------------------------------------------------------------------------


def test_retry_stops_at_max_attempts() -> None:
    """一直失败 → 调用次数正好等于 max_attempts（含首发），一次不多。"""
    calls: list[int] = []

    def always_fail() -> str:
        calls.append(1)
        raise TransientError("still down")

    policy = BackoffPolicy(max_attempts=3, base_delay=0.0)
    with pytest.raises(TransientError):
        retry(always_fail, policy=policy, on_retry=lambda *args: None)
    assert len(calls) == 3


def test_retry_never_exceeds_the_absolute_ceiling() -> None:
    """调用方就算要一百万次，也只给 MAX_ATTEMPTS——「不可无限重试」是
    硬保证，不靠调用方自觉。"""
    calls: list[int] = []

    def always_fail() -> str:
        calls.append(1)
        raise TransientError("still down")

    policy = BackoffPolicy(max_attempts=10**6, base_delay=0.0)
    with pytest.raises(TransientError):
        retry(always_fail, policy=policy, on_retry=lambda *args: None)
    assert len(calls) == MAX_ATTEMPTS
    assert MAX_ATTEMPTS < 10**6


def test_non_retryable_error_is_attempted_once() -> None:
    """不可重试的错误：一次就判死，不浪费用户的时间。"""
    calls: list[int] = []

    def fail() -> str:
        calls.append(1)
        raise PermanentError("key 错了", 403)

    with pytest.raises(PermanentError):
        retry(fail, policy=BackoffPolicy(max_attempts=5, base_delay=0.0))
    assert len(calls) == 1


def test_success_returns_immediately_without_retrying() -> None:
    """成功就返回，不多试一次（多余的重试也是别人的负担）。"""
    calls: list[int] = []

    def ok() -> str:
        calls.append(1)
        return "done"

    assert retry(ok, policy=BackoffPolicy(max_attempts=3, base_delay=0.0)) == "done"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 3. 出声：每次重试都说话，耗尽也说话
# ---------------------------------------------------------------------------


def test_every_retry_is_announced_with_attempt_and_delay() -> None:
    """每次重试都要说出「第几次、等多久、为什么」——静默重试等于让用户
    以为卡死了。"""
    notices: list[tuple[int, float, str]] = []

    def on_retry(attempt: int, delay: float, exc: BaseException) -> None:
        notices.append((attempt, delay, type(exc).__name__))

    def fail() -> str:
        raise TransientError("down")

    with pytest.raises(TransientError):
        retry(
            fail,
            policy=BackoffPolicy(max_attempts=3, base_delay=0.5),
            on_retry=on_retry,
        )
    # 3 次尝试 = 2 次重试，每次都要出声
    assert [item[0] for item in notices] == [1, 2]
    assert all(delay > 0 for _attempt, delay, _name in notices)
    assert all(name == "TransientError" for _a, _d, name in notices)


def test_giving_up_is_announced_and_raises_the_original_error() -> None:
    """重试耗尽必须出声，并把**原异常**如实抛出（不是抛个包装，也不是
    静默返回 None）——调用方因此必须真的把它计进失败数。"""
    given_up: list[tuple[int, BaseException]] = []
    error = TransientError("down", 429)

    def fail() -> str:
        raise error

    with pytest.raises(TransientError) as excinfo:
        retry(
            fail,
            policy=BackoffPolicy(max_attempts=2, base_delay=0.0),
            on_retry=lambda *args: None,
            on_give_up=lambda attempts, exc: given_up.append((attempts, exc)),
        )
    assert excinfo.value is error  # 原样抛出，不包装、不替换
    assert len(given_up) == 1
    assert given_up[0][0] == 2
    assert given_up[0][1] is error


def test_retry_is_audible_by_default_without_callbacks(caplog) -> None:
    """调用方什么都不传也要出声（落到 translation_tool 日志）。

    「静默重试」不是可选项：它最坏的表现是用户以为翻好了，其实那一段是原文。
    """
    def fail() -> str:
        raise TransientError("down", 503)

    with caplog.at_level(logging.WARNING, logger="translation_tool"):
        with pytest.raises(TransientError):
            retry(fail, policy=BackoffPolicy(max_attempts=2, base_delay=0.0))
    messages = [record.getMessage() for record in caplog.records]
    assert any("重试" in message for message in messages)
    assert any("按失败处理" in message for message in messages)


# ---------------------------------------------------------------------------
# 4. 取消：立即放弃，不等退避睡完
# ---------------------------------------------------------------------------


def test_stop_event_aborts_without_sleeping_out_the_backoff() -> None:
    """stop_event 已置位 → 立刻抛 CancelledError，退避一秒都不睡。

    用 ``Event.wait`` 而不是 ``time.sleep`` 才有这个性质；断言方式是
    「注入的 sleep 一次都没被调用」，不碰墙钟。
    """
    import threading

    slept: list[float] = []
    stop = threading.Event()
    stop.set()

    def fail() -> str:
        raise TransientError("down")

    with pytest.raises(CancelledError):
        retry(
            fail,
            policy=BackoffPolicy(max_attempts=5, base_delay=30.0),
            on_retry=lambda *args: None,
            stop_event=stop,
            sleep=slept.append,
        )
    # 第一次尝试前就取消了：连一次都没调用
    assert slept == []


def test_stop_event_set_during_backoff_stops_the_wait() -> None:
    """退避睡到一半用户点了取消：不等睡完，立刻放弃。"""
    import threading

    slept: list[float] = []
    stop = threading.Event()

    def fail() -> str:
        # 第一次尝试后把取消置上，模拟「用户等不及了」
        stop.set()
        raise TransientError("down")

    with pytest.raises(CancelledError):
        retry(
            fail,
            policy=BackoffPolicy(max_attempts=5, base_delay=30.0),
            on_retry=lambda *args: None,
            stop_event=stop,
            sleep=slept.append,
        )
    assert slept == []


# ---------------------------------------------------------------------------
# 5. 退避时长：指数增长、抖动、有上限
# ---------------------------------------------------------------------------


def test_backoff_grows_and_is_capped() -> None:
    """指数增长，但永远不超过 max_delay——没有上限的指数退避会让第 8 次
    重试睡两分钟。"""
    policy = BackoffPolicy(
        max_attempts=8, base_delay=0.5, max_delay=8.0, multiplier=2.0, jitter=False
    )
    delays = [policy.delay_for(attempt) for attempt in range(1, 8)]
    assert delays[0] == 0.5
    assert delays[1] == 1.0
    assert delays[2] == 2.0
    assert all(delay <= 8.0 for delay in delays)
    assert delays[-1] == 8.0


def test_jitter_exists_and_stays_within_the_cap() -> None:
    """抖动必须存在（防惊群），但抖动后的时长不许越过上限。

    断言靠注入 ``rand`` 的极值，不看墙钟、不赌时序。
    """
    policy = BackoffPolicy(max_attempts=4, base_delay=1.0, max_delay=8.0, jitter=True)
    lowest = policy.delay_for(2, rand=lambda: 0.0)
    highest = policy.delay_for(2, rand=lambda: 1.0)
    assert lowest < highest  # 抖动真的存在
    assert lowest >= 0.0
    assert highest <= policy.max_delay


def test_explicit_delay_sequence_is_not_jittered() -> None:
    """付费档沿用确定序列 0.5/1/2（既有测试钉住了它），这条分支不抖动。"""
    policy = BackoffPolicy(max_attempts=4, delays=(0.5, 1.0, 2.0), jitter=True)
    assert policy.delay_for(1, rand=lambda: 0.0) == 0.5
    assert policy.delay_for(1, rand=lambda: 1.0) == 0.5
    assert policy.delay_for(2) == 1.0
    assert policy.delay_for(3) == 2.0
    assert policy.delay_for(9) == 2.0  # 超出的取最后一项，不越界


def test_policy_rejects_nonsense() -> None:
    """策略参数本身就是错的就当场说，别等运行到一半才出怪事。"""
    with pytest.raises(ValueError):
        BackoffPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        BackoffPolicy(base_delay=-1)
    with pytest.raises(ValueError):
        BackoffPolicy(multiplier=0.5)


# ---------------------------------------------------------------------------
# 6. 自定义 should_retry（付费档的「429 三次、其余一次」预算靠它）
# ---------------------------------------------------------------------------


def test_custom_should_retry_can_budget_by_error_kind() -> None:
    """429 允许三次、5xx 只允许一次：这个预算是付费档的行为契约。"""
    attempts_of_kind = {"429": 0, "500": 0}

    def should_retry(exc: BaseException, _attempt: int) -> bool:
        if not is_retryable(exc):
            return False
        status = status_of(exc)
        if status == 429:
            attempts_of_kind["429"] += 1
            return attempts_of_kind["429"] <= 3
        attempts_of_kind["500"] += 1
        return attempts_of_kind["500"] <= 1

    calls: list[int] = []

    def fail_429() -> str:
        calls.append(1)
        raise TransientError("限流", 429)

    with pytest.raises(TransientError):
        retry(
            fail_429,
            should_retry=should_retry,
            policy=BackoffPolicy(max_attempts=10, base_delay=0.0),
            on_retry=lambda *args: None,
        )
    assert len(calls) == 4  # 首发 + 3 次重试

    calls.clear()

    def fail_500() -> str:
        calls.append(1)
        raise TransientError("服务端", 500)

    with pytest.raises(TransientError):
        retry(
            fail_500,
            should_retry=should_retry,
            policy=BackoffPolicy(max_attempts=10, base_delay=0.0),
            on_retry=lambda *args: None,
        )
    assert len(calls) == 2  # 首发 + 1 次重试
