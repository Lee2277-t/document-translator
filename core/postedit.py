"""译后编辑最小闭环（v1.5.4，E2）：疑点定位 + 改段重译 + 回写不坏版式。

翻译完成时，各管线在自检报告旁边额外落一份《原名 译后编辑清单.json》：
每个译成段一条（位置 + 原文 + 译文 + 挂在这段上的疑点）。使用者在界面里
逐条看疑点、手改译文或让引擎重译，保存时把新译文写回成品对应段落——
**只改文字、不动格式**，源文件自始至终不被触碰。

设计口径（与交接稿 §11.2 对齐，勿推翻）：

- **文本匹配为主、索引为兜底**：成品里的译文段按「当前文字 == 上次写回值
  （``applied``）」定位，索引（locator）只在多处文字相同时消歧；对不上就
  跳过并出声，**绝不盲写**——回写错段落比不回写严重得多。
- **PDF 不支持回写**：成品是绘制文本，没有可寻址段落。清单照给（疑点定位
  靠页码），写回时明确告知，不静默失败。
- **脚注/尾注、页眉页脚本轮不进清单**：它们不在正文段落走查范围内，疑点
  仍会出现在自检报告里。
- **改过的段会传播**（v1.5.6，F3）：``tm`` 非空时，写回成功的人工译文以
  ``origin="human"`` + ``confirmed=1`` 记进翻译记忆，相同原文在其他位置
  出现时直接复用——这是「确认一次、全文生效」的唯一写入口，理由是人工
  拍板的译文比任何引擎自检都可信。近似匹配（<100%）命中的记忆只应用不
  回写，避免把近似句的差异复印出境。
- **单段重译不带 hint**：跨引擎没有统一的注入口（``TranslationContext.extras``
  是缓存向字段、没有引擎消费），本期「重译」= 同档位引擎重摇一次，倾向性
  调整用手改框。如实记录为后续项。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from . import formats
from .common import TranslationError, shown_path
from .translator import normalize_source

#: 清单文件名后缀。与自检报告（`` 自检报告.txt``）同一风格，成品旁边一眼成套。
EDIT_LIST_TAG = " 译后编辑清单"

#: 每种成品格式在清单里的 kind（v1.9.11 起由 core/formats.py 登记表派生）。
_KIND_BY_SUFFIX = formats.kind_by_suffix()

#: 回写支持者。PDF 是绘制文本，没有可寻址段落（见模块 docstring）。
_SUPPORTED_SUFFIXES = formats.writable_suffixes()

_PDF_NOTE = (
    "PDF 成品是绘制文本，无法定位回写；请按自检报告页码人工核对，"
    "或改源文件后重新翻译。"
)


@dataclass
class EditItem:
    """一条可回写的译文段。"""

    id: str
    location: str
    source: str
    translation: str
    kind: str
    locator: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    #: 上次实际写进成品里的译文。使用者改 translation 后与本值不同即「待写回」。
    applied: str = ""
    #: 引擎重摇出来的备选译文（v1.6.2，E4）。候选跟着段走、不进数据库：
    #: 它只对这一次成品有意义，清单删了候选就该一起消失。旧清单没有这个
    #: 字段，读回来是空列表（向后兼容）。
    candidates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "location": self.location,
            "source": self.source,
            "translation": self.translation,
            "kind": self.kind,
            "locator": dict(self.locator),
            "issues": list(self.issues),
            "applied": self.applied,
            "candidates": list(self.candidates),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EditItem":
        return cls(
            id=str(data.get("id", "")),
            location=str(data.get("location", "")),
            source=str(data.get("source", "")),
            translation=str(data.get("translation", "")),
            kind=str(data.get("kind", "")),
            locator=dict(data.get("locator") or {}),
            issues=[str(item) for item in (data.get("issues") or [])],
            applied=str(data.get("applied", "")),
            candidates=[str(item) for item in (data.get("candidates") or [])],
        )

    @property
    def pending(self) -> bool:
        """译文被改过（与上次写回值不同）才是待写回。"""
        return self.translation != self.applied


@dataclass
class ApplyResult:
    """一次回写的结果：哪些段写成了、哪些跳过了、有什么要出声的。"""

    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 清单落盘 / 读取
# ---------------------------------------------------------------------------


def edit_list_path(output_path: str) -> str:
    """成品旁边的译后编辑清单路径（《原名 译后编辑清单.json》）。"""
    stem, _ext = os.path.splitext(output_path)
    return stem + EDIT_LIST_TAG + ".json"


def dump_edit_list(output_path: str, items: Sequence[EditItem]) -> str:
    """把清单写成 JSON（utf-8、不转义中文——人要直接打开看）。"""
    path = edit_list_path(output_path)
    payload = {
        "version": 2,
        "output": os.path.basename(output_path),
        "items": [item.to_dict() for item in items],
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=Path(path).parent,
            prefix=".translator-edit-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = stream.name
            json.dump(payload, stream, ensure_ascii=False, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return path


def load_edit_list(path: str) -> list[EditItem]:
    """读回清单。文件不存在或结构不对时抛 TranslationError（给人话）。"""
    target = Path(path)
    if not target.is_file():
        raise TranslationError(f"没有找到译后编辑清单：{shown_path(path)}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TranslationError(f"译后编辑清单不是有效 JSON：{exc}") from exc
    rows = data.get("items") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise TranslationError("译后编辑清单里没有 items 数组。")
    return [EditItem.from_dict(row) for row in rows if isinstance(row, dict)]


def items_from_pairs(
    pairs: Sequence[tuple[str, str]],
    locations: Sequence[str],
    locators: Sequence[dict],
    kind: str,
    prefix: str,
) -> list[EditItem]:
    """管线收尾用：把 (原文, 译文) 对变成清单项。locator 可缺省（{}）。"""
    items: list[EditItem] = []
    for position, ((source, translation), location, locator) in enumerate(
        zip(pairs, locations, locators), 1
    ):
        items.append(
            EditItem(
                id=f"{prefix}{position}",
                location=location,
                source=source,
                translation=translation,
                kind=kind,
                locator=dict(locator or {}),
                applied=translation,
            )
        )
    return items


def attach_issues(items: Sequence[EditItem], issues: Sequence[Any]) -> list[str]:
    """把自检报告里的疑点挂到对应段上，返回挂不上单段的「全文级」疑点。

    位置精确匹配优先；对不上时按原文片段反查——PPT 的数字疑点位置只写到
    「幻灯片」，Word/PDF 的才写到「段落 N」/「第 N 页」。原文片段匹配采
    前缀口径（报告里的 source 是截断版），命中多段时全挂（宁可多显示）。
    """
    by_location: dict[str, list[EditItem]] = {}
    for item in items:
        by_location.setdefault(item.location, []).append(item)
    globals_: list[str] = []
    for issue in issues or ():
        level = issue.level() if hasattr(issue, "level") else "中"
        text = f"[{level}] {issue.kind}：{issue.note or issue.source}"
        targets = by_location.get(issue.location)
        if not targets:
            wanted = _norm(str(issue.source or "")).rstrip("…")
            if len(wanted) >= 8:
                targets = [
                    item
                    for item in items
                    if _norm(item.source).startswith(wanted)
                ]
        if targets:
            for item in targets:
                item.issues.append(text)
        else:
            globals_.append(text)
    return globals_


def emit_edit_list(
    output_path: str,
    pairs: Sequence[tuple[str, str]],
    locations: Sequence[str],
    locators: Sequence[dict],
    kind: str,
    prefix: str,
    issues: Sequence[Any] = (),
) -> tuple[str, list[str]]:
    """管线收尾一键落清单。返回 (清单路径, 全文级疑点)。"""
    items = items_from_pairs(pairs, locations, locators, kind, prefix)
    globals_ = attach_issues(items, issues)
    path = dump_edit_list(output_path, items)
    return path, globals_


def format_edit_list(items: Sequence[EditItem]) -> str:
    """人看的清单文本：疑点优先、待写回标星。CLI --list 与日志共用。"""
    ordered = sorted(items, key=lambda item: (not item.issues, item.id))
    lines: list[str] = []
    for item in ordered:
        mark = "⚠" if item.issues else " "
        star = "*待写回*" if item.pending else ""
        lines.append(f"{mark} {item.id}  {item.location}  {star}".rstrip())
        lines.append(f"    原文：{_clip(item.source)}")
        lines.append(f"    译文：{_clip(item.translation)}")
        for position, candidate in enumerate(item.candidates, 1):
            lines.append(f"    备选{position}：{_clip(candidate)}")
        for issue in item.issues:
            lines.append(f"    疑点：{issue}")
    if not lines:
        lines.append("（清单是空的）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 单段重译
# ---------------------------------------------------------------------------


def retranslate_segment(translator: Any, source: str) -> str:
    """用同一个翻译器把一段原稿重摇一次。空译文抛 TranslationError（说人话）。

    **必须绕开缓存**（v1.6.2 补）：在线档与本地大模型档都先查
    ``translations.sqlite``，不绕开的话「重译」原样把上次的译文还回来，
    点十次也是同一句。绕开只影响这一次调用，缓存里的正式译文不动。
    """
    from .alternatives import bypass_cache

    if not source or not source.strip():
        raise TranslationError("这段没有原文，无法重译。")
    with bypass_cache(translator):
        results = translator.translate_texts([source])
    text = (results[0] if results else "").strip()
    if not text:
        raise TranslationError("引擎没有返回这段的译文，请稍后重试。")
    return text


# ---------------------------------------------------------------------------
# 回写引擎
# ---------------------------------------------------------------------------


def apply_edits(
    output_path: str,
    items: Sequence[EditItem],
    log: Callable[[str], None] | None = None,
    tm: Any | None = None,
) -> ApplyResult:
    """把改过的译文写回成品。按后缀分派；PDF 明确不支持（逐条出声）。

    只传「待写回」的段进来即可（调用方筛 ``item.pending``）；本函数不判断
    新旧，传谁写谁——GUI 的「保存写回」就是传单条。

    ``tm`` 是翻译记忆（可空）。给了就意味着**把人改过的译文当成权威译法**：
    以 ``origin="human"`` + ``confirmed=1`` 写进记忆库，这一步是
    「确认一次、全文生效」的落点——同一句原文在别处再出现时，100% 直通
    直接复用，不必再问引擎。这也是唯一自动写记忆的路径：这里的译文有人拍
    过板，比任何引擎自检都可信（见 core/tm.py「只存通过校验的译文」）。
    """
    log = log or (lambda message: None)
    suffix = os.path.splitext(output_path)[1].lower()
    result = ApplyResult()
    if suffix == ".pdf":
        for item in items:
            result.skipped.append(item.id)
        result.notes.append(_PDF_NOTE)
        return result
    if suffix not in _SUPPORTED_SUFFIXES:
        result.notes.append(f"暂不支持回写 {suffix or '无后缀'} 格式的成品。")
        for item in items:
            result.skipped.append(item.id)
        return result
    if not items:
        return result
    handler = _WRITERS[suffix]
    result = handler(output_path, items, log)
    if tm is not None and result.applied:
        _remember_edits(items, result, tm, log)
    return result


def _remember_edits(
    items: Sequence[EditItem],
    result: ApplyResult,
    tm: Any,
    log: Callable[[str], None],
) -> None:
    """把写回成功的人工译文记进翻译记忆（失败浮在现有的 traceback 里）。

    为什么这里抓异常一封了之：记忆是**增强**，写不进去也要让成品照常更新；
    而出了什么错要说出来，不能让用户以为记下了。
    """
    remembered = 0
    try:
        for item in items:
            if item.id not in result.applied:
                continue
            source = (item.source or "").strip()
            target = (item.translation or "").strip()
            if not source or not target or source == target:
                continue
            confirm = getattr(tm, "confirm", None)
            if callable(confirm):
                confirm(source, target)
            remembered += 1
    except Exception as exc:  # 记忆写入失败不该让写好的成品回滚
        log(f"人工译文没能记入翻译记忆（{type(exc).__name__}），成品已照常更新。")
        return
    if remembered:
        log(
            f"翻译记忆：{remembered} 段人工确认译文已入库，"
            "相同原文在其他位置出现时直接复用（译后编辑一次，全文生效）。"
        )


def _norm(text: str) -> str:
    return normalize_source(text or "")


def _clip(text: str, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _locate(
    entries: Sequence[tuple[Any, Any]], item: EditItem
) -> Any | None:
    """在 (key, 当前文字) 列表里找 item 对应的那条。

    主匹配 = 当前文字等于上次写回值；多处命中时用 locator["index"] 消歧，
    对不上就返回 None（跳过 + 出声，不盲写）。value 可以是 str 或 str 列表
    （SRT 的「去说话人行 / 原始行」两种口径，见 _apply_srt）。
    """
    wanted = _norm(item.applied or item.translation)
    if not wanted:
        return None
    hits: list[Any] = []
    for key, value in entries:
        candidates = value if isinstance(value, (list, tuple)) else [value]
        if any(_norm(text) == wanted for text in candidates):
            hits.append(key)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        preferred = item.locator.get("index")
        if preferred in hits:
            return preferred
        return None
    return None


def _replace_paragraph_text(paragraph: Any, text: str) -> None:
    """整段替换文字，保留首个 run 的格式（译文 run 的中文字体/字号/颜色）。"""
    runs = paragraph.runs
    if runs:
        runs[0].text = text
        for run in runs[1:]:
            run.text = ""
    else:
        paragraph.add_run(text)


def iter_docx_paragraphs(doc: Any) -> Iterator[Any]:
    """正文段落 + 各级表格单元格里的段落（顺序确定，落盘与回写共用）。"""
    for paragraph in doc.paragraphs:
        yield paragraph
    yield from _iter_table_paragraphs(doc.tables)


def _iter_table_paragraphs(tables: Sequence[Any]) -> Iterator[Any]:
    for table in tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    yield paragraph
                yield from _iter_table_paragraphs(cell.tables)


def iter_pptx_paragraphs(prs: Any) -> Iterator[Any]:
    """幻灯片里的文本段落：组合形状递归、表格单元格覆盖（与收集端同序）。"""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    def walk(shapes: Sequence[Any]) -> Iterator[Any]:
        for shape in shapes:
            try:
                is_group = shape.shape_type == MSO_SHAPE_TYPE.GROUP
            except Exception:
                is_group = False
            if is_group:
                yield from walk(shape.shapes)
                continue
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    for cell in row.cells:
                        for paragraph in cell.text_frame.paragraphs:
                            yield paragraph
                continue
            if getattr(shape, "has_text_frame", False):
                for paragraph in shape.text_frame.paragraphs:
                    yield paragraph

    for slide in prs.slides:
        yield from walk(slide.shapes)


def docx_paragraph_index(doc: Any, paragraph: Any) -> int:
    """段落对象在走查顺序里的下标（落清单时算 locator 用）。找不到返回 -1。"""
    for index, candidate in enumerate(iter_docx_paragraphs(doc)):
        if candidate._p is paragraph._p:
            return index
    return -1


def pptx_paragraph_index(prs: Any, paragraph: Any) -> int:
    for index, candidate in enumerate(iter_pptx_paragraphs(prs)):
        if candidate._p is paragraph._p:
            return index
    return -1


def _apply_docx(
    path: str, items: Sequence[EditItem], log: Callable[[str], None]
) -> ApplyResult:
    from docx import Document

    result = ApplyResult()
    doc = Document(path)
    paragraphs = list(iter_docx_paragraphs(doc))
    entries = [(index, para.text) for index, para in enumerate(paragraphs)]
    plan: list[tuple[int, EditItem]] = []
    for item in items:
        found = _locate(entries, item)
        if found is None:
            result.skipped.append(item.id)
            result.notes.append(
                f"{item.location}：成品里找不到对应的译文段（内容可能已被改动），未写回"
            )
            continue
        plan.append((found, item))
    for index, item in plan:
        _replace_paragraph_text(paragraphs[index], item.translation)
        result.applied.append(item.id)
    if result.applied:
        doc.save(path)
        for item in items:
            if item.id in result.applied:
                log(f"已写回 {item.location}")
    return result


def _apply_pptx(
    path: str, items: Sequence[EditItem], log: Callable[[str], None]
) -> ApplyResult:
    from pptx import Presentation

    result = ApplyResult()
    prs = Presentation(path)
    paragraphs = list(iter_pptx_paragraphs(prs))
    entries = [(index, para.text) for index, para in enumerate(paragraphs)]
    plan: list[tuple[int, EditItem]] = []
    for item in items:
        found = _locate(entries, item)
        if found is None:
            result.skipped.append(item.id)
            result.notes.append(
                f"{item.location}：成品里找不到对应的译文段（内容可能已被改动），未写回"
            )
            continue
        plan.append((found, item))
    for index, item in plan:
        _replace_paragraph_text(paragraphs[index], item.translation)
        result.applied.append(item.id)
    if result.applied:
        prs.save(path)
        for item in items:
            if item.id in result.applied:
                log(f"已写回 {item.location}")
    return result


def _apply_epub(
    path: str, items: Sequence[EditItem], log: Callable[[str], None]
) -> ApplyResult:
    """EPUB 回写：按译文块的当前文字定位，只改文字，其余 zip 条目不动。"""
    from .epub_translator import iter_translations, write_translations

    result = ApplyResult()
    available = {
        (name, index): text for name, index, text in iter_translations(path)
    }
    updates: dict[tuple[str, int], str] = {}
    for item in items:
        wanted = _norm(item.applied or item.translation)
        hits = [key for key, text in available.items() if _norm(text) == wanted]
        preferred = (str(item.locator.get("file") or ""), item.locator.get("index"))
        if preferred in hits:
            key = preferred
        elif len(hits) == 1:
            key = hits[0]
        else:
            key = None
        if key is None:
            result.skipped.append(item.id)
            result.notes.append(
                f"{item.location}：成品里找不到对应的译文段（内容可能已被改动），未写回"
            )
            continue
        updates[key] = item.translation
        result.applied.append(item.id)
    written = write_translations(path, updates)
    for item in items:
        if item.id in result.applied:
            log(f"已写回 {item.location}")
    undone = [
        key
        for (name, index), _text in updates.items()
        if (name, index) not in written
    ]
    if undone:
        result.notes.append(f"{len(undone)} 段没写进成品（章节可能无法解析）。")
    return result


def _apply_srt(
    path: str, items: Sequence[EditItem], log: Callable[[str], None]
) -> ApplyResult:
    from .script_srt import parse_srt, render_srt

    result = ApplyResult()
    text = Path(path).read_text(encoding="utf-8-sig")
    blocks = parse_srt(text)
    # parse_srt 只填 lines/text_lines（译文-only 成品里它们就是当前译文），
    # translated 留空。两种口径都进候选：「译文：xx」整行会被说话人正则
    # 误拆成 speaker=译文 + text=xx，只认一种口径就会漏匹配。
    entries = [
        (
            index,
            [
                "\n".join(block.text_lines),
                "\n".join(block.lines),
            ],
        )
        for index, block in enumerate(blocks)
    ]
    plan: list[tuple[int, EditItem]] = []
    for item in items:
        found = _locate(entries, item)
        if found is None:
            result.skipped.append(item.id)
            result.notes.append(
                f"{item.location}：成品里找不到对应的字幕块（内容可能已被改动），未写回"
            )
            continue
        plan.append((found, item))
    for index, item in plan:
        block = blocks[index]
        lines = [row.strip() for row in item.translation.split("\n") if row.strip()]
        if len(lines) != len(block.text_lines):
            # 行数校验在翻译时是 Critical（宁可保留原文），但人工改段是明确
            # 意图——按所写，只把行数不齐说出去（字幕换行可能变化）。
            result.notes.append(
                f"{item.location}：译文 {len(lines)} 行、原文 "
                f"{len(block.text_lines)} 行，行数不一致仍按所写"
            )
        block.translated = lines or list(block.text_lines)
        result.applied.append(item.id)
        log(f"已写回 {item.location}")
    if result.applied:
        Path(path).write_text(render_srt(blocks), encoding="utf-8")
    return result


def _apply_json(
    path: str, items: Sequence[EditItem], log: Callable[[str], None]
) -> ApplyResult:
    from .script_json import TARGET_KEY

    result = ApplyResult()
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    rows: Any = data
    if isinstance(data, dict):
        for key in ("lines", "data", "items", "scripts"):
            if isinstance(data.get(key), list):
                rows = data[key]
                break
    if not isinstance(rows, list):
        result.notes.append("清单对应的 JSON 成品结构认不出来，未写回。")
        for item in items:
            result.skipped.append(item.id)
        return result
    entries = [
        (index, str(row.get(TARGET_KEY) or ""))
        for index, row in enumerate(rows)
        if isinstance(row, dict)
    ]
    plan: list[tuple[int, EditItem]] = []
    for item in items:
        found = _locate(entries, item)
        if found is None:
            result.skipped.append(item.id)
            result.notes.append(
                f"{item.location}：成品里找不到对应的行（内容可能已被改动），未写回"
            )
            continue
        plan.append((found, item))
    for index, item in plan:
        rows[index][TARGET_KEY] = item.translation
        result.applied.append(item.id)
        log(f"已写回 {item.location}")
    if result.applied:
        Path(path).write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    return result


def _apply_tpp(
    path: str, items: Sequence[EditItem], log: Callable[[str], None]
) -> ApplyResult:
    from openpyxl import load_workbook

    result = ApplyResult()
    workbook = load_workbook(path)
    sheet = workbook.active
    if sheet is None:
        result.notes.append("这个 Excel 文件里没有工作表，未写回。")
        for item in items:
            result.skipped.append(item.id)
        return result

    def cell_text(row: int, column: int) -> str:
        value = sheet.cell(row=row, column=column).value
        return "" if value is None else str(value)

    def search_cell(item: EditItem) -> tuple[int, int] | None:
        """locator 对不上时按文字反查整表，唯一命中才用。"""
        wanted = _norm(item.applied or item.translation)
        if not wanted:
            return None
        hits: list[tuple[int, int]] = []
        for row in sheet.iter_rows():
            for cell in row:
                if _norm(str(cell.value or "")) == wanted:
                    hits.append((cell.row, cell.column))
        return hits[0] if len(hits) == 1 else None

    plan: list[tuple[int, int, EditItem]] = []
    for item in items:
        row = int(item.locator.get("row") or 0)
        column = int(item.locator.get("col") or 0)
        target: tuple[int, int] | None = None
        if row >= 1 and column >= 1:
            if _norm(cell_text(row, column)) == _norm(item.applied or item.translation):
                target = (row, column)
        if target is None:
            target = search_cell(item)
        if target is None:
            result.skipped.append(item.id)
            result.notes.append(
                f"{item.location}：成品里找不到对应的单元格（内容可能已被改动），未写回"
            )
            continue
        plan.append((target[0], target[1], item))
    for row, column, item in plan:
        sheet.cell(row=row, column=column, value=item.translation)
        result.applied.append(item.id)
        log(f"已写回 {item.location}")
    if result.applied:
        workbook.save(path)
    return result

#: 后缀 → 回写实现。键必须与 ``formats.writable_suffixes()`` 一致（v1.9.11 起
#: 由用例守住）：登记表里说能回写、这里却没有实现，用户会在保存那一步才看到
#: 「暂不支持」，那时译文已经改完了。
_WRITERS = {
    ".docx": _apply_docx,
    ".pptx": _apply_pptx,
    ".epub": _apply_epub,
    ".srt": _apply_srt,
    ".vtt": _apply_srt,
    ".json": _apply_json,
    ".xlsx": _apply_tpp,
}
