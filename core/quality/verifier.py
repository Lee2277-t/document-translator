"""校验器：规则层先行，LLM 层只补规则判不了的。

优先级原则（写死在架构里，不是建议）
------------------------------------
**能用规则判的绝不麻烦模型**。规则层零成本、确定性、可复现；
LLM 层每一次调用都是用户的等待时间，只在两种情况下出动：
① 规则层已经发现问题（带着问题去复核）；② 调用方开了抽样率。

行内一致性、术语命中、占位符完整、行数完整都是确定性问题——译文里
有没有出现该出现的译名，是字符串包含关系，不需要"智能"。真正需要
判断力的是人设偏离与施受关系，这两样交给 LLM 层，且要求结构化输出。

LLM 层的输出解析必须防御性：模型经常不按格式回答。解析失败时
**不产生问题**（而不是抛异常或猜测）——校验层的失败不能拖垮翻译。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..glossary import in_source, target_present

#: 严重度。Critical 是"影响理解或玩法"的红线，Minor 是润色级。
SEVERITIES = ("critical", "major", "minor")

#: 检查维度。与 MQM 报告的分类一一对应（阶段 4 直接用）。
DIMENSIONS = (
    "terminology",
    "persona",
    "agent_patient",
    "format",
    "coherence",
)


@dataclass
class Issue:
    """一条质量问题。severity/dimension 取值见模块常量。"""

    severity: str
    dimension: str
    segment_id: str
    detail: str
    source: str = ""
    translation: str = ""

    def is_blocking(self) -> bool:
        return self.severity == "critical"

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "dimension": self.dimension,
            "segment_id": self.segment_id,
            "detail": self.detail,
            "source": self.source,
            "translation": self.translation,
        }


@dataclass
class VerificationReport:
    """一段/一批的校验结果。"""

    issues: list[Issue] = field(default_factory=list)
    checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues

    def by_severity(self, severity: str) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity == severity]

    def merge(self, other: "VerificationReport") -> None:
        self.issues.extend(other.issues)
        self.checked += other.checked


# ---------------------------------------------------------------- 规则层


class RuleVerifier:
    """确定性校验：术语命中 / 占位符完整 / 行数完整 / 人称粗检 / 禁用表达。"""

    def __init__(
        self,
        termbase: Any = None,
        character_store: Any = None,
        check_line_count: bool = False,
    ) -> None:
        self.termbase = termbase
        self.character_store = character_store
        #: 注意属性名不能叫 check_line_count——那会把这个方法本身遮蔽掉，
        #: 调用时变成 'bool' object is not callable。踩过一次，写在这里。
        self.line_count_enabled = check_line_count
        #: 术语表与角色名在一次翻译运行里基本不变，但 verify 是逐段调的——
        #: 不缓存的话一千段就是一千次 SQL 查询。写操作（adopt/rename）
        #: 都发生在运行外，缓存整个运行期有效。
        self._terms_cache: list[tuple[str, str]] | None = None
        self._names_cache: tuple[str, ...] | None = None

    def _adopted_terms(self) -> list[tuple[str, str]]:
        if self._terms_cache is None:
            if self.termbase is None:
                self._terms_cache = []
            else:
                self._terms_cache = list(self.termbase.adopted_terms())
        return self._terms_cache

    def _character_names(self) -> tuple[str, ...]:
        if self._names_cache is None:
            if self.character_store is None:
                self._names_cache = ()
            else:
                self._names_cache = tuple(self.character_store.all())
        return self._names_cache

    # ------------------------------------------------------------ 单项检查

    def check_terms(
        self, segment_id: str, source: str, translation: str
    ) -> list[Issue]:
        """源文里命中的已采用术语，译文里必须出现对应译名。

        两个判定都收到 ``core.glossary`` 那一处（v1.6.4，G2）：命中要带词边界
        并认屈折（原来写 ``term_source in source``，``note`` 会在 ``denote`` 里
        命中并报出假问题），译名是否在场要对润色插的空格免疫（「AI 技术」
        是排版结果，不是术语丢失）。
        """
        if self.termbase is None:
            return []
        issues: list[Issue] = []
        for term_source, target in self._adopted_terms():
            if not in_source(term_source, source):
                continue
            if not target_present(target, translation):
                issues.append(
                    Issue(
                        severity="major",
                        dimension="terminology",
                        segment_id=segment_id,
                        detail=f"术语「{term_source}」应译为「{target}」，译文中未出现",
                        source=source,
                        translation=translation,
                    )
                )
        return issues

    def check_placeholders(
        self, segment_id: str, source: str, translation: str
    ) -> list[Issue]:
        """占位符丢失是 Critical：它意味着术语/格式标记被模型吃掉了。"""
        source_tokens = set(_find_placeholders(source))
        if not source_tokens:
            return []
        translation_tokens = set(_find_placeholders(translation))
        missing = source_tokens - translation_tokens
        if not missing:
            return []
        return [
            Issue(
                severity="critical",
                dimension="format",
                segment_id=segment_id,
                detail=f"占位符丢失：{'、'.join(sorted(missing))}",
                source=source,
                translation=translation,
            )
        ]

    def check_line_count(
        self, segment_id: str, source: str, translation: str
    ) -> list[Issue]:
        """脚本模式专用：行数必须一致，否则回写时会对错行。"""
        if not self.line_count_enabled:
            return []
        source_lines = [line for line in source.splitlines() if line.strip()]
        translation_lines = [line for line in translation.splitlines() if line.strip()]
        if len(source_lines) == len(translation_lines):
            return []
        return [
            Issue(
                severity="critical",
                dimension="format",
                segment_id=segment_id,
                detail=(
                    f"行数不一致：原文 {len(source_lines)} 行，"
                    f"译文 {len(translation_lines)} 行"
                ),
                source=source,
                translation=translation,
            )
        ]

    def check_pronouns(
        self, segment_id: str, source: str, translation: str, speaker: str = ""
    ) -> list[Issue]:
        """人称粗检：整段只涉及一个男性角色时，不应出现「她」指代他。

        刻意保守：多角色同段时人称可能分属不同人，规则层分不清，
        那种情况交给 LLM 层。宁可漏报，不可误报——误报会训练用户忽略报告。
        """
        if self.character_store is None:
            return []
        mentioned = [
            name for name in self._character_names() if name and name in source
        ]
        if len(mentioned) != 1:
            return []
        sheet = self.character_store.get(mentioned[0])
        if sheet is None or sheet.gender != "male":
            return []
        if "她" in translation:
            return [
                Issue(
                    severity="major",
                    dimension="agent_patient",
                    segment_id=segment_id,
                    detail=(
                        f"本段仅涉及角色「{mentioned[0]}」（设定为男性），"
                        "译文中出现「她」，疑似人称错误"
                    ),
                    source=source,
                    translation=translation,
                )
            ]
        return []

    def check_forbidden(
        self, segment_id: str, source: str, translation: str, speakers: Sequence[str] = ()
    ) -> list[Issue]:
        """在场角色的禁用表达出现在译文里——人设违规的确定性证据。"""
        if self.character_store is None or not speakers:
            return []
        issues: list[Issue] = []
        for name in speakers:
            sheet = self.character_store.get(name)
            if sheet is None:
                continue
            for word in sheet.violations(translation):
                issues.append(
                    Issue(
                        severity="major",
                        dimension="persona",
                        segment_id=segment_id,
                        detail=f"角色「{name}」的禁用表达「{word}」出现在译文中",
                        source=source,
                        translation=translation,
                    )
                )
        return issues

    # ------------------------------------------------------------ 汇总

    def verify(
        self,
        segment_id: str,
        source: str,
        translation: str,
        speaker: str = "",
        speakers: Sequence[str] = (),
    ) -> list[Issue]:
        issues: list[Issue] = []
        issues.extend(self.check_placeholders(segment_id, source, translation))
        issues.extend(self.check_line_count(segment_id, source, translation))
        issues.extend(self.check_terms(segment_id, source, translation))
        issues.extend(self.check_pronouns(segment_id, source, translation, speaker))
        issues.extend(self.check_forbidden(segment_id, source, translation, speakers))
        return issues


def _find_placeholders(text: str) -> list[str]:
    """找出文本里的占位符 token。

    两类都认：``Zxq0Z`` / ``Ztq12Z`` 系（术语锁定用）。模式要求
    首尾都是字母（``Z…Z``），这样 ``X1``、``A4纸`` 这类正常写法不会被
    误认成占位符——误报一条 Critical 就足以让用户不再看报告。
    """
    if not text:
        return []
    return re.findall(r"\b[ZX][A-Za-z]{1,4}\d+[A-Za-z]{1,4}\b", text)


# ---------------------------------------------------------------- LLM 层

_LLM_SYSTEM = (
    "你是翻译质量审校。用户给出原文、译文和角色设定，"
    "只判断两类问题：\n"
    "1. 人设偏离：译文的语气/自称/称谓是否符合角色设定；\n"
    "2. 施受关系：主语宾语是否颠倒、代词指代是否明确。\n"
    "严格按 JSON 输出：{\"issues\": [{\"type\": \"persona|agent_patient\","
    " \"severity\": \"critical|major|minor\", \"detail\": \"中文说明\"}]}。\n"
    "没有发现问题就输出 {\"issues\": []}，不要输出任何其他内容。"
)


class LLMVerifier:
    """LLM 校验层。engine 需要满足 ``translate(text, ctx) -> str`` 接口。"""

    def __init__(self, engine: Any, character_store: Any = None) -> None:
        self.engine = engine
        self.character_store = character_store

    def _build_prompt(
        self,
        source: str,
        translation: str,
        speakers: Sequence[str],
        rule_issues: Sequence[Issue],
    ) -> str:
        parts = [f"原文：{source}", f"译文：{translation}"]
        if speakers and self.character_store is not None:
            for name in speakers:
                sheet = self.character_store.get(name)
                if sheet is not None and not sheet.is_empty():
                    parts.append(f"角色「{name}」设定：{sheet.to_prompt()}")
        if rule_issues:
            listed = "；".join(issue.detail for issue in rule_issues)
            parts.append(f"规则检查已发现的问题（请重点复核）：{listed}")
        parts.append("请输出审校结论 JSON。")
        return "\n".join(parts)

    def verify(
        self,
        segment_id: str,
        source: str,
        translation: str,
        speakers: Sequence[str] = (),
        rule_issues: Sequence[Issue] = (),
    ) -> list[Issue]:
        """调一次模型做审校。任何失败都返回空列表——校验不拖垮翻译。"""
        if self.engine is None:
            return []
        prompt = self._build_prompt(source, translation, speakers, rule_issues)
        try:
            raw = self.engine.translate(prompt, None)
        except Exception:
            return []
        return self._parse(segment_id, raw, source, translation)

    def _parse(
        self, segment_id: str, raw: str, source: str, translation: str
    ) -> list[Issue]:
        text = (raw or "").strip()
        # 模型爱把 JSON 包在 ```json 里，先剥掉
        fenced = re.search(r"\{.*\}", text, re.S)
        if not fenced:
            return []
        try:
            data = json.loads(fenced.group(0))
        except Exception:
            return []
        rows = data.get("issues") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return []
        issues: list[Issue] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            kind = str(row.get("type") or "").strip()
            if kind not in ("persona", "agent_patient"):
                continue
            severity = str(row.get("severity") or "minor").strip()
            if severity not in SEVERITIES:
                severity = "minor"
            detail = str(row.get("detail") or "").strip()
            if not detail:
                continue
            issues.append(
                Issue(
                    severity=severity,
                    dimension=kind,
                    segment_id=segment_id,
                    detail=detail,
                    source=source,
                    translation=translation,
                )
            )
        return issues
