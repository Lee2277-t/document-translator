"""本地大模型引擎：介于"离线小模型"和"付费 API"之间的那一档。

为什么用 OpenAI 兼容接口而不是 Ollama 私有接口
---------------------------------------------
我们同时需要两种运行方式，而它们**都**提供 OpenAI 兼容端点：

* Ollama（``http://localhost:11434/v1``）——日常翻译，一行 ``ollama pull qwen2.5:7b`` 即可；
* llama.cpp server / vLLM（同样 ``/v1``）——跑 Sakura 这类 Galgame 专用权重。

写一个 endpoint 就两边都能用，比维护两套协议划算。同理，换成任何 OpenAI 兼容的
本地服务（LM Studio、LocalAI）也都不用改代码。

三条硬约束
----------
1. **行数与占位符必须保留**。丢一段或把 ``Zxq0Z`` 译掉都比翻得差更严重，
   所以系统提示里把这条写死，并且译文回来后还要清洗一次模型爱加的前后缀。
2. **上下文分批注入**。完整术语库 + 全部角色卡塞不进上下文，只送"本批次真正命中"
   的那几条（``TranslationContext.terms`` / ``character_sheets``），是把有限上下文
   当作可调度资源来用，而不是到时候再手忙脚乱地砍东西。
3. **不可自动 failure**。Ollama 没装、模型没拉、端口不通，一律回退到小模型，
   不能让用户的文档卡在这里。健康自检用短超时（3 秒），避免启动慢。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Callable, Sequence

import requests

from .common import TranslationError, check_cancel
from .concurrency import DEFAULT_JOBS, JobGovernor, clamp_jobs, run_ordered
from .custom_engine import endpoint_scope
from .engine_base import Engine
from .no_translate import intro, lock_and_mask, unlock_and_restore
from . import direction, mixed
from .translator import TranslationCache, finish_text

if TYPE_CHECKING:  # 运行时靠注入（旧路径仍懒导入），不必背一层运行时依赖
    from .tm import TranslationMemory
from .tm import fuzzy_hit  # v1.9.9：近似复用明细，四档同一形状

DEFAULT_BASE_URL = "http://localhost:11434/v1"

#: 本地服务一般不校验，但字段得带上，否则部分服务端会 401
_PLACEHOLDER_KEY = "local"

#: 推荐模型。用户可以在界面里填别的名字，这里只是给默认值一个交代。
RECOMMENDED_MODELS: dict[str, tuple[str, str]] = {
    "general": ("qwen2.5:7b", "英→中日常文档：体积小、CPU 也能跑"),
    "quality": ("qwen2.5:14b", "质量更好：建议 12G 以上显存"),
    "galgame": (
        "sakura-13b-lnovel",
        "日→中 Galgame 专用权重，需自行导入 llama.cpp / Ollama",
    ),
}

SYSTEM_PROMPT = (
    "你是专业翻译引擎，把用户给出的内容译成简体中文。\n"
    "必须遵守：\n"
    "1. 只输出译文本身，不要加引号、不要解释、不要前后缀说明；\n"
    "2. 原样保留形如 Zxq0Z、Ztq0Z、Znt0Z 的占位符，绝不翻译或改写它们；\n"
    "3. 原样保留数字、单位、换行与标点形态；\n"
    "4. 不确定的地方宁可直译，也不要添加或删减信息。"
)

ENGLISH_SYSTEM_PROMPT = (
    "You are a professional translation engine. Translate the user's content "
    "into natural, idiomatic English.\n"
    "Rules:\n"
    "1. Output only the translation itself — no quotes, no explanation, no preamble;\n"
    "2. Keep tokens of the form Zxq0Z, Ztq0Z, Znt0Z exactly as they are — never "
    "translate or rewrite them;\n"
    "3. Keep numbers, units, line breaks and punctuation shape unchanged;\n"
    "4. When unsure, translate literally rather than adding or dropping information."
)


def system_prompt() -> str:
    """按当前方向给系统提示。

    译入英文时**必须换成英文提示**：中文提示会让小模型在「译成英文」与
    「用中文回答」之间摇摆，实测更容易把原文抄回来（回显）。
    """
    return ENGLISH_SYSTEM_PROMPT if direction.is_english() else SYSTEM_PROMPT

#: 模型爱加的装饰。清洗掉它们，免得每句话前面都顶着一句「译文：」。
_DECORATION_RE = re.compile(
    r"^\s*(?:译文|翻译|以下是译文|Chinese translation)\s*[:：]?\s*", re.I
)
_WRAPPING_QUOTES = re.compile(r"^[\"'“”‘’]\s*(.*?)\s*[\"'“”‘’]$", re.S)


def _http_get_json(url: str, timeout: float) -> Any:
    return _http_json("GET", url, timeout=timeout)


def _http_post_json(url: str, payload: dict, timeout: float) -> Any:
    return _http_json("POST", url, timeout=timeout, json=payload)


def _http_json(method: str, url: str, timeout: float, **kwargs) -> Any:
    with requests.Session() as session:
        # 本机和内网的文档请求只能直连用户选定的服务。
        session.trust_env = endpoint_scope(url) == "public"
        response = session.request(method, url, timeout=timeout, allow_redirects=False, **kwargs)
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise requests.HTTPError("翻译服务返回了重定向，请填写实际服务地址。")
        return response.json()


def clean_translation(raw: str) -> str:
    """去掉模型常加的装饰：前后缀说明、包裹式引号、首尾空行。"""
    text = (raw or "").strip()
    text = _DECORATION_RE.sub("", text)
    wrapped = _WRAPPING_QUOTES.match(text)
    if wrapped:
        text = wrapped.group(1).strip()
    return text


def build_context_block(ctx: Any, budget: int = 900) -> str:
    """把上下文压成一段系统提示。按术语→角色卡→摘要的优先级抢占预算。

    优先级不是随便排的：一条术语或一个人物设定违反了就是实打实的错译，
    而摘要只是让行文连贯些——预算不够时，砍摘要。
    """
    if ctx is None:
        return ""
    lines: list[str] = []

    terms = getattr(ctx, "terms", ()) or ()
    if terms:
        pairs = "；".join(f"{source} → {target}" for source, target in terms[:40])
        lines.append(f"必须使用的译名（违反即为错译）：{pairs}")

    sheets = getattr(ctx, "character_sheets", ()) or ()
    if sheets:
        for name, sheet in sheets[:10]:
            lines.append(f"【{name}】{sheet}")

    summary = (getattr(ctx, "summary", "") or "").strip()
    if summary and sum(len(line) for line in lines) < budget:
        room = budget - sum(len(line) for line in lines)
        if room > 60:
            lines.append(f"前文摘要：{summary[:room]}")

    if not lines:
        return ""
    return "参考上下文（这些背景不要译出来）：\n" + "\n".join(lines)


class LocalLLMEngine(Engine):
    """本地大模型引擎。``supports_ctx = True``，即它会消费上下文。"""

    name = "local-llm"
    label = "本地大模型"
    supports_ctx = True

    def __init__(
        self,
        model: str = RECOMMENDED_MODELS["general"][0],
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 180.0,
        temperature: float = 0.2,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.log = log or (lambda message: None)

    # -------------------------------------------------------------- 自检

    def installed_models(self) -> list[str]:
        payload = _http_get_json(f"{self.base_url}/models", timeout=3.0)
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        return [str(row.get("id") or row.get("name")) for row in rows]

    def available(self) -> bool:
        try:
            models = self.installed_models()
        except Exception:
            return False
        return self._matches(models)

    def _matches(self, models: Sequence[str]) -> bool:
        """判断列表里有没有"我们要的"模型，容忍 Ollama 的 tag 差异。

        曾经用过的粗暴写法是 ``self.model.split(":")[0] in name``——它会把
        ``qwen2.5:3b`` 当成 ``qwen2.5:7b`` 的匹配，用户明明只拉了 3B 却被
        告知"模型已就绪"，然后拿 3B 的质量交差。现在按 基名 + tag 精确比对：

        * ``qwen2.5:7b`` 对 ``qwen2.5:7b-instruct-q4_K_M`` → 同一个（tag 前缀一致）；
        * 列表只有基名 ``qwen2.5``（llama.cpp 常见）→ 算匹配，让服务端自己去认领；
        * ``qwen2.5:3b`` 对 ``qwen2.5:7b`` → 不算，tag 不同就是不同的模型。
        """
        wanted = (self.model or "").lower()
        if not wanted:
            return False
        base, sep, tag = wanted.partition(":")
        for name in models:
            current = (name or "").lower()
            if not current:
                continue
            if current == wanted:
                return True
            current_base, current_sep, current_tag = current.partition(":")
            if current_base != base:
                continue
            if not current_sep:
                return True  # 列表里是不带 tag 的基名
            if not sep:
                return True  # 用户没指定 tag，列表里有 tag
            if current_tag == tag or current_tag.startswith(f"{tag}-"):
                return True
        return False

    # -------------------------------------------------------------- 翻译

    def translate(self, text: str, ctx: Any = None) -> str:
        if not text or not text.strip():
            return text
        system = system_prompt()
        block = build_context_block(ctx)
        if block:
            system = f"{system}\n\n{block}"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            "stream": False,
            "temperature": self.temperature,
        }
        try:
            data = _http_post_json(
                f"{self.base_url}/chat/completions", payload, self.timeout
            )
        except Exception as exc:
            raise TranslationError(
                f"本地大模型调用失败（{type(exc).__name__}），已改用其他引擎"
            ) from exc
        try:
            raw = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TranslationError("本地大模型返回格式不符合预期") from exc
        cleaned = clean_translation(raw)
        # 译空等于翻了个寂寞：宁可报错让上层换引擎，也不要把空白塞进文档里
        if not cleaned:
            raise TranslationError("本地大模型返回了空译文")
        return cleaned

    def translate_batch(self, texts: Sequence[str], ctx: Any = None) -> list[str]:
        """本地大模型没有可靠的批量通道，逐条请求。

        为什么不做"一次传 N 行、用编号让模型分行输出"：一旦模型自作主张合并或
        拆分某几行，就会出现"第 7 段的译文被贴到第 8 段上"这种最难查的错位。
        逐条慢一点，但错位开销远高于这点延迟。
        """
        return [self.translate(text, ctx) for text in texts]


def probe_local_llm(
    model: str = RECOMMENDED_MODELS["general"][0],
    base_url: str = DEFAULT_BASE_URL,
    log: Callable[[str], None] | None = None,
) -> tuple[LocalLLMEngine | None, str]:
    """探一次本地大模型是否可用，返回（引擎, 原因）。

    三种不可用的原因要说人话，否则用户根本不知道下一步该干嘛：
    服务没起 / 起了但没拉模型 / 端口被别的程序占了。
    """
    engine = LocalLLMEngine(model=model, base_url=base_url, log=log)
    try:
        models = engine.installed_models()
    except Exception as exc:
        reason = "服务未启动或连接失败"
        return None, f"{reason}（{type(exc).__name__}）"
    if not models:
        return None, "已连到服务，但它没有列出任何模型"
    if not engine._matches(models):
        names = "、".join(str(item) for item in models[:5])
        return None, f"没找到模型 {model}；本机可用模型：{names}"
    return engine, "ok"


class _RawTranslator:
    """把 LocalLLMTranslator 的裸翻译路径包一层给质量管线。

    为什么需要它：管线内部要调 ``translator.translate_texts``，而
    LocalLLMTranslator.translate_texts 的 quality 分支又会去建管线——
    直接把自己传进去就是无限递归。包一层让管线拿到的是"不过质量层的
    裸翻译"，管线自己负责质量层。
    """

    def __init__(self, owner: "LocalLLMTranslator") -> None:
        self.owner = owner

    def translate_texts(self, texts, progress=None, ctx=None):
        return self.owner._translate_plain(texts, progress, ctx)


class LocalLLMTranslator:
    """本地大模型档的完整翻译器：接口与 ``Translator`` / ``LocalTranslator`` 一致。

    四个设计要点：

    1. **探测一次，全程可用性确定**。构造时就 ``probe_local_llm``，不可用立刻
       回退，不会等到翻了五十段才在中间炸掉。
    2. **默认回退离线小模型**（``fallback="local"``）。自托管档由登记表按
       端点范围决定：本机/内网端点用 local——服务一挂就把单位内部文档改发
       免费云端，正是选这一档的人要避免的事；公网端点用 cloud（用户已经确认
       文本可以出境，回退不越出同一边界）。
    3. **逐段救回**。本地服务中途挂掉（显存溢出、进程被杀）时，只把失败的段
       交给回退引擎，已翻好的不重翻——和在线模式的"换模型兜底"同一思路。
    4. **质量引擎可关**。``quality=True`` 时走 core/quality 管线（场景分片、
       分层上下文、术语校验、定向校改）；关掉后是纯逐句翻译，行为与旧版一致。
    """

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        stop_event=None,
        log: Callable[[str], None] | None = None,
        style: str = "standard",
        cache_path: str | None = None,
        fallback: str = "local",
        quality: bool = True,
        state_path: str | None = None,
        use_tm: bool = True,
        tm: TranslationMemory | None = None,
        jobs: int = DEFAULT_JOBS,
        target_label: str = "本地大模型",
        unavailable_hint: str | None = None,
        batch_items: int | None = None,
        batch_chars: int | None = None,
        chunk_chars: int | None = None,
        retry_delays: tuple[float, ...] | None = None,
    ) -> None:
        import threading

        self.stop_event = stop_event or threading.Event()
        self.log = log or (lambda message: None)
        #: 并发度（默认 1）。本地大模型跑在本机（Ollama），没有封禁风险，
        #: 上限按 CPU 核数放开；但**默认仍是 1**——并发是用户显式要的才有。
        self.jobs = clamp_jobs("local-llm", jobs)
        self.governor = JobGovernor("local-llm", jobs, log=self.log)
        # 构造时就说明实际并发度（>1 说几路，被夹低说原因），别让用户猜。
        self.governor.announce()
        self.style = style
        self.fallback_kind = "cloud" if fallback == "cloud" else "local"
        self.model = model or RECOMMENDED_MODELS["general"][0]
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.quality_enabled = quality
        #: 日志里怎么称呼这台服务。本机的 Ollama 是「本地大模型」，自托管档
        #: 传「单位自托管服务」——同一句话术说两种端点会误导（"安装 Ollama"
        #: 对本机对，对隔壁那台 vLLM 服务器就是废话）。
        self.target_label = target_label or "本地大模型"
        self.unavailable_hint = (
            unavailable_hint
            if unavailable_hint is not None
            else f"想启用{self.target_label}：安装 Ollama 后执行 "
            f"ollama pull {self.model}。"
        )

        self.engine, self.unavailable_reason = probe_local_llm(
            self.model, self.base_url, self.log
        )
        if self.engine is not None:
            self.log(
                f"已连接{self.target_label}（{self.model}），翻译质量档位：{self.target_label}。"
            )
        else:
            target = "免费云端通道" if self.fallback_kind == "cloud" else "离线小模型"
            self.log(
                f"未检测到可用的{self.target_label}（{self.unavailable_reason}），"
                f"本次将改用{target}。{self.unavailable_hint}"
            )

        # 统计字段：summary() 与逐段计数都从这里读，先全部初始化。
        self.translated_count = 0
        self.skipped_count = 0
        self.cache_hits = 0
        self.failed_count = 0
        self.term_count = 0
        self.echo_count = 0
        self.rescued_count = 0
        self.language_counts: dict[str, int] = {}
        #: 翻译记忆命中计数：**精确**（100% 直通，没进引擎）与**模糊**
        #: （近似复用，待确认）分开记——两者在报告里是不同的可信度。
        self.tm_exact = 0
        self.tm_fuzzy = 0
        #: 近似复用明细（v1.9.9）：{段号, 原文, 相似度}，进自检报告。
        self.tm_fuzzy_hits: list[dict] = []
        #: 质量引擎发现的问题，跨多次 translate_texts 累积，供自检报告取用
        self.quality_issues: list = []

        self.cache = TranslationCache(cache_path, self._cache_profile())
        #: 阈值旋钮（v1.6.9）：本档一条一问，这四个值在本档没有可拧的地方；
        #: 但服务连不上时会回退到免费云端或离线小模型，那两条通道是有批次概念
        #: 的——用户写了的值必须原样带过去，不能因为「这一档用不上」就悄悄丢掉。
        self._fallback_tuning = {
            "batch_items": batch_items,
            "batch_chars": batch_chars,
            "chunk_chars": chunk_chars,
            "retry_delays": retry_delays,
        }
        self._fallback = None
        # 翻译记忆：模糊匹配复用历史译文。与缓存同一库不同表。
        # ``tm`` 由调用方注入（四档共用同一个实例，命中口径才一致）；
        # 没注入时才自己按 state_path 建一个——老调用方一行都不用改。
        self.tm = tm
        if self.tm is None and use_tm:
            try:
                from .state import StateStore
                from .tm import TranslationMemory

                self.tm = TranslationMemory(
                    StateStore(state_path or self.cache.path or None), log=self.log
                )
            except Exception:
                self.tm = None
        self.quality_pipeline = self._build_quality_pipeline(state_path)
        if self.quality_pipeline is not None:
            self.log("质量引擎已启用：场景分片 + 分层上下文 + 术语校验 + 定向校改。")
        elif self.tm is not None:
            # 没质量引擎时 TM 只读不写：没有校验把关，写进去怕把错误复印出去
            self.log("翻译记忆已启用（模糊匹配复用历史译文）。")

    # ---------------------------------------------------------------- 质量引擎

    def _build_quality_pipeline(self, state_path: str | None):
        """构造质量管线。任何一步不满足就返回 None——质量是增强，不是前提。"""
        if not self.quality_enabled:
            return None
        if self.engine is None:
            return None  # 引擎都不可用，走回退路径更直接
        try:
            from .state import StateStore
            from .termbase import Termbase
            from .quality.characters import CharacterStore
            from .quality.pipeline import QualityPipeline

            store = StateStore(state_path or self.cache.path or None)
            termbase = Termbase(store, log=self.log)
            # 用户手写的术语表是预先裁决过的权威译名，直接播种为「已采用」
            termbase.seed_from_glossary()
            return QualityPipeline(
                translator=_RawTranslator(self),
                termbase=termbase,
                character_store=CharacterStore(store),
                store=store,
                engine=self.engine,
                tm=self.tm,
                log=self.log,
            )
        except Exception as exc:
            self.log(f"质量引擎不可用（{type(exc).__name__}），按普通模式翻译。")
            return None

    # ---------------------------------------------------------------- 内部

    def _cache_profile(self) -> str:
        """缓存键要带模型名：换个模型等于换个译者，旧译文不能混用。"""
        from . import glossary

        try:
            terms = glossary.fingerprint()
        except Exception:
            terms = ""
        return f"local-llm:{self.model}|{terms}|{self.style}"

    def _ensure_fallback(self):
        """按需构造回退翻译器。离线小模型要加载几百 MB 权重，不能白加载。"""
        if self._fallback is not None:
            return self._fallback
        if self.fallback_kind == "local":
            from .local_translator import LocalTranslator

            self._fallback = LocalTranslator(
                stop_event=self.stop_event,
                log=self.log,
                style=self.style,
                **self._fallback_tuning,
            )
        else:
            from .translator import Translator

            self._fallback = Translator(
                cache_path=self.cache.path,
                stop_event=self.stop_event,
                log=self.log,
                style=self.style,
                **self._fallback_tuning,
            )
        return self._fallback

    # ---------------------------------------------------------------- 翻译

    def translate_texts(
        self,
        texts: list[str],
        progress=None,
        ctx=None,
    ) -> list[str]:
        """翻译一批文本。

        质量引擎开启时走 ``core/quality`` 管线（场景分片 → 分层上下文 →
        翻译 → 校验 → 定向校改）；关闭或不可用时退化为逐句独立翻译，
        行为与旧版完全一致。``ctx`` 只有质量引擎内部会传。
        """
        if self.quality_pipeline is not None and self.engine is not None:
            try:
                results = self._run_quality(texts, progress)
            except Exception as exc:
                # 质量层是增强层：它自己出错不该让文档翻不出来
                self.log(f"质量引擎异常（{type(exc).__name__}），已按普通模式继续。")
                results = self._translate_plain(texts, progress, ctx)
        else:
            results = self._translate_plain(texts, progress, ctx)
        # 混排补译走裸翻译路径：质量层按整段组织上下文，子句是句子碎片，
        # 再进一次场景分片/校验只会拖慢并引入噪声。
        mixed.fill_for_engine(
            self,
            texts,
            results,
            lambda spans: self._translate_plain(spans, None, ctx),
        )
        return results

    def _run_quality(self, texts: list[str], progress=None) -> list[str]:
        from .quality.segments import segments_from_texts

        result = self.quality_pipeline.run(
            segments_from_texts(list(texts)), progress
        )
        if result.issues:
            self.quality_issues.extend(result.issues)
            blocking = sum(1 for issue in result.issues if issue.is_blocking())
            self.log(
                f"质量引擎：{result.scenes} 个场景，校改 {result.corrected} 段，"
                f"LLM 复核 {result.llm_checks} 段，待处理问题 "
                f"{len(result.issues)} 条（其中 Critical {blocking} 条）。"
            )
        return result.translations

    def pending_terms(self) -> list[dict]:
        """未决术语池（候选 + 存疑）。供自检报告与人在回路界面取用。"""
        if self.quality_pipeline is None:
            return []
        try:
            return self.quality_pipeline.pending_terms()
        except Exception:
            return []

    def _translate_plain(
        self,
        texts: list[str],
        progress=None,
        ctx=None,
    ) -> list[str]:
        """不过质量层的裸翻译：术语锁定 → 引擎 → 术语换回 → 风格润色 → 缓存。

        ``ctx`` 携带本批命中的术语、在场角色卡与前文摘要（质量引擎注入）。
        """
        from .ctx import TranslationContext
        from .translator import normalize_source, should_translate

        if self.engine is None:
            return self._ensure_fallback().translate_texts(texts, progress)

        total = len(texts)
        results: list[str | None] = [None] * total
        pending: list[int] = []
        resolved = 0

        # 缓存键带上 ctx 的稳定维度（在场角色/术语指纹）。场景摘要按设计
        # 不进 cache_profile，不会把长文档的命中率打到零。
        cache_engine = self.model
        if ctx is not None and not ctx.is_trivial():
            cache_engine = f"{self.model}|{ctx.cache_profile()}"
            if not ctx.target_lang:
                ctx = ctx.with_(target_lang=direction.web_code())
        else:
            ctx = TranslationContext(
                target_lang=direction.web_code(),
                style=self.style,
                engine=self.engine.name,
            )

        for index, source in enumerate(texts):
            check_cancel(self.stop_event)
            normalized = normalize_source(source)
            if not normalized:
                results[index] = source
                self.skipped_count += 1
                resolved += 1
                continue
            term = direction.lookup_heading(normalized)
            if term:
                results[index] = term
                self.term_count += 1
                resolved += 1
                continue
            if not should_translate(normalized):
                results[index] = source
                self.skipped_count += 1
                resolved += 1
                continue
            cached = self.cache.get(cache_engine, normalized)
            if cached is not None:
                results[index] = cached
                self.cache_hits += 1
                resolved += 1
                continue
            # 统一走 ``match``：本档与其余三档同口径——100% 直通不进引擎，
            # 近似命中**应用但不回写记忆**（回写会把近似句的差异复印到全文，
            # 见 core/tm.py 的取舍说明）。
            if self.tm is not None:
                remembered = self.tm.match(normalized)
                if remembered is not None:
                    results[index] = remembered.target
                    if remembered.ratio >= 1.0:
                        self.tm_exact += 1
                    else:
                        self.tm_fuzzy += 1
                        self.tm_fuzzy_hits.append(
                            fuzzy_hit(index + 1, normalized, remembered)
                        )
                    resolved += 1
                    continue
            pending.append(index)

        failures: list[int] = []

        def worker(_position: int, index: int) -> tuple[str, str]:
            """只做「问模型」这一件纯活，返回（原文归一化, 译文）。"""
            normalized = normalize_source(texts[index])
            # 免翻区先掩、术语后锁（顺序理由见 core/no_translate.py）——
            # 四档同一口径。
            masked, mapping = lock_and_mask(normalized)
            translated = self.engine.translate(masked, ctx)
            if mapping:
                translated = unlock_and_restore(translated, mapping)
            return normalized, finish_text(translated, self.style)

        def on_done(position: int, value: object) -> None:
            nonlocal resolved
            if value is None:  # 失败：on_error 已出声并记进 failures
                return
            index = pending[position]
            normalized, final = value  # type: ignore[misc]
            if normalize_source(final) == normalized:
                self.echo_count += 1
            results[index] = final
            self.cache.set(cache_engine, normalized, final)
            self.translated_count += 1
            self.governor.note_success()
            self.jobs = self.governor.jobs
            resolved += 1
            if progress is not None:
                progress(min(resolved, total), total, "")

        def on_error(position: int, exc: BaseException) -> None:
            nonlocal resolved
            if not isinstance(exc, TranslationError):
                # 不是引擎的翻译失败（真·意外）：照改动前的语义向上抛，
                # 不冒充「服务挂了」去走回退——那会把 bug 藏成静默降级。
                raise exc
            index = pending[position]
            # 本地服务中途挂掉：记下来交给回退引擎，不拖垮整份文档。
            self.log(f"本地大模型第 {index + 1} 段失败（{exc}），将回退重译。")
            failures.append(index)
            self.failed_count += 1
            self.governor.note_failure(exc)
            self.jobs = self.governor.jobs
            resolved += 1
            if progress is not None:
                progress(min(resolved, total), total, "")

        if self.jobs > 1:
            self.governor.announce()
        run_ordered(
            pending,
            worker,
            jobs=self.jobs,
            stop_event=self.stop_event,
            on_done=on_done,
            on_error=on_error,
        )

        if failures:
            if len(failures) == len(pending) and pending:
                # 全挂：本地服务大概率已经没了，整体切回退，别一段一段地撞墙。
                self.log("本地大模型持续不可用，本次内容已整体切换回退引擎。")
            fallback = self._ensure_fallback()
            rescued = fallback.translate_texts([texts[index] for index in failures])
            for index, translated in zip(failures, rescued):
                results[index] = translated
            self.rescued_count += len(failures)
            self.failed_count -= len(failures)

        return [text if item is None else item for item, text in zip(results, texts)]

    @property
    def tm_hits(self) -> int:
        """翻译记忆命中总数（精确 + 模糊）。

        做成只读属性而不是计数器：老的自检报告与既有测试读 ``tm_hits``，
        而计数器口径一旦和 ``tm_exact + tm_fuzzy`` 少加一处就会悄悄对不上
        ——属性让它**只能**等于两者之和，不存在写漏的可能。
        """
        return self.tm_exact + self.tm_fuzzy

    def summary(self) -> str:
        if self.engine is not None:
            head = "本地大模型"
        else:
            head = "本地大模型（不可用，已回退）"
        text = (
            f"{head}翻译 {self.translated_count} 段，跳过 {self.skipped_count} 段，"
            f"缓存命中 {self.cache_hits} 段，失败 {self.failed_count} 段"
        )
        if self.term_count:
            text += f"，标题词条直译 {self.term_count} 段"
        if self.echo_count:
            text += f"，{self.echo_count} 段只返回原文"
        if self.tm_exact:
            text += f"，翻译记忆直通 {self.tm_exact} 段（完全一致，未进引擎）"
        if self.tm_fuzzy:
            text += f"，记忆近似复用 {self.tm_fuzzy} 段（待确认）"
        locked = intro()
        if locked:
            text += f"，{locked}"
        if self.rescued_count:
            text += f"，回退引擎救回 {self.rescued_count} 段"
        return text
