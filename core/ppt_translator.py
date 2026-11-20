"""PPT 双语翻译。

复用在线/离线 `Translator` 的同一套能力（术语表锁定、缓存、熔断、风格档位），
把 .pptx 翻成「原文 + 中文」对照版，成品名为 `<原名>_双语.pptx`。

实现要点：
- 遍历所有幻灯片，递归处理三种文本载体：文本框（含占位符）、表格（单元格）、
  组合形状（group，递归进子形状）。
- 收集每段原文 → 一次性批量送进 `Translator.translate_texts`，享受缓存与批处理。
- 写回时**在原文段落之后插入**一个中文段落：克隆原段落的 `<a:pPr>`（对齐/缩进/层级/
  项目符号），克隆原段落首个 run 的 `<a:rPr>`（字体/字号/颜色/粗斜体），并把字体名
  设成中文字体，保证中文用正确的字形渲染而不是沿用原来的 Latin-only 字体。
- SmartArt、图表这类 graphic frame 在 python-pptx 里读不到文本，跳过并提示，
  不会因此崩掉整份文件。

注意：占位符、母版标题、批注页都不动——只翻幻灯片正文，模板与版式原样保留。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Callable

from lxml import etree

from .common import TranslationError, check_cancel, shown_path, unique_output_path
from . import page_range
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
from .translator import Translator, normalize_source

#: 译文 run 使用的中文字体。Microsoft YaHei 同时覆盖 Latin 与 CJK 字形，
#: 即使译文里夹了少量英文也不会变成方块。
CJK_FONT = "Microsoft YaHei"


def translate_ppt(
    src_path: str,
    translator: Translator,
    progress: Callable[[int, int, str], None] | None = None,
    log: Callable[[str], None] | None = None,
    pages: str | None = None,
) -> str:
    """把 .pptx 翻成中英对照版，返回成品路径。

    `translator` 既可以是在线 `Translator` 也可以是保密模式的 `LocalTranslator`——
    两者都暴露 `translate_texts(texts, progress)` 与 `summary()`，本函数不关心底层引擎。

    `pages` 是「只翻其中几张幻灯片」的区间写法（`1-3,5`），与 PDF 的 `--pages=` 同一套
    解析。这里的裁法语义只有一种：**不重建幻灯片**，未选中的那些张原样留在成品里、
    成品张数 == 源张数——与 PDF 的「原位重排」同口径。这条口径要在日志里说出来，
    不能让用户以为成品里只剩他点名的那几张。
    """
    log = log or (lambda message: None)
    progress = progress or (lambda done, total, message: None)

    try:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        from pptx.oxml.ns import qn
    except ImportError as exc:
        raise TranslationError(
            "PPT 支持需要 python-pptx 依赖，请先安装（见 requirements.txt 中的 "
            "python-pptx 行，取消注释后 pip install -r requirements.txt）"
        ) from exc

    prs = Presentation(src_path)

    # 上界检查只能在这里做：写范围的时候还没有张数（`describe` 验写法、这里验上界），
    # 且必须按这份文件自己的张数判——批量排多个 PPT 时没有一个全局的「共几张」。
    # 全空白等同「没填」= 整份都翻，口径与 translate_pdf 一致（那里是 `pages and pages.strip()`）。
    slide_count = len(prs.slides)
    spec = (pages or "").strip()
    selected = page_range.parse_pages(spec, slide_count, page_range.SLIDES) if spec else None
    if selected is not None:
        log(
            f"幻灯片范围：{page_range.describe(spec, page_range.SLIDES)}"
            f"（这份 PPT 共 {slide_count} 张，选中 {len(selected)} 张）。"
        )
        log("PPT 不重建幻灯片：未选中的那些张原样留在成品里，成品还是 "
            f"{slide_count} 张，只是其中 {slide_count - len(selected)} 张不翻。")

    # 收集「值得翻译」的段落（Paragraph 对象）及其所在幻灯片序号（1 起）。
    # 序号供自检报告标注位置——译名疑点必须告诉用户去哪张看，只给词不给
    # 位置等于让用户全文 Ctrl+F。只收主幻灯片正文，不动母版/版式/批注。
    paragraphs: list[Paragraph] = []
    slide_numbers: list[int] = []
    unsupported_shapes = 0

    def collect_text_frame(text_frame, slide_no: int) -> None:
        for para in text_frame.paragraphs:
            if para.text and para.text.strip():
                paragraphs.append(para)
                slide_numbers.append(slide_no)

    def walk_shape(shape, slide_no: int) -> None:
        nonlocal unsupported_shapes
        # 组合形状：递归进子形状，层级深的也能翻到。
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            for child in shape.shapes:
                walk_shape(child, slide_no)
            return
        if shape.has_table:
            for row in shape.table.rows:
                for cell in row.cells:
                    collect_text_frame(cell.text_frame, slide_no)
            return
        if shape.has_text_frame:
            collect_text_frame(shape.text_frame, slide_no)
            return
        # graphic frame（SmartArt、图表）在 python-pptx 里读不到文本，跳过。
        unsupported_shapes += 1

    for slide_no, slide in enumerate(prs.slides, 1):
        check_cancel(getattr(translator, "stop_event", None))
        if selected is not None and slide_no - 1 not in selected:
            continue
        for shape in slide.shapes:
            walk_shape(shape, slide_no)

    if not paragraphs:
        if selected is not None:
            raise TranslationError(
                f"选中的这 {len(selected)} 张里没有可翻译的文字内容"
                "（可能全是图片或 SmartArt）。想翻别的张请改「范围」那一栏；"
                "整份都翻就把这栏清空。"
            )
        raise TranslationError("这份 PPT 里没有可翻译的文字内容（可能全是图片或 SmartArt）。")

    total = len(paragraphs)
    log(f"已识别 {total} 个文本段落，开始翻译…")

    sources = [para.text for para in paragraphs]
    translated = translator.translate_texts(sources, progress=progress)

    inserted = 0
    #: 译后编辑清单的原料：(原文, 译文, 译文段对象)——回写按文本匹配定位，
    #: 对象只为在走查顺序里算个索引消歧用。
    edit_written: list[tuple[str, str, object]] = []
    for para, source, target in zip(paragraphs, sources, translated):
        check_cancel(getattr(translator, "stop_event", None))
        # 只有真正翻译出来的段落才插中文；原样返回（数字、URL、术语直译未变等）
        # 不重复插入，避免「中文段」和「原文段」一模一样。
        if not target or normalize_source(target) == normalize_source(source):
            continue
        edit_written.append((source, target, _insert_translation_after(para, target, qn)))
        inserted += 1

    if inserted == 0:
        raise TranslationError(
            "没有段落被成功翻译（可能内容全是公式、数字或已被术语表直译）。"
        )

    if unsupported_shapes:
        log(
            f"有 {unsupported_shapes} 个图形（SmartArt/图表）的文本无法读取，已跳过，"
            "建议人工核对这些幻灯片。"
        )

    output_path = unique_output_path(src_path, "双语", ".pptx")
    prs.save(output_path)
    progress(total, total, "")

    # 自检报告：与 PDF/Word 一致。数字疑点逐段查，质量问题从翻译器取
    # （只有本地大模型档带质量引擎，其余档位自然不出现 MQM 段）。
    report = CheckReport()
    pairs: list[tuple[str, str]] = []
    locations: list[str] = []
    for para, source, target, slide_no in zip(
        paragraphs, sources, translated, slide_numbers
    ):
        if not target or normalize_source(target) == normalize_source(source):
            continue
        for issue in check_numbers(source, target):
            # 位置要带序号：只写「幻灯片」等于让用户自己挨张找（EPUB 那侧一直带章号）。
            report.add("数字疑点", f"第 {slide_no} 张", issue.source, issue.detail)
        pairs.append((source, target))
        locations.append(f"第 {slide_no} 张")

    # 译名一致性检查（v1.4.2 补上）：PPT 是 v1.3.0 才接入的翻译链路，当时
    # 漏接了这条质检——新链路接入清单缺了一环，见交接稿决策 50①。
    # 疑点横跨多张，位置取第一次出现的张（具体清单在 note 里）。
    for issue in check_term_consistency(pairs, locations):
        report.add(
            "译名不一致",
            issue.places[0] if issue.places else "全文",
            issue.term,
            issue_note(issue),
        )
    if unsupported_shapes:
        report.add(
            "保留原文",
            "全文",
            "",
            f"{unsupported_shapes} 个 SmartArt/图表文本无法读取",
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

    # v1.5.4 译后编辑闭环：清单与报告同源（pairs/locations 就是上面算好的），
    # locator 用译文段对象在走查顺序里的下标——只为多处同文时消歧。
    if pairs:
        from .postedit import emit_edit_list, pptx_paragraph_index

        if len(edit_written) == len(pairs):
            locators = [
                {"index": pptx_paragraph_index(prs, para)}
                for _source, _target, para in edit_written
            ]
        else:  # 防御：两条收集链路意外不一致时退回纯文本匹配
            locators = [{} for _ in pairs]
        edit_path, edit_globals = emit_edit_list(
            output_path, pairs, locations, locators, "pptx", "p", report.issues
        )
        log(
            f"译后编辑清单：{shown_path(edit_path)}"
            f"（{len(pairs)} 段可改，{len(edit_globals)} 条全文级疑点）"
        )

    log(f"PPT 翻译完成：插入中文段落 {inserted} 个，成品已保存。")
    return output_path


def _insert_translation_after(para, text: str, qn) -> "Paragraph":
    """在原文段落后克隆出一个中文段落：保留对齐/缩进/项目符号与首 run 字体，
    把字体名设成中文字体，再把译文写进单个 run。返回新段落（译后编辑回写
    要按对象算位置）。"""
    from pptx.text.text import _Paragraph as Paragraph

    p_el = para._p
    text_frame = para._parent

    # 整段克隆：pPr（段落属性）一并带走，中文段落与原文同款版式。
    new_p = deepcopy(p_el)
    # 清掉克隆里的 run / 换行，只保留段落属性骨架，稍后塞一个干净的译文 run。
    for tag in ("a:r", "a:br"):
        for child in new_p.findall(qn(tag)):
            new_p.remove(child)

    # 首 run 的 rPr（字体/字号/颜色/粗斜体）作为译文 run 的格式基础。
    orig_runs = p_el.findall(qn("a:r"))
    rPr_src = orig_runs[0].find(qn("a:rPr")) if orig_runs else None

    new_r = etree.SubElement(new_p, qn("a:r"))
    if rPr_src is not None:
        new_r.append(deepcopy(rPr_src))
    else:
        rPr = etree.SubElement(new_r, qn("a:rPr"))
        rPr.set("lang", "zh-CN")
    t = etree.SubElement(new_r, qn("a:t"))
    t.text = text  # lxml 会自动处理 XML 转义

    p_el.addnext(new_p)

    # 用 python-pptx 对象设一次字体名：同时写入 latin / ea / cs 三种 typeface，
    # 中文（ea）才能落到中文字体上，而不是沿用原来的西文字体导致显示异常。
    new_para = Paragraph(new_p, text_frame)
    if new_para.runs:
        new_para.runs[0].font.name = CJK_FONT
    return new_para
