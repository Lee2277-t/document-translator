"""TMX（Translation Memory eXchange）翻译记忆交换：最小骨架的解析与生成。

TMX 是 CAT 行业的事实标准（LISA / OSCAR 制定，Trados、memoQ、OmegaT 都
认），本模块**按标准从零实现**：文法取自公开的规范文档，实现手法全部取自
本项目自己的 ``core/tbx.py``（``_read_source`` 三态入参、``_DECLARATION_RE``
剃声明、``_DECODE_CANDIDATES`` 编码宽容、``_local()`` 剥命名空间、确定化
输出）。刻意不去看也不复制任何第三方工具的实现——它们的许可证是著佐权，
抄一行代码就会把整个项目拖进义务里，而标准本身不属于任何人。

为什么 TMX 值得做（对应差距 F2）
--------------------------------
翻译记忆此前锁死在私库 SQLite 里：本机翻译时能复用，但别人给的一百万
句旧语料灌不进来、自己攒的资产也带不走（换电脑、交接给同事、给别的工具用，
全部只能带着那个 sqlite 文件跑）。TMX 是唯一不需要对方装我们这套工具的
通道。

支持的骨架（刻意最小，认不出的结构按容错策略处理）::

    <tmx version="1.4">                     ← 根元素，解析据此判定「是不是 TMX」
      <header … srclang="*all*"/>           ← 可选；缺失不阻挠解析
      <body>
        <tu srclang="en">                   ← 一个翻译单元（一句原文 + 它的译文）
          <tuv xml:lang="en"><seg>原文</seg></tuv>
          <tuv xml:lang="zh-Hans"><seg>译文</seg></tuv>
          <note>备注</note>                 ← 可选
        </tu>
      </body>
    </tmx>

三条判定口径（改动前先看这里，它们是故意的，不是疏忽）
------------------------------------------------------
1. **方向锁定**：与本工具 TBX 的实现同口径——``zh`` 语言集的 ``tuv`` **恒当
   译文**，其余第一个当原文。理由与 TBX 一致：本工具的记忆库语义是「译入
   中文」，不猜语言角色。**反向（中译英）不在支持范围**，留给路线图
   v1.5.6 扩展语言角色判定（到时候 TBX / TMX 一起扩展）。

2. **``srclang="*all*"``**：我方 ``tm`` 表不按语种分表，一张表里混着英文、
   日文与其他源语言的记忆，给 header 一律标 ``en`` 是在骗下游；TMX 规范
   允许 header 用 ``*all*`` 并在每个 ``<tu>`` 上标真实 ``srclang``，所以：
   header 永远是 ``*all*``，逐个 tu 写自己那一行的真实语言，标不出来的
   （老记忆没有语种信息）继承 header 的 ``*all*``。解析侧把 ``*all*`` 读成
   空串——"不知道"就是不知道，不猜。

3. **内联标签**：``<bpt>/<ept>/<ph>/<ut>`` 这四个元素承载的是**标记本身的
   文字**（如 ``<b>``、``<br/>``），不是句子内容，算进 ``<seg>`` 会把标记
   代码混进记忆里污染它；``<hi>/<it>`` 是样式标签，它们包着的是真正的文本，
   与纯文本一样收进译文。参见 ``_SKIP_INLINE_TAGS`` 与 ``_seg_text``。

导出口径（与 TBX 故意不同，理由写在这里）
------------------------------------------
TBX 默认只导 ``adopted``，因为 candidate / disputed 是**未决**的术语裁决，
导出去等于把半成品决策推给下游。TMX 默认**全导**：翻译记忆的入库门槛本来
就是「零问题遗留才写入」（见 ``core/quality/pipeline.py`` 把记忆写入放在
最后且要求 ``not remaining``），库里的每一条都已经背过书了。想要更严的
口径，显式传 ``only_confirmed=True``——只导人工确认过（译后编辑改出来、
或外部标了 confirmed）的那些。

去重规则（全项目与 ``core/tbx.py::import_entries`` 一字不差）
--------------------------------------------------------------
**已存在的原文一律保留库内原值**，绝不用外部文件覆盖；冲突逐条出声（``log``
回调 + ``conflicts`` 列表），绝不静默。记忆和术语一样是用户攒下来的资产：
外部 TMX 说它的译法更好也不算数，要改由人在这个工具里改。

容错策略（对应「不许崩」）
--------------------------
* 缺 header、空 ``<tu>``、缺某个语言、注释、GBK 编码——全部正常处理或安静
  跳过；
* 明显不是 TMX 的文件（XML 拼不出、根不是 ``<tmx>``）抛 ``ValueError`` 说
  人话，由调用方（GUI 弹窗 / CLI 打印）负责出声；
* 纯标准库 ``xml.etree.ElementTree``，不引第三方依赖。

保密提醒（重要，调用方必须先过闸门）
------------------------------------
**导出**会把「原文句子」写成明文 XML 落盘，这是保密模式的红线行为——
``app.py`` 的 CLI 出口用独立开关 ``--yes-i-know-tmx`` 拦一道（不复用
``--yes-i-know``：那是「内容出境给 DeepL」，本项是「内容落盘」，共用标记会
让为付费档点过确认的用户被静默放行），GUI 出口用确认对话框拦；保密模式
（``--offline``）下直接拒绝。**导入**不受限——往里灌不属于外发。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable
import xml.etree.ElementTree as ET

#: ``xml:lang`` 在 ElementTree 里的完整键名（与 core/tbx.py 同）。
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

#: 目标语言的判断前缀：**zh 语言集恒当译文**（见模块 docstring「方向锁定」）。
_TARGET_LANG_PREFIX = "zh"

#: 生成时的默认译文语言码。本工具译入简体中文。
DEFAULT_TGT_LANG = "zh-Hans"

#: TMX 规范里「所有语言」的通配值。header 用它表示混合源语言。
_ALL_LANGS = "*all*"

#: 写在生成文件里的 TMX 版本号与生成器标识。
_TMX_VERSION = "1.4"
_CREATION_TOOL = "文档翻译工具"

#: 内联标签里**不**收进 seg 的那四类：它们承载的是标记本身的文字。
#: bpt/ept = 成对标记的起止（<b>…</b> 的标记部分），ph = 孤立标记（<br/>），
#: ut = 未转换的行内片段。``<hi>/<it>`` 不在其中——它们包着真正的文本。
_SKIP_INLINE_TAGS = frozenset({"bpt", "ept", "ph", "ut"})

#: XML 声明。解码后的文本要先剃掉它再交给 ElementTree——str 形态的
#: ``fromstring`` 遇到 encoding 声明直接报错，而字节形态又不吃 GBK 这类
#: 多字节编码声明（"multi-byte encodings are not supported"）。
#: 与 core/tbx.py 同一套手法，刻意各自维护一份：TBX 与 TMX 的编码容错策略
#: 将来可能分叉（例如将来只允许 TMX 吃 utf-16），互不影响才好改。
_DECLARATION_RE = re.compile(r"\s*<\?xml[^>]*\?>", re.I)

#: 解码候选顺序：UTF-8（带不带 BOM 都行）→ GBK。理由同 ``core/tbx.py``：
#: 国内 Windows 上手工保存的交换文件常是 GBK，只认 UTF-8 会把编码问题
#: 误判成格式问题。UTF-16 刻意不进盲试列表（它的解码太宽松，GBK 字节也能
#: 被"成功"解成乱码），只有带 BOM 才按 UTF-16 解。
_DECODE_CANDIDATES = ("utf-8-sig", "gbk")
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def _local(tag: str) -> str:
    """取元素标签的本地名（剥掉命名空间）。

    TMX 文件带不带默认命名空间完全看导出工具的心情，按本地名匹配才能
    两头都读。``comment`` 之类的标签没有命名空间，原样返回即可。
    """
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _clean_lang(lang: str) -> str:
    """语言码归一化：``*all*``（含各种大小写）当「未知」处理，返回空串。

    header/tu 上的 ``*all*`` 表示"混合语言或未知语言"，而我方记忆库里
    ``src_lang`` 空串表达的正是同一件事。把它读成空串，generate→parse 的
    roundtrip 才自洽（否则空语言会被写成 ``*all*`` 再读回来变成字符串）。
    """
    value = (lang or "").strip()
    if value.lower() == _ALL_LANGS:
        return ""
    return value


def _seg_text(node: ET.Element) -> str:
    """取一个 ``<seg>`` 的文本：保留文本节点，跳过标记类内联标签。

    为什么不能直接用 ``"".join(node.itertext())``：那会把 ``<bpt>`` 里
    ``&lt;b&gt;`` 这类**标记本身的文字**也收进来，记忆里就混进了格式标记。
    这里的规则是：文本节点一律收；``_SKIP_INLINE_TAGS`` 里的元素跳过其
    子树但**保留它的 tail**（tail 是标签后面的正文，丢了会掉字）；其余元素
    （``<hi>/<it>`` 这类样式标签）递归下去把里面的真文本取出来。
    """
    parts: list[str] = [node.text or ""]
    for child in node:
        if _local(child.tag) not in _SKIP_INLINE_TAGS:
            parts.append(_seg_text(child))
        parts.append(child.tail or "")
    return "".join(parts).strip()


def _read_tmx_file(path: Path) -> bytes:
    """读 TMX 文件的原始字节。"""
    if not path.is_file():
        raise ValueError(f"TMX 文件不存在或不可读：{path}")
    return path.read_bytes()


def _read_source(source: str | bytes | os.PathLike[str]) -> bytes:
    """把入参统一成原始字节：路径读文件，XML 文本按 UTF-8 编码。

    三态入参（路径 / 文本 / 字节）是给调用方省事：GUI 拿 FileDialog 的结果、
    CLI 拿 argv、测试想直接喂一小段 XML 都不用先落盘。
    """
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    if isinstance(source, os.PathLike):
        return _read_tmx_file(Path(source))
    if isinstance(source, str):
        stripped = source.lstrip("\ufeff \t\r\n")
        if stripped.startswith("<"):
            return stripped.encode("utf-8")
        return _read_tmx_file(Path(source))
    raise ValueError(
        f"不支持的 TMX 输入类型：{type(source).__name__}（应为路径或 XML 文本）"
    )


def _decode_tmx(raw: bytes) -> str:
    """把 TMX 字节解成文本。带 BOM 的 UTF-16 优先，其余按候选编码试。"""
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


def _normalize_segments(segments: Any) -> list[dict[str, str]]:
    """把调用方给的翻译单元统一成 ``{source, target, src_lang, note}``。

    接受两种形态：字典（键 source/target/src_lang/note 可选）或序列
    （source、target、src_lang、note 依次可选）。原文译文全空的单元没有
    意义，直接丢掉——这是唯一一处静默丢弃，见 docstring 的「容错策略」。
    """
    normalized: list[dict[str, str]] = []
    for item in segments or []:
        if isinstance(item, dict):
            source = str(item.get("source") or "").strip()
            target = str(item.get("target") or "").strip()
            src_lang = str(item.get("src_lang") or "").strip()
            note = str(item.get("note") or "").strip()
        else:
            sequence = list(item)
            source = str(sequence[0]).strip() if len(sequence) > 0 else ""
            target = str(sequence[1]).strip() if len(sequence) > 1 else ""
            src_lang = str(sequence[2]).strip() if len(sequence) > 2 else ""
            note = str(sequence[3]).strip() if len(sequence) > 3 else ""
        if not source and not target:
            continue
        normalized.append(
            {"source": source, "target": target, "src_lang": src_lang, "note": note}
        )
    return normalized


def parse_tmx(source: str | bytes | os.PathLike[str]) -> list[dict[str, str]]:
    """解析 TMX，返回单元列表，每条 ``{"source", "target", "src_lang", "note"}``。

    ``source`` 可以是文件路径（str / Path）或 XML 文本 / 字节。文件不存在、
    XML 拼不出、根元素不是 ``<tmx>`` 时抛 ``ValueError``（人话错误）。没有
    ``<tu>``（合法但空的记忆库）返回空列表，不算错误。

    没有中文 ``tuv`` 的单元会被解析成 ``target=""``（而不是被丢掉）：这句
    原文配不上译文是**数据结构信息**，「导入 Nothing 可写」与「这一条压根
    不存在」是两回事，调用方（``import_segments``）会把它计进 ``unpaired``
    并逐条出声。静默丢弃等于让用户以为全部导入成功了。
    """
    raw = _read_source(source)
    text = _DECLARATION_RE.sub("", _decode_tmx(raw), count=1)

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"不是有效的 TMX 文件：XML 解析失败（{exc}）") from exc

    if _local(root.tag) != "tmx":
        raise ValueError(
            f"不是有效的 TMX 文件：根元素是 <{_local(root.tag)}>，"
            "应为 <tmx>（TMX 翻译记忆交换格式）"
        )

    segments: list[dict[str, str]] = []
    for element in root.iter():
        if _local(element.tag) != "tu":
            continue
        segment = _parse_tu(element)
        if segment is not None:
            segments.append(segment)
    return segments


def _tu_note(element: ET.Element) -> str:
    """``<tu>`` 直下挂的 ``<note>``（``<prop>`` 是另一种写法，本轮不认）。"""
    for child in element:
        if _local(child.tag) == "note":
            text = _seg_text(child)
            if text:
                return text
    return ""


def _tuv_rows(element: ET.Element) -> list[tuple[str, str]]:
    """取一个 tu 下所有 tuv 的 ``(语言码, seg 文本)``，按文档顺序。

    找不到 ``<seg>`` 的 tuv（空 tuv）直接跳过：它没有文本可贡献，但也不会
    让整个单元失效。
    """
    rows: list[tuple[str, str]] = []
    for tuv in element:
        if _local(tuv.tag) != "tuv":
            continue
        text = ""
        for seg in tuv:
            if _local(seg.tag) == "seg":
                text = _seg_text(seg)
                break
        if text:
            rows.append((tuv.get(_XML_LANG, ""), text))
    return rows


def _parse_tu(element: ET.Element) -> dict[str, str] | None:
    """解析单个 ``<tu>``；一个带文本的 tuv 都没有时返回 None（安静跳过）。"""
    note = _tu_note(element)
    rows = _tuv_rows(element)
    if not rows:
        return None

    # 方向锁定：zh 语言集恒当译文，其余第一个当原文（见模块 docstring）。
    # zh 只有 tuv 而没有非 zh 时 → 这是中译英方向的数据，本轮不支持，
    # 原文字段留空（后面会被算作 skipped 并出声），绝不把中文当原文。
    target = next(
        (text for lang, text in rows if lang.lower().startswith(_TARGET_LANG_PREFIX)),
        "",
    )
    source_row = next(
        (
            (lang, text)
            for lang, text in rows
            if not lang.lower().startswith(_TARGET_LANG_PREFIX)
        ),
        None,
    )
    source = source_row[1] if source_row else ""
    declared = _clean_lang(element.get("srclang", ""))
    src_lang = declared or (
        _clean_lang(source_row[0]) if source_row else ""
    )
    return {
        "source": source,
        "target": target,
        "src_lang": src_lang,
        "note": note,
    }


def generate_tmx(segments: Any, tgt_lang: str = DEFAULT_TGT_LANG) -> str:
    """把翻译单元写成合法 TMX XML 文本（带头部声明，UTF-8）。

    单元形态同 ``_normalize_segments``（字典或序列均可）。``tgt_lang`` 是译文
    语言标签，默认 ``zh-Hans``——本工具译入简体中文，方向锁定的另一半含义是
    **反向不在支持范围**（见模块 docstring 第 1 条）。

    输出刻意确定化（无时间戳、无哈希、按入参顺序编号 TU）：同一份单元两次
    生成逐字节一致，roundtrip（generate→parse→generate）才稳定可测——导出的
    文件要能进 git diff，每次字节都变就没人愿意版本化它。
    """
    normalized = _normalize_segments(segments)
    root = ET.Element("tmx", {"version": _TMX_VERSION})
    # header 的 srclang 恒为 *all*：我方记忆库混合源语言，逐个 tu 标真实语言
    # 比一律标 en 诚实（见模块 docstring 第 2 条）。
    ET.SubElement(
        root,
        "header",
        {
            "creationtool": _CREATION_TOOL,
            "segtype": "sentence",
            "adminlang": DEFAULT_TGT_LANG,
            "srclang": _ALL_LANGS,
            "datatype": "PlainText",
            "o-tmf": _ALL_LANGS,
        },
    )
    body = ET.SubElement(root, "body")
    for segment in normalized:
        src_lang = _clean_lang(segment["src_lang"])
        attributes = {"srclang": src_lang or _ALL_LANGS}
        tu = ET.SubElement(body, "tu", attributes)
        source_tuv = ET.SubElement(
            tu, "tuv", {_XML_LANG: src_lang or _ALL_LANGS}
        )
        ET.SubElement(source_tuv, "seg").text = segment["source"]
        target_tuv = ET.SubElement(tu, "tuv", {_XML_LANG: tgt_lang or DEFAULT_TGT_LANG})
        ET.SubElement(target_tuv, "seg").text = segment["target"]
        if segment["note"]:
            ET.SubElement(tu, "note").text = segment["note"]
    ET.indent(root, space="  ")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + ET.tostring(root, encoding="unicode")
        + "\n"
    )


def export_segments(store: Any, only_confirmed: bool = False) -> list[dict[str, str]]:
    """从 StateStore 收集可导出的翻译记忆。

    默认**全导**——入库门槛本来就是「零问题遗留」（``core/quality/pipeline.py``
    只在某段没有任何遗留问题且译文与原文不同时才 ``tm.add``），所以库里每
    一条都已经背书过了；这与 TBX 默认只导已采用**故意不同**，理由写在模块
    docstring 里。``only_confirmed=True`` 是更严的口径：只导人工确认过的
    （译后编辑改出来的 ``origin="human", confirmed=1``）。

    返回值按 source 排序：这样同一份记忆导出的文件稳定可 diff，不受 SQLite
    返回顺序与 ``updated_at`` 并列时的抖动影响。没有 notes 列的概念——记忆
    库没有备注字段，一律给空串，保证与 ``parse_tmx`` 的输出形状一致。
    """
    rows: list[dict[str, Any]] = []
    try:
        rows = list(store.all_tm_full())
    except Exception:
        rows = []
    if not rows:
        try:
            rows = [
                {"source": source, "target": target, "src_lang": "", "confirmed": False}
                for source, target in store.all_tm()
            ]
        except Exception:
            rows = []
    segments: list[dict[str, str]] = []
    for row in rows:
        source = str(row.get("source") or "").strip()
        target = str(row.get("target") or "").strip()
        if not source or not target:
            continue  # 没有配对的原文/译文，导出没有意义
        if only_confirmed and not row.get("confirmed"):
            continue
        segments.append(
            {
                "source": source,
                "target": target,
                "src_lang": str(row.get("src_lang") or "").strip(),
                "note": "",
            }
        )
    segments.sort(key=lambda item: item["source"])
    return segments


def _clip(text: str, limit: int = 24) -> str:
    """日志/提示里用的一小段原文。宁可截断也不要把整段刷屏。"""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def import_segments(
    memory: Any,
    segments: Any,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """把 ``parse_tmx`` 的结果灌进翻译记忆，返回计数与冲突明细。

    去重规则与 ``core/tbx.py::import_entries`` **一字不差**：已存在的原文
    一律保留库内原值，绝不用外部 TMX 覆盖，冲突逐条出声并收进 ``conflicts``。
    之所以连口径都不重新设计：记忆和术语都是用户攒下来的资产，两处规则不一
    致迟早会出现「术语没被覆盖、记忆被覆盖了」这种说不清的事故。

    返回 ``{"added", "kept", "skipped", "conflicts", "unpaired"}``：

    * ``added``——新进库条数（``origin="tmx"``、``confirmed=0``：外部文件
      带来的译文没有被这个工具校验过，也不该自动获得「已确认」资格）；
    * ``kept``——已存在未导入条数；
    * ``skipped``——连原文都没有（或原文与译文相同、没有记忆价值）的条数；
    * ``unpaired``——**有原文但没有中文译文**的条数（逐条出声，理由见下）；
    * ``conflicts``——``[(原文, 库内译文, 未导入译文)]``。

    ``unpaired`` 单独计数而不是混进 skipped：一个只有外文原文、没有中文
    译文的 tu 是外部工具导出的常态（特别是不同语言对混在一个文件里），
    用户需要知道「有 N 句没能进来」，而不是被一个「跳过 N 条」糊过去。
    """
    added = 0
    kept = 0
    skipped = 0
    unpaired = 0
    conflicts: list[tuple[str, str, str]] = []
    for segment in _normalize_segments(segments):
        source = segment["source"]
        target = segment["target"]
        if not source:
            skipped += 1
            continue
        if not target or target == source:
            # 没有中文译文（或译文就是原文）：不是一句可用的记忆
            unpaired += 1
            if log is not None:
                log(f"TMX 导入跳过：{_clip(source)} 没有中文译文，未入库")
            continue
        existing = memory.store.get_tm(source)
        if existing is not None:
            # 用户既有资产优先：外部 TMX 说啥都不覆盖，只出声说明保留了什么。
            kept += 1
            old = (existing or "").strip() or "（空）"
            conflicts.append((source, old, target))
            if log is not None:
                log(
                    f"TMX 导入冲突保留：{_clip(source)}（库内「{_clip(old, 16)}」，"
                    f"未导入「{_clip(target, 16)}」）"
                )
            continue
        memory.add(source, target, origin="tmx", confirmed=0, src_lang=segment["src_lang"])
        added += 1
    return {
        "added": added,
        "kept": kept,
        "skipped": skipped,
        "conflicts": conflicts,
        "unpaired": unpaired,
    }
