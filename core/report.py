"""成品自检报告。

把「哪里必须人工看一眼」写成清单，跟成品放在一起，文件名《<成品名> 自检报告.txt》。
没疑点也写一份（「未发现需要复核的疑点」），用户才知道核对跑过、结果如何。

已落地：
- 数字疑点（number_check）、保留原文（漏翻/回显）、字号缩小、译名不一致四类汇入；
- 按严重程度分级（高/中/低）+ 按位置排序 + 条数上限。长文档不再吐几千行——
  报告一旦长到没人看，就等于没做；
- **MQM 质量段**（阶段 4）：质量引擎（core/quality）发现的术语/人设/人称/格式/
  叙事问题按 Critical/Major/Minor 分级列入，并给出「通过/存疑/需人工」结论；
  附未决术语池（候选 + 存疑），那是人工介入成本最低的地方；
- **术语命中对照表**（v1.4.1）：哪段话命中了术语表、指定译法是什么、译文里
  是否真的用上了；未生效的命中同时以「高」级疑点进入上方清单；
- **未生效疑点念出残留标记**（v1.4.2）：模型把占位符改写成怪 token
  （``Zxq0Z`` → ``ZxqOZ``）时，note 直接列出译文里残留的标记，用户不用
  自己全文 Ctrl+F 去找；
- **译名不一致带出现位置**（v1.4.2）：note 里列出该术语出现在哪些段/页，
  回答「去哪段看」；
- **付费引擎用量段**（v1.5.0，C3-a）：``--paid=deepl`` 时报告末尾展示
  「本次送检/计费字符、本月本地累计、DeepL 账户页官方口径（若拿到）、
  本地预算上限（若设置）」；没有付费引擎时整段不出现。计费口径以 DeepL
  响应的 ``billed_characters`` 为准（本地流水可能漏记少算）；
- **扫描页/图片 OCR 低置信**（v1.9.5，v1.9.8 起带位置）：OCR 自己没把握、
  低于阈值被丢掉的那些文字行不再无声消失——逐条进「OCR 识别不清」，带原文片段、
  置信度与画面内的相对位置（``drop_entries()``），一页/一张图超过 8 条时合并；
- **记忆近似复用逐条带位置**（v1.9.9）：翻译记忆按相似度复用的旧译文，
  过去只在结论行念一个「N 段（待确认）」——用户知道有几句是「抄来的」，
  却不知道是哪几句。现在逐段进报告（``fuzzy_entries()``），带相似度与
  原文片段；一段话超过 12 段时合并成一条「其余 N 段」。
- **逐行成品也有报告**（v1.9.5）：字幕（srt/vtt）、脚本（json）、T++ 表格
  （xlsx）与图片过去只有译后编辑清单、没有自检报告；现在与文档同一套判据
  （数字疑点 / 译名不一致 / 保留原文），入口是 ``lines_report()``。
"""
from __future__ import annotations

import datetime
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

REPORT_TAG = " 自检报告"

_SOURCE_LIMIT = 60
#: 超过这个条数就截断。340 页文档的数字疑点能到几百条，全列出来没人会看。
MAX_ISSUES = 120

#: 各类疑点的默认严重程度。高 = 数字/金额对不上、术语未生效，
#: 中 = 可能漏翻或译名不统一，低 = 版式与提示类。
SEVERITY = {
    "数字疑点": "高",
    "术语未生效": "高",
    "保留原文": "中",
    "译名不一致": "中",
    "OCR 识别不清": "中",
    "记忆近似复用": "中",
    "译文与原文相同": "低",
    "字号缩小": "低",
}
_SEVERITY_ORDER = {"高": 0, "中": 1, "低": 2}

#: 术语命中对照表的条数上限，与未决术语池同一个量级：全列出来没人看。
#: 未生效的命中已经以「高」级疑点的身份进了上方清单，不会被截断漏掉。
MAX_TERM_HITS = 40

#: 被 OCR 丢掉的行逐条标注位置的上限；再多合并成一条，免得把这些行淹进报告。
MAX_POSITIONED_DROPS = 8

#: 「记忆近似复用」逐段列出的上限。重译改过的稿子可能有大半段走近似复用，
#: 全列出来会把这些「待确认」淹掉，超过就合并成一条。
MAX_TM_FUZZY = 12

_LOCATION_RE = re.compile(r"(\d+)")


def box_rect(box) -> tuple[float, float, float, float] | None:
    """OCR 框（四点多边形）折成 (x0, y0, x1, y1)；形状认不出来就返回 None。"""
    try:
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
    except (TypeError, ValueError, IndexError):
        return None
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def relative_position(
    rect: tuple[float, float, float, float], size: tuple[float, float]
) -> str:
    """一块墨迹在整幅画面里的相对位置：横纵向各占宽高的百分比。

    截图与扫描页都没有可念的坐标，能指给用户的只有「在哪一块」；百分比人挑得出来，
    也不必依赖 OCR 引擎自己的坐标系（报告是给人照着找的）。
    """
    width, height = size
    x0, y0, x1, y1 = rect
    return (
        f"横向 {x0 / width:.0%}~{x1 / width:.0%}、"
        f"纵向 {y0 / height:.0%}~{y1 / height:.0%}"
    )


def drop_entries(
    dropped: Sequence[tuple[str, float, tuple[float, float, float, float] | None]],
    size: tuple[float, float],
    *,
    prefix: str = "",
    limit: int = MAX_POSITIONED_DROPS,
) -> list[tuple[str, str, str, str]]:
    """把「被 OCR 丢掉的行」变成报告条目：逐条带相对位置，超上限合并成一条。

    ``dropped`` 是（原文, 置信度, 位置矩形 或 None）；``size`` 是原图/原页的宽高，
    百分比按它折算。``prefix`` 加在位置前面（PDF 那边念「第 N 页 · 」）。
    图片与扫描件共用这一份，口径才不至于一处念百分比、一处念「整张图」。
    """
    located = sorted(
        (item for item in dropped if item[2] is not None),
        key=lambda item: (item[2][1], item[2][0]),
    )
    head = located[:limit]
    rest = located[limit:] + [item for item in dropped if item[2] is None]
    entries: list[tuple[str, str, str, str]] = []
    for position, (text, score, rect) in enumerate(head, start=1):
        entries.append(
            (
                "OCR 识别不清",
                f"{prefix}第 {position} 处（{relative_position(rect, size)}）",
                text,
                f"识别置信度 {score:.2f}，已跳过、未翻译；请对照原文该位置复核",
            )
        )
    if rest:
        sample = " / ".join(text for text, _score, _rect in rest[:3])
        entries.append(
            (
                "OCR 识别不清",
                f"{prefix}其余位置",
                sample,
                f"另有 {len(rest)} 处置信度过低的文字未逐条标注位置；请整幅复核",
            )
        )
    return entries


def fuzzy_entries(
    hits: Sequence[dict],
    *,
    location_of=None,
    limit: int = MAX_TM_FUZZY,
) -> list[tuple[str, str, str, str]]:
    """把「记忆近似复用」明细变成报告条目：逐段带相似度，超上限合并成一条。

    ``hits`` 是各引擎记的 {段号, 原文, 相似度}（见 ``core/tm.fuzzy_hit``）。
    ``location_of`` 把一条明细翻成这个前端能念的位置（逐行成品念「第 N 行」，
    文档念「段落 N」）；不传就念「段落 N」。
    """
    entries: list[tuple[str, str, str, str]] = []
    ordered = sorted(hits, key=lambda hit: hit.get("paragraph") or 0)
    for hit in ordered[:limit]:
        paragraph = hit.get("paragraph") or 0
        location = location_of(hit) if location_of is not None else f"段落 {paragraph}"
        entries.append(
            (
                "记忆近似复用",
                location,
                _clip(str(hit.get("source") or "")),
                f"沿用的是记忆里相似度 {float(hit.get('ratio') or 0):.0%} 的旧译文"
                "（并非完全一致），差异可能就是关键信息，请对照确认",
            )
        )
    rest = ordered[limit:]
    if rest:
        sample = " / ".join(str(hit.get("source") or "") for hit in rest[:3])
        entries.append(
            (
                "记忆近似复用",
                f"其余 {len(rest)} 段",
                sample,
                f"另有 {len(rest)} 段沿用了记忆里的近似旧译文，未逐条列出；"
                "这些段落同样需要复核",
            )
        )
    return entries


def _clip(text: str, limit: int = _SOURCE_LIMIT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class ReportIssue:
    kind: str  # 数字疑点 / 保留原文 / 译名不一致 / 译文与原文相同 / 字号缩小
    location: str  # 「第 N 页」或「段落 N」
    source: str  # 原文片段（已截断）
    note: str = ""
    severity: str = ""  # 留空则按 kind 查 SEVERITY

    def level(self) -> str:
        return self.severity or SEVERITY.get(self.kind, "中")

    def sort_key(self) -> tuple:
        match = _LOCATION_RE.search(self.location)
        place = int(match.group(1)) if match else 10**9
        return (_SEVERITY_ORDER.get(self.level(), 1), place)


@dataclass
class CheckReport:
    issues: list[ReportIssue] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def add(
        self,
        kind: str,
        location: str,
        source: str,
        note: str = "",
        severity: str = "",
    ) -> None:
        self.issues.append(ReportIssue(kind, location, _clip(source), note, severity))

    @property
    def count(self) -> int:
        return len(self.issues)


def _report_path(output_path: str) -> str:
    stem, extension = os.path.splitext(output_path)
    return stem + REPORT_TAG + ".txt"


def _failed_hit_note(row: dict) -> str:
    """「术语未生效」疑点的 note（v1.4.2 动态化）。

    译文里残留了未还原的占位符标记（模型把 ``Zxq0Z`` 改写成 ``ZxqOZ``，
    ``unlock`` 换不回去）时，把标记念出来——用户知道术语没生效之后，
    下一个动作永远是「译文里那个怪 token 是什么」，note 要替他答了。
    没有残留时（模型可能整个删掉了占位符，译文里什么也不剩）保持原文案
    逐字不变——老测试钉住的就是这条分支。
    """
    leftover = [str(token) for token in (row.get("leftover") or [])][:3]
    if leftover:
        return (
            f"译文里残留了未还原的标记 {'、'.join(leftover)}，指定译法没有进入译文"
            "（模型可能改写或删除了占位符）"
        )
    return "指定译法没有进入译文，占位符可能被模型改写或删除"


def build_report(
    output_path: str,
    report: CheckReport,
    quality_issues: Sequence | None = None,
    pending_terms: Sequence[dict] | None = None,
    term_hits: Sequence[dict] | None = None,
    paid_usage: dict | None = None,
) -> str | None:
    """在成品旁写《<成品名> 自检报告.txt》，返回报告路径。

    ``quality_issues`` 是质量引擎（core/quality）产出的 Issue 列表；
    ``pending_terms`` 是未决术语池（候选 + 存疑）；``term_hits`` 是翻译器
    记录的术语命中明细（{段落号, 原文术语, 指定译法, 是否生效}）；
    ``paid_usage`` 是付费引擎（DeepL）的用量字典（``usage_payload()``
    的返回值，见 ``core/deepl.py``）。四者可选——不带这些属性的引擎，
    报告里就不出现对应段落。写盘失败静默返回 None——自检报告是辅助物，
    不能因为写不进去让翻译整体失败。

    「记忆近似复用」不在这里做：它要计入成品旁的结论行「N 条疑点」，
    各前端在自己那份 CheckReport 上用 ``fuzzy_entries()`` 加，
    这样日志里念的条数与报告里列的对得上。
    """
    try:
        path = _report_path(output_path)
        issues = sorted(report.issues, key=ReportIssue.sort_key)
        rows = list(term_hits or [])
        if rows:
            # 未生效的命中必须同时是一条疑点：那是用户必须人工看一眼的
            # 地方，不能只躺在对照表里等人自己翻。
            for row in rows:
                if row.get("ok", True):
                    continue
                issues.append(
                    ReportIssue(
                        "术语未生效",
                        f"段落 {row.get('paragraph', 0)}",
                        _clip(str(row.get("source", ""))),
                        _failed_hit_note(row),
                        "高",
                    )
                )
            issues = sorted(issues, key=ReportIssue.sort_key)

        by_level: dict[str, int] = {}
        for issue in issues:
            by_level[issue.level()] = by_level.get(issue.level(), 0) + 1
        summary = "、".join(
            f"{level} {by_level[level]} 条"
            for level in ("高", "中", "低")
            if level in by_level
        )

        lines = [
            f"《{os.path.basename(output_path)}》自检报告",
            f"生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M}",
            "",
        ]
        if not issues:
            lines.append("未发现需要复核的疑点。")
        else:
            lines.append(f"共 {len(issues)} 条疑点（{summary}），建议逐条人工复核。")
            lines.append("")
            shown = issues[:MAX_ISSUES]
            for issue in shown:
                line = f"[{issue.level()}] {issue.kind} · {issue.location}"
                if issue.source:
                    line += f" | {issue.source}"
                if issue.note:
                    line += f"（{issue.note}）"
                lines.append(line)
            hidden = len(issues) - len(shown)
            if hidden > 0:
                lines.append("")
                lines.append(
                    f"另有 {hidden} 条低优先级疑点未列出，"
                    f"处理完上面这些再重新翻译即可看到。"
                )
        lines.extend(_term_hits_section(rows))
        lines.extend(
            _quality_section(quality_issues or [], pending_terms or [])
        )
        lines.extend(_paid_usage_section(paid_usage))
        lines.append("")
        lines.append("说明：本报告只标出「值得人眼看一遍」的位置，不代表译文有误。")
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
    except OSError:
        return None


def quality_payload(translator) -> tuple[list, list]:
    """从翻译器对象里取出（质量问题, 未决术语）。

    只有本地大模型档（LocalLLMTranslator）带质量引擎；云端/离线小模型
    没有这些属性，返回两个空列表——报告里就不出现 MQM 段。
    用 getattr 而不是 isinstance：翻译器实现可以替换，报告不该因此崩。
    """
    issues = list(getattr(translator, "quality_issues", None) or [])
    pending: list = []
    getter = getattr(translator, "pending_terms", None)
    if callable(getter):
        try:
            pending = list(getter() or [])
        except Exception:
            pending = []
    return issues, pending


def term_hits_of(translator) -> list[dict]:
    """从翻译器对象里取出术语命中明细。

    在线 ``Translator`` 与保密 ``LocalTranslator`` 会记 ``term_hits``；
    本地大模型档的术语校验走质量引擎（Ztq 占位符 + Verifier），没有这个
    属性，返回空列表——报告里就不出现「术语命中对照」段。
    用 getattr 而不是 isinstance：翻译器实现可以替换，报告不该因此崩。
    """
    return list(getattr(translator, "term_hits", None) or [])


def tm_fuzzy_of(translator) -> list[dict]:
    """从翻译器对象里取出「记忆近似复用」明细（{段号, 原文, 相似度}）。

    四档（云端 / 离线 / 本地大模型 / 付费）在 v1.9.9 起都记这份明细；
    保密模式不接记忆，列表恒为空。用 getattr 而不是 isinstance：
    翻译器实现可以替换，报告不该因此崩。
    """
    return list(getattr(translator, "tm_fuzzy_hits", None) or [])


def paid_usage_of(translator) -> dict | None:
    """从翻译器对象里取出付费引擎（DeepL）用量字典。

    只有 ``DeepLTranslator`` 带 ``usage_payload()``；免费云端 / 本地大模型 /
    离线小模型三档返回 None——报告里就不出现「付费引擎用量」段。
    官方口径（GET /v2/usage）由 ``usage_payload`` 内部取用，失败时只给
    本地口径；这里再包一层 try：对账是附加信息，不能让它把报告搞失败。
    用 getattr 而不是 isinstance：翻译器实现可以替换，报告不该因此崩。
    """
    getter = getattr(translator, "usage_payload", None)
    if not callable(getter):
        return None
    try:
        payload = getter()
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def lines_report(
    output_path: str,
    pairs: Sequence[tuple[str, str]],
    locations: Sequence[str],
    translator=None,
    *,
    kept: Sequence[tuple[str, str, str]] = (),
    extra: Sequence[tuple[str, str, str, str]] = (),
) -> tuple[str | None, int]:
    """给「逐行/逐块」成品（字幕、脚本、表格、图片）出自检报告。

    文档四格式（docx / pdf / pptx / epub）各有各的报告拼法，但它们的判据是同一套：
    数字疑点、译名不一致、保留原文。字幕与脚本过去只有译后编辑清单、没有报告——
    翻译一行台词和翻译一段正文，用户要复核的东西是一样的。

    ``pairs`` 是（原文, 译文）序列，``locations`` 与它等长（「块 #3（00:01:02）」
    「第 12 行」这类人能照着找的位置）。``kept`` 是（位置, 原文, 说明）三元组，
    用于调用方已经知道「这段落回原文」的情形（如字幕行数对不上）；``extra`` 是
    （疑点类别, 位置, 原文, 说明），给 OCR 识别不清这类只在个别前端发生的事。

    返回（报告路径 或 None, 疑点数）。写盘失败时路径为 None——自检报告是辅助物，
    不能因为写不进去让翻译整体失败（与 ``build_report`` 同一口径）。
    """
    from .number_check import check_numbers
    from .term_check import check_term_consistency, issue_note

    report = CheckReport()
    for (source, translated), location in zip(pairs, locations):
        for issue in check_numbers(source, translated):
            report.add("数字疑点", location, issue.source, issue.detail)
    for issue in check_term_consistency(list(pairs), list(locations)):
        report.add("译名不一致", "全文", issue.term, issue_note(issue))
    for location, source, note in kept:
        report.add("保留原文", location, source, note)
    for kind, location, source, note in extra:
        report.add(kind, location, source, note)
    # 近似复用（v1.9.9）：逐行成品的位置就是 `locations` 里那一行自己的写法，
    # 不让用户拿「段落 37」去字幕里数。
    for kind, location, source, note in fuzzy_entries(
        tm_fuzzy_of(translator),
        location_of=lambda hit: _hit_location(hit, locations),
    ):
        report.add(kind, location, source, note)

    quality_issues, pending_terms = quality_payload(translator)
    path = build_report(
        output_path,
        report,
        quality_issues,
        pending_terms,
        term_hits_of(translator),
        paid_usage_of(translator),
    )
    return path, report.count


def _hit_location(hit: dict, locations: Sequence[str]) -> str:
    """把段号翻成这一行自己的位置写法；段号越界退回「段落 N」。"""
    try:
        paragraph = int(hit.get("paragraph") or 0)
    except (TypeError, ValueError):
        paragraph = 0
    if 1 <= paragraph <= len(locations):
        return locations[paragraph - 1]
    return f"段落 {paragraph}"


def _term_hits_section(term_hits: Sequence[dict]) -> list[str]:
    """术语命中对照：哪段话命中了术语表、指定译法是什么、译文里用上了没有。

    没有任何命中时返回空列表（不占版面）——用户没填术语表时，
    报告不该多出一段空标题。
    """
    if not term_hits:
        return []
    failed = sum(1 for row in term_hits if not row.get("ok", True))
    lines = [
        "",
        "── 术语命中对照 ──",
        f"共 {len(term_hits)} 处命中术语表，{failed} 处未生效。",
    ]
    for row in term_hits[:MAX_TERM_HITS]:
        paragraph = row.get("paragraph", 0)
        source = row.get("source", "")
        target = row.get("target", "") or "（待定）"
        line = f"  段落 {paragraph} | {source} → {target}"
        if not row.get("ok", True):
            line += "  [!] 译文未含指定译法"
        lines.append(line)
    hidden = len(term_hits) - MAX_TERM_HITS
    if hidden > 0:
        note = "；未生效的已进入上方疑点清单" if failed else ""
        lines.append(f"  ……另有 {hidden} 处未列出{note}")
    return lines


def _paid_usage_section(paid_usage: dict | None) -> list[str]:
    """付费引擎（DeepL）用量段。没有付费引擎时整段不出现（不占版面）。

    双口径展示：本地流水（送检/计费分别累计，可能因漏记少算）与 DeepL
    账户页官方口径（``GET /v2/usage``，真收的钱）。官方口径没拿到时
    只显示本地口径——usage 接口失败不该让报告缺一块或直接报错。
    """
    if not paid_usage:
        return []
    session_chars = int(paid_usage.get("session_chars", 0) or 0)
    session_billed = int(paid_usage.get("session_billed", 0) or 0)
    month_chars = int(paid_usage.get("month_chars", 0) or 0)
    month_billed = int(paid_usage.get("month_billed", 0) or 0)
    budget = int(paid_usage.get("budget", 0) or 0)
    lines = [
        "",
        "── 付费引擎用量 ──",
        f"本次：送检 {session_chars} 字符，DeepL 计费 {session_billed} 字符。",
        f"本月本地累计：送检 {month_chars} 字符，计费 {month_billed} 字符。",
    ]
    official = paid_usage.get("official")
    if isinstance(official, dict):
        lines.append(
            f"DeepL 账户页显示：{official.get('character_count', 0)} / "
            f"{official.get('character_limit', 0)} 字符（官方口径）。"
        )
    if budget:
        lines.append(f"本地预算上限：{budget} 字符/月（超限仅警告，不拦截）。")
    return lines


def _quality_section(quality_issues: Sequence, pending_terms: Sequence[dict]) -> list[str]:
    """MQM 质量段 + 未决术语池。没有任何内容时返回空列表（不占版面）。"""
    if not quality_issues and not pending_terms:
        return []
    from .mqm import dimension_breakdown, render_lines, summarize

    lines = ["", "── 质量引擎（MQM）──"]
    if quality_issues:
        summary = summarize(quality_issues)
        lines.append(summary.one_line())
        breakdown = dimension_breakdown(list(quality_issues))
        if breakdown:
            detail = "、".join(f"{name} {count}" for name, count in breakdown)
            lines.append(f"按维度：{detail}")
        lines.append("")
        lines.extend(render_lines(list(quality_issues)))
    else:
        lines.append("质量引擎未发现问题。")
    if pending_terms:
        lines.append("")
        lines.append("未决术语（人工裁决后重命名，可触发受影响段落增量重译）：")
        for row in pending_terms[:40]:
            source = row.get("source", "")
            target = row.get("target", "") or "（待定）"
            state = "存疑" if row.get("state") == "disputed" else "候选"
            note = row.get("note", "")
            suffix = f"（{note}）" if note else ""
            lines.append(f"  [{state}] {source} → {target}{suffix}")
    return lines
