"""分段规则的用户可编辑表（v1.8.0，差距清单 C5 剩下的另一半「规则表可编辑」）。

v1.6.9 把「一次送多少段/多少字」这类**阈值**开放成命令行旋钮，但没有动**切在哪里**的规则：
哪些算句末标点、什么情况下不许下刀，全部写死在 ``core/segment.py`` 里。真实文档最常不服的两件事：

1. 西文缩写后面照样是空格——``e.g. this is`` / ``No. 3`` 会被「标点须跟空白才切」那条
   从缩写中间劈开，两半各送一次翻译，回来就是错译；
2. 编号不是句子结尾——``1.2.3 系统概述`` 末尾那个点也会被当成句边界，编号单独成句后
   离线小模型会把它译成「系统 1.2.3」。

本模块提供一张 JSON 表来改这两件事。四个字段，**默认空表 = 现有切句行为逐字不变**：

- ``追加句末标点``：单个字符，紧跟其后即可切开（中文式，不要求后面有空白）；
- ``追加句末标点_需空白``：单个字符，须后面紧跟空白才算句边界（西文式）；
- ``不切短语``：字面串，候选切点的**前文**以它结尾就放弃这一刀；
- ``不切正则``：同上但自己写正则，命中即不切。

刻意**没有**做成「用户自带整套切句正则」：那等于把 ``segment.py`` 的主循环（含小数点
判定、引号归属、换行优先）交给一段无法校验的外部脚本，一份写坏的正则能把整份文档切成
碎末，而且没有任何地方能提前发现。表只能**收紧与放宽边界判定**，主干仍在本项目手里。

护栏三条：单条正则 ≤ ``MAX_PATTERN_CHARS`` 字符、每类 ≤ ``MAX_ENTRIES`` 条、回看窗口固定
``LOOKBACK`` 字符——回看窗口是对灾难性回溯唯一的缓解手段（命中范围越短，嵌套尝试越少），
**这不是数学保证，如实记为已知边界**。认不出的写法（JSON 语法错、顶层不是对象、正则编译
失败、标点不是单个字符）一律出声并跳过该条，其余照常生效；只有整份文件读不了才退回内置
默认——切句规则写错只影响断句好不好看，不该挡住整份翻译，与 ``core/tuning.py`` 同口径。

规则文件落在 ``app_root() / 分段规则.json``（与 ``自定义引擎.json`` 同一处，用户看得见改得了），
含标点与正则、**不含任何文档内容**，因此保密模式照读不误，也不新增落盘。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .common import app_root

#: 出声口：默认吞掉，命令行与自检脚本传 print。
Silence = Callable[[str], None]

RULES_FILE_NAME = "分段规则.json"

MAX_PATTERN_CHARS = 200
MAX_ENTRIES = 64
LOOKBACK = 80

KEY_CJK = "追加句末标点"
KEY_LATIN = "追加句末标点_需空白"
KEY_PHRASES = "不切短语"
KEY_REGEX = "不切正则"
KEY_NOTE = "_说明"
VALID_KEYS = (KEY_CJK, KEY_LATIN, KEY_PHRASES, KEY_REGEX, KEY_NOTE)

#: ``--dump-segment-rules`` 写出的模板：例子都是真会踩穿的形态，不是占位文案。
TEMPLATE: dict = {
    KEY_NOTE: (
        "分段规则表：只改「哪里算句边界」，不改翻译本身。四栏都可省略，省略即按程序内置规则。"
        "追加句末标点里的每一项必须是单个字符；不切短语按字面匹配，不切正则自己写。"
        "本文件可以整份删掉，删掉就是默认行为。"
    ),
    KEY_CJK: [],
    KEY_LATIN: [],
    KEY_PHRASES: ["e.g.", "i.e.", "No.", "Fig.", "vs."],
    KEY_REGEX: [r"\d+(?:\.\d+)*\.$"],
}


@dataclass(frozen=True)
class Rules:
    """生效中的规则。空实例就是内置行为。"""

    cjk_extra: str = ""
    latin_extra: str = ""
    blockers: tuple[re.Pattern, ...] = ()

    @property
    def is_default(self) -> bool:
        return not self.cjk_extra and not self.latin_extra and not self.blockers

    def summary(self) -> str:
        parts: list[str] = []
        if self.cjk_extra:
            parts.append(f"紧跟即切的标点 {len(self.cjk_extra)} 个（{''.join(self.cjk_extra)}）")
        if self.latin_extra:
            parts.append(f"须跟空白的标点 {len(self.latin_extra)} 个（{''.join(self.latin_extra)}）")
        if self.blockers:
            parts.append(f"不许下刀的规则 {len(self.blockers)} 条")
        return "、".join(parts)


def rules_file() -> Path:
    return app_root() / RULES_FILE_NAME


def _single_chars(raw: object, key: str, log: Silence, source: Path) -> str:
    """把「追加句末标点」这类字段收成一条字符串；不是单字符的项点名丢弃。"""
    if raw is None:
        return ""
    if not isinstance(raw, list):
        log(f"分段规则 {source.name} 的「{key}」要一个列表，这一栏整个忽略。")
        return ""
    chars: list[str] = []
    for item in raw[:MAX_ENTRIES]:
        if len(item) != 1:
            log(f"分段规则「{key}」里的 {item!r} 不是单个字符，这条忽略——多字符的写法请放「{KEY_PHRASES}」。")
            continue
        chars.append(item)
    if len(raw) > MAX_ENTRIES:
        log(f"分段规则「{key}」超过 {MAX_ENTRIES} 条，只取前面 {MAX_ENTRIES} 条。")
    return "".join(dict.fromkeys(chars))


def _blocker(item: object, label: str, log: Silence, *, literal: bool) -> re.Pattern | None:
    """编译一条「不许下刀」规则；短语按字面转义，锚在候选切点之前。"""
    if not isinstance(item, str) or not item:
        log(f"分段规则「{label}」里有一条不是文字，忽略。")
        return None
    if len(item) > MAX_PATTERN_CHARS:
        log(
            f"分段规则「{label}」里有一条超过 {MAX_PATTERN_CHARS} 字符，忽略——"
            "这么长的正则没人能在出错后看懂它。"
        )
        return None
    body = re.escape(item) if literal else item
    try:
        return re.compile(f"(?:{body})$")
    except re.error as exc:
        log(f"分段规则「{label}」里的 {item!r} 不是合法正则（{exc.msg}），忽略这一条。")
        return None


def build(data: object, log: Silence, source: Path | None = None) -> Rules:
    """把表里的字段编成 :class:`Rules`；坏条目逐条出声并跳过。"""
    if not isinstance(data, dict):
        name = source.name if source else RULES_FILE_NAME
        log(f"分段规则 {name} 的最外层不是 {{...}}，整份规则按内置处理。")
        return Rules()
    if source is not None:
        unknown = [k for k in data if k not in VALID_KEYS]
        if unknown:
            log(
                "分段规则里有认不出的栏位：" + "、".join(str(k) for k in unknown)
                + f"；可用的只有 {'、'.join(VALID_KEYS)}。"
            )
    cjk = _single_chars(data.get(KEY_CJK), KEY_CJK, log, source or Path(RULES_FILE_NAME))
    latin = _single_chars(data.get(KEY_LATIN), KEY_LATIN, log, source or Path(RULES_FILE_NAME))
    blockers: list[re.Pattern] = []
    for key, literal in ((KEY_PHRASES, True), (KEY_REGEX, False)):
        raw = data.get(key)
        if raw is None:
            continue
        if not isinstance(raw, list):
            log(f"分段规则「{key}」要一个列表，这一栏整个忽略。")
            continue
        if len(raw) > MAX_ENTRIES:
            log(f"分段规则「{key}」超过 {MAX_ENTRIES} 条，只取前面 {MAX_ENTRIES} 条。")
        for item in raw[:MAX_ENTRIES]:
            pattern = _blocker(item, key, log, literal=literal)
            if pattern is not None:
                blockers.append(pattern)
    return Rules(cjk_extra=cjk, latin_extra=latin, blockers=tuple(blockers))


def load(path: Path, log: Silence) -> Rules:
    """读一份规则文件；读不了就出声退回内置规则，绝不因此中断翻译。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError:
        log(f"分段规则文件 {path.name} 读不出来（权限或路径问题），这一份按内置规则走。")
        return Rules()
    except json.JSONDecodeError as exc:
        log(f"分段规则文件 {path.name} 不是合法 JSON（第 {exc.lineno} 行：{exc.msg}），按内置规则走。")
        return Rules()
    return build(data, log, path)


def startup(log: Silence) -> tuple[Rules, Path | None]:
    """程序启动时用一次：工具目录下有 ``分段规则.json`` 就读，没有就安静返回默认。"""
    path = rules_file()
    if not path.exists():
        return Rules(), None
    return load(path, log), path


def write_template(log: Silence, path: Path | None = None) -> tuple[bool, Path]:
    """生成一份可编辑的规则表；已存在的文件绝不覆盖。"""
    target = path or rules_file()
    if target.exists():
        log(f"分段规则文件已经在这里，没有动它：{target}")
        return False, target
    target.write_text(
        json.dumps(TEMPLATE, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log(f"已生成分段规则模板：{target}")
    log("改完保存即可，下一次翻译自动生效；这一栏整份删掉就是回到内置规则。")
    return True, target


_ACTIVE: Rules = Rules()


def set_active(rules: Rules) -> None:
    global _ACTIVE
    _ACTIVE = rules


def active() -> Rules:
    return _ACTIVE
