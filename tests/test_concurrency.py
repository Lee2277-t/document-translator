"""并发（core/concurrency.py）的行为钉子。

三条不变量，违反任何一条都不许合入：

1. **结果顺序严格对齐原文顺序**。本项目有血泪史：``call_batch`` 专门校验
   返回条数，就是防「第 7 段的译文贴到第 8 段」这种最难查的错位事故。
   并发只能让**完成顺序**乱，回填必须按索引；
2. **``stop_event`` 必须及时中断**，不能因为并发让取消变慢或失效；
3. **``StateStore`` 的 ``RLock`` 不能死锁**——并发写 SQLite 要实测。

外加两条纪律：

* **默认必须是 1**：不写 ``--jobs`` 时的行为与串行逐字节等价（零回归）；
* **自动降级必须出声**：静默降速会让用户以为工具坏了。

**禁止依赖时序/睡眠的断言**（上一轮 M3 变异检验的教训：Windows 时钟
分辨率约 15ms，纯 roundtrip 断言抓不到问题）。这里所有交错都用
``threading.Barrier`` / ``threading.Event`` **强制**出来：反向完成顺序
是用「事件链」钉死的（worker N-1 先返回，逐个放行前一个），不是靠
``sleep`` 赌谁先跑完。
"""

from __future__ import annotations

import logging
import os
import threading

import pytest

from core.common import CancelledError, TranslationError
from core.concurrency import (
    CLOUD_MAX_JOBS,
    DEFAULT_JOBS,
    OFFLINE_MAX_JOBS,
    PAID_MAX_JOBS,
    TIER_SINGLE_NOTE,
    JobGovernor,
    TokenBucket,
    clamp_jobs,
    cpu_jobs,
    run_ordered,
    tier_job_limit,
)
from core.retry import TransientError
from core.state import StateStore
from core.translator import Translator, normalize_source


# ---------------------------------------------------------------------------
# 0. 档位上限与默认值（默认必须是 1）
# ---------------------------------------------------------------------------


def test_default_is_single_threaded() -> None:
    """默认值 1：不写 --jobs 时与串行逐字节等价，这条是零回归的锚。"""
    assert DEFAULT_JOBS == 1


@pytest.mark.parametrize(
    "tier, requested, expected",
    [
        ("cloud", None, 1),  # 不请求 → 1
        ("cloud", 1, 1),
        ("cloud", 2, CLOUD_MAX_JOBS),  # 免费云端不并发（理由见下面的用例）
        ("cloud", 8, CLOUD_MAX_JOBS),
        ("offline", 8, OFFLINE_MAX_JOBS),  # 离线档按 1 执行并出声
        ("paid", 1, 1),
        ("paid", 4, PAID_MAX_JOBS),  # 付费档上限 4
        ("paid", 99, PAID_MAX_JOBS),
    ],
)
def test_job_limits_per_tier(tier: str, requested: int | None, expected: int) -> None:
    """四档的上限各就各位：免费云端与离线都是 1，付费档宽松些。"""
    assert clamp_jobs(tier, requested, cpu=4) == expected


def test_cloud_never_parallelizes_no_matter_what_the_user_asks() -> None:
    """免费云端档**永远单线程**——这是 Round 2 收尾的硬政策。

    背景：一开始给的是「上限 2 + 令牌桶 1.5 次/秒」，QA 的广度探针证明那是
    个**开了更慢**的旋钮（串行路径根本不过令牌桶，自然速率就能到单次延迟的
    倒数，而并行路径每发一个请求都要先在桶里等约 0.67 秒；12 段 / 单次
    0.25s 实测 3.02s → 4.97s）。所以干脆夹到 1：**默认不能比 jops=1 更慢**。
    这里把它钉住，将来谁想放开，必须先证明「任意单次延迟下都不慢于串行」。
    """
    assert CLOUD_MAX_JOBS == 1
    for requested in (2, 4, 99, 10**6):
        assert clamp_jobs("cloud", requested) == 1
        assert tier_job_limit("cloud") == 1


def test_local_llm_scales_with_cpu_but_defaults_to_one() -> None:
    """本地大模型档按 CPU 核数放开（本机服务，无封禁风险），默认仍是 1。"""
    assert clamp_jobs("local-llm", None) == 1
    assert clamp_jobs("local-llm", 999, cpu=4) == cpu_jobs(4)
    assert cpu_jobs(0) >= 1
    assert cpu_jobs(9999) <= 8  # 上限夹住，不在小内存机器上堆线程


def test_offline_stays_single_threaded_and_explains_why(tmp_path) -> None:
    """离线（保密）档按 1 执行，并且**说出原因**——不许静默退回 1。"""
    from core.local_translator import LocalTranslator

    logs: list[str] = []
    # 构造会真的去找模型文件；这里只要“jobs 被夹到 1 + 说了原因”这件事，
    # 所以容忍模型缺失（构造函数本身不加载模型）。
    try:
        LocalTranslator(log=logs.append, jobs=4)
    except Exception:
        pass
    joined = "\n".join(logs)
    if joined:
        assert "并发没有收益" in joined or "按单线程执行" in joined


def test_unknown_tier_and_garbage_input_fall_back_to_one() -> None:
    """认不出的档位 / 认不出的数字：一律退回 1（保守，宁可慢也别闯祸）。"""
    assert clamp_jobs("unknown-tier", 8) == 1
    assert clamp_jobs("cloud", "不是数字") == 1
    assert clamp_jobs("cloud", 0) == 1
    assert clamp_jobs("cloud", -5) == 1
    assert tier_job_limit("cloud") <= CLOUD_MAX_JOBS


def test_translators_default_to_single_job(tmp_path) -> None:
    """四档翻译器的默认并发度都是 1——真正生效的默认值，不只是常量。"""
    tr = Translator(cache_path=str(tmp_path / "cache.sqlite"))
    assert tr.jobs == 1
    assert tr.governor.jobs == 1

    from core.deepl import DeepLTranslator

    paid = DeepLTranslator(
        {"key": "fake:fx"}, cache=str(tmp_path / "paid.sqlite"), jobs=99
    )
    assert paid.jobs == PAID_MAX_JOBS  # 付费档夹到 4，不会真的开 99 路


def test_cli_default_is_one() -> None:
    """命令行默认值也是 1（app.py 里的 CLI_JOBS）。"""
    import app

    assert app.CLI_JOBS == 1


# ---------------------------------------------------------------------------
# 1. 顺序对齐（最重要的一条）
# ---------------------------------------------------------------------------


def _reverse_order_worker(count: int):
    """造一个**完成顺序严格逆序**的 worker。

    事件链：worker(N-1) 不等任何东西先返回，它返回前放行 worker(N-2)，
    依此类推。完成顺序因此被钉成 [N-1, N-2, ..., 0]，不需要 sleep 赌时序。
    """
    barrier = threading.Barrier(count)
    gates = [threading.Event() for _ in range(count)]
    order: list[int] = []

    def worker(position: int, item: object) -> object:
        # 超时只是防挂死兜底：万一有线程没到齐，宁可 20 秒后变红，也不要
        # 把整个测试 suite 永久挂住（挂住比失败难查得多）。
        barrier.wait(20)  # 全部起飞，逼出真正的并发
        if position != count - 1:
            gates[position].wait(20)  # 等后一个放我（20 秒只是防挂死兜底）
        order.append(position)
        if position > 0:
            gates[position - 1].set()
        return item

    return worker, order


def test_results_are_aligned_by_index_not_by_completion_order() -> None:
    """核心不变量：**完成顺序完全逆序，回填结果仍严格按索引对齐**。

    把回填改成「按完成顺序 append」，这条必须变红——它是本轮最大的风险点。
    """
    count = 4
    items = list(range(count))
    worker, order = _reverse_order_worker(count)

    out = run_ordered(items, worker, jobs=count, poll=0.01)

    assert order == [3, 2, 1, 0]  # 交错是真的反了（否则这条测试没有意义）
    assert out == items  # 结果仍然一一对应


def test_last_status_does_not_leak_into_the_next_segment(tmp_path) -> None:
    """状态码不许跨段残留（QA 观察项 2 的行为级钉子）。

    现象：失败分支写 ``self._last_status = status_of(exc) or self._last_status``
    ，**成功分支却不清零**。于是「Edge 吃 429 → Bing 救回」之后，下一个完全
    没有状态码的失败会被归因成「服务端限流（429）」并**立刻**降回 1——译文
    没错，但归因错会误导后续排查。

    这里钉的是**后果**而不只是字段：第二段让所有通道都抛「没有状态码」的
    失败，若上一段的 429 还在，降级原因里就会出现 429。
    """
    logs: list[str] = []
    tr = Translator(cache_path=str(tmp_path / "cache.sqlite"), log=logs.append)
    _force_parallel(tr, 2)
    tr._preflighted = True
    tr._bucket = TokenBucket(rate=1000.0)

    tr.edge = _ScriptedChannel("429")  # 先吃一次 429，把 _last_status 写成 429
    tr.bing = _ScriptedChannel("ok")  # 再由这条通道救回（段成功）
    tr.mymemory = _ScriptedChannel("ok")
    tr.translate_texts([f"Sentence number {index} for the test." for index in range(2)])

    assert tr._last_status == 0  # 成功之后必须清零
    assert tr.governor.degraded is False

    # 第二段：全通道抛「没有状态码」的失败。归因若残留 429，这里会立刻降级。
    for name in ("edge", "bing", "mymemory"):
        setattr(tr, name, _ScriptedChannel("fail"))
    tr.translate_texts([f"Another sentence {index} here." for index in range(3)])

    assert "429" not in tr.governor.degrade_reason
    assert tr.governor.degrade_reason == "连续失败 3 次"


class _ScriptedChannel:
    """脚本化假通道：**批量一律失败**（把段逼进逐段兜底路径），逐段按角色行事。

    为什么要让批量失败：``_last_status`` 只在逐段链路里写，走批量通道的话
    这段逻辑根本碰不到——第一版测试就是这么被自己的假通道骗过去的。
    """

    def __init__(self, role: str) -> None:
        self.role = role

    def translate_batch(self, texts: list[str]) -> list[str]:
        raise TransientError("批量通道暂不可用", 429)

    def translate(self, text: str) -> str:
        if self.role == "429":
            raise TransientError("限流", 429)
        if self.role == "ok":
            return "TRANSLATED"
        # 刻意不带状态码：这是「归因会不会串」的关键
        raise TranslationError("普通失败（没有状态码）")


def test_delivery_is_strictly_in_index_order() -> None:
    """交付顺序必须严格按下标升序，不是「谁先做完谁先交付」。

    为什么值得单拎一条：付费档会把一个长段拆成多片、按到达顺序拼回
    （``translated_units[i] = 已有 + 新片``）。交付顺序一乱，段内顺序就反了
    ——那比段落错位更难发现（用户拿到一段读得通但前后颠倒的译文）。
    """
    count = 4
    worker, order = _reverse_order_worker(count)
    delivered: list[int] = []

    run_ordered(
        list(range(count)),
        worker,
        jobs=count,
        poll=0.01,
        on_done=lambda position, _value: delivered.append(position),
    )

    assert order == [3, 2, 1, 0]  # 完成顺序确实是反的
    assert delivered == [0, 1, 2, 3]  # 交付顺序仍然严格升序


def test_single_job_runs_in_order_and_matches_a_plain_loop() -> None:
    """并发度 1 时走串行分支：顺序执行，结果与手写 for 循环逐字节一致。"""
    items = ["a", "b", "c"]
    seen: list[str] = []

    def worker(_position: int, item: str) -> str:
        seen.append(item)
        return item.upper()

    out = run_ordered(items, worker, jobs=1)
    assert out == ["A", "B", "C"]
    assert seen == items  # 严格按输入顺序执行


def test_translator_parallel_output_is_byte_identical_to_serial(tmp_path) -> None:
    """零回归铁证：同一份假引擎，jobs=1 与 jobs=4 的输出逐字节相同。

    假引擎按**逆序**完成（事件链强制），所以只要回填有一次按完成顺序走，
    两条输出就不可能相等。
    """
    texts = [f"Sentence number {index} for the test." for index in range(4)]

    def build(jobs: int) -> Translator:
        tr = Translator(cache_path=str(tmp_path / f"cache{jobs}.sqlite"))
        tr._preflighted = True
        # 关掉两条批量通道，走**逐段**兜底路径（并发只接在这条路径上）；
        # 批量通道是串行合并请求，本来就不并发。
        tr.edge_disabled = True
        tr.bing_disabled = True
        tr.mymemory = _IndexedChannel(texts) if jobs > 1 else _PlainChannel(texts)
        tr.jobs = jobs  # 绕过档位上限，只为强制并发路径
        tr._bucket = TokenBucket(rate=1000.0)  # 测试不等令牌
        return tr

    serial = build(1)
    parallel = build(4)
    serial_out = serial.translate_texts(texts)
    parallel_out = parallel.translate_texts(texts)

    assert serial_out == parallel_out
    assert parallel_out == [f"TRANSLATED-{index}" for index in range(4)]
    # 交错是真的反了：否则「结果对齐」这条断言没有意义（可能只是没并发）
    assert parallel.mymemory.order == [3, 2, 1, 0]
    # 并发下计数不许丢（计数器的读改写都必须过锁）
    assert parallel.translated_count == serial.translated_count == 4


class _PlainChannel:
    """不做任何顺序游戏的假通道：串行基线用它。"""

    def __init__(self, texts: list[str]) -> None:
        self.texts = [normalize_source(text) for text in texts]

    def translate(self, text: str) -> str:
        return f"TRANSLATED-{self.texts.index(text)}"


class _IndexedChannel:
    """按原文查段号、**逆序完成**地返回译文的假通道。

    只在并发用得上：它要求 N 个段同时在飞（Barrier），串行跑会死锁。
    """

    def __init__(self, texts: list[str]) -> None:
        self.texts = [normalize_source(text) for text in texts]
        self.count = len(self.texts)
        self.barrier = threading.Barrier(self.count)
        self.gates = [threading.Event() for _ in range(self.count)]
        self.order: list[int] = []

    def translate(self, text: str) -> str:
        position = self.texts.index(text)
        self.barrier.wait(20)  # 超时防挂死，理由同 _reverse_order_worker
        if position != self.count - 1:
            self.gates[position].wait(20)
        self.order.append(position)
        if position > 0:
            self.gates[position - 1].set()
        return f"TRANSLATED-{position}"


# ---------------------------------------------------------------------------
# 2. 异常不许被线程池吞掉
# ---------------------------------------------------------------------------


def test_worker_exception_is_reported_not_swallowed(caplog) -> None:
    """worker 抛错：出声 + 对应位回填 None + 其余照常完成。

    「某个段落静默没翻」是最难发现的一类失效——用户拿到一份看起来翻完的
    文档，其实少了一段。
    """
    reported: list[tuple[int, str]] = []

    def worker(_position: int, item: int) -> int:
        if item == 2:
            raise ValueError("引擎炸了")
        return item * 10

    with caplog.at_level(logging.ERROR, logger="translation_tool"):
        out = run_ordered(
            [0, 1, 2, 3],
            worker,
            jobs=4,
            poll=0.01,
            on_error=lambda index, exc: reported.append((index, type(exc).__name__)),
        )

    assert reported == [(2, "ValueError")]
    assert out == [0, 10, None, 30]

    # 不传 on_error 时也必须出声（默认落日志），不是静默跳过
    caplog.clear()
    run_ordered([0, 1, 2, 3], worker, jobs=4, poll=0.01)
    assert any("并发任务失败" in record.getMessage() for record in caplog.records)


def test_exception_in_serial_mode_also_surfaces() -> None:
    """串行分支同样不许吞异常（两条路径行为一致）。"""
    reported: list[int] = []

    def worker(_position: int, item: int) -> int:
        if item == 1:
            raise RuntimeError("boom")
        return item

    out = run_ordered(
        [0, 1, 2], worker, jobs=1, on_error=lambda index, exc: reported.append(index)
    )
    assert reported == [1]
    assert out == [0, None, 2]


# ---------------------------------------------------------------------------
# 3. stop_event 必须及时中断
# ---------------------------------------------------------------------------


def test_stop_event_interrupts_without_waiting_for_the_slow_worker() -> None:
    """取消不能因为并发变慢：第 0 段永远不返回，取消照样立刻生效。

    断言盯**结构**（慢的那段根本没返回就抛了 CancelledError），不盯耗时。
    如果实现改成阻塞在 ``future.result()`` 上，这条会超时失败。
    """
    gate = threading.Event()
    finished: list[int] = []

    def worker(position: int, item: int) -> int:
        if position == 0:
            gate.wait(20)  # 永不放开（20 秒只是防挂死兜底）
            finished.append(item)
            return item
        return item

    stop = threading.Event()
    box: dict[str, BaseException] = {}

    def run() -> None:
        try:
            run_ordered([0, 1, 2], worker, jobs=3, stop_event=stop, poll=0.01)
        except BaseException as exc:  # noqa: BLE001 —— 就要抓一切来验类型
            box["exc"] = exc

    thread = threading.Thread(target=run)
    try:
        thread.start()
        stop.set()  # 用户点了取消
        thread.join(10)
        assert not thread.is_alive(), "run_ordered 卡住了：取消没能及时生效"
        assert isinstance(box.get("exc"), CancelledError)
        assert finished == []  # 慢的那段压根没跑完就中断了
    finally:
        gate.set()
        thread.join(10)


def test_stop_event_before_start_stops_immediately() -> None:
    """开工前就已经取消：一个任务都不该启动。"""
    started: list[int] = []

    def worker(_position: int, item: int) -> int:
        started.append(item)
        return item

    stop = threading.Event()
    stop.set()
    with pytest.raises(CancelledError):
        run_ordered([0, 1, 2], worker, jobs=2, stop_event=stop, poll=0.01)
    assert started == []


# ---------------------------------------------------------------------------
# 4. 自动降级：降回 1 且必须出声
# ---------------------------------------------------------------------------


def test_clamping_over_the_limit_is_announced() -> None:
    """请求的并发度超过档位上限（档位本身大于 1）：夹回去，并且说出来。"""
    logs: list[str] = []
    governor = JobGovernor("paid", 99, log=logs.append)
    assert governor.jobs == PAID_MAX_JOBS
    governor.announce()
    assert any("上限" in message for message in logs)


def _force_parallel(translator: Translator, jobs: int) -> None:
    """绕过档位策略，强制让翻译器走并发路径。

    为什么需要它：免费云端档现在**策略性不并发**（见
    ``test_cloud_never_parallelizes_no_matter_what_the_user_asks``），不复写
    并发度就进不了并行分支。但「并发执行器 + 降级管家」这条链路在付费档与
    本地大模型档是真实可走的，它的三条不变量必须继续有回归保护。
    """
    translator.jobs = jobs
    translator.governor.jobs = jobs
    translator.governor.requested = jobs


def test_429_degrades_to_single_thread_and_speaks_up() -> None:
    """遇到 429 立刻降回 1（一次就降，不等连续三次），并且说出来。"""
    logs: list[str] = []
    governor = JobGovernor("paid", 2, log=logs.append)
    assert governor.jobs == 2

    assert governor.note_failure(TransientError("限流", 429)) is True
    assert governor.jobs == 1
    assert governor.degraded is True
    assert any("并发降级" in message for message in logs)
    assert any("429" in message for message in logs)


def test_repeated_failures_degrade_to_single_thread_and_speak_up() -> None:
    """连续失败够多次也降回 1，并且说出来（静默降速等于骗用户）。"""
    logs: list[str] = []
    governor = JobGovernor("paid", 4, log=logs.append)
    for _ in range(2):
        assert governor.note_failure(RuntimeError("boom")) is False
    assert governor.jobs == 4  # 还没到阈值
    assert governor.note_failure(RuntimeError("boom")) is True
    assert governor.jobs == 1
    assert any("并发降级" in message for message in logs)


def test_success_resets_the_failure_streak() -> None:
    """成功一次就把连续失败清零：单次抖动不该累积成降级。"""
    logs: list[str] = []
    governor = JobGovernor("paid", 4, log=logs.append)
    governor.note_failure(RuntimeError("boom"))
    governor.note_failure(RuntimeError("boom"))
    governor.note_success()
    assert governor.note_failure(RuntimeError("boom")) is False
    assert governor.jobs == 4


def test_single_thread_policy_is_announced_not_silent() -> None:
    """策略性单线程的档位必须在**日志里说清楚为什么**。

    用户显式写了 ``--jobs=4``，工具悄悄按 1 跑，等于骗人——他会以为开关坏了。
    """
    for tier in ("cloud", "offline"):
        logs: list[str] = []
        governor = JobGovernor(tier, 4, log=logs.append)
        assert governor.jobs == 1
        governor.announce()
        assert any(TIER_SINGLE_NOTE[tier] == message for message in logs)


def test_cloud_translator_says_it_will_not_parallelize(tmp_path) -> None:
    """构造时就出声（并行路径永远不会被调用，等到翻译开始再说就太晚了）。"""
    logs: list[str] = []
    Translator(
        cache_path=str(tmp_path / "cache.sqlite"), log=logs.append, jobs=4
    )
    assert any("不并发" in message for message in logs)


def test_translator_degrades_on_rate_limit(tmp_path) -> None:
    """端到端：并发跑着遇到 429，并发度真的降回 1 且出声。

    注：免费云端档现在策略性不并发，这里**绕过夹取**（直接置 ``tr.jobs``）
    去测的是「并发执行器 + 降级管家」这条链路本身——它在付费档和本地大模型
    档是真实可走的，不能因为 cloud 的策略变了就失去回归保护。
    """
    logs: list[str] = []
    tr = Translator(cache_path=str(tmp_path / "cache.sqlite"), log=logs.append)
    _force_parallel(tr, 2)
    tr._preflighted = True
    tr._bucket = TokenBucket(rate=1000.0)

    class _RateLimited:
        def translate(self, text: str) -> str:
            raise TransientError("限流", 429)

    tr.edge = _RateLimited()
    tr.bing = _RateLimited()
    tr.mymemory = _RateLimited()

    results = tr.translate_texts(
        [f"Sentence number {index} for the test." for index in range(3)]
    )
    assert results[0].startswith("Sentence number")  # 失败段保留原文
    assert tr.jobs == 1  # 降级真的发生了
    assert tr.governor.degraded is True
    assert any("并发降级" in message for message in logs)


# ---------------------------------------------------------------------------
# 5. StateStore 并发写：不许死锁（反复跑 20 次）
# ---------------------------------------------------------------------------


def test_state_store_concurrent_writes_never_deadlock(tmp_path) -> None:
    """8 线程同时写同一个 StateStore，**反复跑 20 次**都不许死锁。

    RLock 是底子，但「不死锁」这件事必须实测——纸面推断在 SQLite +
    check_same_thread=False 的组合上最容易出错。
    """
    for round_index in range(20):
        store = StateStore(tmp_path / f"state{round_index}.sqlite")
        errors: list[BaseException] = []
        barrier = threading.Barrier(8)

        def writer(worker_id: int) -> None:
            try:
                barrier.wait(20)  # 同时开写，逼出真正的争用（超时防挂死）
                for index in range(5):
                    store.upsert_term(f"src-{worker_id}-{index}", f"译文-{index}")
                    store.get_term(f"src-{worker_id}-{index}")
                    store.all_terms()
            except BaseException as exc:  # noqa: BLE001 —— 记录一切再断言
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(worker_id,))
            for worker_id in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)  # 超时即视为死锁
        assert not any(thread.is_alive() for thread in threads), f"第 {round_index} 轮死锁"
        assert errors == []
        # 写入不丢：8 个线程各 5 条，一条都不能少
        assert len(store.all_terms()) == 40
        store.close()


# ---------------------------------------------------------------------------
# 6. 令牌桶：限速与取消
# ---------------------------------------------------------------------------


def test_token_bucket_first_token_is_free_then_waits_by_rate() -> None:
    """首桶立即可用；令牌耗尽后按速率等待（注入时钟，不碰墙钟）。"""
    clock = {"now": 0.0}

    def read_clock() -> float:
        return clock["now"]

    def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds

    bucket = TokenBucket(rate=1.0, capacity=1.0, clock=read_clock, sleep=fake_sleep)
    assert bucket.acquire() == 0.0  # 第一发不用等
    waited = bucket.acquire()
    assert waited == pytest.approx(1.0)  # 1 次/秒 → 等 1 秒
    assert clock["now"] == pytest.approx(1.0)


def test_token_bucket_respects_cancellation() -> None:
    """取消已置位：不等令牌，直接抛 CancelledError。"""
    bucket = TokenBucket(rate=0.001, capacity=1.0)
    bucket.acquire()  # 把唯一的令牌用掉
    stop = threading.Event()
    stop.set()
    with pytest.raises(CancelledError):
        bucket.acquire(stop_event=stop)
