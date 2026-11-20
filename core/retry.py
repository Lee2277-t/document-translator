"""统一重试 / 退避。

为什么要有这个模块
------------------
本项目有五条网络通路（Edge、微软网页、MyMemory、DeepL、本地 Ollama），
改动前它们各自手写重试：有的 ``for attempt in range(2)`` + ``sleep(0.6)``，
有的 ``while True`` 配一堆计数器。三处后果：

* **该重试的没重试**（HTTP 500 在某些分支直接判死）；
* **不该重试的瞎重试**（401/403 是 key 错误，重试三次只会让用户干等三倍
  时间，还给对方风控系统送三倍异常流量）；
* **重试静默**（睡完再来一次，界面上一句话没有，用户以为卡死）。

这里把三件事收成一处：分类（什么值得重试）、退避（等多久）、出声（每次
都说话）。**只用标准库**——项目要打包成免安装版，每多一个依赖都是分发成本。

分类口径（本模块最重要的判断）
------------------------------
* 可重试：网络错误 / 超时 / 429 / 5xx——这些是「对方或链路暂时不行」，
  等一会儿再来是理性行为；
* 不可重试：400 参数错、401/403 key 错、404、422…… 请求本身就错了，
  重试一万次也是同一个结果；
* 认不出来的（自建异常、不带状态码）**一律不重试**：宁可少试一次让用户
  早点看到错误，也不要把一个未知错误当成抖动反复撞墙。
  已知局限：这个保守口径会漏掉「自定义异常其实代表网络抖动」的情况，
  需要重试的调用方应当显式抛 ``TransientError``（带 status 更好）。

退避口径
--------
指数退避 + 抖动。抖动不是为了好看：并发/多实例同时被限流时，固定延迟会让
它们在**同一秒**集体重试（惊群），把刚恢复的服务再打挂一次。
``retry()`` 另有一个绝对上限 ``MAX_ATTEMPTS``——调用方就算传
``max_attempts=10**6`` 也不会真跑一百万次；「不可无限重试」是硬保证，
不靠调用方自觉。

取消与出声
----------
``stop_event`` 一到，退避等待**立刻结束**（用 ``Event.wait`` 而不是
``time.sleep``，唤醒是即时的）并抛 ``CancelledError``；用户点了取消，
没有理由再让他等一轮退避。
每次重试都会出声（回调或日志），重试耗尽也会出声——**绝不静默吞掉**：
静默重试耗尽等于告诉用户「翻好了」，其实那一段是原文。
"""

from __future__ import annotations

import http.client
import logging
import random
import socket
import ssl
import time
import urllib.error
from dataclasses import dataclass
from typing import Any, Callable

from .common import CancelledError, TranslationError, check_cancel

#: 模块日志器。与主程序同一个名字（app.py 里的 ``translation_tool``），
#: 调用方没传回调时，重试仍然会落进日志——「静默重试」不是可选项。
LOGGER = logging.getLogger("translation_tool")

#: ``retry()`` 的绝对重试尝试上限（含首次调用）。再高的值只会让用户白等，
#: 所以这里硬顶，不依赖调用方自觉。调用方想要更多次也拿不到。
MAX_ATTEMPTS = 10

#: 值得再试一次的状态码：限流、超时、服务端错误。
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 507, 509, 520, 522, 524})

#: 不该重试的状态码：请求本身就是错的（参数错 / key 错 / 找不到 / 语义错）。
FATAL_STATUS = frozenset({400, 401, 403, 404, 405, 409, 410, 413, 415, 422})

#: 判断状态码时依次看这几个属性：不同 HTTP 库的异常字段命名不同
#: （urllib 用 code、requests 用 response.status_code、自建异常用 status）。
_STATUS_ATTRS: tuple[str, ...] = ("status", "status_code", "code", "http_status")

#: 网络类异常。urllib.error.URLError 与 http.client.HTTPException 覆盖了
#: 本项目免费通道的全部连接失败；TimeoutError/ConnectionError/gaierror/SSLError
#: 覆盖 DNS、握手与读写超时。
_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    socket.timeout,
    socket.gaierror,
    ssl.SSLError,
    urllib.error.URLError,
    http.client.HTTPException,
)

#: 第三方 HTTP 库（requests / urllib3）的异常按模块名认，避免在模块导入期
#: 就把 requests 拉起来——付费档才需要它，免费通道不该为它付启动成本。
_FOREIGN_NETWORK_MODULES: tuple[str, ...] = ("requests.", "urllib3.", "http.client")


class TransientError(TranslationError):
    """可重试的失败：网络抖动、超时、429、5xx。

    继承 ``TranslationError`` 是为了让既有的 ``except TranslationError``
    一律照旧接住，接线不需要改调用方的异常清单。
    """

    def __init__(self, message: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class PermanentError(TranslationError):
    """不可重试的失败：key 错 / 参数错。重试这类错误是纯浪费。"""

    def __init__(self, message: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def status_of(exc: BaseException) -> int | None:
    """从异常上取 HTTP 状态码。取不到返回 None（交给类型判断兜底）。

    先看自身属性，再看 ``exc.response``（requests 把状态码藏在响应对象里）。
    """
    for name in _STATUS_ATTRS:
        value = getattr(exc, name, None)
        if isinstance(value, bool):  # bool 是 int 的子类，不当状态码
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    response = getattr(exc, "response", None)
    if response is not None:
        for name in _STATUS_ATTRS:
            value = getattr(response, name, None)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
    return None


def http_error(status: int, message: str = "") -> TranslationError:
    """按状态码造一个带 ``status`` 的异常：429/5xx → 可重试，4xx → 不可重试。

    为什么不直接 ``raise TranslationError``：那样异常身上不带状态码，
    上层只能靠字符串匹配去猜「这是限流还是 key 错了」——而字符串匹配
    一旦文案改动就悄悄失效。带上状态码，分类永远是对的。
    """
    text = message or f"HTTP {status}"
    if status in FATAL_STATUS:
        return PermanentError(text, status)
    if status in RETRYABLE_STATUS:
        return TransientError(text, status)
    if 400 <= status < 500:
        # 其余 4xx 一律按「请求本身有问题」处理：重试不会改变结果。
        return PermanentError(text, status)
    return TransientError(text, status)


def looks_like_network_error(exc: BaseException) -> bool:
    """是不是网络类异常（超时/断连/DNS/握手）。"""
    module = type(exc).__module__ or ""
    if module.startswith(_FOREIGN_NETWORK_MODULES):
        return True
    return isinstance(exc, _NETWORK_ERRORS)


def is_retryable(exc: BaseException) -> bool:
    """值不值得再试一次。这是本模块的核心判断，分类错了整套退避都白搭。

    * 取消不是失败，绝不以「重试」的名义去抵消用户的取消意图；
    * 带状态码的按码判（429/5xx 可重试，4xx 不可）；
    * 显式类型（``TransientError`` / ``PermanentError``）按类型判；
    * 其余按「是不是网络异常」判；认不出来就**不重试**（保守口径，
      理由写在模块说明里）。
    """
    if isinstance(exc, CancelledError):
        return False
    status = status_of(exc)
    if status is not None:
        if status in FATAL_STATUS:
            return False
        if status in RETRYABLE_STATUS:
            return True
        if 400 <= status < 500:
            return False
        return True
    if isinstance(exc, PermanentError):
        return False
    if isinstance(exc, TransientError):
        return True
    return looks_like_network_error(exc)


@dataclass(frozen=True)
class BackoffPolicy:
    """退避策略。

    ``delays`` 给了就按这个序列取值（付费档沿用既有 0.5/1/2 秒口径），
    没给就按 ``base_delay * multiplier ** (attempt - 1)`` 算，并夹在
    ``max_delay`` 以内——没有上限的指数退避会让第 8 次重试睡两分钟。
    ``jitter=True`` 时在 [1-jitter_ratio, 1] 区间里取比例，避免惊群；
    显式 ``delays`` 序列**不抖动**（确定序列是被既有测试钉住的行为）。
    """

    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    multiplier: float = 2.0
    jitter: bool = True
    jitter_ratio: float = 0.5
    delays: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts 至少为 1（1 表示只试一次、不重试）")
        if self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("退避时长不能为负")
        if self.multiplier < 1:
            raise ValueError("multiplier 小于 1 会越退避越快，不合逻辑")

    def delay_for(
        self, attempt: int, rand: Callable[[], float] | None = None
    ) -> float:
        """第 ``attempt`` 次重试前该等多久（attempt 从 1 起）。"""
        if attempt < 1:
            return 0.0
        if self.delays:
            # 显式序列**不抖动**：付费档的 0.5/1/2 秒是被既有测试与文档
            # 钉住的确定行为，抖动会让它变成「大约 0.5 秒」。
            index = min(attempt - 1, len(self.delays) - 1)
            return max(0.0, min(float(self.delays[index]), self.max_delay))
        raw = self.base_delay * (self.multiplier ** (attempt - 1))
        raw = max(0.0, min(raw, self.max_delay))
        if not self.jitter or raw <= 0.0:
            return raw
        ratio = min(max(self.jitter_ratio, 0.0), 1.0)
        roll = (rand or random.random)()
        return raw * (1.0 - ratio) + raw * ratio * roll


#: 默认策略：最多 3 次尝试（首发 + 2 次重试），0.5 秒起、8 秒封顶、带抖动。
DEFAULT_POLICY = BackoffPolicy()


def _reason(exc: BaseException) -> str:
    """给人看的原因：有状态码说状态码，没有就说异常类型。"""
    status = status_of(exc)
    if status is not None:
        return f"HTTP {status}"
    return type(exc).__name__


def _default_retry_notice(label: str) -> Callable[[int, float, BaseException], None]:
    def notice(attempt: int, delay: float, exc: BaseException) -> None:
        LOGGER.warning(
            "%s失败（%s），%.1f 秒后第 %d 次重试", label, _reason(exc), delay, attempt
        )

    return notice


def _default_give_up_notice(label: str) -> Callable[[int, BaseException], None]:
    def notice(attempts: int, exc: BaseException) -> None:
        LOGGER.warning(
            "%s已试 %d 次仍未成功（%s），按失败处理，不静默跳过",
            label,
            attempts,
            _reason(exc),
        )

    return notice


def wait_or_cancel(
    delay: float,
    stop_event: Any = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """等 ``delay`` 秒；``stop_event`` 一到立刻返回并抛 ``CancelledError``。

    用 ``Event.wait(delay)`` 而不是 ``sleep(delay)``：后者醒来才知道用户
    已经点了取消，白等一轮退避。
    """
    if delay > 0 and stop_event is not None:
        stop_event.wait(delay)
    elif delay > 0:
        (sleep or time.sleep)(delay)
    check_cancel(stop_event)


def retry(
    fn: Callable[[], Any],
    *,
    should_retry: Callable[[BaseException, int], bool] | None = None,
    policy: BackoffPolicy = DEFAULT_POLICY,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
    on_give_up: Callable[[int, BaseException], None] | None = None,
    stop_event: Any = None,
    sleep: Callable[[float], None] | None = None,
    rand: Callable[[], float] | None = None,
    label: str = "请求",
) -> Any:
    """带退避地执行 ``fn``，成功就返回它的返回值，失败就把最后一个异常原样抛出。

    * ``should_retry(exc, attempt)`` 不给就按 ``is_retryable`` 分类；
    * 每次重试前都会出声（``on_retry``，没给就写日志）；
    * 重试耗尽也会出声（``on_give_up``）并**抛出原异常**——调用方因此
      必须真的把这次失败计进失败数，没有「悄悄返回 None」这条路；
    * ``stop_event`` 一到，不等退避睡完就抛 ``CancelledError``。

    注意 ``max_attempts`` 是**尝试总次数**（含首发），不是「额外重试次数」。
    """
    attempts = max(1, min(int(policy.max_attempts), MAX_ATTEMPTS))
    notify = on_retry or _default_retry_notice(label)
    give_up = on_give_up or _default_give_up_notice(label)
    decide = should_retry or (lambda exc, _attempt: is_retryable(exc))
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        check_cancel(stop_event)
        try:
            return fn()
        except CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 分类交给 decide，不在这里挑
            last = exc
            if attempt >= attempts or not decide(exc, attempt):
                give_up(attempt, exc)
                raise
            delay = policy.delay_for(attempt, rand)
            notify(attempt, delay, exc)
            wait_or_cancel(delay, stop_event, sleep)
    # 循环只可能通过 return 或 raise 离开；这一行是兜底，防止将来有人改坏了
    # 循环条件之后函数「静默返回 None」——那是最难查的一类失效。
    raise last if last is not None else RuntimeError("重试逻辑异常终止")
