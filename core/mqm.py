"""MQM 风格的质量度量：把质量引擎发现的问题变成可读、可回归的数字。

为什么用 MQM 而不是"感觉变好了"
--------------------------------
BLEU 这类自动指标在文学性文本上会系统性低估"读起来对"的译文
（TransAgents 的 d-BLEU 低于 GPT-4 Turbo，人类偏好率却高达 77.8%）。
反过来，"我觉得顺了一点"又无法复盘。MQM 的做法是把错误按
**维度 × 严重度**归类，每个错误带权重，于是：

* 每一轮管线改动都能报告"Critical 从 7 降到 2"这种可复现的数字；
* 人工复核时知道先看哪条（Critical 优先）；
* "通过 / 存疑 / 需人工"的判定有确定规则，不靠心情。

严重度语义（与 core/quality/verifier.py 的 Issue 对应）：

* **Critical**——影响理解或回写正确性（行数不匹配、占位符丢失、主宾颠倒）；
* **Major**——实打实的错误但不致命（术语未采用、人设偏离、禁用表达）；
* **Minor**——润色级（语气稍欠，意思没错）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

#: 检查维度 → 中文名。顺序即报告里的展示顺序。
DIMENSION_LABELS: tuple[tuple[str, str], ...] = (
    ("terminology", "术语一致性"),
    ("persona", "角色语气"),
    ("agent_patient", "人称/施受"),
    ("format", "格式完整"),
    ("coherence", "叙事连贯"),
)

#: 严重度 → 展示名与排序权重。
SEVERITY_LABELS: tuple[tuple[str, str, int], ...] = (
    ("critical", "Critical", 0),
    ("major", "Major", 1),
    ("minor", "Minor", 2),
)

_SEVERITY_ORDER = {name: weight for name, _, weight in SEVERITY_LABELS}


@dataclass
class MqmSummary:
    """一批问题的汇总数字。"""

    total: int = 0
    by_severity: dict[str, int] = field(default_factory=dict)
    by_dimension: dict[str, int] = field(default_factory=dict)

    @property
    def critical(self) -> int:
        return self.by_severity.get("critical", 0)

    @property
    def major(self) -> int:
        return self.by_severity.get("major", 0)

    @property
    def minor(self) -> int:
        return self.by_severity.get("minor", 0)

    def verdict(self) -> str:
        """通过 / 存疑 / 需人工。规则确定，不靠心情。"""
        if self.critical:
            return "需人工"
        if self.major:
            return "存疑"
        return "通过"

    def one_line(self) -> str:
        parts = [
            f"{label} {self.by_severity[name]} 条"
            for name, label, _ in SEVERITY_LABELS
            if self.by_severity.get(name)
        ]
        body = "、".join(parts) if parts else "无问题"
        return f"共 {self.total} 条（{body}），结论：{self.verdict()}"


def summarize(issues: Iterable) -> MqmSummary:
    summary = MqmSummary(total=0)
    for issue in issues:
        severity = getattr(issue, "severity", "") or "minor"
        dimension = getattr(issue, "dimension", "") or "coherence"
        summary.total += 1
        summary.by_severity[severity] = summary.by_severity.get(severity, 0) + 1
        summary.by_dimension[dimension] = (
            summary.by_dimension.get(dimension, 0) + 1
        )
    return summary


def dimension_breakdown(issues: Sequence) -> list[tuple[str, int]]:
    """按维度汇总，报告里画一行小表用。顺序固定，便于逐版对比。"""
    counts: dict[str, int] = {}
    for issue in issues:
        dimension = getattr(issue, "dimension", "") or "coherence"
        counts[dimension] = counts.get(dimension, 0) + 1
    return [
        (label, counts[key])
        for key, label in DIMENSION_LABELS
        if counts.get(key)
    ]


def render_lines(issues: Sequence, limit: int = 60) -> list[str]:
    """把问题清单渲染成报告行。按严重度排序，Critical 永远在最前面。"""
    ordered = sorted(
        issues,
        key=lambda issue: (
            _SEVERITY_ORDER.get(getattr(issue, "severity", ""), 3),
            str(getattr(issue, "segment_id", "")),
        ),
    )
    lines: list[str] = []
    for issue in ordered[:limit]:
        label = dict(
            (name, display) for name, display, _ in SEVERITY_LABELS
        ).get(getattr(issue, "severity", ""), "Minor")
        dimension = dict(DIMENSION_LABELS).get(
            getattr(issue, "dimension", ""), "其他"
        )
        segment = getattr(issue, "segment_id", "") or "-"
        detail = getattr(issue, "detail", "")
        lines.append(f"[{label}] {dimension} · {segment} | {detail}")
    hidden = len(ordered) - limit
    if hidden > 0:
        lines.append(f"……另有 {hidden} 条未列出（按严重度排序，先处理上面的）")
    return lines
