"""DeepL 付费 API 引擎（C3-a）。

接口契约
--------
``translate_texts(texts, progress=None, ctx=None) -> list[str]`` 与在线
``Translator`` **完全同签名**，word/pdf/ppt 三个排版器与质量管线零改动。
术语锁定 / 标题词表 / 风格润色 / 缓存 / term_hits 全部复用现有机制
（``core/translator.py`` 的判定函数、``core/glossary.py`` 的 lock/unlock、
``core/style.py`` 的 polish、``TranslationCache``），不重写。

为什么不用官方 SDK（``pip install deepl``）
------------------------------------------
项目已有 requests，直连 HTTP 少一个依赖、打包体积可控；DeepL 的
``/v2/translate`` 协议本身很简单（一个 JSON POST + 一个 JSON 响应）。

三条硬约束
----------
1. **首次真正翻译前必须探测 key**（``text=["Hello"]``）。不要翻了一半
   才发现 key 错了——那时用户已经等了半分钟，文档里一半是原文。
2. **分批规则：单请求 ≤50 段且 ≤100KB**。128KiB 是 DeepL 的请求体硬限，
   留 28KiB 余量给 JSON 结构与转义；超长段先按句拆开（``split_for_api``，
   与 EdgeEngine 同款），保证任何单请求都在限内。
3. **配额与 key 错误必须说人话**。403（key 无效）/ 456（免费额度用完）是
   付费档最高频的失败，技术错误码对用户不可行动；429 指数退避重试
   （0.5s/1s/2s，最多 3 次），5xx/超时重试 1 次，仍失败只让**该批**
   保留原文，不拖垮整轮（与在线 Translator 的逐段救回同一思路）。

计费口径
--------
请求带 ``show_billed_characters=true``，用响应里的 ``billed_characters``
记账——这才是 DeepL 真收的钱。本地流水同时记送检字符（``record_usage``），
两个口径都进自检报告；``GET /v2/usage`` 的账户页口径由 ``official_usage``
取用，失败只降级不报错。

网络层可注入：``session`` / ``post`` / ``get`` 都能在构造时传或构造后赋值，
测试不真发 HTTP。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from . import direction, glossary, mixed
from .api_keys import month_usage, record_usage
from .common import CancelledError, TranslationError, app_root, check_cancel, data_root
from .concurrency import DEFAULT_JOBS, JobGovernor, clamp_jobs, run_ordered
from .tuning import cap_int, warn_unused
from .glossary import hit_rows
from .no_translate import intro, lock_and_mask, unlock_and_restore
from .style import STANDARD

if TYPE_CHECKING:  # 运行时靠注入，不必为类型名背一层导入
    from .tm import TranslationMemory
from .tm import fuzzy_hit  # v1.9.9：近似复用明细，四档同一形状
from .retry import BackoffPolicy, TransientError, http_error, is_retryable, retry, status_of
from .translator import (
    TranslationCache,
    finish_text,
    normalize_source,
    should_translate,
    split_for_api,
)

#: DeepL 域名：Free 档（key 以 ``:fx`` 结尾）与 Pro 档不同，选错直接 403。
PRO_DOMAIN = "https://api.deepl.com"
FREE_DOMAIN = "https://api-free.deepl.com"

#: 单请求段数上限（自设，保守于 DeepL 未明文的实践上限）。
DEEPL_BATCH_ITEMS = 50

#: 单请求体字节上限。128KiB 是 DeepL 硬限，留 28KiB 给 JSON 结构与转义。
DEEPL_MAX_BATCH_BYTES = 100 * 1024

#: 超长段的预拆分粒度（字符数）。按 CJF 三字节/字最坏估算 30000 字 ≈ 90KB，
#: 仍在 DEEPL_MAX_BATCH_BYTES 之内，拆完的单片直接就是合法请求单元。
DEEPL_SEGMENT_CHARS = 30000

#: 429 频率超限的指数退避序列（秒）。最多重试 3 次，仍失败该批保留原文。
DEEPL_RETRY_DELAYS = (0.5, 1.0, 2.0)

#: 单次请求超时（秒）。DeepL 批量翻译比免费通道慢，给足 60 秒。
DEEPL_TIMEOUT = 60.0


class DeepLError(TranslationError):
    """DeepL 调用失败。

    ``fatal=True`` 表示重试无意义（key 无效 / 免费额度用完）：整轮翻译应该
    中止并把这句话传给用户，而不是一段一段地去撞墙。非 fatal 的错误
    （429 重试用尽 / 5xx / 网络异常）只让当前批次保留原文，其余批次继续。
    """

    def __init__(
        self, message: str, status: int | None = None, fatal: bool = False
    ) -> None:
        super().__init__(message)
        self.status = status
        self.fatal = fatal


class DeepLTranslator:
    """DeepL 付费档翻译器。接口与在线 ``Translator`` 一致。"""

    name = "deepl"
    label = "DeepL"

    #: 请求尺寸默认值放类上（同 core/local_translator.py 的计数器口径）：
    #: 用 ``__new__`` 拼装的半实例不该在 translate_batch 里撞 AttributeError。
    batch_items = DEEPL_BATCH_ITEMS
    chunk_chars = DEEPL_SEGMENT_CHARS

    def __init__(
        self,
        key_config: dict | None,
        style: str = STANDARD,
        stop_event=None,
        log: Callable[[str], None] | None = None,
        cache: bool | str | None = True,
        session=None,
        post: Callable[[str, dict], object] | None = None,
        get: Callable[[str], object] | None = None,
        timeout: float = DEEPL_TIMEOUT,
        tm: "TranslationMemory | None" = None,
        jobs: int = DEFAULT_JOBS,
        batch_items: int | None = None,
        batch_chars: int | None = None,
        chunk_chars: int | None = None,
        retry_delays: tuple[float, ...] | None = None,
    ) -> None:
        self.key = str((key_config or {}).get("key") or "").strip()
        try:
            self.budget = max(0, int((key_config or {}).get("monthly_char_budget") or 0))
        except (TypeError, ValueError):
            self.budget = 0
        self.log = log or (lambda message: None)
        self.style = style
        self.stop_event = stop_event
        self.timeout = timeout
        #: 网络层可注入：传假 session（有 .post / .get）或直接换掉
        #: self.post / self.get 两个调用点。默认走 requests。
        self.session = session
        self.post = post or self._http_post
        self.get = get or self._http_get
        # 请求尺寸旋钮（v1.6.9，--batch-items= / --chunk-chars=）：
        # chunk_chars 是「超长段先按句拆开」的目标长度，拆开后的每一片都要能
        # 单独塞进一个请求，所以它只能比 DEEPL_MAX_BATCH_BYTES 对应的长度更短。
        self.batch_items = cap_int(
            batch_items, label="单次请求段数", engine="DeepL",
            default=DEEPL_BATCH_ITEMS, log=self.log,
        )
        self.chunk_chars = cap_int(
            chunk_chars, label="单段字符数", engine="DeepL",
            default=DEEPL_SEGMENT_CHARS, log=self.log,
        )
        # --batch-chars 在付费档**故意不接**：上限是 100KB 字节数，拿字符数去夹
        # 必然算错，而算错的后果是整批 400。说清「这个开关对本档无效」比忽略好。
        if batch_chars is not None:
            warn_unused("paid", ["--batch-chars="], self.log)
        #: 429 退避序列与重试暂停。实例属性而非模块常量，测试可以置 0 加速，
        #: 生产行为仍是文档写明的 0.5/1/2 秒；``--retry-delays=`` 也走这里。
        self._rate_delays = retry_delays or DEEPL_RETRY_DELAYS
        self._pause = 1.0
        #: 并发度（默认 1 = 与改动前逐字节等价）。付费档上限 4，出事自动降回 1。
        self.jobs = clamp_jobs("paid", jobs)
        self.governor = JobGovernor("paid", jobs, log=self.log)
        # 构造时就说明实际并发度（>1 说几路，被夹低说原因），别让用户猜。
        self.governor.announce()
        # 这里**不需要**计数锁：`run_ordered` 保证 on_done / on_error 都在
        # 主线程按索引顺序执行，chars_sent / failed_count 的累加不存在争用。
        # worker 只做「发一次请求」，连日志都不写。
        #: key 以 :fx 结尾 → Free 域名；否则 Pro 域名。选错域名会 403，
        #: 而 403 的排查成本很高，所以在构造时就分流。
        self.domain = FREE_DOMAIN if self.key.endswith(":fx") else PRO_DOMAIN

        # 统计字段：summary() 与逐段计数都从这里读，必须全部先初始化。
        self.translated_count = 0
        self.skipped_count = 0
        self.cache_hits = 0
        self.failed_count = 0
        self.term_count = 0
        self.echo_count = 0
        #: 翻译记忆（可注入）。付费档省的是**字符配额**：命中一次记忆就是
        #: 一次不计费的请求，所以记忆查询必须在批量发送之前。
        #: ``tm_exact``（完全一致，100% 直通）与 ``tm_fuzzy``（近似复用，
        #: 待确认且绝不回写记忆）分开计数。
        self.tm = tm
        self.tm_exact = 0
        self.tm_fuzzy = 0
        #: 近似复用明细（v1.9.9）：{段号, 原文, 相似度}，进自检报告。
        self.tm_fuzzy_hits: list[dict] = []
        #: 本次会话的字符消耗：chars_sent 是送检口径（本地流水），
        #: chars_billed 是 DeepL 计费口径（真收的钱）。
        self.chars_sent = 0
        self.chars_billed = 0
        #: 当前翻译的文件名，进用量流水的 file 字段。CLI 在 _convert 里
        #: 逐个文件设置；GUI 接线（v1.5.1）同。
        self.current_file = ""
        #: 术语命中明细（本次运行），字段与在线 Translator 逐字一致；
        #: 缓存命中的段不记（报告反映本次运行，不是历史缓存的去向）。
        self.term_hits: list[dict] = []
        #: 质量引擎问题。付费档不挂 core/quality 管线，保持空列表——
        #: 报告的 getattr 模式取到就是「无质量问题」，接口兼容。
        self.quality_issues: list = []
        self._probed = False

        # 缓存：True 用默认库（与 StateStore 同一个文件、不同表）；
        # 字符串按路径；False/None 不用缓存（--no-cache）。
        if cache is True:
            self._cache_path: str | None = str(
                data_root() / "cache" / "translations.sqlite"
            )
        elif cache:
            self._cache_path = str(cache)
        else:
            self._cache_path = None
        self.cache = (
            TranslationCache(self._cache_path, self._cache_profile())
            if self._cache_path
            else None
        )

        intro = glossary.intro()
        if intro:
            self.log(intro)

    # ---------------------------------------------------------------- 缓存键

    def _cache_profile(self) -> str:
        """术语表指纹 + 风格档位。两者任何一个变了，缓存都必须整体换一批键
        ——与在线 Translator 的 ``_cache_profile`` 同一套口径。"""
        try:
            return f"{glossary.fingerprint()}:{self.style}"
        except Exception:
            return str(self.style)

    def _sync_cache_profile(self, ctx) -> None:
        """把上下文里的**稳定**维度合并进缓存 profile（照搬在线 Translator）。

        只取术语指纹 / 风格 / 目标语种 / 在场角色四项；场景摘要刻意排除
        （它每批次都在变，进了缓存键长文档命中率会归零）。
        """
        if self.cache is None:
            return
        base = self._cache_profile()
        extra = "" if ctx is None or ctx.is_trivial() else ctx.cache_profile()
        profile = f"{base}|{extra}" if extra else base
        if profile != getattr(self.cache, "_profile", None):
            self.cache = TranslationCache(self._cache_path, profile)

    def _cache_lookup(self, text: str) -> str | None:
        """只查 deepl 自己的缓存，不跨引擎复用免费通道的旧译文。

        付费档的缓存语义是「上次用 DeepL 翻过什么」：免费通道的译文质量
        差一截，拿它冒充 DeepL 的结果，用户付了钱却拿到旧质量。
        译文 == 原文的历史命中照在线口径跳过（回显不进缓存）。
        """
        if self.cache is None:
            return None
        cached = self.cache.get("deepl", text)
        if cached is None or normalize_source(cached) == text:
            return None
        return cached

    # ---------------------------------------------------------------- 网络层

    def _http_post(self, path: str, payload: dict):
        import requests

        url = f"{self.domain}{path}"
        client = self.session if self.session is not None else requests
        return client.post(
            url,
            headers={
                "Authorization": f"DeepL-Auth-Key {self.key}",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=self.timeout,
        )

    def _http_get(self, path: str):
        import requests

        url = f"{self.domain}{path}"
        client = self.session if self.session is not None else requests
        return client.get(
            url,
            headers={"Authorization": f"DeepL-Auth-Key {self.key}"},
            timeout=self.timeout,
        )

    # ---------------------------------------------------------------- 翻译

    def translate_texts(
        self,
        texts: list[str],
        progress: Callable[[int, int, str], None] | None = None,
        ctx=None,
    ) -> list[str]:
        """翻译一批文本。签名与在线 ``Translator`` 完全一致。"""
        self._sync_cache_profile(ctx)
        total = len(texts)
        results: list[str | None] = [None] * total
        pending: list[int] = []
        resolved = 0

        for index, source in enumerate(texts):
            check_cancel(self.stop_event)
            normalized = normalize_source(source)
            if not normalized:
                results[index] = source
                self.skipped_count += 1
                resolved += 1
                continue
            term = direction.lookup_heading(normalized)
            if term and term != normalized:
                results[index] = term
                self.term_count += 1
                # 整段恰好就是一条术语：译文直接是指定译法，必然「生效」。
                # 与在线 Translator 同口径记录，报告里两档长得一样。
                self._record_term_hit(index + 1, normalized, term, True)
                resolved += 1
                continue
            if not should_translate(normalized):
                results[index] = source
                self.skipped_count += 1
                resolved += 1
                continue
            # 记忆查在缓存之前、更在批量发送之前：付费档按字符计费，
            # 命中一次就是省一次钱。
            if self.tm is not None:
                remembered = self.tm.match(normalized)
                if remembered is not None:
                    results[index] = remembered.target
                    if remembered.ratio >= 1.0:
                        self.tm_exact += 1
                    else:
                        # 模糊命中用归用，绝不回写记忆（理由是付费译文也没
                        # 过我方质量校验，见 core/tm.py 的取舍说明）。
                        self.tm_fuzzy += 1
                        self.tm_fuzzy_hits.append(
                            fuzzy_hit(index + 1, normalized, remembered)
                        )
                    resolved += 1
                    continue
            cached = self._cache_lookup(normalized)
            if cached is not None:
                results[index] = cached
                self.cache_hits += 1
                resolved += 1
                continue
            pending.append(index)

        if pending:
            # 首次真正翻译前探一次 key：不要翻了一半才发现 key 错了。
            self._ensure_probed()
            self._warn_budget([normalize_source(texts[index]) for index in pending])
            resolved = self._batch_pass(
                pending, texts, results, resolved, progress, ctx
            )

        # 混排补译按子句计费：只把漏掉的那句送进 DeepL，不重发整段。
        mixed.fill_for_engine(
            self, texts, results, lambda spans: self.translate_texts(spans, ctx=ctx)
        )
        if progress is not None:
            progress(total, total, "")
        return [
            results[index] if results[index] is not None else texts[index]
            for index in range(total)
        ]

    def _batch_pass(
        self,
        indices: list[int],
        texts: list[str],
        results: list[str | None],
        resolved: int,
        progress: Callable[[int, int, str], None] | None,
        ctx=None,
    ) -> int:
        """锁定术语 → 按批调 DeepL → 换回译法 → 润色 → 记命中 → 写缓存。

        分批规则：单请求 ≤ ``self.batch_items`` 段且 ≤100KB（128KiB 是 DeepL 硬限，
        先到者为准）；段数与单段长度可被 ``--batch-items=`` / ``--chunk-chars=`` 收紧。
        单个批次失败（429 重试用尽 / 5xx / 网络异常）只让该批保留原文，
        其余批次继续——付费档一次请求带 50 段，整轮中止的代价太大。
        """
        plain = [normalize_source(texts[index]) for index in indices]
        prepared: list[tuple[str, dict[str, str], list[tuple[str, str, str]]]] = []
        for item in plain:
            hits: list[tuple[str, str, str]] = []
            # 免翻区先掩、术语后锁（顺序理由见 core/no_translate.py）；
            # 四个档位都这么做，同一份文档在任意档位得到同样的保护口径。
            masked, mapping = lock_and_mask(item, hits)
            prepared.append((masked, mapping, hits))

        # 超长段先按句拆开（EdgeEngine 同款 split_for_api），再统一切请求。
        units: list[list[str]] = []
        for masked, _mapping, _hits in prepared:
            if len(masked.encode("utf-8")) > DEEPL_MAX_BATCH_BYTES:
                pieces = split_for_api(masked, self.chunk_chars)
                units.append(pieces or [masked])
            else:
                units.append([masked])

        # 切请求：单片逐个入堆，到限（50 段 / 100KB，先到者为准）就flush。
        # 同一段的拆分片允许跨请求——DeepL 段间本就不共享上下文，按序发、
        # 按 unit 拼回即可；把单元当成不可拆的整块，会让「一段就超限」的
        # 长文永远塞不进合法请求（实测 150KB 单段整块发出，直接 400）。
        requests_out: list[list[str]] = []
        piece_owners: list[list[int]] = []
        current: list[str] = []
        current_owners: list[int] = []
        size = 0

        def _flush() -> None:
            nonlocal current, current_owners, size
            if current:
                requests_out.append(current)
                piece_owners.append(current_owners)
            current, current_owners, size = [], [], 0

        for unit_index, unit in enumerate(units):
            for piece in unit:
                piece_size = len(piece.encode("utf-8"))
                if current and (
                    len(current) >= self.batch_items
                    or size + piece_size > DEEPL_MAX_BATCH_BYTES
                ):
                    _flush()
                current.append(piece)
                current_owners.append(unit_index)
                size += piece_size
        _flush()

        translated_units: list[str | None] = [None] * len(units)

        def worker(_position: int, request_index: int) -> tuple[list[str], int]:
            """只负责把这一次请求的结果取回来，不碰任何共享状态。"""
            return self._translate_request(requests_out[request_index], ctx)

        def on_done(position: int, value: object) -> None:
            nonlocal resolved
            if value is None:  # 失败：on_error 已经出声并记过账
                return
            request_texts = requests_out[position]
            owners = piece_owners[position]
            reply, billed = value  # type: ignore[misc]
            # 用量记账：本地口径记送检字符，计费口径记 billed_characters。
            sent = sum(len(piece) for piece in request_texts)
            self.chars_sent += sent
            self.chars_billed += billed
            record_usage("deepl", sent, billed, self.current_file)
            # 按 unit 归并（同一段被拆成多 piece 时按序拼回；
            # 跨请求的拆分片要累加，不能后写覆盖先写）
            collected: dict[int, list[str]] = {}
            for unit_index, text in zip(owners, reply):
                collected.setdefault(unit_index, []).append(text)
            for unit_index, pieces_texts in collected.items():
                translated_units[unit_index] = (
                    translated_units[unit_index] or ""
                ) + "".join(pieces_texts)
            self.governor.note_success()
            self.jobs = self.governor.jobs

        def on_error(position: int, exc: BaseException) -> None:
            nonlocal resolved
            if isinstance(exc, DeepLError) and exc.fatal:
                # 403 / 456：重试无意义，整轮中止，把这句话传给用户。
                raise exc
            # 429 重试用尽 / 5xx / 网络异常：本批保留原文，其余继续。
            self.log(f"{exc}（本批保留原文，其余内容继续）")
            owners = piece_owners[position]
            failed_units = set(owners)
            for unit_index in failed_units:
                # 拆片的段只要有一个请求失败就整段保留原文：
                # 半段译文比不翻更难发现。
                translated_units[unit_index] = None
            self.failed_count += len(failed_units)
            resolved += len(failed_units)
            # 429 命中一次就把并发降回 1：付费接口明说「你太快了」，
            # 硬扛下去只会让后面的批次一起废掉。
            self.governor.note_failure(exc)
            self.jobs = self.governor.jobs
            if progress is not None:
                progress(min(resolved, len(texts)), len(texts), "")

        # 并发度 1 时 run_ordered 走串行分支，与改动前的 for 循环同构。
        # >1 时多个 HTTP 请求并行，但**结果一律按下标回填**、共享状态一律
        # 在主线程改——顺序对齐的不变量不因并发而松动。
        self.governor.announce()
        run_ordered(
            list(range(len(requests_out))),
            worker,
            jobs=self.jobs,
            stop_event=self.stop_event,
            on_done=on_done,
            on_error=on_error,
        )

        for unit_index, original_index in enumerate(indices):
            check_cancel(self.stop_event)
            masked, mapping, hits = prepared[unit_index]
            normalized = plain[unit_index]
            translated = translated_units[unit_index]
            if translated is None:
                # 该批失败：保留原文。
                results[original_index] = texts[original_index]
                continue
            if mapping:
                translated = unlock_and_restore(translated, mapping)
            final = finish_text(translated, self.style)
            if hits:
                # 术语命中只在译文被真正接受时记（v1.4.1 口径，与在线一致）。
                self.term_hits.extend(hit_rows(original_index + 1, hits, final))
            if normalize_source(final) == normalized:
                # DeepL 把原文原样送回：不算翻译成果，也不写缓存
                # （回演进缓存会永久命中，那份文档永远翻不好）。
                self.echo_count += 1
                results[original_index] = texts[original_index]
                continue
            results[original_index] = final
            self.translated_count += 1
            if self.cache is not None:
                self.cache.set("deepl", normalized, final)
            resolved += 1
            if progress is not None:
                progress(min(resolved, len(texts)), len(texts), "")
        return resolved

    def _translate_request(
        self, texts: list[str], ctx=None
    ) -> tuple[list[str], int]:
        """发一次 DeepL 翻译请求（含统一退避重试），返回（译文列表, 计费字符数）。

        重试策略走 ``core.retry`` 的统一分类，但保留付费档自己的预算：

        * 429：最多 3 次退避重试，延迟序列沿用 ``_rate_delays``（0.5/1/2 秒）；
        * 5xx / 网络异常：最多 1 次；
        * 403 / 456：``fatal``，一次都不重试（key 错或额度用完，重试只会
          让用户白等）；
        * 400 参数错：**不再重试**。改前它会跟着 5xx 一起重试一次，但参数错
          重试一万次也是同一个结果——这条由统一分类纠正，属于有意的改动。

        退避时长：429 走 ``_rate_delays`` 序列（既有口径不变）；其余重试走
        同一序列的首项（改前是 ``_pause``=1 秒，现在是 0.5 秒，差别无实质
        影响，换来的是「一处定义、处处一致」）。
        """
        payload: dict = {
            "text": list(texts),
            "target_lang": direction.deepl_code(),
            "show_billed_characters": True,
        }
        context = self._context_of(ctx)
        if context:
            # context 不计费，只作附加语境；术语一致性靠本地占位符，不依赖它。
            payload["context"] = context
        rate_retries = 0
        other_retries = 0
        max_rate = len(self._rate_delays)
        policy = BackoffPolicy(
            max_attempts=1 + max_rate,
            delays=tuple(self._rate_delays) or (0.0,),
            jitter=False,  # 付费档沿用确定序列，既有测试钉住了它
        )

        def should_retry(exc: BaseException, _attempt: int) -> bool:
            """统一分类 + 付费档自己的次数预算（429 三次，其余一次）。"""
            nonlocal rate_retries, other_retries
            if not is_retryable(exc):
                return False
            if status_of(exc) == 429:
                if rate_retries < max_rate:
                    rate_retries += 1
                    return True
                return False
            if other_retries < 1:
                other_retries += 1
                return True
            return False

        def call_once() -> tuple[list[str], int]:
            check_cancel(self.stop_event)
            try:
                response = self.post("/v2/translate", payload)
            except CancelledError:
                raise
            except DeepLError:
                raise
            except Exception as exc:
                # 连接失败 / 超时：标成可重试的网络错误。必须显式标类型——
                # 裸 TranslationError 不带状态码也不带类型标记，分类器会按
                # 保守口径拒绝重试，网络抖动就永远得不到重试。
                raise TransientError(f"无法连接 DeepL（{type(exc).__name__}）") from exc
            try:
                status = int(getattr(response, "status_code", 0) or 0)
            except (TypeError, ValueError):
                status = 0
            if status == 200:
                return self._parse_translations(response, len(texts))
            if status == 403:
                raise DeepLError(
                    "DeepL key 无效或已过期，请检查 api_keys.local.json 里的 key。",
                    403,
                    fatal=True,
                )
            if status == 456:
                raise DeepLError(
                    "本月 DeepL 免费额度已用完（Free 版每月 50 万字符），"
                    "可升级 Pro 或下月再用。",
                    456,
                    fatal=True,
                )
            # 带状态码抛出：429/5xx 会被判成可重试，4xx 判成不可重试。
            raise http_error(status, f"DeepL 返回 HTTP {status}")

        def on_retry(attempt: int, delay: float, exc: BaseException) -> None:
            status = status_of(exc)
            if status == 429:
                self.log(
                    f"DeepL 频率超限（429），{delay:.1f} 秒后重试"
                    f"（第 {attempt} 次，最多 {max_rate} 次）…"
                )
            elif status:
                self.log(f"DeepL 返回 HTTP {status}，{delay:.1f} 秒后重试…")
            else:
                self.log(
                    f"DeepL 网络异常（{type(exc).__name__}），{delay:.1f} 秒后重试…"
                )

        try:
            return retry(
                call_once,
                should_retry=should_retry,
                policy=policy,
                on_retry=on_retry,
                stop_event=self.stop_event,
                label="DeepL 请求",
            )
        except CancelledError:
            raise
        except DeepLError:
            raise
        except Exception as exc:
            # 重试耗尽：换成付费档自己的错误类型（调用方只认 DeepLError），
            # 并把状态码带上，让上层能区分「限流」与「服务不可用」。
            status = status_of(exc) or 0
            if status == 429:
                raise DeepLError("DeepL 请求过于频繁（429），请稍后重试。", 429) from exc
            if status:
                raise DeepLError(
                    f"DeepL 服务暂时不可用（HTTP {status}），请稍后重试。", status
                ) from exc
            raise DeepLError(f"{exc}，请检查网络后重试。") from exc

    def _parse_translations(self, response: object, count: int) -> tuple[list[str], int]:
        """解析 ``/v2/translate`` 响应：段数必须对齐，逐段取 text 与计费数。"""
        try:
            payload = response.json()
        except Exception as exc:
            raise DeepLError("DeepL 返回了解析不了的内容，请稍后重试。") from exc
        rows = payload.get("translations") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) != count:
            actual = len(rows) if isinstance(rows, list) else "?"
            raise DeepLError(
                f"DeepL 返回段数不符（应 {count} 段，实 {actual} 段），请稍后重试。"
            )
        texts: list[str] = []
        billed = 0
        for row in rows:
            if not isinstance(row, dict):
                raise DeepLError("DeepL 返回格式异常，请稍后重试。")
            text = str(row.get("text") or "")
            if not text.strip():
                raise DeepLError("DeepL 返回了空译文，请稍后重试。")
            texts.append(text)
            try:
                billed += int(row.get("billed_characters") or 0)
            except (TypeError, ValueError):
                billed += 0
        return texts, billed

    def _ensure_probed(self) -> None:
        """首次真正翻译前的最小探测：``text=["Hello"]``，验证 key 与域名。"""
        if self._probed:
            return
        if not self.key:
            raise TranslationError(
                "未配置 DeepL key。请先在 api_keys.local.json 里填写 deepl.key"
                "（申请地址：deepl.com/pro-api）。"
            )
        try:
            reply, billed = self._translate_request(["Hello"])
        except DeepLError as exc:
            if not exc.fatal:
                # 探测阶段的 429 / 网络错：整轮翻译无从谈起，换成探测口径
                # 的文案（原消息里的「本批」在探测场景没有意义）。
                raise TranslationError(f"DeepL 探测未通过：{exc}") from exc
            raise
        # 探测也是真会计费的请求（5 字符左右），照实记流水，对账才对得上。
        sent = len("Hello")
        self.chars_sent += sent
        self.chars_billed += billed
        record_usage("deepl", sent, billed, self.current_file or "（密钥探测）")
        self._probed = True
        tier = "Free" if self.key.endswith(":fx") else "Pro"
        self.log(f"已连接 DeepL（{tier} 档）。")

    def _context_of(self, ctx) -> str:
        """DeepL 的 context 参数：只带前文摘要，不计费、不影响翻译本身。

        术语走本地 lock/unlock（占位符机制），角色卡不进 API——付费档的
        术语一致性由本地机制保证，不依赖服务端 glossary。
        """
        if ctx is None:
            return ""
        return str(getattr(ctx, "summary", "") or "").strip()

    # ---------------------------------------------------------------- 用量

    def official_usage(self) -> dict | None:
        """``GET /v2/usage``：DeepL 账户页的真实月度用量口径。

        失败（没网 / 非 200 / 解析不了）一律返回 None——报告只显示本地
        口径，不报错：对账是附加信息，不能让它把翻译或报告搞失败。
        """
        if not self.key:
            return None
        try:
            response = self.get("/v2/usage")
            status = int(getattr(response, "status_code", 0) or 0)
            if status != 200:
                return None
            payload = response.json()
            if not isinstance(payload, dict):
                return None
            return {
                "character_count": int(payload.get("character_count")),
                "character_limit": int(payload.get("character_limit")),
            }
        except Exception:
            return None

    def usage_payload(self) -> dict:
        """自检报告「付费引擎用量」段的数据：本地口径 + 官方口径（若拿到）。"""
        month = datetime.now().strftime("%Y-%m")
        used = month_usage("deepl", month)
        return {
            "engine": "deepl",
            "session_chars": self.chars_sent,
            "session_billed": self.chars_billed,
            "month_chars": used.get("chars", 0),
            "month_billed": used.get("billed", 0),
            "month_calls": used.get("calls", 0),
            "budget": self.budget,
            "official": self.official_usage(),
        }

    def _warn_budget(self, pending_texts: list[str]) -> None:
        """本地预算预警：本月累计 + 本次预估超过预算上限时醒目警告并继续。

        只警告不拦截——用户可能买了 Pro，本地不该替云端拦。预算为 0
        （缺省）表示不设本地上限，连警告都没有。
        """
        if not self.budget:
            return
        month = datetime.now().strftime("%Y-%m")
        used = month_usage("deepl", month).get("billed", 0)
        estimate = sum(len(text) for text in pending_texts)
        if used + estimate <= self.budget:
            return
        self.log(
            f"⚠ DeepL 预算提醒：本月已计费 {used} 字符，本次预计再耗约 "
            f"{estimate} 字符，合计将超过本地预算上限 {self.budget} 字符"
            "（DeepL 账户真实额度以其账户页为准）。继续翻译可能产生超额费用。"
        )

    # ---------------------------------------------------------------- 汇总

    def _record_term_hit(
        self, paragraph: int, source: str, target: str, ok: bool
    ) -> None:
        """记一条术语命中。paragraph 是 1 起的段落号（与在线口径一致）。"""
        self.term_hits.append(
            {"paragraph": paragraph, "source": source, "target": target, "ok": ok}
        )

    def summary(self) -> str:
        text = (
            f"DeepL 翻译 {self.translated_count} 段，跳过 {self.skipped_count} 段，"
            f"缓存命中 {self.cache_hits} 段，失败 {self.failed_count} 段"
        )
        if self.term_count:
            text += f"，标题词条直译 {self.term_count} 段"
        if self.echo_count:
            text += f"，{self.echo_count} 段只返回原文"
        if self.tm_exact:
            text += f"，翻译记忆直通 {self.tm_exact} 段（完全一致，未计费）"
        if self.tm_fuzzy:
            text += f"，记忆近似复用 {self.tm_fuzzy} 段（待确认）"
        locked = intro()
        if locked:
            text += f"，{locked}"
        if self.term_hits:
            failed = sum(1 for hit in self.term_hits if not hit["ok"])
            text += f"，术语命中 {len(self.term_hits)} 处，{failed} 处未生效"
        if self.chars_billed:
            # 双口径都在：本次消耗看 chars_billed（DeepL 真收的钱），
            # 本月累计看本地流水（可能因漏记少算，官方口径见自检报告）。
            month = datetime.now().strftime("%Y-%m")
            used = month_usage("deepl", month).get("billed", 0)
            text += f"；DeepL 本次消耗 {self.chars_billed} 字符，本月累计 {used} 字符"
        return text
