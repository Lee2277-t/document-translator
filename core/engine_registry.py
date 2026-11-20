"""引擎档位登记表：界面单选、命令行开关、翻译器构造共用的唯一一份真相。

v1.6.2 之前，「有哪几档、哪档能显示、选了它该构造哪个类」这件事散在三处：
界面 ``_on_engine_change`` 的 if 链、``_make_translator`` 的 if 链、CLI
``_build_cli_translator`` 的 if 链。加一档要同时改三处，漏一处的表现是
「界面上能选、实际用的还是上一档」——最难查的那种。对标项目里
PDFMathTranslate-next 用 ``@register_service`` 装饰器 + 服务基类声明参数、
OmegaT 用 ``TranslationEngine`` 接口 + 插件目录发现、Argos Translate 用
``TRANSLATORS`` 列表 + ``get_translators()`` 探测可用性（设计思路参考，
代码一行没抄：前三者是 GPL/AGPL，只有 Argos 是 MIT）。本项目的形态更简单：
档位是固定四档 + 保密档，不需要用户装插件，所以登记 = 一张表 + 一个构造入口。

对外的四个承诺：

1. **档位清单只在这里**。加一档只改 ``SPECS``，界面单选、CLI 校验、
   ``--help`` 文案、模式说明文字都从表里长出来。
2. **保密模式的口径只在这里**。「哪一档在 ``--offline`` 下存在」是合规问题，
   不能散在界面渲染代码里——``visible_keys(offline=True)`` 只有保密档，
   界面与 CLI 都问它。
3. **互斥组合在这里判**。``--paid`` 与 ``--local-llm``、``--custom`` 与
   ``--paid`` 这类同时给出，以前是「后者生效、不报错」（交接稿记录的已知小
   缺口），现在统一抛人话错误，谁赢不再取决于解析顺序。
4. **构造只走 ``build()``**。类在函数体内 import，测试可以照旧 patch
   ``core.translator.Translator`` / ``core.local_llm.LocalLLMTranslator``
   来断言「选了对应的构造器」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from .common import TranslationError
from .tuning import warn_unused

#: 五个档位的 key（``offline`` 是保密模式专用，不在界面单选里出现）。
CLOUD = "cloud"
LOCAL_LLM = "local-llm"
PAID = "paid"
CUSTOM = "custom"
OFFLINE = "offline"


@dataclass(frozen=True)
class EngineSpec:
    """一档引擎的元数据 + 构造入口。"""

    key: str
    #: 界面单选上的短标签（全角字体下要排进同一行）。
    radio_label: str
    #: 日志里念出来的全名。
    full_label: str
    #: 切到这一档时打给用户的一段说明（含数据去向）。
    hint: str
    #: 主窗口右下角的模式文字。
    footer: str
    #: CLI 自检/转换日志里的「模式」文字。
    mode_text: str
    #: 这一档要不要显示「质量引擎」开关（只有吃上下文的档位有意义）。
    shows_quality: bool
    #: ``api_keys.local.json`` 里的 provider 名；空串表示不需要 key 与涉密确认。
    provider: str
    #: 保密模式（``--offline``）下这一档是否还存在。联网档一律 False。
    offline_visible: bool
    #: 构造翻译器：吃 :class:`EngineParams`，返回带 ``translate_texts`` 的对象。
    factory: Callable[["EngineParams"], object]


@dataclass
class EngineParams:
    """构造一档引擎需要的公共参数。字段与 ``_make_translator`` 原来的局部变量一一对应。"""

    log: Callable[[str], None]
    style: str = "standard"
    #: 缓存库路径；None = 不写缓存（保密模式或用户关了缓存）。
    cache_path: str | None = None
    tm: object = None
    jobs: int = 1
    quality: bool = True
    stop_event: object = None
    #: 本地大模型/自托管服务的模型名；None = 用推荐默认或从服务抓。
    model: str | None = None
    #: 自托管档：命令行 ``--custom-url=`` 临时覆盖配置文件里的地址。
    base_url: str | None = None
    #: 请求尺寸与退避旋钮（v1.6.9，``--batch-items=`` 等）。None = 用户没写，
    #: 各引擎按自己的硬限走；写了但本档吃不下的，由 ``warn_unused`` 点名说明。
    batch_items: int | None = None
    batch_chars: int | None = None
    chunk_chars: int | None = None
    retry_delays: tuple[float, ...] | None = None


#: 旋钮字段名 → 命令行开关名，用于「本档用不上」时把用户写的那几个点出来。
KNOB_FLAGS = {
    "batch_items": "--batch-items=",
    "batch_chars": "--batch-chars=",
    "chunk_chars": "--chunk-chars=",
    "retry_delays": "--retry-delays=",
}

ALL_KNOBS = ("batch_items", "batch_chars", "chunk_chars", "retry_delays")


def _knobs(params: "EngineParams", *names: str) -> list[str]:
    """用户实际写了的开关名（没写的不出声——出声只为「你写了但没生效」服务）。"""
    return [KNOB_FLAGS[name] for name in names if getattr(params, name) is not None]


def _tuning(params: "EngineParams") -> dict:
    """把四个旋钮原样摊成构造参数：夹取、夹紧、用不上的解释都在各档构造器里做，
    这里不替它们决定谁能吃哪些——加一档时只改这一档的 builder。"""
    return {name: getattr(params, name) for name in ALL_KNOBS}


def _build_cloud(params: EngineParams):
    from .translator import Translator

    return Translator(
        cache_path=params.cache_path,
        stop_event=params.stop_event,
        log=params.log,
        style=params.style,
        tm=params.tm,
        jobs=params.jobs,
        **_tuning(params),
    )


def _build_local_llm(params: EngineParams):
    from .local_llm import LocalLLMTranslator

    # 这一档一条一问，旋钮对它自己的请求没有可拧的地方——写了就必须点名说清
    # （口径见 core/tuning.py 的 TIER_UNUSED_NOTE）；同时仍原样传进去，因为
    # 离线小模型回退是有批次概念的。
    warn_unused(LOCAL_LLM, _knobs(params, *ALL_KNOBS), params.log)
    return LocalLLMTranslator(
        model=params.model,
        stop_event=params.stop_event,
        log=params.log,
        style=params.style,
        cache_path=params.cache_path,
        fallback="local",
        quality=params.quality,
        tm=params.tm,
        jobs=params.jobs,
        **_tuning(params),
    )


def _build_paid(params: EngineParams):
    from .api_keys import load_key
    from .deepl import DeepLTranslator

    key_config = load_key("deepl")
    if not key_config:
        raise TranslationError(
            "未配置 DeepL key，无法使用付费档。请先在 api_keys.local.json "
            "里填写 deepl.key（申请地址 deepl.com/pro-api）。"
        )
    return DeepLTranslator(
        key_config=key_config,
        stop_event=params.stop_event,
        log=params.log,
        style=params.style,
        # DeepLTranslator 的 cache 参数用 False 表示「不缓存」，与 Translator 的 None 同义。
        cache=params.cache_path if params.cache_path else False,
        tm=params.tm,
        jobs=params.jobs,
        **_tuning(params),
    )


def _build_custom(params: EngineParams):
    """自托管档：读 ``自定义引擎.json``，用 ``LocalLLMTranslator`` 打用户填的端点。

    模型名的取舍（用户填 > 服务只挂一个 > 留空用默认）见
    ``core.custom_engine.pick_model``；服务连不上时 ``LocalLLMTranslator``
    自己会说明并回退，这一档不额外发明兜底路径。

    **回退方向由端点范围决定**：本机/内网的自托管
    服务连不上时改发免费云端，等于把「本来不肯出门的文档」偷偷送出去了——
    这恰好是选这一档的人要防的事，所以内网端点只能回退离线小模型；公网端点
    已经过一次出境确认，回退免费云端不越出同一边界。
    """
    from .custom_engine import (
        discover_models,
        endpoint_scope,
        load_config,
        pick_model,
        require_usable,
        save_config,
    )
    from .local_llm import LocalLLMTranslator

    config = load_config()
    if params.base_url:
        config.base_url = params.base_url
        config.enabled = True
    if params.model:
        config.model = params.model
    if not config.usable():
        raise TranslationError(
            "还没配置自托管服务，无法使用自托管档。请在界面点「配置自托管…」"
            "填入服务地址（vLLM / llama-server / LM Studio / 另一台机器上的 Ollama，"
            "都要 OpenAI 兼容的 /v1/chat/completions），或用 --custom-url=地址 临时指定。"
        )
    config.base_url = require_usable(config.base_url)
    scope = endpoint_scope(config.base_url)
    models = discover_models(config.base_url)
    if models and models != config.discovered:
        config.discovered = list(models)
        if not params.base_url:
            save_config(config)
    model = pick_model(config, models)
    if model:
        params.log(f"自托管服务：{config.base_url}，模型 {model}。")
    else:
        params.log(
            f"自托管服务：{config.base_url}。这台服务没报告模型名，"
            "将用默认模型名尝试；如服务上模型名不是默认的，请用「配置自托管…」填准。"
        )
    # 同 _build_local_llm：先点名「这一档吃不下」，再把值传给回退通道。
    warn_unused(CUSTOM, _knobs(params, *ALL_KNOBS), params.log)
    return LocalLLMTranslator(
        model=model or None,
        base_url=config.base_url,
        stop_event=params.stop_event,
        log=params.log,
        style=params.style,
        cache_path=params.cache_path,
        fallback="cloud" if scope == "public" else "local",
        quality=params.quality,
        tm=params.tm,
        jobs=params.jobs,
        **_tuning(params),
        target_label="单位自托管服务",
        unavailable_hint=(
            "请确认这台服务已经启动、地址与模型名填得对（界面点「配置自托管…」）。"
            + (
                "这一档连的是公网，回退走免费云端通道。"
                if scope == "public"
                else "这一档连的是本机/内网，服务连不上时只会改走本机离线小模型；"
                "没装离线小模型的段落会明确报失败，绝不会把原文改发到联网翻译服务。"
            )
        ),
    )


def _build_offline(params: EngineParams):
    from .local_translator import LocalTranslator

    # 保密模式零落盘：记忆读写在 SQLite 里，因此不注入 tm。不明说的话，
    # 用户会以为「这次怎么没复用记忆」。
    params.log("保密模式：翻译记忆不参与（记忆读写需要落盘，术语/进程内复用照常）。")
    return LocalTranslator(
        stop_event=params.stop_event,
        log=params.log,
        style=params.style,
        jobs=params.jobs,
        **_tuning(params),
    )


#: 档位表。顺序即界面单选的排列顺序（保密档除外，它不在单选中）。
SPECS: dict[str, EngineSpec] = {
    spec.key: spec
    for spec in (
        EngineSpec(
            key=CLOUD,
            radio_label="免费云端",
            full_label="免费云端",
            hint="翻译引擎：免费云端（默认，无需安装任何东西）。",
            footer="免费翻译 · 无需 API Key",
            mode_text="联网（免费通道）",
            shows_quality=False,
            provider="",
            offline_visible=False,
            factory=_build_cloud,
        ),
        EngineSpec(
            key=LOCAL_LLM,
            radio_label="本地大模型（更强）",
            full_label="本地大模型",
            hint=(
                "翻译引擎：本地大模型。需要本机安装 Ollama 并拉取模型"
                "（如 ollama pull qwen2.5:7b）；服务不可用时仅回退本机离线小模型。"
                "译文数据只在本机处理，不上传外部服务器。"
            ),
            footer="本地大模型 · 数据不出本机",
            mode_text="联网（本地大模型）",
            shows_quality=True,
            provider="",
            offline_visible=False,
            factory=_build_local_llm,
        ),
        EngineSpec(
            key=PAID,
            radio_label="付费 API",
            full_label="付费 API（DeepL）",
            hint=(
                "翻译引擎：付费 API（DeepL）。质量更强但按量计费；"
                "需要自备 key（未配置时会提示申请方法）；文档原文会发送至 "
                "DeepL（境外服务器），涉密文档请用保密模式。"
            ),
            footer="付费 API · 原文发送至 DeepL",
            mode_text="联网（付费 DeepL）",
            shows_quality=False,
            provider="deepl",
            offline_visible=False,
            factory=_build_paid,
        ),
        EngineSpec(
            key=CUSTOM,
            radio_label="自托管服务",
            full_label="自托管服务",
            hint="",  # 自托管的地址是用户填的，说明文字必须现拼，见 app._on_engine_change
            footer="自托管服务 · 地址由你配置",
            mode_text="联网（自托管服务）",
            shows_quality=True,
            provider="custom",
            offline_visible=False,
            factory=_build_custom,
        ),
        EngineSpec(
            key=OFFLINE,
            radio_label="保密模式（本机离线）",
            full_label="保密模式（本机离线小模型）",
            hint="保密模式：只用本机 models/ 里的离线模型，全程不联网。",
            footer="本地离线模型 · 处理过程不联网",
            mode_text="保密（本机离线）",
            shows_quality=False,
            provider="",
            offline_visible=True,
            factory=_build_offline,
        ),
    )
}


#: 转换日志第一行用的短名。沿用 v1.6.2 之前的文案（保密 / 联网 / 付费），
#: 自托管单独写清楚——「联网」两个字盖不住「发到了你自己配的那台机器」。
SHORT_MODES = {
    CLOUD: "联网",
    LOCAL_LLM: "联网",
    PAID: "付费",
    CUSTOM: "自托管",
    OFFLINE: "保密",
}


def get(key: str) -> EngineSpec:
    spec = SPECS.get((key or "").strip())
    if spec is None:
        raise TranslationError(
            f"未知的翻译引擎档位：{key}。可用档位："
            + "、".join(item.key for item in SPECS.values())
        )
    return spec


def label(key: str) -> str:
    return get(key).full_label


def mode_text(key: str) -> str:
    return get(key).mode_text


def visible_keys(offline: bool) -> list[str]:
    """当前模式下存在的档位。**联网四档在保密模式下全灭**——这是合规红线，
    不是「隐藏入口」的界面技巧：``--offline`` 的语义是只用本机离线模型。"""
    if offline:
        return [OFFLINE]
    return [key for key in SPECS if key != OFFLINE]


def radio_choices(offline: bool) -> list[tuple[str, str]]:
    """界面单选的 (value, 文字)。保密模式下返回空表——那一档不可切换，
    渲染四选一反而是误导。"""
    if offline:
        return []
    return [(SPECS[key].key, SPECS[key].radio_label) for key in visible_keys(offline)]


def resolve_cli(
    *,
    offline: bool = False,
    paid: str = "",
    engine: str = "",
    custom: bool = False,
    notice: Callable[[str], None] | None = None,
) -> str:
    """把 CLI 的三个档位开关合成一个档位 key。

    ``--offline`` 优先，但两种「优先」的口径不同，分开处理：

    * 付费档与自托管档会把文档发往**网络另一端**（境外服务器 / 用户自填的
      地址），与保密模式的承诺正面冲突，且不是「提醒一下就能放行」的事
      ——直接报错，让用户二选一；
    * ``--local-llm`` 的地址恒为本机 Ollama（``--offline`` 下 socket 已断，
      它也连不上），数据不出机器，冲突只在质量档位。沿用既有行为：改用
      离线小模型，但必须**出声**，否则用户以为这次在用大模型。
    """
    if paid and paid != "deepl":
        raise TranslationError(
            f"未知的付费引擎：--paid={paid}。当前只支持 --paid=deepl（DeepL）。"
        )
    requested: list[str] = []
    if paid:
        requested.append(PAID)
    if engine == LOCAL_LLM:
        requested.append(LOCAL_LLM)
    if custom:
        requested.append(CUSTOM)
    if len(requested) > 1:
        raise TranslationError(
            "引擎档位开关只能给一个，现在给了：" + "、".join(requested)
            + "。请去掉多余的开关。"
        )
    if offline:
        if PAID in requested or CUSTOM in requested:
            raise TranslationError(
                f"保密模式（--offline）只能用本机离线小模型，不能用"
                f"{label(requested[0])}：文档会发到网络另一端。"
                "涉密文档请继续用保密模式；要用这一档请去掉 --offline。"
            )
        if LOCAL_LLM in requested and notice is not None:
            notice(
                "保密模式：已忽略 --local-llm，改用本机离线小模型"
                "（--offline 下网络被硬断开，本机 Ollama 也连不上）。"
            )
        return OFFLINE
    return requested[0] if requested else CLOUD


def build(key: str, params: EngineParams):
    """按档位构造翻译器。``params.log`` 必须在构造前就接好，
    保密档与自托管档都在构造期出声。"""
    return get(key).factory(params)


def tiers_for_help() -> Sequence[str]:
    return tuple(SPECS.keys())
