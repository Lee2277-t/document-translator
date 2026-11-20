"""TBX（TermBase eXchange，ISO 30042）术语库交换：最小骨架的解析与生成。

为什么只要「术语条目层」：本工具的术语库是给翻译管线用的源词→译名对照表，
不需要 TBX  full 特性的术语属性（subjectField / administrative status /
语言变体 ntig 那一大套）。本模块刻意只认以下骨架，认不出的结构按容错
策略处理（见下），而不是试图实现整个标准::

    <martif type="TBX">                 ← 根元素，解析时据此判定「是不是 TBX」
      <martifHeader>…</martifHeader>    ← 可选；缺失不阻挠解析
      <text><body>                      ← body 位置不锁死（有的文件没有 text 层）
        <termEntry id="…">
          <langSet xml:lang="en">       ← 源词所在语言（原文语言）
            <tig><term>bank</term></tig>
          </langSet>
          <langSet xml:lang="zh">       ← 译名所在语言（目标语言，默认中文）
            <tig>
              <term>银行</term>
              <descrip type="note">金融机构</descrip>   ← 备注
            </tig>
          </langSet>
        </termEntry>
      </body></text>
    </martif>

双语句目组织（generate 的口径）：每个 ``termEntry`` 两个 ``langSet``——
``source_lang``（默认 en）放源词、``target_lang``（默认 zh）放译名，备注写成
目标 langSet 内 tig 下的 ``<descrip type="note">``。parse 时反向拆解：
**带 zh 的 langSet 当译名、其余第一个 langSet 当源词**；没有 zh 语言集时
按位置取前两个；只有一个语言集时当「只有源词、译名待定」。parse 也认
``termEntry`` 层的 descrip / tig 层的 termNote，三种挂法都能读回来。

方向锁定（已知局限，如实写明）：本工具的术语库语义恒为「译入中文」，
parse 侧**不猜语言角色**——zh 语言集一律当译名。反向 TBX（zh 放源词、
外文放译名，即中译英方向）**不在支持范围**：generate 的
``source_lang`` / ``target_lang`` 只是给 langSet 打标签，不改变 parse 的
解读规则，反向传参生成的文件 parse 回来 source/target 会对调。将来做
中译英方向（路线图 v1.5.6）时再扩展 parse 的语言角色判定。

容错策略（对应「不许崩」的要求）：
* 缺 descrip、缺 martifHeader、多 langSet、空 termEntry、XML 声明与注释——
  全部正常处理或安静跳过；
* 编码宽容：UTF-8（含 BOM）/ UTF-16 / GBK 依次尝试解码——国内 Windows 上
  手工保存的 TBX 常是 GBK，只认 UTF-8 会把编码问题误判成格式问题；
* 明显不是 TBX 的文件（XML 都拼不出来、根元素不是 martif）抛
  ``ValueError``，消息说人话，由调用方（GUI 弹窗 / CLI 打印）负责出声；
* 纯标准库 ``xml.etree.ElementTree``，不引第三方依赖。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

#: ``xml:lang`` 在 ElementTree 里的完整键名。生成时直接写这个键，
#: 序列化出来就是合法的 ``xml:lang="en"``（xml 前缀是保留前缀）。
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

#: 目标语言的判断前缀：术语库是「外文译中文」，中文语言集放译名。
_TARGET_LANG_PREFIX = "zh"

#: XML 声明。解完码的文本要先剃掉它再交给 ElementTree——str 形态的
#: ``fromstring`` 遇到 encoding 声明直接报错，而字节形态又不吃 GBK 这类
#: 多字节编码声明（"multi-byte encodings are not supported"）。
_DECLARATION_RE = re.compile(r"\s*<\?xml[^>]*\?>", re.I)

#: 解码候选顺序：UTF-8（带不带 BOM 都行）→ GBK。
#: 国内 Windows 上手工保存的 TBX 常是 GBK，只认 UTF-8 会把它们误判成
#: 「不是有效的 TBX 文件」——那是编码问题，不是格式问题。
#: UTF-16 刻意不进盲试列表：它的解码太宽松，GBK 字节也可能被「成功」解成
#: 乱码（每个字节都凑得成合法码元）。带 BOM 才按 UTF-16 解，见 _decode_tbx。
_DECODE_CANDIDATES = ("utf-8-sig", "gbk")
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def _local(tag: str) -> str:
    """取元素标签的本地名（剥掉命名空间）。

    TBX 文件带不带默认命名空间完全看导出工具的心情，按本地名匹配才能
    两头都读。``comment`` 之类的标签没有命名空间，原样返回即可。
    """
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _node_text(node: ET.Element) -> str:
    """取元素全部文本（含后代文本），首尾空白剃掉。term 里可能嵌字体标记。"""
    return "".join(node.itertext()).strip()


def _read_tbx_file(path: Path) -> bytes:
    """读 TBX 文件的原始字节。"""
    if not path.is_file():
        raise ValueError(f"TBX 文件不存在或不可读：{path}")
    return path.read_bytes()


def _read_source(source: str | bytes | os.PathLike[str]) -> bytes:
    """把入参统一成原始字节：路径读文件，XML 文本按 UTF-8 编码。"""
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    if isinstance(source, os.PathLike):
        return _read_tbx_file(Path(source))
    if isinstance(source, str):
        stripped = source.lstrip("\ufeff \t\r\n")
        if stripped.startswith("<"):
            # XML 文本：按 UTF-8 编码交给后续解码（与 generate 的声明一致）
            return stripped.encode("utf-8")
        return _read_tbx_file(Path(source))
    raise ValueError(
        f"不支持的 TBX 输入类型：{type(source).__name__}（应为路径或 XML 文本）"
    )


def _decode_tbx(raw: bytes) -> str:
    """把 TBX 字节解成文本，并剃掉 XML 声明。

    为什么不能直接把字节喂给 ``ET.fromstring``：ElementTree 的字节形态
    只吃 UTF-8/ASCII 一类编码声明，遇到 GBK 声明直接抛
    "multi-byte encodings are not supported"；而文本形态又拒绝带
    encoding 声明的字符串。所以这里自己按候选编码解，再剥声明。
    UTF-16 只在带 BOM 时启用（盲试会把 GBK 解成乱码，见 _DECODE_CANDIDATES
    上方的注释）。
    """
    if raw.startswith(_UTF16_BOMS):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for encoding in _DECODE_CANDIDATES:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # 全试爆也不猜了：按 UTF-8 容错解码，后面 ET 解析失败会给人话错误
    return raw.decode("utf-8", errors="replace")


def parse_tbx(source: str | bytes | os.PathLike[str]) -> list[dict[str, str]]:
    """解析 TBX，返回条目列表，每条 ``{"source", "target", "note"}``。

    ``source`` 可以是文件路径（str / Path）或 XML 文本 / 字节。文件不存在、
    XML 拼不出、根元素不是 ``<martif>`` 时抛 ``ValueError``（人话错误）。
    没有 termEntry（合法但空的术语库）时返回空列表，不算错误。
    """
    raw = _read_source(source)
    text = _DECLARATION_RE.sub("", _decode_tbx(raw), count=1)

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"不是有效的 TBX 文件：XML 解析失败（{exc}）") from exc

    if _local(root.tag) != "martif":
        raise ValueError(
            f"不是有效的 TBX 文件：根元素是 <{_local(root.tag)}>，"
            "应为 <martif>（TBX 术语库交换格式）"
        )

    entries: list[dict[str, str]] = []
    for element in root.iter():
        if _local(element.tag) != "termEntry":
            continue
        entry = _parse_term_entry(element)
        if entry is not None:
            entries.append(entry)
    return entries


def _parse_lang_set(lang_set: ET.Element) -> dict[str, str]:
    """解析一个 langSet：语言代码 + 第一个 term 的文本 + 第一条备注。"""
    lang = lang_set.get(_XML_LANG, "")
    term_text = ""
    note = ""
    for tig in lang_set:
        if _local(tig.tag) != "tig":
            continue
        for node in tig:
            local = _local(node.tag)
            if local == "term" and not term_text:
                term_text = _node_text(node)
            elif local in ("descrip", "termNote") and not note:
                note = _node_text(node)
    return {"lang": lang, "term": term_text, "note": note}


def _entry_note(element: ET.Element) -> str:
    """termEntry 直下挂的 descrip / termNote（条目级备注）。"""
    for child in element:
        if _local(child.tag) in ("descrip", "termNote"):
            text = _node_text(child)
            if text:
                return text
    return ""


def _pick_pair(lang_terms: list[dict[str, str]]) -> tuple[dict[str, str], dict[str, str] | None]:
    """从语言集列表里挑（源词, 译名）。

    * 有 zh 语言集：zh 当译名，其余第一个语言集当源词；
    * 没有 zh：按位置取前两个（第一个源词、第二个译名）；
    * 只有一个语言集：它是源词，译名待定（None）。
    """
    zh = [item for item in lang_terms if item["lang"].lower().startswith(_TARGET_LANG_PREFIX)]
    zh_ids = {id(item) for item in zh}
    others = [item for item in lang_terms if id(item) not in zh_ids]
    if zh and others:
        return others[0], zh[0]
    if len(lang_terms) >= 2:
        return lang_terms[0], lang_terms[1]
    return lang_terms[0], None


def _parse_term_entry(element: ET.Element) -> dict[str, str] | None:
    """解析单个 termEntry；没有任何 term 的空条目返回 None（安静跳过）。"""
    lang_terms = [
        _parse_lang_set(child)
        for child in element
        if _local(child.tag) == "langSet"
    ]
    # 备注的收集先于语言集筛选：译名待定的条目（目标 term 为空）也要能把
    # 备注带回来，否则 generate→parse→generate roundtrip 会丢 note。
    notes = [_entry_note(element)] + [item["note"] for item in lang_terms]
    note = next((text for text in notes if text), "")
    term_holders = [item for item in lang_terms if item["term"]]
    if not term_holders:
        return None

    source_item, target_item = _pick_pair(term_holders)
    return {
        "source": source_item["term"],
        "target": target_item["term"] if target_item else "",
        "note": note,
    }


def _normalize_entries(entries: Any) -> list[dict[str, str]]:
    """把调用方给的条目统一成 ``{"source", "target", "note"}`` 字典列表。

    接受两种形态：字典（键 source/target/note）或序列（source、target、
    note 依次可选）。源词译名全空的条目没有意义，直接丢掉。
    """
    normalized: list[dict[str, str]] = []
    for item in entries or []:
        if isinstance(item, dict):
            source = str(item.get("source") or "").strip()
            target = str(item.get("target") or "").strip()
            note = str(item.get("note") or "").strip()
        else:
            sequence = list(item)
            source = str(sequence[0]).strip() if len(sequence) > 0 else ""
            target = str(sequence[1]).strip() if len(sequence) > 1 else ""
            note = str(sequence[2]).strip() if len(sequence) > 2 else ""
        if not source and not target:
            continue
        normalized.append({"source": source, "target": target, "note": note})
    return normalized


def generate_tbx(
    entries: Any,
    source_lang: str = "en",
    target_lang: str = "zh",
) -> str:
    """把术语条目写成合法 TBX XML 文本（带头部声明，UTF-8）。

    条目形态同 ``_normalize_entries``（字典或序列均可）。``source_lang`` /
    ``target_lang`` 是双语句目上的**语言标签**：术语库语义恒为「译入中文」，
    默认 en（源词）→ zh（译名）。

    **反向（中译英）不在支持范围**：parse 侧恒以 zh 语言集为译名、其余
    第一个语言集为源词（见模块 docstring 的「方向锁定」段），所以
    ``("zh", "en")`` 这类反向传参生成的文件，parse 回来 source/target 会
    对调——传参只换标签，不改变解读规则。将来做中译英方向（路线图
    v1.5.6）时再扩展 parse 的语言角色判定。

    输出刻意确定化（无时间戳、条目 id 按顺序编号）：同一份条目两次生成
    结果逐字节一致，roundtrip（generate→parse→generate）才稳定可测
    （正向口径下）。
    """
    normalized = _normalize_entries(entries)
    root = ET.Element("martif", {"type": "TBX"})
    header = ET.SubElement(root, "martifHeader")
    file_desc = ET.SubElement(header, "fileDesc")
    source_desc = ET.SubElement(file_desc, "sourceDesc")
    paragraph = ET.SubElement(source_desc, "p")
    paragraph.text = "由文档翻译工具术语库导出"
    text = ET.SubElement(root, "text")
    body = ET.SubElement(text, "body")
    for index, entry in enumerate(normalized, start=1):
        term_entry = ET.SubElement(body, "termEntry", {"id": f"e{index}"})
        source_set = ET.SubElement(term_entry, "langSet", {_XML_LANG: source_lang})
        source_tig = ET.SubElement(source_set, "tig")
        ET.SubElement(source_tig, "term").text = entry["source"]
        target_set = ET.SubElement(term_entry, "langSet", {_XML_LANG: target_lang})
        target_tig = ET.SubElement(target_set, "tig")
        ET.SubElement(target_tig, "term").text = entry["target"]
        if entry["note"]:
            descrip = ET.SubElement(target_tig, "descrip", {"type": "note"})
            descrip.text = entry["note"]
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def export_entries(store: Any, states: tuple[str, ...] = ("adopted",)) -> list[dict[str, str]]:
    """从 StateStore 收集可导出的术语条目。

    默认只收 ``adopted``（已采用）——已采用是用户拍过板的决策，导出给
    别的工具/别的项目是安全且有用的；``disputed`` / ``candidate`` 是
    **未决**状态，导出去等于把半成品决策推给下游，拒绝同理（用户已判定
    不是术语）。要连带导出未决条目，显式传 ``states``（如
    ``("adopted", "disputed", "candidate")``），GUI / CLI 目前都用默认值。
    """
    entries: list[dict[str, str]] = []
    for state in states:
        for row in store.terms_by_state(state):
            target = (row.get("target") or "").strip()
            if not target:
                continue  # 没有译名的候选导出去没有意义
            entries.append(
                {
                    "source": row["source"],
                    "target": target,
                    "note": (row.get("note") or "").strip(),
                }
            )
    return entries


def import_entries(
    termbase: Any,
    entries: Any,
    log: Any = None,
) -> dict[str, Any]:
    """把 ``parse_tbx`` 的结果灌进术语库，返回计数与冲突明细。

    去重规则（GUI 与 CLI 共用，全项目只此一处实现）：**已存在条目一律
    保留原值，绝不用外部 TBX 覆盖用户既有裁决**——包括 rejected（用户
    已判定不是术语）与 disputed（用户尚未拍板）。冲突逐条出声（``log``
    回调）并收进返回值的 ``conflicts`` 列表，绝不静默。

    返回 ``{"added", "kept", "skipped", "conflicts"}``：added=新进库条数
    （有译名按已采用进、无译名按候选进）、kept=已存在未导入条数、
    skipped=源词为空的无效条目数、conflicts=[(源词, 库内译名, 未导入译名)]。
    """
    added = 0
    kept = 0
    skipped = 0
    conflicts: list[tuple[str, str, str]] = []
    for entry in _normalize_entries(entries):
        source = entry["source"]
        target = entry["target"]
        note = entry["note"]
        if not source:
            skipped += 1
            continue
        existing = termbase.store.get_term(source)
        if existing is not None:
            # 用户既有裁决优先：不管是 adopted / disputed / rejected，
            # 外部 TBX 说啥都不覆盖，只出声说明保留了什么。
            kept += 1
            old = (existing.get("target") or "").strip() or "（未定）"
            conflicts.append((source, old, target or "（空）"))
            if log is not None:
                log(
                    f"TBX 导入冲突保留：{source}（库内「{old}」，"
                    f"未导入「{target or '空'}」）"
                )
            continue
        if target:
            termbase.adopt(source, target, note=note or "TBX 导入")
        else:
            # 只有源词没有译名的条目：按候选进库，进未决池等用户裁决
            termbase.add_candidate(source, note=note)
        added += 1
    return {"added": added, "kept": kept, "skipped": skipped, "conflicts": conflicts}
