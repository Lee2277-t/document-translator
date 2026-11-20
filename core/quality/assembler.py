"""分层上下文装配：把有限的上下文预算当作可调度资源来分配。

为什么必须分层
--------------
SakuraLLM 建议单线程上下文不小于 1536 tokens，但 1536 tokens 装不下
完整术语库 + 全部角色卡 + 前文摘要。如果不做预算分配，常见做法有两种，
都是错的：① 全量塞然后被服务端静默截断（用户以为注入了，其实没有）；
② 手工挑几条，挑哪条全凭当时的心情，不可复现。

分层规则（优先级从高到低，预算不足时从低往高砍）：

1. **命中术语**——只注入与当前批次源文有字符串匹配的术语。
   一个 1000 条的术语库，单批次实际命中的通常只有 5–15 条。
   违反了就是实打实的错译，优先级最高。
2. **在场角色卡**——只注入本批次的说话人与被提及角色。
3. **前文摘要**——前几个场景的滚动记忆。只影响行文连贯，优先级最低。
4. **原文**——剩下的预算全给原文，这是翻译的原料，永远不能砍。

场景摘要默认不调模型生成，而是取上一个场景的尾部原文（verbatim tail）：
零额外推理开销，且给模型的正是最直接的语篇衔接。需要真摘要时
可以换 SummaryProvider（阶段 4 的人回路里做）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..ctx import TranslationContext
from ..glossary import in_source

#: 默认预算（字符数，不是 token）。中文约 1 字 1 token，留点余量。
DEFAULT_BUDGET = 1200

#: 单批最多注入多少条命中术语。再多就说明术语库过大，应该先清理。
MAX_TERMS_PER_BATCH = 40


@dataclass
class AssemblyResult:
    """一次装配的结果。``context`` 直接给引擎，``stats`` 给日志/报告。"""

    context: TranslationContext
    block: str
    stats: dict[str, int]


class ContextAssembler:
    """按预算装配翻译上下文。术语来源与角色来源都可注入，便于测试。"""

    def __init__(
        self,
        termbase: Any = None,
        character_store: Any = None,
        budget: int = DEFAULT_BUDGET,
        max_terms: int = MAX_TERMS_PER_BATCH,
        tail_chars: int = 400,
    ) -> None:
        self.termbase = termbase
        self.character_store = character_store
        self.budget = budget
        self.max_terms = max_terms
        self.tail_chars = tail_chars
        self._names_cache: tuple[str, ...] | None = None

    # ---------------------------------------------------------------- 术语

    def matched_terms(
        self, sources: Sequence[str]
    ) -> list[tuple[str, str]]:
        """当前批次源文里真正命中的术语（源词, 译名），保持术语库顺序。

        命中判定走 ``glossary.in_source``（v1.6.4，G2）：原来这里写的是
        ``source in joined`` 纯子串，与正文锁定的词边界口径不同，``note``
        会在 ``denote`` 里「命中」并挤掉真正该注入的术语名额。
        """
        if self.termbase is None:
            return []
        joined = "\n".join(sources)
        hits: list[tuple[str, str]] = []
        for source, target in self.termbase.adopted_terms():
            if source and target and in_source(source, joined):
                hits.append((source, target))
            if len(hits) >= self.max_terms:
                break
        return hits

    # ---------------------------------------------------------------- 角色

    def present_characters(
        self, sources: Sequence[str], speakers: Iterable[str] = ()
    ) -> tuple[str, ...]:
        """在场角色：本批说话人 + 源文里被提及的角色名。"""
        if self.character_store is None:
            return ()
        joined = "\n".join(sources)
        names: list[str] = []
        for name in speakers:
            if name and name not in names:
                names.append(name)
        for name in self._known_names():
            if name and name not in names and name in joined:
                names.append(name)
        return tuple(names)

    def _known_names(self) -> tuple[str, ...]:
        """角色名缓存：assemble 是逐场景调的，不能每次都查库。"""
        if self._names_cache is None:
            try:
                self._names_cache = tuple(self.character_store.all())
            except Exception:
                self._names_cache = ()
        return self._names_cache

    # ---------------------------------------------------------------- 装配

    def assemble(
        self,
        sources: Sequence[str],
        speakers: Sequence[str] = (),
        summary: str = "",
        style: str = "",
        source_lang: str = "",
        target_lang: str = "zh",
        scene_id: str = "",
    ) -> AssemblyResult:
        terms = self.matched_terms(sources)
        characters = self.present_characters(sources, speakers)
        sheets: tuple[tuple[str, str], ...] = ()
        if self.character_store is not None and characters:
            sheets = tuple(self.character_store.present_sheets(characters))

        lines: list[str] = []
        used = 0
        if terms:
            pairs = "；".join(f"{source} → {target}" for source, target in terms)
            line = f"必须使用的译名（违反即为错译）：{pairs}"
            lines.append(line)
            used += len(line)
        for name, rendered in sheets:
            line = f"【{name}】{rendered}"
            if used + len(line) > self.budget:
                break  # 角色卡也超预算：砍后面的，摘要本来就更可牺牲
            lines.append(line)
            used += len(line)
        summary_text = (summary or "").strip()
        if summary_text and used + len(summary_text) + 6 <= self.budget:
            lines.append(f"前文摘要：{summary_text}")
            used += len(summary_text) + 6

        fingerprint = ""
        if self.termbase is not None:
            try:
                fingerprint = self.termbase.fingerprint()
            except Exception:
                fingerprint = ""

        context = TranslationContext(
            source_lang=source_lang,
            target_lang=target_lang,
            style=style,
            terms_fingerprint=fingerprint,
            characters=characters,
            terms=tuple(terms),
            character_sheets=sheets,
            summary=summary_text if lines and lines[-1].startswith("前文摘要") else "",
            scene_id=scene_id,
        )
        block = ""
        if lines:
            block = "参考上下文（这些背景不要译出来）：\n" + "\n".join(lines)
        stats = {
            "terms": len(terms),
            "characters": len(characters),
            "sheets": len(sheets),
            "summary": len(context.summary),
            "budget_used": used,
        }
        return AssemblyResult(context=context, block=block, stats=stats)

    # ---------------------------------------------------------------- 摘要

    def rolling_summary(self, previous_scene_texts: Sequence[str]) -> str:
        """默认摘要来源：上一场景的尾部原文。零推理开销的直接语篇衔接。"""
        if not previous_scene_texts:
            return ""
        tail = "\n".join(previous_scene_texts)[-self.tail_chars :]
        return tail.strip()
