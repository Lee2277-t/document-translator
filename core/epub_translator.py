"""EPUB 双语翻译（v1.6.1）。

EPUB 就是一个规定得很死的 zip：``mimetype`` 必须是第一个条目且不压缩，
``META-INF/container.xml`` 指向 OPF，OPF 的 spine 列出正文顺序。所以这里
不走任何第三方库，直接按包结构读写——第三方库保存时几乎都会重排 zip，
把 mimetype 压成 deflate，阅读器就整本打不开了。

抽取口径与 Word/PDF 一致：**按块送翻、译文原位插回**。

- 只翻 spine 列出的正文 XHTML；目录导航（``properties="nav"``）与 NCX、
  OPF 元数据一概不动——翻译它们会让阅读器的章节树和正文对不上。
- 「叶子块」才算一段：``<p>``/``<h1>``…/``<li>``/``<td>`` 里如果还套着块级
  元素，文字归内层，外层跳过，否则同一句话会翻两遍。
- ``<ruby>`` 的注音（``<rt>``/``<rp>``）不参与翻译，只翻基底文字。
- ``<td>``/``<th>``/``<li>`` 的译文接在原元素内部（另插一个单元格会多出一列）；
  其余块在原文之后插入同标签的译文块。
- ``<img>`` 的 ``alt``/``title`` 改成「原文（译文）」，屏幕阅读器两种语言都能读。
- 除改动过的 XHTML 外，其余 zip 条目逐字节照抄（图片、字体、CSS 不重编码）。
"""

from __future__ import annotations

import html.entities
import io
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Callable, Iterator
from urllib.parse import unquote

from lxml import etree

from . import page_range
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

XHTML_NS = "http://www.w3.org/1999/xhtml"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
EPUB_TYPE = "{http://www.idpf.org/2007/ops}type"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

MIMETYPE = b"application/epub+zip"
TRANSLATION_CLASS = "xlate-zh"
TRANSLATION_STYLE = "color:#1e4976;"
#: 译文块的 lang 属性。阅读器据此选择中文字形，也方便用户全局搜到所有译文。
TRANSLATION_LANG = "zh-CN"

#: 块级元素：只有它们才值得单独成段送翻。``body``/``html`` 不在内——它们是容器。
_BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "dt", "dd", "blockquote", "figcaption",
    "td", "th", "caption", "div", "section", "article",
    "header", "footer", "aside", "address",
}

#: 译文接在元素内部而不是另起同级元素的容器：表格单元格与列表项。
#: 另插一个 ``<td>`` 会多出一列，另插一个 ``<li>`` 会多出一个项目符号。
_INLINE_CONTAINERS = {"td", "th", "li", "dt", "dd", "caption"}

#: 不翻的子树：脚本、样式、预格式代码，以及 ruby 的注音。
_SKIP_TAGS = {"script", "style", "pre", "code", "rt", "rp", "svg", "math"}

_NAMED_ENTITY = re.compile(r"&([A-Za-z][A-Za-z0-9]{1,31});")
_XML_BUILTINS = {"amp", "lt", "gt", "quot", "apos"}


@dataclass
class _Entry:
    """一段待翻译内容。``node`` 是 lxml 元素，翻译完直接改它。"""

    doc: int
    node: object
    text: str
    kind: str  # block / inline / alt
    attr: str = ""


def _local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return etree.QName(tag).localname.lower()


def _decode(data: bytes) -> str:
    """EPUB 规范要求 UTF-8，但现实中确有 GBK 的私制文件，按常见顺序试一遍。"""
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _expand_entities(text: str) -> str:
    """把 HTML 命名实体换成数字引用。

    XHTML 里的 ``&nbsp;`` 等实体由外部 DTD 声明，离线解析不加载 DTD，
    留着解析器会直接报错并把文字吞掉。内置的五个 XML 实体原样保留。
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in _XML_BUILTINS:
            return match.group(0)
        codepoint = html.entities.name2codepoint.get(name)
        if codepoint is None:
            return match.group(0)
        return f"&#{codepoint};"

    return _NAMED_ENTITY.sub(replace, text)


def _parse(data: bytes) -> tuple[object, str]:
    """返回 (根元素, doctype 字符串)。doctype 要原样写回，否则 EPUB2 的校验会挂。"""
    text = _expand_entities(_decode(data))
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, recover=True, huge_tree=True
    )
    tree = etree.parse(io.BytesIO(text.encode("utf-8")), parser)
    return tree.getroot(), tree.docinfo.doctype or ""


def _serialize(root, doctype: str) -> bytes:
    return etree.tostring(
        root, xml_declaration=True, encoding="utf-8", doctype=doctype or None
    )


def _read_opf(zf: zipfile.ZipFile) -> str:
    try:
        container, _doctype = _parse(zf.read("META-INF/container.xml"))
    except KeyError as exc:
        raise TranslationError(
            "这个 .epub 缺少 META-INF/container.xml，不是有效的 EPUB 文件。"
        ) from exc
    except Exception as exc:
        raise TranslationError(f"EPUB 的 container.xml 解析失败：{exc}") from exc
    for element in container.iter():
        if _local(element.tag) == "rootfile" and element.get("full-path"):
            return element.get("full-path", "")
    raise TranslationError("container.xml 里没有指向 OPF 的 rootfile。")


def _resolve(base: str, href: str) -> str:
    """把 OPF 里的相对 href 变成 zip 内的路径（POSIX 分隔符）。"""
    href = unquote(href.split("#", 1)[0]).replace("\\", "/")
    if not href:
        return ""
    if href.startswith("/"):
        return href.lstrip("/")
    directory = os.path.dirname(base).replace("\\", "/")
    parts = (directory.split("/") if directory else []) + href.split("/")
    resolved: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        resolved.append(part)
    return "/".join(resolved)


def _spine_documents(opf_path: str, root, names: set[str]) -> list[str]:
    """按 spine 顺序返回正文 XHTML 的 zip 路径。目录导航与 NCX 不在其中。"""
    manifest: dict[str, tuple[str, str]] = {}
    for element in root.iter():
        if _local(element.tag) != "item":
            continue
        item_id = element.get("id") or ""
        href = element.get("href") or ""
        properties = (element.get("properties") or "").lower()
        if not item_id or not href:
            continue
        manifest[item_id] = (href, properties)

    skipped_nav: set[str] = set()
    for _item_id, (href, properties) in manifest.items():
        if "nav" in properties.split():
            skipped_nav.add(_resolve(opf_path, href))

    docs: list[str] = []
    for element in root.iter():
        if _local(element.tag) != "itemref":
            continue
        idref = element.get("idref") or ""
        entry = manifest.get(idref)
        if not entry:
            continue
        href, properties = entry
        if "nav" in properties.split():
            continue
        path = _resolve(opf_path, href)
        if not path or path in skipped_nav or path not in names:
            continue
        if os.path.splitext(path)[1].lower() not in (".xhtml", ".html", ".htm"):
            continue
        docs.append(path)
    return docs


def _fallback_documents(names: set[str]) -> list[str]:
    """OPF 读不出来时的退路：把包里所有 XHTML 都当正文翻。"""
    docs = [
        name
        for name in sorted(names)
        if os.path.splitext(name)[1].lower() in (".xhtml", ".html", ".htm")
        and "nav" not in os.path.basename(name).lower()
    ]
    return sorted(docs, key=lambda name: (name.count("/"), name))


def _text_of(element) -> str:
    parts: list[str] = []

    def walk(node) -> None:
        if node.text:
            parts.append(node.text)
        for child in node:
            tag = _local(child.tag)
            if tag == "br":
                parts.append(" ")
            elif tag not in _SKIP_TAGS:
                walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(element)
    return " ".join("".join(parts).split())


def _inside_skipped(element) -> bool:
    node = element.getparent()
    while node is not None:
        tag = _local(node.tag)
        if tag in ("head", "nav") or tag in _SKIP_TAGS:
            return True
        node = node.getparent()
    return False


def _is_leaf_block(element) -> bool:
    for child in element.iterdescendants():
        if _local(child.tag) in _BLOCK_TAGS:
            return False
    return True


def _collect(doc_index: int, root) -> list[_Entry]:
    entries: list[_Entry] = []
    for element in root.iter():
        tag = _local(element.tag)
        if tag not in _BLOCK_TAGS:
            continue
        if (element.get("class") or "").find(TRANSLATION_CLASS) >= 0:
            continue  # 二次翻译时别把上次的译文当原文再翻一遍
        if _inside_skipped(element) or not _is_leaf_block(element):
            continue
        text = _text_of(element)
        if not text or not is_translatable(text):
            continue
        kind = "inline" if tag in _INLINE_CONTAINERS else "block"
        entries.append(_Entry(doc_index, element, normalize_source(text), kind))

    for element in root.iter():
        if _local(element.tag) != "img":
            continue
        for attr in ("alt", "title"):
            value = element.get(attr)
            if not value or not value.strip() or not is_translatable(value):
                continue
            entries.append(
                _Entry(doc_index, element, normalize_source(value), "alt", attr)
            )
    return entries


def _apply(entry: _Entry, translated: str):
    """写入一段译文，返回译文的那个节点（``alt`` 没有节点，返回 None）。"""
    element = entry.node
    if entry.kind == "alt":
        original = (element.get(entry.attr) or "").strip()
        element.set(entry.attr, f"{original}（{translated}）")
        return None

    namespace = etree.QName(element).namespace or XHTML_NS
    if entry.kind == "inline":
        # 另插一个 <td> 会多出一列、另插一个 <li> 会多出一个项目符号，
        # 所以这两种容器把译文接在原文后面，用软换行隔开。
        line_break = element.makeelement(f"{{{namespace}}}br", {})
        span = element.makeelement(f"{{{namespace}}}span", {})
        span.set("class", TRANSLATION_CLASS)
        span.set("lang", TRANSLATION_LANG)
        span.set(XML_LANG, TRANSLATION_LANG)
        span.set("style", TRANSLATION_STYLE)
        span.text = translated
        element.append(line_break)
        element.append(span)
        return span

    # 克隆标签（标题译文仍是标题）但不带原元素的 id——重复 id 会让阅读器错乱。
    block = element.makeelement(element.tag, {})
    block.set("class", TRANSLATION_CLASS)
    block.set("lang", TRANSLATION_LANG)
    block.set(XML_LANG, TRANSLATION_LANG)
    block.set("style", TRANSLATION_STYLE)
    block.text = translated
    element.addnext(block)
    return block


def _translation_blocks(root) -> Iterator[object]:
    for element in root.iter():
        if (element.get("class") or "").find(TRANSLATION_CLASS) >= 0:
            yield element


def _rewrite_zip(source: str, target: str, replacements: dict[str, bytes]) -> None:
    """按原顺序重写 zip：mimetype 第一个且不压缩，其余条目逐字节照抄。"""
    with zipfile.ZipFile(source) as zin:
        infos = zin.infolist()
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zout:
            mimetype = zipfile.ZipInfo("mimetype")
            mimetype.compress_type = zipfile.ZIP_STORED
            zout.writestr(mimetype, MIMETYPE)
            for info in infos:
                if info.filename == "mimetype":
                    continue
                data = replacements.get(info.filename)
                if data is None:
                    data = zin.read(info.filename)
                copy = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                copy.compress_type = info.compress_type
                copy.external_attr = info.external_attr
                zout.writestr(copy, data)


def iter_translations(path: str) -> list[tuple[str, int, str]]:
    """成品里所有译文块：（zip 内文件, 该文件内第几个, 当前文字）。

    译后编辑回写用它定位：文字匹配为主，``(文件, 序号)`` 只在多处同文时消歧。
    """
    found: list[tuple[str, int, str]] = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if os.path.splitext(name)[1].lower() not in (".xhtml", ".html", ".htm"):
                continue
            try:
                root, _doctype = _parse(archive.read(name))
            except Exception:
                continue
            for index, node in enumerate(_translation_blocks(root)):
                found.append((name, index, _text_of(node)))
    return found


def write_translations(
    path: str, updates: dict[tuple[str, int], str]
) -> set[tuple[str, int]]:
    """把人工改过的译文写回成品，只改译文块的文字，其余条目逐字节保留。"""
    if not updates:
        return set()
    grouped: dict[str, dict[int, str]] = {}
    for (name, index), text in updates.items():
        grouped.setdefault(name, {})[index] = text

    replacements: dict[str, bytes] = {}
    applied: set[tuple[str, int]] = set()
    with zipfile.ZipFile(path) as archive:
        for name, wanted in grouped.items():
            try:
                root, doctype = _parse(archive.read(name))
            except Exception:
                continue
            touched = False
            for index, node in enumerate(_translation_blocks(root)):
                text = wanted.get(index)
                if text is None:
                    continue
                node.text = text
                applied.add((name, index))
                touched = True
            if touched:
                replacements[name] = _serialize(root, doctype)

    if not applied:
        return applied
    directory = os.path.dirname(os.path.abspath(path))
    handle, temp = tempfile.mkstemp(suffix=".epub", dir=directory)
    os.close(handle)
    try:
        _rewrite_zip(path, temp, replacements)
        os.replace(temp, path)
    except Exception:
        if os.path.exists(temp):
            os.remove(temp)
        raise
    return applied


def translate_epub(
    src_path: str,
    translator: Translator,
    progress: Callable[[int, int, str], None] | None = None,
    log: Callable[[str], None] | None = None,
    pages: str | None = None,
) -> str:
    """把 .epub 翻成中英对照版，返回成品路径。

    `pages` 按 **spine 顺序**挑章节（第 1 章 = spine 第一篇正文）。这里只有一种
    裁法语义：**不重建包**，未选中的章节逐字节原样留在成品里，成品章数 == 源章数，
    只是其中若干章不翻。与 PDF 的原位重排同口径，这条口径要在日志里说出来——
    用户看到成品还是 30 章，会以为范围没生效。
    """
    log = log or (lambda message: None)
    progress = progress or (lambda done, total, message: None)

    try:
        archive = zipfile.ZipFile(src_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise TranslationError(
            "这个 .epub 打不开（可能不是 zip 结构或文件已损坏）。"
        ) from exc

    with archive:
        names = set(archive.namelist())
        opf_path = _read_opf(archive)
        try:
            opf_root, _opf_doctype = _parse(archive.read(opf_path))
            documents = _spine_documents(opf_path, opf_root, names)
        except TranslationError:
            raise
        except Exception:
            documents = []
        if not documents:
            log("OPF 里没有可用的 spine 列表，改为翻译包里所有 XHTML。")
            documents = _fallback_documents(names)
        if not documents:
            raise TranslationError("这个 EPUB 里没有可翻译的 XHTML 正文。")

        # 范围按 spine 序号校验；documents 保持完整不裁剪，遍历时跳过未选中的章——
        # 下面的 `entry.doc`、`documents[entry.doc]`、`index_maps[doc_index]` 全都拿
        # 列表位置当身份用，一旦压缩列表，成品里的「第 N 章」就会集体错位。
        # 全空白等同「没填」= 整本都翻，口径与 translate_pdf 一致。
        spec = (pages or "").strip()
        selected = (
            page_range.parse_pages(spec, len(documents), page_range.CHAPTERS)
            if spec
            else None
        )
        if selected is not None:
            log(
                f"章节范围：{page_range.describe(spec, page_range.CHAPTERS)}"
                f"（这份 EPUB 正文共 {len(documents)} 章，选中 {len(selected)} 章）。"
            )
            log(
                f"EPUB 不重建包：未选中的 {len(documents) - len(selected)} 章原样留在成品里，"
                f"成品还是 {len(documents)} 章，只是其中 {len(selected)} 章插译文。"
            )

        roots: list[tuple[int, str, object, str]] = []
        entries: list[_Entry] = []
        unreadable = 0
        for index, name in enumerate(documents):
            if selected is not None and index not in selected:
                continue
            check_cancel(getattr(translator, "stop_event", None))
            try:
                root, doctype = _parse(archive.read(name))
            except Exception:
                unreadable += 1
                continue
            roots.append((index, name, root, doctype))
            entries.extend(_collect(index, root))

        if not entries:
            if selected is not None:
                raise TranslationError(
                    f"选中的这 {len(selected)} 章里没有可翻译的外文内容。"
                    "想翻别的章请改「范围」那一栏；整本书都翻就把这栏清空。"
                )
            raise TranslationError("没有在这些章节里找到需要翻译的外文内容。")

        log(
            f"共 {len(roots)} 章正文、{len(entries)} 段外文内容，开始翻译…"
        )
        progress(0, len(entries), "正在翻译…")

        def report_progress(done: int, total: int, _message: str) -> None:
            progress(done, total, f"正在翻译第 {done}/{total} 段")

        sources = [entry.text for entry in entries]
        translations = translator.translate_texts(sources, progress=report_progress)
        check_cancel(getattr(translator, "stop_event", None))
        progress(len(entries), len(entries), "正在写入双语 EPUB…")

        pairs: list[tuple[str, str]] = []
        locations: list[str] = []
        pending_locators: list[tuple[int, object, str]] = []
        inserted = 0
        alt_done = 0
        changed: dict[str, bytes] = {}
        touched: set[int] = set()

        for entry, source, translated in zip(entries, sources, translations):
            if not translated or not translated.strip():
                continue
            target = translated.strip()
            if normalize_source(target) == source:
                continue
            node = _apply(entry, target)
            touched.add(entry.doc)
            if entry.kind == "alt":
                alt_done += 1
                continue
            inserted += 1
            pairs.append((source, target))
            locations.append(f"第 {entry.doc + 1} 章")
            pending_locators.append((entry.doc, node, documents[entry.doc]))

        if inserted == 0 and alt_done == 0:
            raise TranslationError("没有获得可写入的译文，请稍后重试或更换文档。")

        # 译文块在序列化后的文件里的序号，供译后编辑清单回写定位。
        index_maps: dict[int, dict[int, int]] = {}
        for doc_index, _name, root, _doctype in roots:
            if doc_index not in touched:
                continue
            positions: dict[int, int] = {}
            for position, node in enumerate(_translation_blocks(root)):
                positions[id(node)] = position
            index_maps[doc_index] = positions

        for doc_index, name, root, doctype in roots:
            if doc_index in touched:
                changed[name] = _serialize(root, doctype)

        output_path = unique_output_path(src_path, "双语", ".epub")
        _rewrite_zip(src_path, output_path, changed)

    log(
        f"已写入 {inserted} 段译文（{len(changed)} 章有改动）；{translator.summary()}"
    )
    if alt_done:
        log(f"其中图片替代文字 {alt_done} 处（写成「原文（译文）」，读屏两种语言都能读）")
    if unreadable:
        scope = "选中的章节中有" if selected is not None else "有"
        log(f"{scope} {unreadable} 个章节 XHTML 解析失败，已原样保留，建议人工核对。")
    log("书名与目录导航按原样保留——翻它们会让阅读器的目录和正文对不上。")

    check = CheckReport()
    checked_pairs: list[tuple[str, str]] = []
    checked_locations: list[str] = []
    for entry, source, translated in zip(entries, sources, translations):
        if not translated or not translated.strip():
            check.add("保留原文", f"第 {entry.doc + 1} 章", source, "未获得译文")
            continue
        target = translated.strip()
        if normalize_source(target) == source:
            continue
        for issue in check_numbers(source, target):
            check.add("数字疑点", f"第 {entry.doc + 1} 章", issue.source, issue.detail)
        if entry.kind == "alt":
            continue
        checked_pairs.append((source, target))
        checked_locations.append(f"第 {entry.doc + 1} 章")

    for issue in check_term_consistency(checked_pairs, checked_locations):
        check.add("译名不一致", "全文", issue.term, issue_note(issue))
    if unreadable:
        check.add("保留原文", "全文", "", f"{unreadable} 个章节 XHTML 解析失败")

    # 记忆近似复用（v1.9.9）：进本份报告的疑点清单，结论行「N 条」才数得准。
    for entry in fuzzy_entries(tm_fuzzy_of(translator)):
        check.add(*entry)
    quality_issues, pending_terms = quality_payload(translator)
    report_path = build_report(
        output_path,
        check,
        quality_issues,
        pending_terms,
        term_hits_of(translator),
        paid_usage_of(translator),
    )
    if report_path:
        log(f"自检报告：{shown_path(report_path)}（{check.count} 条疑点）")

    if pairs:
        from .postedit import emit_edit_list

        edit_locators = []
        for doc_index, node, name in pending_locators:
            position = index_maps.get(doc_index, {}).get(id(node))
            if position is None:
                edit_locators.append({})
            else:
                edit_locators.append({"file": name, "index": position})
        edit_path, edit_globals = emit_edit_list(
            output_path, pairs, locations, edit_locators, "epub", "e", check.issues
        )
        log(
            f"译后编辑清单：{shown_path(edit_path)}"
            f"（{len(pairs)} 段可改，{len(edit_globals)} 条全文级疑点）"
        )
    return output_path
