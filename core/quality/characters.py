"""角色卡：把"这个人怎么说话"变成可注入、可校验的结构化字段。

为什么不能是一段描述文本
------------------------
GalTransl 的 GPT 字典允许用户写"桜是个傲娇，嘴上不饶人"。这种自由文本
有两个问题：① 没法校验——Verifier 无法判断译文是否违反了"傲娇"；
② 没法跨批次稳定——每批提示词里那段话的位置、措辞都可能变。

结构化字段（自称/对称/语气基线/禁用表达）既能稳定注入提示词，
又能让规则层做粗检：比如"禁用表达"里的词出现在译文里就是实打实的违规。

性别字段的用途要特别说明：它只服务于**人称粗检**（规则层），而且判定
刻意保守——只在"整段只有这一个角色名"时才报，宁可漏报也不误报。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

#: 角色卡的字段名。写库/读库都走这套 key，避免各处各写各的。
FIELDS = ("first_person", "second_person", "tone", "forbidden", "gender", "notes")


@dataclass
class CharacterSheet:
    """一个角色的说话方式档案。所有字段都可空——空卡等于没有卡。"""

    name: str
    #: 自称：我 / 人家 / 僕 / 俺 / 私……
    first_person: str = ""
    #: 对称：对听话人用什么称呼（你 / 您 / 小子 / 你们……）
    second_person: str = ""
    #: 语气基线：敬语 / 常体 / 粗鲁 / 古风 / 傲娇……
    tone: str = ""
    #: 禁用表达：这个角色绝不应该出现的词（"人家"对一个古风角色就是禁用）
    forbidden: tuple[str, ...] = ()
    #: male / female / other。只用于人称粗检，不参与提示词。
    gender: str = ""
    #: 自由备注：关系、身份背景等，会进提示词但不参与校验。
    notes: str = ""

    # ------------------------------------------------------------ 序列化

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "first_person": self.first_person,
            "second_person": self.second_person,
            "tone": self.tone,
            "forbidden": list(self.forbidden),
            "gender": self.gender,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CharacterSheet":
        forbidden = data.get("forbidden") or ()
        if isinstance(forbidden, str):
            forbidden = tuple(
                item.strip() for item in re_split(forbidden) if item.strip()
            )
        return cls(
            name=str(data.get("name") or "").strip(),
            first_person=str(data.get("first_person") or "").strip(),
            second_person=str(data.get("second_person") or "").strip(),
            tone=str(data.get("tone") or "").strip(),
            forbidden=tuple(str(item).strip() for item in forbidden if str(item).strip()),
            gender=str(data.get("gender") or "").strip(),
            notes=str(data.get("notes") or "").strip(),
        )

    # ------------------------------------------------------------ 渲染

    def is_empty(self) -> bool:
        return not (
            self.first_person
            or self.second_person
            or self.tone
            or self.forbidden
            or self.notes
        )

    def to_prompt(self) -> str:
        """渲染成提示词里的一行。空字段直接不出现，不浪费 token。"""
        if self.is_empty():
            return ""
        parts: list[str] = []
        if self.first_person:
            parts.append(f"自称用「{self.first_person}」")
        if self.second_person:
            parts.append(f"称呼对方用「{self.second_person}」")
        if self.tone:
            parts.append(f"语气基线：{self.tone}")
        if self.forbidden:
            parts.append(f"绝不要使用：{'、'.join(self.forbidden)}")
        if self.notes:
            parts.append(self.notes)
        return "；".join(parts)

    def violations(self, translation: str) -> list[str]:
        """译文里出现了哪些禁用表达。空译文/空禁用表返回空列表。"""
        if not translation or not self.forbidden:
            return []
        return [word for word in self.forbidden if word in translation]


def re_split(text: str) -> list[str]:
    """禁用表达的分隔符宽松些：中英文逗号/分号/斜杠都认。"""
    import re

    return re.split(r"[,，;；/、\s]+", text)


class CharacterStore:
    """角色卡的持久化。底层就是 StateStore 的 characters 表。

    这里再包一层是为了：① 提示词渲染有唯一出处；② 调用方不用关心 JSON。
    """

    def __init__(self, store) -> None:
        self.store = store

    def save(self, sheet: CharacterSheet) -> None:
        if not sheet.name:
            return
        self.store.upsert_character(sheet.name, sheet.to_dict())

    def get(self, name: str) -> CharacterSheet | None:
        data = self.store.get_character(name)
        if not data:
            return None
        return CharacterSheet.from_dict(data)

    def all(self) -> dict[str, CharacterSheet]:
        return {
            name: CharacterSheet.from_dict(data)
            for name, data in self.store.all_characters().items()
        }

    def present_sheets(self, names: Iterable[str]) -> list[tuple[str, str]]:
        """返回（角色名, 渲染后的提示词）——只包含真有内容的卡。"""
        result: list[tuple[str, str]] = []
        for name in names:
            sheet = self.get(name)
            if sheet is None:
                continue
            rendered = sheet.to_prompt()
            if rendered:
                result.append((name, rendered))
        return result
