from __future__ import annotations

import re
from copy import deepcopy
from typing import Callable

from lxml import etree

from docx import Document
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import qn
from docx.shared import RGBColor
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from . import direction, mixed
from .common import TranslationError, check_cancel, shown_path, unique_output_path
from .number_check import check_numbers
from .report import (
    CheckReport,
    build_report,
    fuzzy_entries,
    paid_usage_of,
    quality_payload,
    term_hits_of,
    tm_fuzzy_of,
)
from .term_check import check_term_consistency, issue_note
from .translator import Translator, is_translatable, normalize_source

CJK_FONT = "微软雅黑"
TRANSLATION_COLOR = RGBColor(0x1E, 0x49, 0x76)

#: 段落里出现汉字就说明这是中文为主的段落，其中的英文子句要单独扣出来翻。
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _element_text(element) -> str:
    """从一个 w:p 元素里取出可见文字（含 tab / 换行 / 不换行连字符）。"""
    parts: list[str] = []

    def walk(node) -> None:
        for child in node.iterchildren():
            tag = child.tag
            if tag == qn("w:p"):
                continue
            if tag == qn("w:t"):
                parts.append(child.text or "")
            elif tag == qn("w:tab"):
                parts.append(" ")
            elif tag in (qn("w:br"), qn("w:cr")):
                parts.append(" ")
            elif tag == qn("w:noBreakHyphen"):
                parts.append("-")
            elif tag == qn("w:softHyphen"):
                continue
            else:
                walk(child)

    walk(element)
    return "".join(parts)


def _paragraph_text(paragraph: Paragraph) -> str:
    return _element_text(paragraph._p)


def _source_font_size(paragraph: Paragraph):
    for run in paragraph.runs:
        if run.font.size is not None:
            return run.font.size
    try:
        if paragraph.style is not None and paragraph.style.font.size is not None:
            return paragraph.style.font.size
    except Exception:
        pass
    return None


#: 脚注/尾注部件的 content type。python-docx 1.2.0 还没有脚注 API，只能自己从
#: 包里按 content type 把部件捞出来；找不到就直接跳过（没有脚注的文档是常态）。
_NOTE_CONTENT_TYPES = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml",
)


def _collect_note_parts(doc) -> list[tuple]:
    """脚注/尾注部件的 [(part, 已解析的 XML 根), ...]。

    python-docx 把不认识的部件加载成裸 Part，没有 `.element`，得自己 parse_xml；
    而且改完之后必须把 XML 回写进 `part._blob`，否则保存时改动会被整个丢掉——
    保存在用的是 blob，不是我们 parse 出来的那棵树。
    """
    try:
        package = doc.part.package
    except Exception:
        return []
    found: list[tuple] = []
    try:
        for part in package.iter_parts():
            if str(getattr(part, "content_type", "")) not in _NOTE_CONTENT_TYPES:
                continue
            try:
                found.append((part, parse_xml(part.blob)))
            except Exception:
                continue
    except Exception:
        return []
    return found


#: 文本框内容住在 ``w:txbxContent`` 里。``mc:AlternateContent`` 的 Fallback 是
#: 同一段文字的 VML 副本（Word 显示 Choice 那份），两份都收就会翻两遍、插两遍。
_TXBX_CONTENT = qn("w:txbxContent")
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


def _ancestor_is(element, tag: str) -> bool:
    node = element.getparent()
    while node is not None:
        if node.tag == tag:
            return True
        node = node.getparent()
    return False


def _in_textbox(element) -> bool:
    return _ancestor_is(element, _TXBX_CONTENT)


def _in_fallback(element) -> bool:
    return _ancestor_is(element, _MC_FALLBACK)


def _collect_comment_paragraphs(doc, seen: set) -> list[tuple[object, str, str]]:
    """批注正文。批注住在单独的 comments part 里，不扫就整份漏翻。

    python-docx 1.2.0 才有 comments API；更早的版本没有这个部件，按「没有批注」
    处理即可，不要让整个 Word 流程因为它起不来。
    """
    entries: list[tuple[object, str, str]] = []
    try:
        comments = doc.comments
    except Exception:
        return entries
    for comment in comments:
        try:
            paragraphs = comment.paragraphs
        except Exception:
            continue
        for paragraph in paragraphs:
            element = paragraph._p
            if element in seen:
                continue
            seen.add(element)
            text = _paragraph_text(paragraph)
            if is_translatable(text):
                entries.append((paragraph, normalize_source(text), "comment"))
    return entries


def _has_revisions(doc) -> bool:
    """文档里有没有修订标记。插入的文字照常翻，删除的文字不译（delText 不是 w:t）。"""
    try:
        body = doc.element.body
    except Exception:
        return False
    return any(next(body.iter(qn(tag)), None) is not None for tag in ("w:ins", "w:del"))


def _collect_paragraphs(doc, note_parts=None) -> list[tuple[object, str, str]]:
    """返回 [(段落对象, 原文, 容器种类), ...]。

    种类：``body`` 正文/页眉页脚，``note`` 脚注/尾注，``comment`` 批注，
    ``textbox`` 文本框。脚注段落给的是裸 lxml 元素而不是 Paragraph——它住在
    别的 part 里，套 Paragraph 会因为没有父容器而炸，写入时按种类走三条路径
    （文本框接在原文同一段内，其余各插一个新段）。
    """
    entries: list[tuple[object, str, str]] = []
    seen = set()

    def add(container, parent) -> None:
        for paragraph_element in container.iter(qn("w:p")):
            if paragraph_element in seen or _in_fallback(paragraph_element):
                continue
            seen.add(paragraph_element)
            paragraph = Paragraph(paragraph_element, parent)
            text = _paragraph_text(paragraph)
            if is_translatable(text):
                kind = "textbox" if _in_textbox(paragraph_element) else "body"
                entries.append((paragraph, normalize_source(text), kind))

    add(doc.element.body, doc)

    # 脚注与尾注不和正文住在同一个 part 里，不单独扫就整份漏翻。学术与法律文档
    # 的实质性内容常常就在脚注里。
    if note_parts is None:
        note_parts = _collect_note_parts(doc)
    for _part, root in note_parts:
        for paragraph_element in root.iter(qn("w:p")):
            if paragraph_element in seen:
                continue
            seen.add(paragraph_element)
            text = _element_text(paragraph_element)
            if not text.strip():
                continue
            if is_translatable(text):
                entries.append((paragraph_element, normalize_source(text), "note"))

    entries.extend(_collect_comment_paragraphs(doc, seen))

    header_footer_seen: set[str] = set()
    for section in doc.sections:
        candidates = (
            section.header,
            section.footer,
            section.first_page_header,
            section.first_page_footer,
            section.even_page_header,
            section.even_page_footer,
        )
        for container in candidates:
            try:
                if container.is_linked_to_previous:
                    continue
            except Exception:
                continue
            try:
                part_name = str(container.part.partname)
            except Exception:
                continue
            if part_name in header_footer_seen:
                continue
            header_footer_seen.add(part_name)
            add(container._element, container)
    return entries


def _apply_translation_style(run: Run) -> None:
    """译文 run 的统一外观：中文字体 + 译文蓝 + 中文语言标记。

    放在这里而不是各写一遍，是因为文本框走的是「接在原段内」而不是「新插一段」，
    两条路径的外观必须一致，否则同一份文档里两种译文的颜色会不一样。
    """
    run.font.name = CJK_FONT
    run_properties = run._element.get_or_add_rPr()
    fonts = run_properties.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        run_properties.insert(0, fonts)
    fonts.set(qn("w:ascii"), CJK_FONT)
    fonts.set(qn("w:hAnsi"), CJK_FONT)
    fonts.set(qn("w:eastAsia"), CJK_FONT)
    run.font.color.rgb = TRANSLATION_COLOR

    language = OxmlElement("w:lang")
    language.set(qn("w:val"), "zh-CN")
    language.set(qn("w:eastAsia"), "zh-CN")
    run_properties.append(language)


def _append_inline_translation(paragraph: Paragraph, text: str) -> None:
    """文本框里的译文用软换行接在原文同一段内。

    文本框是固定外形，往 ``w:txbxContent`` 里再插一个 ``w:p`` 会被 Word 裁掉
    （形状不会自动加高），所以这里另起一行而不是另起一段。
    """
    paragraph.add_run().add_break()
    run = paragraph.add_run(text)
    _apply_translation_style(run)


def _insert_translation_after(source: Paragraph, text: str) -> Paragraph:
    """在源段后面插一个译文段，返回它（译后编辑回写要按对象算位置）。"""
    new_element = OxmlElement("w:p")
    source._p.addnext(new_element)
    new_paragraph = Paragraph(new_element, source._parent)

    if source._p.pPr is not None:
        properties = deepcopy(source._p.pPr)
        for tag in (
            "w:numPr",
            "w:sectPr",
            "w:pageBreakBefore",
            "w:framePr",
            "w:pBdr",
            "w:shd",
        ):
            for element in properties.findall(qn(tag)):
                properties.remove(element)
        spacing = properties.find(qn("w:spacing"))
        if spacing is None:
            spacing = OxmlElement("w:spacing")
            properties.append(spacing)
        spacing.set(qn("w:before"), "0")
        new_element.insert(0, properties)

    run = new_paragraph.add_run(text)
    _apply_translation_style(run)

    size = _source_font_size(source)
    if size is not None:
        run.font.size = size

    source_runs = [r for r in source.runs if r.text.strip()]
    if source_runs and all(r.bold for r in source_runs):
        run.font.bold = True
    if source_runs and all(r.italic for r in source_runs):
        run.font.italic = True

    return new_paragraph


def _flush_note_parts(note_parts) -> None:
    """把改过的脚注/尾注 XML 写回部件。保存走的是 blob，不是我们 parse 的树。"""
    for part, root in note_parts or []:
        try:
            part._blob = etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )
        except Exception:
            continue


def _insert_note_translation_after(source_element, text: str) -> None:
    """脚注/尾注部件的译文插入。这里没有 Paragraph 可用，直接拼 XML。

    只保留原文段的段落属性（去掉编号等），中文换 CJK 字体，颜色沿用正文译文色。
    """
    new_element = OxmlElement("w:p")
    source_element.addnext(new_element)

    if source_element.pPr is not None:
        properties = deepcopy(source_element.pPr)
        for tag in ("w:numPr", "w:sectPr", "w:pageBreakBefore", "w:framePr"):
            for element in properties.findall(qn(tag)):
                properties.remove(element)
        new_element.insert(0, properties)

    run = OxmlElement("w:r")
    run_properties = OxmlElement("w:rPr")

    fonts = OxmlElement("w:rFonts")
    fonts.set(qn("w:ascii"), CJK_FONT)
    fonts.set(qn("w:hAnsi"), CJK_FONT)
    fonts.set(qn("w:eastAsia"), CJK_FONT)
    run_properties.append(fonts)

    color = OxmlElement("w:color")
    color.set(qn("w:val"), "%02X%02X%02X" % (TRANSLATION_COLOR[0], TRANSLATION_COLOR[1], TRANSLATION_COLOR[2]))
    run_properties.append(color)

    language = OxmlElement("w:lang")
    language.set(qn("w:val"), "zh-CN")
    language.set(qn("w:eastAsia"), "zh-CN")
    run_properties.append(language)

    run.append(run_properties)
    node = OxmlElement("w:t")
    node.set(qn("xml:space"), "preserve")
    node.text = text
    run.append(node)
    new_element.append(run)


def translate_word(
    src_path: str,
    translator: Translator,
    progress: Callable[[float, int, str], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> str:
    log = log or (lambda message: None)
    doc = Document(src_path)
    note_parts = _collect_note_parts(doc)
    entries = _collect_paragraphs(doc, note_parts)
    if not entries:
        raise TranslationError("没有找到需要翻译的外文内容。")

    log(f"共找到 {len(entries)} 段外文内容，开始翻译…")
    if _has_revisions(doc):
        log(
            "文档含修订标记：插入的文字照常翻译，删除的文字不译，"
            "成品保留修订状态。"
        )
    texts = [item[1] for item in entries]

    def report(done: int, total: int, _message: str) -> None:
        if progress is not None:
            progress(done, total, f"正在翻译第 {done}/{total} 段")

    translations = translator.translate_texts(texts, progress=report)
    check_cancel(translator.stop_event)

    if progress is not None:
        progress(len(entries), len(entries), "正在写入双语 Word…")

    inserted = 0
    counts = {"note": 0, "comment": 0, "textbox": 0}
    #: 译后编辑清单的原料：(条目序号, 原文, 译文, 译文段对象)。脚注/尾注/批注/
    #: 文本框不在正文段落走查范围内，不进清单（疑点仍在自检报告里）。
    edit_written: list[tuple[int, str, str, Paragraph]] = []
    for index, ((paragraph, source_text, kind), translated) in enumerate(
        zip(entries, translations), 1
    ):
        check_cancel(translator.stop_event)
        if not translated.strip():
            continue
        if normalize_source(translated) == source_text:
            continue
        if kind == "note":
            _insert_note_translation_after(paragraph, translated.strip())
            counts["note"] += 1
        elif kind == "textbox":
            _append_inline_translation(paragraph, translated.strip())
            counts["textbox"] += 1
        else:
            new_paragraph = _insert_translation_after(
                paragraph, translated.strip()
            )
            if kind == "comment":
                counts["comment"] += 1
            else:
                edit_written.append(
                    (index, source_text, translated.strip(), new_paragraph)
                )
        inserted += 1

    if inserted == 0:
        raise TranslationError("没有获得可写入的译文，请稍后重试或更换文档。")

    if _MIXED_RUNS_ENABLED:
        # 骨架开关（默认关）：把中英混排段里的英文子句单独补翻后插回段落中间。
        mixed = _translate_mixed_paragraphs(doc, translator)
        if mixed:
            log(f"中英混排：补翻 {mixed} 处英文子句")

    # 脚注/尾注的改动落在我们自己 parse 的树上，必须在保存前回写进 part 的 blob，
    # 否则 python-docx 保存时用的是原始 blob，译文会被整个丢掉。
    _flush_note_parts(note_parts)

    output_path = unique_output_path(src_path, "双语", ".docx")
    doc.save(output_path)
    log(f"已写入 {inserted} 段{direction.label()}译文；{translator.summary()}")
    if counts["note"]:
        log(f"其中脚注/尾注 {counts['note']} 段")
    if counts["comment"]:
        log(f"批注 {counts['comment']} 条（译文接在批注内容下方）")
    if counts["textbox"]:
        log(
            f"文本框 {counts['textbox']} 段"
            "（译文接在原文同一段内，避免撑破固定框高）"
        )

    report = CheckReport()
    pairs: list[tuple[str, str]] = []
    locations: list[str] = []
    for index, ((_paragraph, source_text, _kind), translated) in enumerate(
        zip(entries, translations), 1
    ):
        if not translated.strip() or normalize_source(translated) == source_text:
            report.add("保留原文", f"段落 {index}", source_text, "未获得译文")
            continue
        for issue in check_numbers(source_text, translated):
            report.add(
                "数字疑点", f"段落 {index}", issue.source, issue.detail
            )
        pairs.append((source_text, translated))
        locations.append(f"段落 {index}")

    # v1.4.2：译名疑点带上出现位置（段落号），报告的 note 里念出来——
    # 用户知道有这个词之后，下一个动作永远是「它在哪几段」。
    for issue in check_term_consistency(pairs, locations):
        report.add(
            "译名不一致",
            "全文",
            issue.term,
            issue_note(issue),
        )

    # 记忆近似复用（v1.9.9）：进本份报告的疑点清单，结论行「N 条」才数得准。
    for entry in fuzzy_entries(tm_fuzzy_of(translator)):
        report.add(*entry)
    quality_issues, pending_terms = quality_payload(translator)
    report_path = build_report(
        output_path,
        report,
        quality_issues,
        pending_terms,
        term_hits_of(translator),
        paid_usage_of(translator),
    )
    if report_path:
        log(f"自检报告：{shown_path(report_path)}（{report.count} 条疑点）")
    if edit_written:
        # v1.5.4 译后编辑闭环：疑点清单与报告同源，段落号对齐，界面里可逐条改。
        from .postedit import docx_paragraph_index, emit_edit_list

        edit_path, edit_globals = emit_edit_list(
            output_path,
            [(source, target) for _index, source, target, _para in edit_written],
            [f"段落 {index}" for index, _s, _t, _para in edit_written],
            [
                {"index": docx_paragraph_index(doc, para)}
                for _i, _s, _t, para in edit_written
            ],
            "docx",
            "w",
            report.issues,
        )
        log(
            f"译后编辑清单：{shown_path(edit_path)}"
            f"（{len(edit_written)} 段可改，{len(edit_globals)} 条全文级疑点）"
        )
    return output_path


# ---------------------------------------------------------------------------
# 中英混排骨架（默认关）。PDF 侧已能把「有汉字的段落里夹的英文子句」单独抠出来
# 翻（pdf_translator._english_runs），Word 侧还没做：要在段落中间插 run 并处理
# 原 run 的样式继承。先立好接口与调用点，实现后再把开关打开。
# ---------------------------------------------------------------------------

#: 中英混排补翻：含汉字段落里的外文子句会被单独抠出、批量翻译后
#: 就地插回子句之后。抽取规则与 PDF / 引擎侧共用 core/mixed 一份。
_MIXED_RUNS_ENABLED = True


def _mixed_clause_spans(text: str) -> list[tuple[int, int, str]]:
    """返回（起, 止, 子句文本）。规则见 ``core.mixed.foreign_spans``。"""
    return mixed.foreign_spans(text)


def extract_mixed_runs(text: str) -> list[str]:
    """挑出文本里属于「少数语种」的子句，URL / 邮箱直接排除。"""
    return [clause for _, _, clause in _mixed_clause_spans(text)]


def _run_spans(paragraph: Paragraph) -> list[tuple[int, int, Run]]:
    """算出每个 run 在段落整文里的字符区间，用于把子句位置映射回具体的 run。"""
    spans: list[tuple[int, int, Run]] = []
    cursor = 0
    for run in paragraph.runs:
        length = len(run.text)
        spans.append((cursor, cursor + length, run))
        cursor += length
    return spans


def _insert_clause_translation(reference_run: Run, translated: str) -> None:
    """在某个 run 之后插入中文译法 run。

    做法是整体深拷贝原 run 再改字——这样粗体/斜体/字号/下划线全部天然继承，
    只需要覆盖字体与颜色。逐个属性去抄（bold/italic/size/underline/strike…）
    既容易漏，也难以对齐以后新遇见的属性。
    """
    element = deepcopy(reference_run._element)
    reference_run._element.addnext(element)
    new_run = Run(element, reference_run._parent)
    new_run.text = f"（{translated}）"

    properties = new_run._element.get_or_add_rPr()
    fonts = properties.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        properties.insert(0, fonts)
    fonts.set(qn("w:ascii"), CJK_FONT)
    fonts.set(qn("w:hAnsi"), CJK_FONT)
    fonts.set(qn("w:eastAsia"), CJK_FONT)
    new_run.font.color.rgb = TRANSLATION_COLOR

    language = OxmlElement("w:lang")
    language.set(qn("w:val"), "zh-CN")
    language.set(qn("w:eastAsia"), "zh-CN")
    properties.append(language)


def _split_run_at(run: Run, cut: int) -> None:
    """把 run 从 cut 处劈成两个：原 run 留前半段，后半段进一个深拷贝的新 run。

    不劈开的话，碰到「整段就是一个 run」这种 Word 里最常见的形态，
    子句后面那截中文也属于同一个 run，译文只能插到整段最末，
    就地插入就名存实亡了——这正是第一版实现被测试抓住的缺陷。
    """
    content = run.text
    if cut <= 0 or cut >= len(content):
        return
    trailing = deepcopy(run._element)
    run._element.addnext(trailing)
    trailing_run = Run(trailing, run._parent)
    trailing_run.text = content[cut:]
    run.text = content[:cut]


def _translate_mixed_paragraphs(doc: "Document", translator: Translator) -> int:
    """补翻「有汉字的段落里夹着的英文子句」。

    为什么要单独做这一段：主流程判断"这段含汉字"就整段跳过了，
    于是合同、年报、说明书里最常见的中夹英条款会原文不动留在译文里。
    这里把英文子句抠出来单独翻，译法**就地插回**该子句之后，
    而不是统一堆到段末——位置对不上，读者就没法把译法和条款绑起来看。
    """
    if direction.is_english():
        # 反向时主流程会把整段中文译成英文，「中夹英补翻」针对的是
        # 「整段判成中文被跳过」这个中文方向的问题，在英文方向只会把
        # 段里的英文子句再送一遍英文翻译（纯浪费，还可能插出重复句）。
        return 0
    done = 0
    for paragraph in list(doc.paragraphs):
        text = paragraph.text or ""
        if not text.strip() or not _CJK_RE.search(text):
            continue  # 纯外文段落交给主流程整段翻译
        clauses = _mixed_clause_spans(text)
        if not clauses:
            continue
        try:
            translations = translator.translate_texts([clause for *_, clause in clauses])
        except Exception:
            # 补翻是锦上添花：失败最多是没有子句译法，不该让整份文档翻不出来。
            continue

        # 从右往左处理：每一次插入都发生在更早子句的右侧，
        # 因此不会把还没处理的子句的字符位置挤偏移。
        for (start, end, clause), translated in reversed(list(zip(clauses, translations))):
            if not translated or not translated.strip():
                continue
            if normalize_source(translated) == normalize_source(clause):
                continue
            anchor = None
            cut = 0
            for run_start, run_end, run in _run_spans(paragraph):
                # 以「子句最后一个字符落在哪个 run 里」为锚点，
                # 译文才会紧跟子句结尾，而不是出现在子句开头或段末。
                if run_start <= end - 1 < run_end:
                    anchor = run
                    cut = end - run_start
                    break
            if anchor is None:
                continue
            _split_run_at(anchor, cut)
            _insert_clause_translation(anchor, translated.strip())
            done += 1
    return done
