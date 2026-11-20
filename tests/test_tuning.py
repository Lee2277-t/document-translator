"""请求尺寸与退避阈值可配置（v1.6.9，差距清单 C5+E2）。

钉住四条口径，它们决定了「用户写了开关」这件事到底有没有意义：

1. **只收紧不放宽**：各家接口有硬限（Edge 40 段 / 6000 字符 / 单段 4000，Bing
   10 段 / 820 / 900，MyMemory 单段 450，DeepL 50 段 / 单段 30000，离线 24 段 /
   6000 / 1200）。写大了夹回硬限并**出声**——静默采纳越限值等于替用户去撞限流。
2. **该出声的出声、该闭嘴的闭嘴**：没写这个开关、或写的值正好等于硬限，都不许
   播报。四个引擎 × 三个旋钮每次启动刷一遍常量表，日志就没人看了。
3. **吃了亏要说清是谁吃的**：付费档不吃字符口径（它按字节算）、离线档不吃退避
   秒数（它没有网络请求）、本地大模型与自托管档四个都不吃（一条一问、原文不能
   截断）。写了但没生效却一个字不说，就是骗人。
4. **夹完的值必须真的被用到**：只把值存进实例属性、切批代码还读模块常量，等于
   开关是个摆设。所以这里既测属性，也测 ``_groups`` / ``_make_batches`` 的实际刀口。

必须在进程内测的是「装配链」：CLI 解析 → ``EngineParams`` → 各档构造器，
断一条链路就出现「命令行写了、界面里没生效」这类 v1.6.3 之前的老毛病。

跑法： ./.venv\\Scripts\\python.exe -X utf8 -m pytest tests -q
"""
from __future__ import annotations

import dataclasses

import pytest

from core import engine_registry
from core.deepl import DEEPL_BATCH_ITEMS, DEEPL_RETRY_DELAYS, DEEPL_SEGMENT_CHARS, DeepLTranslator
from core.local_translator import BATCH_CHARS, BATCH_ITEMS, LocalTranslator, MAX_CHUNK_CHARS
from core.translator import (
    BING_BATCH_CHARS,
    BING_BATCH_ITEMS,
    BING_CHUNK,
    EDGE_BATCH_CHARS,
    EDGE_BATCH_ITEMS,
    EDGE_CHUNK,
    MYMEMORY_CHUNK,
    Translator,
)
from core.tuning import (
    MAX_DELAY_SECONDS,
    MAX_DELAY_STEPS,
    TIER_UNUSED_NOTE,
    TM_THRESHOLD_FLOOR,
    cap_int,
    parse_delays,
    parse_int,
    parse_tm_threshold,
    warn_unused,
)

FAKE_KEY = "123e4567-e89b-12d3-a456-426614174000:fx"


# ---------------------------------------------------------------------------
# 1. 整数开关的解析：认不出来要退回默认，并且说清为什么
# ---------------------------------------------------------------------------


def test_parse_int_takes_a_positive_integer_silently():
    """合法值一个都不该说——「已按 10 执行」这种话对写开关的人来说是噪音。"""
    logs: list[str] = []
    assert parse_int(" 10 ", flag="--batch-items=", label="段数",
                     example="--batch-items=10", log=logs.append) == 10
    assert logs == []


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "abc", "1.5", "10段", "0", "-3"],
)
def test_parse_int_refuses_every_unusable_shape(raw):
    """空值 / 非整数 / 小于 1 都退回默认（返回 None），但必须带上原因与正确写法。

    0 与负数尤其危险：批循环 ``while`` 的推进量变成 0 就是死循环，
    所以宁可当没写，也不能把它塞进引擎。
    """
    logs: list[str] = []
    got = parse_int(raw, flag="--batch-items=", label="段数",
                    example="--batch-items=10", log=logs.append)
    assert got is None
    assert len(logs) == 1
    assert "--batch-items=" in logs[0]
    assert "--batch-items=10" in logs[0]
    assert "已按默认执行" in logs[0]


# ---------------------------------------------------------------------------
# 2. 退避序列的解析：全角逗号、档数与秒数护栏
# ---------------------------------------------------------------------------


def test_parse_delays_reads_a_plain_sequence():
    logs: list[str] = []
    got = parse_delays("0.5,1,2", flag="--retry-delays=", log=logs.append)
    assert got == (0.5, 1.0, 2.0)
    assert logs == []


def test_parse_delays_accepts_fullwidth_comma():
    """中文输入法下打出来的逗号是全角「，」——只认半角等于让中文用户白写一次。"""
    assert parse_delays("0.5，1，2", flag="--retry-delays=", log=lambda _m: None) == (0.5, 1.0, 2.0)


@pytest.mark.parametrize("raw", ["", "0.5,1,2,3,4,5,6,7", "0.5,,1", "1;2"])
def test_parse_delays_rejects_bad_step_count(raw):
    """超过 6 档、空值、以及拆出空档（连续逗号 / 分号）都退回默认。

    护栏不是洁癖：``BackoffPolicy`` 的失败上限只有几档，多余的是死代码；
    而 ``float("")`` 会抛 ValueError，不拦就是把堆栈甩给用户。
    """
    logs: list[str] = []
    assert parse_delays(raw, flag="--retry-delays=", log=logs.append) is None
    assert len(logs) == 1 and "已按默认执行" in logs[0]


@pytest.mark.parametrize("raw", ["0.5,abc", "-1", f"0.5,{MAX_DELAY_SECONDS + 1}"])
def test_parse_delays_rejects_out_of_range_seconds(raw):
    logs: list[str] = []
    assert parse_delays(raw, flag="--retry-delays=", log=logs.append) is None
    assert "已按默认执行" in logs[0]


def test_parse_delays_allows_zero_and_the_boundary_step_count():
    """0 秒合法（立刻重试是有人要的），恰好 6 档与恰好 60 秒也合法。"""
    assert parse_delays("0", flag="--retry-delays=", log=lambda _m: None) == (0.0,)
    assert len(parse_delays(",".join(["1"] * MAX_DELAY_STEPS),
                            flag="--retry-delays=", log=lambda _m: None)) == MAX_DELAY_STEPS
    assert parse_delays(f"{MAX_DELAY_SECONDS:g}", flag="--retry-delays=",
                        log=lambda _m: None) == (MAX_DELAY_SECONDS,)


# ---------------------------------------------------------------------------
# 3. 夹取：只收紧不放宽，而且只在有必要时出声
# ---------------------------------------------------------------------------


def test_cap_int_is_silent_when_the_flag_was_not_written():
    """没写开关 → 返回硬限，且一个字不说。四个引擎 × 三个旋钮每次都播报就是刷屏。"""
    logs: list[str] = []
    assert cap_int(None, label="段数", engine="Edge", default=40, log=logs.append) == 40
    assert logs == []


def test_cap_int_is_silent_when_the_value_equals_the_hard_limit():
    """写 40、上限也是 40：行为与不写逐字相同，说一句「你写了但……」反而是误导。"""
    logs: list[str] = []
    assert cap_int(40, label="段数", engine="Edge", default=40, log=logs.append) == 40
    assert logs == []


def test_cap_int_clamps_upward_and_says_so():
    """写大了夹回硬限，并且必须把「你写了几个 / 上限是几个 / 现在按几个」说全。"""
    logs: list[str] = []
    assert cap_int(9999, label="段数", engine="Edge", default=40, log=logs.append) == 40
    assert len(logs) == 1
    assert "9999" in logs[0] and "40" in logs[0] and "硬限" in logs[0]


def test_cap_int_tightens_downward_and_says_so():
    logs: list[str] = []
    assert cap_int(4, label="段数", engine="Edge", default=40, log=logs.append) == 4
    assert logs == ["Edge：段数按你的设置从 40 收紧到 4。"]


def test_no_message_leaks_a_placeholder():
    """任何一句出声都不许带 ``{``——夹取的说明是拼出来的多行 f-string，
    少写一个 ``f`` 前缀就会把 ``{default}`` 原样打给用户，而它只出现在
    「写大了」这一条分支里，平时不写越界值根本看不见。
    """
    logs: list[str] = []
    parse_int("", flag="--f=", label="段数", example="--f=10", log=logs.append)
    parse_int("x", flag="--f=", label="段数", example="--f=10", log=logs.append)
    parse_int("0", flag="--f=", label="段数", example="--f=10", log=logs.append)
    parse_delays("", flag="--f=", log=logs.append)
    parse_delays("1,2,3,4,5,6,7", flag="--f=", log=logs.append)
    parse_delays("abc", flag="--f=", log=logs.append)
    parse_delays("900", flag="--f=", log=logs.append)
    cap_int(99, label="段数", engine="Edge", default=40, log=logs.append)
    cap_int(4, label="段数", engine="Edge", default=40, log=logs.append)
    for tier in TIER_UNUSED_NOTE:
        warn_unused(tier, ["--f="], logs.append)
    assert len(logs) == 3 + 4 + 2 + len(TIER_UNUSED_NOTE)  # 每条分支都要真的说过话
    leaked = [line for line in logs if "{" in line or "}" in line]
    assert leaked == []


# ---------------------------------------------------------------------------
# 4. 「本档吃不下」的点名
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["paid", "offline", "local-llm", "custom"])
def test_warn_unused_names_the_written_flags(tier):
    """写了却没生效 → 必须点名是哪几个开关，并给出这一档的具体原因。"""
    logs: list[str] = []
    warn_unused(tier, ["--batch-chars=", "--retry-delays="], logs.append)
    assert len(logs) == 1
    assert logs[0].startswith("--batch-chars=、--retry-delays= 这一档用不上：")
    assert logs[0] != TIER_UNUSED_NOTE[tier]  # 原因前必须挂上开关名


def test_warn_unused_says_nothing_when_nothing_was_written():
    """没写开关就一句不出——这与 cap_int(None) 的口径逐字一致。"""
    logs: list[str] = []
    warn_unused("paid", [], logs.append)
    warn_unused("no-such-tier", ["--batch-chars="], logs.append)
    assert logs == []


# ---------------------------------------------------------------------------
# 5. 免费云端档：三个引擎各夹各的硬限
# ---------------------------------------------------------------------------


def test_cloud_defaults_are_untouched_and_silent():
    """不写旋钮时逐字节等价于改动前：三个引擎停在各自硬限，日志里没有阈值字样。"""
    logs: list[str] = []
    tr = Translator(log=logs.append)
    assert (tr.edge.batch_items, tr.edge.batch_chars, tr.edge.chunk_chars) == (
        EDGE_BATCH_ITEMS, EDGE_BATCH_CHARS, EDGE_CHUNK)
    assert (tr.bing.batch_items, tr.bing.batch_chars, tr.bing.chunk_chars) == (
        BING_BATCH_ITEMS, BING_BATCH_CHARS, BING_CHUNK)
    assert tr.mymemory.chunk_chars == MYMEMORY_CHUNK
    assert [line for line in logs if "收紧" in line or "硬限" in line] == []


def test_cloud_tightens_all_three_channels_and_snapshots_the_limits():
    """收紧值要落到每个引擎，还要落到 ``_edge_limits`` / ``_bing_limits`` 快照。

    快照不是多余字段：``translate_texts`` 读它而不是读引擎实例，因为测试替身与
    A2 注册表挂进来的第三方引擎只保证有 translate / translate_batch。

    这组值故意挑「同一个数在不同通道上是三种命运」：1000 字符对 Edge（上限 6000）
    是收紧、对微软（上限 820）是越界；500 字符对备用通道（单段上限 450）也是越界。
    各夹各的硬限才是对的，用一条「统一取最小值」糊过去会把微软通道的吞吐白砍一半。
    """
    logs: list[str] = []
    tr = Translator(log=logs.append, batch_items=10, batch_chars=1000, chunk_chars=500)
    assert (tr.edge.batch_items, tr.edge.batch_chars, tr.edge.chunk_chars) == (10, 1000, 500)
    assert (tr.bing.batch_items, tr.bing.batch_chars, tr.bing.chunk_chars) == (10, 820, 500)
    assert tr.mymemory.chunk_chars == MYMEMORY_CHUNK
    assert tr._edge_limits == (10, 1000, 0)
    assert tr._bing_limits == (10, 820, 28)
    assert "Edge：单次请求段数按你的设置从 40 收紧到 10。" in logs
    assert "微软：单次请求字符数你写了 1000，但这一档的上限就是 820（硬限，写大了也不生效），已按 820 执行。" in logs


def test_cloud_clamps_each_engine_to_its_own_hard_limit():
    """同一个越界值在三条通道上夹出三个不同结果——各家的限流口径本来就不一样。

    MyMemory 是「每次一问」的 GET 接口，只有单段长度这一个维度，所以给它写
    批量开关不该被采纳，也不该被塞进一个没人读的属性里骗后来人。
    """
    logs: list[str] = []
    tr = Translator(log=logs.append, batch_items=9999, batch_chars=9999, chunk_chars=5000)
    assert tr.edge.batch_items == EDGE_BATCH_ITEMS
    assert tr.bing.batch_items == BING_BATCH_ITEMS
    assert tr.edge.batch_chars == EDGE_BATCH_CHARS
    assert tr.bing.batch_chars == BING_BATCH_CHARS
    assert tr.edge.chunk_chars == EDGE_CHUNK
    assert tr.bing.chunk_chars == BING_CHUNK
    assert tr.mymemory.chunk_chars == MYMEMORY_CHUNK  # 5000 > 450，夹回备用档硬限
    assert not hasattr(tr.mymemory, "batch_items")
    assert not hasattr(tr.mymemory, "batch_chars")
    notes = [line for line in logs if "硬限" in line]
    assert len(notes) == 7  # Edge 3 + 微软 3 + 备用 1


def test_cloud_retry_delays_reach_all_three_channels():
    """退避序列三条通道共用，并且备用档的 ``_policy()`` 必须跟着变。

    ``max_delay=max(delays)`` 是这里真正的钉子：``BackoffPolicy`` 会把每一档夹到
    ``max_delay``（默认 8 秒、备用档 1 秒），不抬高就会出现「用户写 20 秒、
    实际等 1 秒」的静默失效。
    """
    logs: list[str] = []
    tr = Translator(log=logs.append, retry_delays=(0.5, 20))
    assert tr.edge.retry_delays == tr.bing.retry_delays == (0.5, 20)
    assert tr.mymemory.retry_delays == (0.5, 20)
    policy = tr.mymemory._policy()
    assert policy.delays == (0.5, 20)
    assert policy.max_attempts == 3
    assert policy.delay_for(1) == 0.5
    assert policy.delay_for(2) == 20  # 夹在 max_delay 之下才会露出用户写的 20 秒
    assert any("最多重试 2 次" in line for line in logs)


def test_cloud_mymemory_keeps_its_built_in_policy_without_the_flag():
    """没写 --retry-delays= 时备用档用内置策略，不因为新加的方法而改写退避。"""
    tr = Translator(log=lambda _m: None)
    assert tr.mymemory._policy() is tr.mymemory.POLICY


def test_edge_groups_split_at_the_tightened_size():
    """夹完的值必须真的决定刀口：只存属性、切批仍读模块常量，开关就是摆设。"""
    tr = Translator(log=lambda _m: None, batch_items=4, batch_chars=999999)
    groups = tr.edge._groups([f"第{i}段" for i in range(10)])
    assert [len(g) for g in groups] == [4, 4, 2]


# ---------------------------------------------------------------------------
# 6. 付费档：接段数与单段，拒字符口径
# ---------------------------------------------------------------------------


def test_paid_knobs_reach_the_deepl_engine():
    logs: list[str] = []
    tr = DeepLTranslator({"key": FAKE_KEY}, log=logs.append,
                         batch_items=10, chunk_chars=500, retry_delays=(1, 2, 3, 4))
    assert tr.batch_items == 10
    assert tr.chunk_chars == 500
    assert tr._rate_delays == (1, 2, 3, 4)
    assert any("DeepL：单次请求段数按你的设置从 50 收紧到 10。" == line for line in logs)


def test_paid_defaults_stay_at_the_documented_limits():
    """不写旋钮：50 段 / 30000 字符 / 0.5-1-2 秒退避，与使用说明里的表一致。"""
    logs: list[str] = []
    tr = DeepLTranslator({"key": FAKE_KEY}, log=logs.append)
    assert tr.batch_items == DEEPL_BATCH_ITEMS
    assert tr.chunk_chars == DEEPL_SEGMENT_CHARS
    assert tr._rate_delays == DEEPL_RETRY_DELAYS
    assert "用不上" not in "".join(logs)


def test_paid_refuses_the_character_budget_flag():
    """DeepL 的上限是 100KB **字节**（一个汉字占 3 字节），字符口径夹不动它。

    算了就必然算错，而错的结果是整批 400——所以这里明确「对本档无效」，
    而不是装作采纳。
    """
    logs: list[str] = []
    DeepLTranslator({"key": FAKE_KEY}, log=logs.append, batch_chars=2000)
    notes = [line for line in logs if "用不上" in line]
    assert len(notes) == 1
    assert notes[0].startswith("--batch-chars= 这一档用不上：")
    assert "字节" in notes[0]


# ---------------------------------------------------------------------------
# 7. 离线档：接请求尺寸，拒退避秒数
# ---------------------------------------------------------------------------


def test_offline_knobs_drive_the_actual_batch_cuts():
    """一次送进模型的段数直接决定内存峰值：写小必须真的变小（小内存机器是真需求）。"""
    logs: list[str] = []
    tr = LocalTranslator(log=logs.append, batch_items=3, batch_chars=1000, chunk_chars=200)
    assert (tr.batch_items, tr.batch_chars, tr.chunk_chars) == (3, 1000, 200)
    batches = tr._make_batches(list(range(8)), ["短"] * 8)
    assert [len(b) for b in batches] == [3, 3, 2]


def test_offline_clamps_above_the_measured_model_limits():
    logs: list[str] = []
    tr = LocalTranslator(log=logs.append, batch_items=9999, batch_chars=999_999, chunk_chars=9999)
    assert (tr.batch_items, tr.batch_chars, tr.chunk_chars) == (
        BATCH_ITEMS, BATCH_CHARS, MAX_CHUNK_CHARS)
    assert len([line for line in logs if "硬限" in line]) == 3


def test_offline_refuses_retry_delays():
    """离线档跑的是本机批推理，没有网络请求就没有退避可退——但要说出来。"""
    logs: list[str] = []
    LocalTranslator(log=logs.append, retry_delays=(1, 2))
    notes = [line for line in logs if "用不上" in line]
    assert len(notes) == 1
    assert notes[0].startswith("--retry-delays= 这一档用不上：")
    assert "本机模型" in notes[0]


def test_offline_half_instance_still_batches_at_class_limits():
    """靠 ``__new__`` 拼装的测试替身不该在 ``_make_batches`` 里撞 AttributeError。

    这里刻意**不**给实例补属性：刀口必须由类属性兜住（24/6000 是本机模型的实测
    上限），否则每个用 ``__new__`` 拼离线替身的测试都要跟着改一遍。
    """
    tr = LocalTranslator.__new__(LocalTranslator)
    assert [len(b) for b in tr._make_batches(list(range(30)), ["短"] * 30)] == [24, 6]


# ---------------------------------------------------------------------------
# 8. 装配链：CLI 全局 → EngineParams → 各档构造器
# ---------------------------------------------------------------------------


def test_engine_params_knobs_default_to_none():
    """默认 None = 用户没写。默认成 0 或 1 会让每次启动都触发夹取与播报。"""
    params = engine_registry.EngineParams(log=lambda _m: None)
    assert all(getattr(params, name) is None for name in engine_registry.ALL_KNOBS)


def test_knobs_only_names_the_written_flags():
    params = engine_registry.EngineParams(log=lambda _m: None, batch_items=10, retry_delays=(1,))
    assert engine_registry._knobs(params, *engine_registry.ALL_KNOBS) == [
        "--batch-items=", "--retry-delays=",
    ]


def test_tuning_spreads_exactly_the_four_keys():
    """四个旋钮摊成构造参数：加一档只改这一档的 builder，不在这里替它决定谁能吃哪些。"""
    params = engine_registry.EngineParams(log=lambda _m: None, chunk_chars=500)
    assert engine_registry._tuning(params) == {
        "batch_items": None, "batch_chars": None, "chunk_chars": 500, "retry_delays": None,
    }


def test_registry_forwards_knobs_to_cloud_and_offline_builders():
    """两条联网/离线链都要真的收到值；断一条就是「命令行写了、界面里没生效」。"""
    logs: list[str] = []
    params = engine_registry.EngineParams(
        log=logs.append, batch_items=10, batch_chars=1000, chunk_chars=500
    )
    cloud = engine_registry._build_cloud(params)
    assert (cloud.edge.batch_items, cloud.edge.batch_chars, cloud.edge.chunk_chars) == (
        10, 1000, 500)
    offline = engine_registry._build_offline(params)
    assert (offline.batch_items, offline.batch_chars, offline.chunk_chars) == (10, 1000, 500)


def test_one_shot_tiers_explain_all_four_flags():
    """本地大模型与自托管是一条一问：四个旋钮对本档无效，但都要点名，且说明回退段仍生效。

    这一条钉的是 ``_knobs`` 与 ``warn_unused`` 的组合：builder 里传的是
    ``*ALL_KNOBS``，漏一个名字就是用户写了那个开关却听不到任何解释。
    """
    for tier in (engine_registry.LOCAL_LLM, engine_registry.CUSTOM):
        params = engine_registry.EngineParams(
            log=lambda _m: None,
            batch_items=10, batch_chars=1000, chunk_chars=500, retry_delays=(1, 2),
        )
        logs: list[str] = []
        warn_unused(tier, engine_registry._knobs(params, *engine_registry.ALL_KNOBS), logs.append)
        assert len(logs) == 1
        for flag in ("--batch-items=", "--batch-chars=", "--chunk-chars=", "--retry-delays="):
            assert flag in logs[0]
        assert "回退" in logs[0]


def test_cli_flags_and_global_names_line_up():
    """CLI 侧的契约：四个开关进了可识别清单，``_cli_tuning()`` 的键与注册表同名。

    ``_CLI_FLAGS`` 同时是报错时打给用户的清单；``_cli_tuning()`` 的键直接摊进
    ``EngineParams``，名字对不上就是 ``TypeError`` 起不了界面。
    """
    import app

    for flag in ("--batch-items=", "--batch-chars=", "--chunk-chars=", "--retry-delays="):
        assert flag in app._CLI_FLAGS
    assert set(app._cli_tuning()) == set(engine_registry.ALL_KNOBS)
    assert set(app._cli_tuning()) <= {
        field.name for field in dataclasses.fields(engine_registry.EngineParams)
    }


# ---------------------------------------------------------------------------
# 5. 翻译记忆门槛（v1.9.10）：只许往上收，写低了夹回并说清为什么
# ---------------------------------------------------------------------------


def test_parse_tm_threshold_accepts_values_up_to_exact_only():
    logs: list[str] = []
    assert parse_tm_threshold("0.95", flag="--tm-threshold=", log=logs.append) == 0.95
    assert parse_tm_threshold("1", flag="--tm-threshold=", log=logs.append) == 1.0
    assert logs == [], "合法值一个都不该说——写开关的人不需要被念一遍"


def test_parse_tm_threshold_clamps_below_the_floor():
    """调低门槛等于让更不像的旧译文顶替新内容——宁可只满足一半的意图。"""
    logs: list[str] = []
    got = parse_tm_threshold("0.8", flag="--tm-threshold=", log=logs.append)
    assert got == TM_THRESHOLD_FLOOR
    assert len(logs) == 1 and "不能低于 0.92" in logs[0]
    assert "1.0" in logs[0], "要告诉用户想更保守该往哪调"


@pytest.mark.parametrize("raw", ["", "abc", "0", "-0.5", "1.5"])
def test_parse_tm_threshold_refuses_unusable_values(raw):
    logs: list[str] = []
    assert parse_tm_threshold(raw, flag="--tm-threshold=", log=logs.append) is None
    assert logs, "退回默认必须出声，否则用户以为设置生效了"
