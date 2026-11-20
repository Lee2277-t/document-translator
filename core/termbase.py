"""术语库状态机：跨批次、全流程追踪一个源词到底被译成了什么。

和 ``core/glossary.py`` 的区别（两者现阶段共存，互不干扰）：

* ``glossary`` 是**用户手写、一次性**的静态映射：用户填一张表，翻译前换占位符、
  译完换回来。它保证了"同一个词全篇一个译法"，但**它不知道模型自己译出了什么**。
* ``Termbase`` 是**有状态**的：它会记录模型第一次给出的译名，之後 batch 强制沿用；
  一旦发现同一个源词出现了两种译法，自动标「存疑」；并支持全局重命名后
  增量重译受影响的句子。这是术语一致性能否真正闭环的关键。

状态流转::

    [candidate] --首次采用--> [adopted] --Verifier发现冲突--> [disputed]
                                  ^                              │
                                  └──── 人工裁决 / 全局重命名 ──────┘
                                                    │
                                               [rejected]（确认不是术语）

占位符命名空间
--------------
``glossary`` 用的是 ``Zxq{n}Z``，这里用 ``Ztq{n}Z``。两者都是纯字母数字
（能 100% 穿过 NLLB / Marian 的分词器），但**前缀不同**，所以同一段文本可以先过
glossary 再过 Termbase 而不会出现 token 撞车的灵异 bug。合并两套替换 Portal 时
如果忘了这点，症状会是"术语换成了另一个术语"，且极难复现。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Callable, Iterable

from .state import StateStore, TERM_STATES

#: 见模块 docstring：与 glossary 的 Zxq 系列刻意错开。
TERM_PLACEHOLDER = "Ztq{}Z"
_TERM_PH_RE = re.compile(r"(?i)ztq(\d+)z")

_ASCII = "A-Za-z0-9"
_CYRILLIC = "\u0400-\u04ff"

#: 单次掩蔽最多处理多少条术语。注册表几千条时全部参与正则替换会拖慢长文档，
#: 这里的取舍和 glossary 一致：宁漏勿慢，但要让用户知道被截断了。
_MASK_LIMIT = 120


def _surface_pattern(surface: str) -> re.Pattern[str] | None:
    """为一个术语表层形式编译正则。

    拉丁/西里尔术语要加词边界（否则 note 会命中 denote）；中文/日文没有词边界，
    直接子串匹配。
    """
    surface = surface.strip()
    if not surface:
        return None
    first, last = surface[0], surface[-1]
    left = right = ""
    if first.isascii() and (first.isalpha() or first.isdigit()):
        left = rf"(?<![{_ASCII}])"
    if last.isascii() and (last.isalpha() or last.isdigit()):
        right = rf"(?![{_ASCII}])"
    if any("\u0400" <= char <= "\u04ff" for char in (first, last)):
        left = rf"(?<![{_CYRILLIC}])"
        right = rf"(?![{_CYRILLIC}])"
    core = r"\s+".join(re.escape(part) for part in surface.split())
    try:
        return re.compile(left + core + right, re.I)
    except re.error:
        return None


class Termbase:
    """术语库。所有状态落在 StateStore（同一个 SQLite 文件）里，跨批次存活。"""

    def __init__(
        self,
        store: StateStore | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store or StateStore()
        self.log = log or (lambda message: None)

    # ------------------------------------------------------------ 播种

    def seed_from_glossary(self) -> int:
        """把用户手写的 术语表.txt 灌进来，这些条目天然是「已采用」。

        用户愿意手写进表的术语，就是预先裁决过的结果——不应该再经历
        候选→采用 的过程，否则模型第一次给的译名会覆盖用户的权威译法。
        已存在的同名条目不会被覆盖，保证用户后续在翻译过程中积累出来的
        「这条我自己确认过」不被重播种抹掉。
        """
        from .heading_terms import USER_TERMS, load_user_terms

        load_user_terms()
        count = 0
        for source, target in USER_TERMS.items():
            source = (source or "").strip()
            target = (target or "").strip()
            if not source or not target:
                continue
            if self.store.get_term(source):
                continue
            self.store.upsert_term(source, target, state="adopted", note="来自术语表")
            count += 1
        if count:
            self.log(f"术语库：已播种 {count} 条来自 术语表.txt 的权威译名")
        return count

    # ------------------------------------------------------------ 写入

    def add_candidate(
        self, source: str, suggested: str = "", note: str = "", first_seen: str = ""
    ) -> None:
        """登记一个候选术语（通常是预处理阶段从源文抽出来的专有名词）。"""
        source = (source or "").strip()
        if not source or self.store.get_term(source):
            return
        if suggested:
            # 已经有推荐译名的，直接按「已采用」进库，免得还要走一遍确认。
            self.store.upsert_term(
                source, suggested, state="adopted", note=note, first_seen=first_seen
            )
        else:
            self.store.upsert_term(
                source, "", state="candidate", note=note, first_seen=first_seen
            )

    def adopt(self, source: str, target: str, note: str = "", first_seen: str = "") -> None:
        """确立某个源词的译名。之后所有批次强制沿用。"""
        source, target = (source or "").strip(), (target or "").strip()
        if not source or not target:
            return
        self.store.upsert_term(
            source, target, state="adopted", note=note, first_seen=first_seen
        )

    def dispute(self, source: str, reason: str = "") -> None:
        self.store.update_state(source, "disputed", note=reason)

    def reject(self, source: str, reason: str = "") -> None:
        self.store.update_state(source, "rejected", note=reason)

    def rename(self, source: str, new_target: str) -> None:
        """全局重命名。阶段 4 会用这个方法驱动受影响句子的增量重译。"""
        if not new_target:
            return
        self.store.update_target(source, new_target)
        self.store.update_state(source, "adopted", note="已人工重命名")

    # -------------------------------------------------- 一致性观测（防漂移）

    def record_observed(self, source: str, observed_target: str) -> str:
        """记录模型实际给出的译名，返回处理结论。

        * ``""``         —— 这不是库里的术语，忽略；
        * ``"consistent"``—— 与既定译名一致，正常；
        * ``"disputed"``  —— 同一个源词出现了不同译法，已自动标存疑。

        这条是"术语漂移"唯一的自动告警途径：没有它，用户只能靠肉眼比对全文。
        """
        term = self.store.get_term(source)
        if not term or term.get("state") == "rejected":
            return ""
        established = (term.get("target") or "").strip()
        observed = (observed_target or "").strip()
        if not established:
            self.store.upsert_term(source, observed, state="adopted", note="首次观察到")
            return "consistent"
        if observed == established:
            return "consistent"
        note = f"发现不同译法：已定「{established}」，本次出现「{observed}」"
        self.store.update_state(source, "disputed", note=note)
        self.log(f"术语存疑：{source} {note}")
        return "disputed"

    # ------------------------------------------------------------ 变体族

    def add_variant(self, canonical: str, variant: str) -> None:
        """登记一个变体。日文里同一个角色会被叫 桜 / 桜さん / 桜ちゃん，
        它们必须收敛到同一个标准源词，否则译名会在敬称之间反复横跳。"""
        canonical = (canonical or "").strip()
        variant = (variant or "").strip()
        if not canonical or not variant:
            return
        real_source = self.store.canonical_of(canonical)
        if not self.store.get_term(real_source):
            return
        self.store.add_variant(real_source, variant)

    def variants_of(self, source: str) -> list[str]:
        return self.store.variants_of(source)

    def canonical_of(self, surface: str) -> str:
        return self.store.canonical_of(surface)

    # ------------------------------------------------------------ 读取

    def lookup(self, source: str) -> str | None:
        term = self.store.get_term(self.store.canonical_of(source))
        if not term or term.get("state") == "rejected":
            return None
        return term.get("target") or None

    def adopted_terms(self) -> list[tuple[str, str]]:
        return [
            (term["source"], term["target"])
            for term in self.store.terms_by_state("adopted")
            if term.get("target")
        ]

    def disputed(self) -> list[dict[str, Any]]:
        return self.store.terms_by_state("disputed")

    def stats(self) -> dict[str, int]:
        counts = {state: 0 for state in TERM_STATES}
        for term in self.store.all_terms():
            state = term.get("state", "")
            counts[state] = counts.get(state, 0) + 1
        return counts

    def fingerprint(self) -> str:
        """术语库指纹，进缓存键：术语一改，旧译文必须作废。"""
        pairs = sorted(self.adopted_terms())
        raw = "\0".join(f"{source}={target}" for source, target in pairs)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------ 掩蔽

    def _entries(self) -> list[tuple[str, str]]:
        """返回（表层形式, 标准译名）列表，长词优先。"""
        entries: list[tuple[str, str]] = []
        for source, target in self.adopted_terms():
            entries.append((source, target))
            for variant in self.store.variants_of(source):
                entries.append((variant, target))
        entries.sort(key=lambda item: (-len(item[0]), item[0]))
        return entries

    def mask(self, text: str) -> tuple[str, dict[str, str]]:
        """把文中命中的术语换成占位符，返回（掩蔽文本, {占位符: 标准译名}）。

        注意：标准词与其所有变体**映射到同一个译名**，这正是变体族的意义——
        桜 / 桜さん / 桜ちゃん 都会被换成同一个占位符，译回来自然也是同一个名字。
        """
        if not text:
            return text, {}
        entries = self._entries()
        if not entries:
            return text, {}
        truncated = len(entries) > _MASK_LIMIT
        masked, mapping = text, {}
        for surface, target in entries[:_MASK_LIMIT]:
            pattern = _surface_pattern(surface)
            if pattern is None:
                continue
            token = TERM_PLACEHOLDER.format(len(mapping))
            new_text, hits = pattern.subn(token, masked)
            if hits:
                masked = new_text
                mapping[token] = target
        if truncated:
            self.log(
                f"术语掩蔽：本次有 {len(entries)} 条命中，只处理前 {_MASK_LIMIT} 条"
                f"（过多术语会拖慢长文档）"
            )
        return masked, mapping

    def unmask(self, text: str, mapping: dict[str, str]) -> str:
        """把占位符换回标准译名，并收掉模型在占位符两侧留下的空格。"""
        if not text or not mapping:
            return text
        result = _TERM_PH_RE.sub(
            lambda match: mapping.get(f"Ztq{match.group(1)}Z", match.group(0)), text
        )
        result = re.sub(r"\s+([，。；：？！、])", r"\1", result)
        result = re.sub(r"([（(])\s+|\s+([）)])", r"\1\2", result)
        result = re.sub(r"(?<=[\u4e00-\u9fff]) +(?=[\u4e00-\u9fff])", "", result)
        return result


def terms_from_text(termbase: Termbase, text: str) -> list[str]:
    """返回文本里命中的所有标准源词（去重、保持出现顺序）。给 Verifier 用。"""
    hits: list[str] = []
    for surface, _ in termbase._entries():  # noqa: SLF001 - 同模块内的读取 helper
        pattern = _surface_pattern(surface)
        if pattern is not None and pattern.search(text):
            canonical = termbase.canonical_of(surface)
            if canonical not in hits:
                hits.append(canonical)
    return hits


def extract_candidates(text: str, limit: int = 200) -> list[str]:
    """从源文里抽可能的专有名词候选。

    这是**粗筛**，目的是给 Termbase 提供候选池让用户裁决，不是要它在这一步判准——
    判准交给模型，翁这里只保证"别漏"。规则刻意保守：
    * 连续片假名（カタカナ）——人名、技能名、地名主力都是从这里来；
    * 汉字复合词里的高频重复段——需要后续按频次 threshold，这里先返回全部。
    """
    if not text:
        return []
    candidates: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"[\u30a1-\u30ff]{2,}", text):
        surface = match.group(0)
        if surface in seen:
            continue
        seen.add(surface)
        candidates.append(surface)
        if len(candidates) >= limit:
            break
    return candidates


def remember_all(termbase: Termbase, pairs: Iterable[tuple[str, str]]) -> int:
    """批量登记已确认的术语，返回实际写入条数。"""
    count = 0
    for source, target in pairs:
        if not source or not target:
            continue
        termbase.adopt(source, target)
        count += 1
    return count
