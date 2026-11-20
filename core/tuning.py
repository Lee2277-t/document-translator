"""批量、分段与记忆门槛的用户覆盖（v1.6.9，差距清单 C5+E2 的「阈值可配置」形态；
v1.9.10 把翻译记忆的近似复用门槛也收进来）。

口径三条，全部照着 ``--jobs`` 那条既有先例定：

- **只能收紧，不能放宽**。各家接口有硬限（Edge 一次 40 段 / 6000 字符、单段 4000 字符；
  Bing 10 段 / 820 字符、单段 900；MyMemory 单段 450；DeepL 50 段 / 100KB；离线 24 段 /
  6000 字符、单段 1200）。用户写大了按该引擎的硬限夹住并**出声**——静默采纳越限值等于
  替用户去撞限流，而直接报「参数非法」又拦住了真有需要的收紧。
- **只在命令行调，界面不加面板**。会调请求尺寸的人在写批处理；桌面双击用户不需要界面上
  多一个输入框。与 ``--jobs`` 一样，界面沿用命令行传进来的值（没传就是默认）。
- **认不出的写法退回默认并说清原因**（与 ``app.py`` 的 ``_parse_jobs_argument`` 同口径）。
  阈值写错只影响快慢，不影响保密与译文正确性，为它弹错误反而挡住真正要做的翻译。

本模块只做「解析 + 夹取 + 出声」，不保存状态：引擎构造时调一次，把结果存成实例属性。
吃不下某个旋钮的档位（离线档的退避、本地大模型与自托管档的全部四个、付费档的
字符口径）由 :func:`warn_unused` 点名说明，一条口径写在 ``TIER_UNUSED_NOTE`` 里。

记忆门槛（``--tm-threshold=``）只走前两条：它不在各引擎的硬限表里，而是挂在
``TranslationMemory`` 上（一条记忆被四档共用，门槛也必须只有一处）。
"""

from __future__ import annotations

from typing import Callable

#: 出声口：默认吞掉，引擎构造时传 self.log，命令行直接传 print。
Silence = Callable[[str], None]

#: 退避序列允许的档数与单档上限（秒）。超过 6 档等于一次请求等到天荒地老，
#: 单档超过 60 秒界面会被当成卡死——两条都是刻意留的护栏，不是经验值。
MAX_DELAY_STEPS = 6
MAX_DELAY_SECONDS = 60.0

#: 翻译记忆「近似复用」门槛的上下界：默认 0.92 是 v1.4 定下的
#: （中文换一个字常等于换意思，复用比不翻更危险），**只许往上调**。
#: 1.0 表示只认完全一致；低于默认值一律夹回默认值并出声。
TM_THRESHOLD_FLOOR = 0.92
TM_THRESHOLD_CEIL = 1.0


def parse_int(
    raw: str,
    *,
    flag: str,
    label: str,
    example: str,
    log: Silence,
) -> int | None:
    """解析 ``--batch-items=10`` 这类整数开关的值。

    返回 None 表示「没听懂，按各引擎默认走」，同时已经把原因说出来了；
    绝不返回 0 或负数——那会让批处理循环要么一段一发要么直接死循环。
    """
    text = (raw or "").strip()
    if not text:
        log(f"{flag} 后面没写值：{label}要一个正整数，正确写法如 {example}，已按默认执行。")
        return None
    try:
        value = int(text)
    except ValueError:
        log(f"{flag} 要一个整数（收到「{raw}」）：{label}正确写法如 {example}，已按默认执行。")
        return None
    if value < 1:
        log(f"{flag} 不能小于 1（收到 {value}）：{label}正确写法如 {example}，已按默认执行。")
        return None
    return value


def parse_delays(
    raw: str,
    *,
    flag: str,
    log: Silence,
) -> tuple[float, ...] | None:
    """解析 ``--retry-delays=0.5,1,2``：429 之后的逐次等待秒数。

    分隔符除半角逗号外也认全角「，」——中文输入法下打出来的就是它。
    """
    text = (raw or "").strip()
    if not text:
        log(f"{flag} 后面没写值：正确写法如 --retry-delays=0.5,1,2，已按默认执行。")
        return None
    steps = [part.strip() for part in text.replace("，", ",").split(",")]
    if not 1 <= len(steps) <= MAX_DELAY_STEPS:
        log(
            f"{flag} 最多 {MAX_DELAY_STEPS} 档（收到 {len(steps)} 档）："
            "重试次数由失败上限决定，多余几档也不会被用到，已按默认执行。"
        )
        return None
    delays: list[float] = []
    for part in steps:
        try:
            value = float(part)
        except ValueError:
            log(f"{flag} 每一档都要是秒数（「{part}」不是），正确写法如 0.5,1,2，已按默认执行。")
            return None
        if value < 0 or value > MAX_DELAY_SECONDS:
            log(
                f"{flag} 每一档得在 0 到 {MAX_DELAY_SECONDS:g} 秒之间（收到 {part}），"
                "再长就等于让界面看起来卡死，已按默认执行。"
            )
            return None
        delays.append(value)
    return tuple(delays)


def parse_tm_threshold(raw: str, *, flag: str, log: Silence) -> float | None:
    """解析 ``--tm-threshold=0.95``：翻译记忆「近似复用」的相似度门槛。

    返回 None 表示「没听懂，按默认 0.92 走」。低于下限一律**夹回**下限而不是
    拒绝：用户想放宽是有理由的（比如只想复用几乎一样的长句），但这条比其余
    旋钮更危险——近似句之间的差异往往正是关键信息，错了不是慢一点，是译文
    悄悄走样，所以宁可只满足一半的意图也要说清为什么。
    """
    text = (raw or "").strip()
    if not text:
        log(
            f"{flag} 后面没写值：要一个 {TM_THRESHOLD_FLOOR}~{TM_THRESHOLD_CEIL:.1f} 之间的小数，"
            "正确写法如 --tm-threshold=0.95，已按默认 0.92 执行。"
        )
        return None
    try:
        value = float(text)
    except ValueError:
        log(
            f"{flag} 要一个数字（收到「{raw}」）：正确写法如 --tm-threshold=0.95，"
            "已按默认 0.92 执行。"
        )
        return None
    if not 0.0 < value <= TM_THRESHOLD_CEIL:
        log(
            f"{flag} 要在 0 到 1 之间（收到 {text}）：1.0 表示只认完全一致，"
            "已按默认 0.92 执行。"
        )
        return None
    if value < TM_THRESHOLD_FLOOR:
        log(
            f"{flag} 不能低于 {TM_THRESHOLD_FLOOR}（收到 {text}）：再低的相似度可能把意思不同的"
            f"旧译文拿来复用，比不翻更危险，已按 {TM_THRESHOLD_FLOOR} 执行。"
            "想更保守请往 1.0 调。"
        )
        return TM_THRESHOLD_FLOOR
    return value


def cap_int(
    requested: int | None,
    *,
    label: str,
    engine: str,
    default: int,
    log: Silence,
) -> int:
    """把用户给的阈值夹到该引擎的硬限之内，并出声说明最后按几走。

    ``requested`` 为 None（没写这个开关）时**不出声**，直接返回硬限默认值——
    四个引擎 × 三个旋钮每次启动都播报一遍，等于把日志刷成常量表。
    写得正好等于硬限时同样不出声：行为与没写一模一样，说一句只是刷屏。
    """
    if requested is None:
        return default
    if requested > default:
        # 不写「接口硬限」：离线档的上限是本机模型的实测内存/长度限制，不是接口给的，
        # 说成接口会让人以为断网也能放宽——那正好是把用户送去撞他不该有的上限。
        log(
            f"{engine}：{label}你写了 {requested}，但这一档的上限就是 {default}"
            f"（硬限，写大了也不生效），已按 {default} 执行。"
        )
        return default
    if requested == default:
        # 写的值正好等于硬限：行为与不写这个开关逐字相同，说一句只会刷屏。
        return default
    log(f"{engine}：{label}按你的设置从 {default} 收紧到 {requested}。")
    return requested


#: 档位 → 「这一档吃不下某些旋钮」的一句话原因。立条口径参照
#: ``core/concurrency.py`` 的 ``TIER_SINGLE_NOTE``：用户显式写了开关、工具却悄悄
#: 按默认跑，等于骗人——必须点名是哪几个开关、为什么说不上。
#: 只有「写了」的开关才会被点名，没写就一句不出。
TIER_UNUSED_NOTE: dict[str, str] = {
    "paid": (
        "DeepL 档的单请求上限按**字节**算（100KB，UTF-8 下一个汉字占 3 字节），"
        "字符口径夹不动它；本档的请求大小请用 --batch-items= 与 --chunk-chars= 调。"
    ),
    "offline": (
        "离线档跑的是本机模型批推理，没有网络请求也就没有退避重试可退，"
        "等待秒数对本档无效（请求尺寸三个开关本档是生效的）。"
    ),
    "local-llm": (
        "本地大模型档是一条一问——服务只提供 /v1/chat/completions，没有批量端点，"
        "提示词里的原文也不能截断（截了就是漏译），也没有退避重试可退，所以这四个"
        "开关对本档的模型请求无效；Ollama 不可用时回退本机离线小模型，那一段"
        "会按你写的值走。要控制快慢请用 --jobs=（本档按 CPU 核数放开）。"
    ),
    "custom": (
        "自托管档是一条一问——OpenAI 兼容服务只有 /v1/chat/completions，没有批量端点，"
        "提示词里的原文也不能截断（截了就是漏译），也没有退避重试可退，所以这四个开关"
        "对本档的服务请求无效；那台服务连不上时回退免费云端或离线小模型，那一段会按"
        "你写的值走。"
    ),
}


def warn_unused(tier: str, flags: list[str], log: Silence) -> None:
    """把「用户写了、但本档吃不下」的开关逐个点名并说明原因。"""
    note = TIER_UNUSED_NOTE.get(tier)
    if note is None or not flags:
        return
    log("、".join(flags) + " 这一档用不上：" + note)

