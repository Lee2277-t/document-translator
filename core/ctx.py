"""翻译上下文包：告诉引擎"这段话处在什么语境里"。

这是第二阶段以后所有质量能力的载体（术语命中、角色卡、场景摘要都从这里进提示词）。
本模块只定义数据结构与**缓存键语义**，不做提示词拼装（拼装在 quality/context_assembler，阶段 3）。

缓存键的一个关键取舍
--------------------
上下文分成两类维度：

* **稳定维度**：目标语种、风格档位、术语表指纹。这些变了，译文理应重算。
* **易变维度**：场景摘要、当前在第几个场景。这些每批次都在变。

``cache_profile()`` **故意不包含场景摘要**。否则同一句话出现在第 3 场和第 17 场，
会因为摘要不同而两次 miss，长文档翻译的缓存命中率会被打到接近零——
为了"更准一点点"把缓存彻底废掉，是明显的负优化。

角色则不一样：同一句「…行こうか」由傲娇角色和古风角色说出来，中文必须是不同的东西，
所以**在场角色 id 进缓存键**。但由于绝大多数文档段落压根没有角色信息，
文档模式拿到的还是 ``""``，不会因此产生额外的碎片化。这条决定了调试时能不能看懂，
所以写在 docstring 里而不是藏在实现里。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Any, Iterable


@dataclass(frozen=True)
class TranslationContext:
    """一次翻译请求的上下文。

    所有字段都是可选的；``TranslationContext()`` 表示"没有任何上下文"，
    此时管线行为必须与旧版本完全一致（阶段 0 的验收标准之一）。
    """

    source_lang: str = ""
    target_lang: str = ""
    style: str = ""
    #: 术语表/术语库指纹。变了就要让旧译文作废，否则改了术语库却拿到旧译文。
    terms_fingerprint: str = ""
    #: 本段在场的角色名（有序）。会进缓存键，因为角色直接影响自称/语气。
    characters: tuple[str, ...] = field(default_factory=tuple)
    #: 本批次实际命中的术语（源词, 译名）。引擎会把它们写进提示词强制沿用。
    #: 注意：缓存键靠 ``terms_fingerprint`` 区分，这里放的是内容本身。
    terms: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    #: 在场角色的人设卡（角色名, 人设描述）。含自称/对称/语气基线/禁用表达。
    character_sheets: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    #: 场景摘要。**不进缓存键**（见模块 docstring）。
    summary: str = ""
    scene_id: str = ""
    #: 段落自身的标识，报告里做定位用。
    segment_id: str = ""
    engine: str = ""
    #: 自由放大多留的钩子（比如某类文档的特有标记），不参与缓存键计算。
    extras: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    # ---------------------------------------------------------------- 查询

    def is_trivial(self) -> bool:
        """上下文是否为空——空上下文表示走旧的、逐句独立的翻译路径。"""
        return not (
            self.summary
            or self.characters
            or self.source_lang
            or self.target_lang
            or self.style
            or self.terms_fingerprint
        )

    def characters_key(self) -> str:
        if not self.characters:
            return ""
        ordered = sorted(str(name) for name in self.characters if name)
        if not ordered:
            return ""
        joined = "\0".join(ordered).encode("utf-8")
        return hashlib.sha256(joined).hexdigest()[:12]

    def cache_profile(self) -> str:
        """缓存 profile：稳定维度 + 在场角色，不含场景摘要。"""
        return "|".join(
            (
                self.target_lang,
                self.style,
                self.terms_fingerprint,
                self.characters_key(),
            )
        )

    def describe(self) -> str:
        """给日志/报告用的一句话概况，方便用户确认上下文真的生效了。"""
        parts: list[str] = []
        if self.source_lang or self.target_lang:
            parts.append(f"{self.source_lang or '?'}→{self.target_lang or '?'}")
        if self.style:
            parts.append(f"风格={self.style}")
        if self.terms_fingerprint:
            parts.append(f"术语指纹={self.terms_fingerprint[:8]}")
        if self.characters:
            shown = "、".join(list(self.characters)[:3])
            more = "…" if len(self.characters) > 3 else ""
            parts.append(f"在场角色={shown}{more}")
        if self.summary:
            parts.append(f"场景摘要={len(self.summary)}字")
        return "，".join(parts)

    # ---------------------------------------------------------------- 变更

    def with_(self, **changes: Any) -> "TranslationContext":
        """返回一个改了若干字段的新实例。上下文是不可变对象，避免被下游改脏。"""
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
            "style": self.style,
            "terms_fingerprint": self.terms_fingerprint,
            "characters": list(self.characters),
            "scene_id": self.scene_id,
            "segment_id": self.segment_id,
            "engine": self.engine,
        }


#: 空上下文常量，热路径上复用，避免反复构造。
EMPTY_CONTEXT = TranslationContext()


def context_from(
    *,
    target_lang: str = "",
    source_lang: str = "",
    style: str = "",
    terms_fingerprint: str = "",
    characters: Iterable[str] | None = None,
    summary: str = "",
    scene_id: str = "",
    segment_id: str = "",
    engine: str = "",
) -> TranslationContext:
    """便捷构造入口：字段还不存在 down the road 时不会因为拼错而静默丢参数。"""
    return TranslationContext(
        source_lang=source_lang,
        target_lang=target_lang,
        style=style,
        terms_fingerprint=terms_fingerprint,
        characters=tuple(characters or ()),
        summary=summary,
        scene_id=scene_id,
        segment_id=segment_id,
        engine=engine,
    )
