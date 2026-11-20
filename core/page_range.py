"""文档范围（`--pages=` 与界面「范围」框）的解析：PDF 的页、PPT 的幻灯片、EPUB 的章节。

这个模块只管一件事：**把用户手写的区间串换成序号**。之所以单独成文，是因为写错
页码的人通常正赶着交差，报错必须直接告诉他哪一段看不懂、正确写法长什么样，而不是
甩一句「格式错误」让他自己猜。

三种容器共用同一套写法，但**报错与回显必须按各自的量词说**（`Unit`）：对一份 PPT
念「这份 PDF 只有 6 页」是假话，而用户正是靠这句话判断自己写错在哪——量词错了，
他得先把「张」翻译成「页」才能读懂错误。PDF 那套字符串与 v1.6.8 逐字一致，
封版验收与交接稿 §5 决策 63 都按它断言过，不因泛化而改动。

口径（与 PDFMathTranslate 的 `-p` 对齐，但按本工具的中文输入习惯放宽）：

* 序号**从 1 开始**，与阅读器/PPT 页脚显示的页码一致；`parse_pages` 返回 0 基，循环直接用。
* 分隔符认半角/全角逗号、顿号、分号与空格；区间符认半角连字符与全角/波浪线。
  用户不会为了一个输入框特意切回英文输入法，这是必须兜的。
* `1-3,5` = 第 1、2、3、5 页；`-3` = 前 3 页；`8-` = 第 8 页到最后一页。
* 越界、起始大于结束、非数字——一律 `TranslationError`，**不做静默夹取**。
  把「第 20 页」悄悄夹到一份 12 页文档的第 12 页，用户会以为真翻到了那一页。

解析分两步是有原因的：界面里用户写完范围时**还没有页数**（可能排了多个文件），
所以先 `describe()` 只验写法并回显，真正的上界检查留到 `parse_pages(spec, count, unit)`
按每个文件自己的数量去做——那里的错误才知道「这份只有 12 页」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.common import TranslationError

#: 全角/异形区间符 → 半角连字符
_DASHES = "－–—‒~～―"
#: 全角/异形分隔符 → 半角逗号
_SEPARATORS = "，、；;｜|"


@dataclass(frozen=True)
class Unit:
    """一种「可以按序号挑选一部分」的容器怎么说人话。

    五个字段分别管报错里的五个位置，凑齐了才能读通：
    「幻灯片范围「5-3」的起始张 5 大于结束张 3。想同时要这两张请分开写：3,5。」
    """

    label: str  # 这条范围叫什么：页范围 / 幻灯片范围 / 章节范围
    counter: str  # 量词：页 / 张 / 章
    kind: str  # 容器名：PDF / PPT / EPUB
    seq: str  # 「页码从 1 开始」里的那个词；PPT/EPUB 没有自然说法，用「序号」
    empty_noun: str  # 「这份 PDF 没有页面」里的宾语


PAGES = Unit("页范围", "页", "PDF", "页码", "页面")
SLIDES = Unit("幻灯片范围", "张", "PPT", "序号", "幻灯片")
CHAPTERS = Unit("章节范围", "章", "EPUB", "序号", "章节")
#: 界面上那一栏与它的即时回显用：写这栏时还不知道它最终落在哪种容器上，
#: 报错里念「页范围」会让 PPT 用户以为自己的输入框被当成 PDF 处理了。
#: 量词暂按最习惯的「页」念，精确的张/章由各 translate_* 按自己的 Unit 念。
NEUTRAL = Unit("文档范围", "页", "文档", "页码", "内容")

#: 界面上那一栏的口径说明。同一串数字在三种容器里数的是不同的东西，而排队时
#: 还看不出这份范围最终落在哪种文件上，所以回显只能给这一句中性的话，
#: 「第 N 张 / 第 N 章」这种精确量词留到每个文件自己开翻时由它按 unit 念。
SCOPE_NOTE = "PDF 按页、PPT 按张、EPUB 按章，各按自己那份文件的总数校验"
#: 认范围的扩展名，其余格式（Word、Excel、字幕、图片）没有可挑的序号界。
RANGE_SUFFIXES = (".pdf", ".pptx", ".epub")


def _example(unit: Unit) -> str:
    return (
        f"正确写法如 1-3,5；第 8 {unit.counter}到最后一{unit.counter}写 8-；"
        "整份文档请把这一栏留空。"
    )

#: 一个区间项：两端都是 1 基闭区间，`end is None` 表示「到最后一页」
Item = tuple[int, int | None]


def _to_number(text: str, spec: str, unit: Unit) -> int:
    if not text.isdigit():
        raise TranslationError(f"看不懂{unit.label}里的「{text}」。{_example(unit)}")
    try:
        value = int(text)
    except ValueError:
        # isdigit() 对「²」这类上标数字也返回 True，而 int() 不认。
        raise TranslationError(f"看不懂{unit.label}里的「{text}」。{_example(unit)}") from None
    if value == 0:
        raise TranslationError(
            f"{unit.seq}从 1 开始，没有第 0 {unit.counter}（你写了「{spec}」）。{_example(unit)}"
        )
    return value


def _split(spec: str, unit: Unit) -> list[str]:
    text = (spec or "").strip()
    if not text:
        raise TranslationError(f"{unit.label}是空的。{_example(unit)}")
    for dash in _DASHES:
        text = text.replace(dash, "-")
    for separator in _SEPARATORS:
        text = text.replace(separator, ",")
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"\s+", ",", text)
    items = [part.strip() for part in text.split(",") if part.strip()]
    if not items:
        raise TranslationError(f"{unit.label}「{spec}」里没有可用的{unit.seq}。{_example(unit)}")
    return items


def _parse_item(item: str, spec: str, unit: Unit) -> Item:
    """把一个区间项解析成 1 基闭区间；`end is None` = 到最后一页。"""
    if "-" in item:
        head, _, tail = item.partition("-")
        if not head and not tail:
            raise TranslationError(f"看不懂{unit.label}里的「{item}」。{_example(unit)}")
        start = _to_number(head, spec, unit) if head else 1
        end = _to_number(tail, spec, unit) if tail else None
        if end is not None and start > end:
            raise TranslationError(
                f"{unit.label}「{item}」的起始{unit.counter} {start} 大于结束{unit.counter} {end}。"
                f"想同时要这两{unit.counter}请分开写：{end},{start}。"
            )

    else:
        start = _to_number(item, spec, unit)
        end = start
    return start, end


def describe(spec: str, unit: Unit = PAGES) -> str:
    """只验写法并回显成人话，不需要数量。界面上的即时反馈用这个。"""
    parts: list[str] = []
    for item in _split(spec, unit):
        start, end = _parse_item(item, spec, unit)
        if end is None:
            parts.append(
                f"第 {start} {unit.counter}到最后一{unit.counter}"
                if start > 1
                else "整份文档"
            )
        elif start == end:
            parts.append(f"第 {start} {unit.counter}")
        else:
            parts.append(f"第 {start}-{end} {unit.counter}")
    return "、".join(parts)


def parse_pages(spec: str, page_count: int, unit: Unit = PAGES) -> list[int]:
    """把 `spec` 解析成升序去重的 0 基序号列表，按 `page_count` 校验上界。"""
    if page_count <= 0:
        raise TranslationError(f"这份 {unit.kind} 没有{unit.empty_noun}，谈不上{unit.label}。")
    selected: set[int] = set()
    for item in _split(spec, unit):
        start, end = _parse_item(item, spec, unit)
        last = page_count if end is None else end
        if last > page_count:
            raise TranslationError(
                f"{unit.label}「{item}」超出文档{unit.counter}数："
                f"这份 {unit.kind} 只有 {page_count} {unit.counter}，"
                f"你写到第 {last} {unit.counter}。"
            )
        selected.update(range(start - 1, last))
    return sorted(selected)


def format_pages(indices: list[int]) -> str:
    """把 0 基页号列表压回「1-3、5、7-9」这样的摘要，只用于日志回报。"""
    if not indices:
        return "无"
    runs: list[tuple[int, int]] = []
    start = previous = indices[0]
    for index in indices[1:]:
        if index == previous + 1:
            previous = index
            continue
        runs.append((start, previous))
        start = previous = index
    runs.append((start, previous))
    return "、".join(
        f"{head + 1}" if head == tail else f"{head + 1}-{tail + 1}"
        for head, tail in runs
    )
