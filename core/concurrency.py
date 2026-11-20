"""并发执行与档位上限（ThreadPoolExecutor，纯标准库）。

为什么默认必须是 1
------------------
本项目的免费云端档走的是**别人的免费接口**（Edge / 微软网页 / MyMemory）。
默认开并发等于默认加速触发对方的限流与风控——一旦被封，用户失去的不是
「慢一点」，而是「整个免费通道」。所以：

* ``DEFAULT_JOBS = 1``：不写 ``--jobs`` 时的行为与串行**逐字节等价**；
* 只有用户显式写 ``--jobs=N`` 才放开，且按档位夹上限；
* 出事（429 / 连续失败）自动降回 1，并且**出声**。

档位上限（当前实际取值，理由写在各常量旁）
------------------------------------------
* ``cloud`` **1**：免费云端档不并发。Round 2 先给了 2 + 令牌桶，QA 的广度
  探针证明那是个「开了更慢」的旋钮（详见 ``CLOUD_MAX_JOBS`` 注释）；
* ``paid`` 4：DeepL 官方允许并发，实测 60 段多批次能到 2 倍重叠、耗时减半；
* ``local-llm`` 按 CPU 核数（本机服务，没有封禁风险；再夹一层 8）；
* ``offline`` **1**：进程内批推理，并发无收益且有 OOM 风险。

一句话守则：**默认值必须是 1，而且开了 N 不能比 1 更慢**。做不到这一条的
档位就该老实按 1 跑，并把原因说出来（``TIER_SINGLE_NOTE``）。

三条不可妥协的不变量（改这个文件前先读）
----------------------------------------
1. **结果顺序严格对齐原文顺序**。本项目有血泪史：``call_batch`` 专门校验
   返回条数，就是防「第 7 段译文贴到第 8 段」的错位事故。并发只能让完成
   顺序乱，**回填必须按索引**——``run_ordered`` 返回的列表下标与输入一一
   对应，且共享状态（缓存、计数、进度）一律在**主线程按索引顺序**写，
   worker 只做「取回结果」这一件纯活。
2. **``stop_event`` 必须及时中断**。收集结果时不阻塞在 ``future.result()``
   上（否则取消要等最慢那一段跑完），而是短轮询 + 每次先看 ``stop_event``。
3. **异常不许被线程池吞掉**。worker 抛错会经 ``on_error`` 出声，对应位置
   回填 ``None``，由调用方如实计入失败——绝不能变成「某段静默没翻」。

已知局限（诚实交代）
--------------------
* Python 无法强杀线程：取消后已在飞的 worker 会跑完当前那一次调用才退出
  （``shutdown(cancel_futures=True)`` 只挡住**还没开始**的任务）。
* 本地离线档（offline）的模型推理是进程内批推理，线程并发没有收益，
  因此它接受 ``jobs`` 参数但按 1 执行，并显式出声说明（见
  ``core/local_translator.py``）。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Callable, Sequence

from .common import CancelledError, check_cancel
from .retry import status_of

LOGGER = logging.getLogger("translation_tool")

#: 默认并发度。**改动这个值等于改动所有用户的默认行为**——它是零回归的锚，
#: tests/test_concurrency.py 里有一条断言专门钉住它。
DEFAULT_JOBS = 1

#: 免费云端档上限：**1，即不并发**。
#:
#: 这里原本是 2 + 令牌桶 1.5 次/秒，被 QA 的广度探针打穿了：串行路径压根
#: 不过那个桶，自然速率就是「单次延迟的倒数」（0.25s 延迟时约 4 次/秒），
#: 而并行路径每发一个请求都得先在桶里等约 0.67 秒——于是开了 ``--jobs=2``
#: 反而比不开更慢（12 段实测 3.02s → 4.97s）。**一个按了开关却更慢的旋钮
#: 是在坑用户**，所以直接夹到 1。
#:
#: 为什么不改成「把速率抬到不再比串行慢」：要让「任意单次延迟下 jobs>1 都
#: 不慢于 jobs=1」，令牌桶速率就不能低于串行的自然速率，那等于把阀门拧到
#: 起不到节流作用；而并发对免费端点的**唯一**净效果就是提高请求速率，那
#: 正是被限流、被封禁的原因（也是 Round 2 把 E2 退避列为并发前置的理由）。
#: 收益只在单次延迟 ≥1s 时才显现，而那时多出来的封号风险远大于省下的等待。
#: 真要提高免费档吞吐，方向是**减少请求数**（更好的批量合并），不是提高并发。
CLOUD_MAX_JOBS = 1

#: 付费档上限：DeepL 官方允许并发，4 是保守取值。
PAID_MAX_JOBS = 4

#: 本地大模型档按 CPU 核数放开，但夹这个上限：核数多的机器上堆一堆线程只会
#: 把内存和上下文切换吃光（本地模型本身已经是多线程批推理）。
LOCAL_MAX_JOBS_CAP = 8

#: 离线（保密）档也是 1：它的翻译是 CTranslate2 的**进程内批推理**——一批段
#: 一起送进模型、已吃满 CPU 线程，外面再开线程只翻倍峰值内存、增加上下文
#: 切换，实测没有收益且有 OOM 风险。这条必须写在**表里**而不是「夹完之后再
#: 改回 1」：否则 ``clamp_jobs`` 对外声称的上限是假的，而事实是该档位的执行
#: 路径从来没有并发过。
OFFLINE_MAX_JOBS = 1

#: 令牌桶速率（每秒几次请求）。**当前没有任何档位在用**：它是留给将来某个
#: 「真需要并发又需要限速」的档位的阀门；免费云端档已策略性不并发（见
#: ``CLOUD_MAX_JOBS`` 的注释），谁动了那个上限，谁就必须重新校准这个值，
#: 否则又会把并发拖得比串行还慢。
CLOUD_RATE_PER_SECOND = 1.5

#: 收集结果时的轮询间隔（秒）。取消的响应延迟上界就是这个值。
POLL_SECONDS = 0.05

#: 连续失败多少次就降回单线程（429 是**一次就降**，不等够次数）。
DEGRADE_AFTER_FAILURES = 3

#: 档位 → 并发上限。只有本地大模型档按 CPU 核数算（依赖核数，单独分支）。
TIER_LIMITS: dict[str, int] = {
    "cloud": CLOUD_MAX_JOBS,
    "paid": PAID_MAX_JOBS,
    "offline": OFFLINE_MAX_JOBS,
}

#: 只能单线程的档位，必须**说出原因**。用户显式写了 ``--jobs=N``，工具却
#: 悄悄按 1 跑，等于骗人——用户会以为开关坏了。口径参照保密档已有的做法
#: （core/local_translator.py 之前就是这么出声的）。
TIER_SINGLE_NOTE: dict[str, str] = {
    "cloud": (
        "免费云端档不并发：能提高的只有请求速率，而那正是免费端点限流与封禁的"
        "原因（实测并行不快反慢），已按单线程执行。"
    ),
    "offline": (
        "离线模型是本机批推理，并发没有收益，本次按单线程执行。"
    ),
}


def cpu_jobs(cpu: int | None = None) -> int:
    """本地档的并发上限：CPU 核数，夹在 [1, LOCAL_MAX_JOBS_CAP]。"""
    count = cpu if cpu is not None else (os.cpu_count() or 1)
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 1
    return max(1, min(count, LOCAL_MAX_JOBS_CAP))


def tier_job_limit(tier: str, cpu: int | None = None) -> int:
    """某档位允许的最大并发度。未知档位一律 1（保守，宁可慢也别闯祸）。"""
    if tier == "local-llm":
        return cpu_jobs(cpu)
    return TIER_LIMITS.get(tier, 1)


def clamp_jobs(tier: str, requested: int | None = None, cpu: int | None = None) -> int:
    """把用户请求的并发度夹到档位允许的范围内。

    ``requested`` 为 None / 非数字 / ≤1 时一律返回 ``DEFAULT_JOBS``（1）——
    **默认不并发**是本模块的立身之本。
    """
    if requested is None:
        return DEFAULT_JOBS
    try:
        value = int(requested)
    except (TypeError, ValueError):
        return DEFAULT_JOBS
    if value <= 1:
        return 1
    return min(value, tier_job_limit(tier, cpu))


class TokenBucket:
    """令牌桶限速：免费云端档并发时的礼貌阀门。

    挂在**请求**粒度上：不管几个线程，全局每秒最多放行 ``rate`` 次。
    ``clock`` / ``sleep`` 可注入，测试不需要真的等墙钟时间。
    """

    def __init__(
        self,
        rate: float = CLOUD_RATE_PER_SECOND,
        capacity: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rate = max(0.001, float(rate))
        self.capacity = max(1.0, float(capacity))
        self._tokens = self.capacity
        self._last: float | None = None
        self._lock = threading.Lock()
        self._clock = clock
        self._sleep = sleep

    def acquire(self, tokens: float = 1.0, stop_event: Any = None) -> float:
        """取令牌，返回等待时长（秒）。

        ``stop_event`` 已置位时抛 ``CancelledError``：用户取消后没道理
        还在等令牌。
        """
        waited = 0.0
        while True:
            with self._lock:
                now = float(self._clock())
                if self._last is None:
                    self._last = now
                else:
                    elapsed = now - self._last
                    if elapsed > 0:
                        self._tokens = min(
                            self.capacity, self._tokens + elapsed * self.rate
                        )
                        self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                delay = (tokens - self._tokens) / self.rate
            check_cancel(stop_event)
            slice_seconds = min(delay, POLL_SECONDS)
            if stop_event is not None:
                stop_event.wait(slice_seconds)
            else:
                self._sleep(slice_seconds)
            waited += slice_seconds


class JobGovernor:
    """并发度管家：按档位夹上限、出事了自动降回 1，并且每次都出声。

    「自动降回 1」必须出声——静默降速会让用户以为工具坏了（明明我开了
    ``--jobs=4`` 怎么还是这么慢）。降级只播报一次，不刷屏。
    """

    def __init__(
        self,
        tier: str,
        jobs: int | None = DEFAULT_JOBS,
        log: Callable[[str], None] | None = None,
        cpu: int | None = None,
    ) -> None:
        self.tier = tier
        self.requested = jobs if isinstance(jobs, int) else DEFAULT_JOBS
        self.limit = tier_job_limit(tier, cpu)
        self.jobs = clamp_jobs(tier, self.requested, cpu)
        self.log = log or (lambda message: LOGGER.info(message))
        self.degraded = False
        self.degrade_reason = ""
        self.consecutive_failures = 0
        self._announced = False

    def announce(self) -> None:
        """开工前把实际并发度说出来（>1 说几路；被夹低或被策略拒了说原因）。

        幂等：一份文档可能多次进并发路径（逐段兜底、多个批次），但同一件事
        没必要说 N 遍——刷屏的日志等于没有日志。
        """
        if self._announced:
            return
        self._announced = True
        if self.jobs > 1:
            self.log(f"并发翻译：{self.jobs} 路（档位上限 {self.limit}）。")
            return
        if self.requested > 1:
            note = TIER_SINGLE_NOTE.get(self.tier, "")
            if note:
                self.log(note)
            else:
                self.log(
                    f"请求的并发度 {self.requested} 超过「{self.tier}」档位上限 "
                    f"{self.limit}，已按上限执行。"
                )

    def note_success(self) -> None:
        self.consecutive_failures = 0

    def note_failure(self, exc: BaseException | None = None) -> bool:
        """记一次失败；触发降级就把并发度降回 1 并返回 True。

        429 命中**一次**就降（对方已经明说「你太快了」，硬扛只会被封）；
        其余错误要连续 ``DEGRADE_AFTER_FAILURES`` 次才降（单次失败可能只是
        这一段本身有问题，不值得为此放弃并发）。
        """
        self.consecutive_failures += 1
        if self.jobs <= 1:
            return False
        status = None if exc is None else status_of(exc)
        reason = ""
        if status == 429:
            reason = "服务端限流（429）"
        elif self.consecutive_failures >= DEGRADE_AFTER_FAILURES:
            reason = f"连续失败 {self.consecutive_failures} 次"
        if not reason:
            return False
        previous = self.jobs
        self.jobs = 1
        self.degraded = True
        self.degrade_reason = reason
        self.log(f"并发降级：{reason}，并发度已从 {previous} 降回 1。")
        return True


def _default_error_report(index: int, exc: BaseException) -> None:
    LOGGER.error("第 %d 项并发任务失败（%s）：%s", index + 1, type(exc).__name__, exc)


def run_ordered(
    items: Sequence[Any],
    worker: Callable[[int, Any], Any],
    *,
    jobs: int = DEFAULT_JOBS,
    stop_event: Any = None,
    on_error: Callable[[int, BaseException], None] | None = None,
    on_done: Callable[[int, Any], None] | None = None,
    poll: float = POLL_SECONDS,
) -> list[Any]:
    """按索引对齐地执行一批任务，返回与输入等长、下标一一对应的结果列表。

    ``jobs <= 1`` 或只有一项时走**串行路径**（与改动前的 for 循环同构）；
    否则用线程池并发，但：

    * 结果按 ``out[index] = value`` 回填，**永不按完成顺序 append**；
    * worker 抛错 → ``on_error`` 出声 + 该位回填 ``None``，其余照做；
    * ``CancelledError`` 一律向上传播（取消优先于一切）；
    * 收集阶段短轮询 + 每次先 ``check_cancel``，取消不必等最慢的那一项。

    ``on_done(index, value)`` 在主线程里按**索引严格升序**调用，调用方可以在
    里面安全地更新计数、写缓存、推进度——这些事绝不放在 worker 里做，
    否则就是多个线程同时改共享状态（也让 GUI 线程被工作线程回调）。
    严格升序不是洁癖：付费档会把一个长段拆成多片、按到达顺序拼回，交付
    顺序一乱，段内顺序就反了。
    """
    total = len(items)
    out: list[Any] = [None] * total
    if total == 0:
        return out
    report = on_error or _default_error_report

    if jobs <= 1 or total == 1:
        for index, item in enumerate(items):
            check_cancel(stop_event)
            try:
                value = worker(index, item)
            except CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 出声并记录，不吞
                report(index, exc)
                value = None
            out[index] = value
            if on_done is not None:
                on_done(index, value)
        return out

    futures: dict[int, Any] = {}
    executor = ThreadPoolExecutor(
        max_workers=min(jobs, total), thread_name_prefix="translate-job"
    )
    try:
        for index, item in enumerate(items):
            check_cancel(stop_event)
            futures[index] = executor.submit(worker, index, item)
        # 交付**严格按下标升序**：不然后面那一项先回来就被先交付，调用方
        # 里「同一段被拆成多片、按到达顺序拼接」的逻辑（付费档就是这么干的）
        # 会把段内顺序拼反——那是比段落错位更难发现的一类错。
        # 等待只发生在「还没轮到的那一项」上，其余线程照飞，吞吐不受影响。
        for index in range(total):
            while not futures[index].done():
                check_cancel(stop_event)
                wait([futures[index]], timeout=poll)
            try:
                value = futures[index].result()
            except CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 不许被线程池吞掉
                report(index, exc)
                value = None
            out[index] = value
            if on_done is not None:
                on_done(index, value)
    finally:
        # cancel_futures 挡住「还没开始」的任务；已经在飞的跑完当前调用
        # 就退（Python 无法强杀线程，这是已知局限，写在模块说明里）。
        executor.shutdown(wait=False, cancel_futures=True)
    return out
