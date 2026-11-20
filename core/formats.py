"""支持格式的唯一登记表（v1.9.11，X4 收口）。

「一种格式算数」这件事过去写了五遍：``app.py`` 的三组后缀常量、文件对话框的
分组、卡片角标与配色、meta 行的中文名、``core/postedit.py`` 的 kind 与可回写
集合。加一种格式要挨个改，漏一处就露出半截状态——v1.9.10 之前对话框的「全部
支持」与「图片」两组一直漏掉 ``.bmp`` / ``.webp``，拖拽却照收。

现在格式的身份只在这里登记一次：``FORMATS`` 一行一个后缀，其余各处按
``category`` 查后缀、按 ``kind`` 查译后编辑口径。

- ``category``：``doc`` / ``script`` / ``image``，决定界面分组的粗类。
- ``name``：界面上的一句话，用于 meta 行与对话框的单项过滤。
- ``kind``：译后编辑清单里的格式名，也是回写 writer 的键；``None`` 表示这种
  格式不出编辑清单（图片是绘制成品，没有可寻址段落）。
- ``writable``：``postedit.apply_edits`` 能否把改后的译文写回成品。

**加一种格式**：在 ``FORMATS`` 里加一行，app 的后缀分组、对话框、角标文案与
``postedit`` 的 kind/可回写集合自动跟上。翻译管线本身仍要在 ``app.py`` 的分派
里加一个分支——那一层是真正的实现，不是登记。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Format:
    """一种成品格式的登记信息。"""

    suffix: str
    category: str
    name: str
    kind: str | None = None
    writable: bool = False


#: 登记表。顺序即界面上「全部支持」与分组过滤的枚举顺序。
FORMATS: tuple[Format, ...] = (
    Format(".docx", "doc", "Word 文档", kind="docx", writable=True),
    # PDF 成品是绘制文本，进编辑清单但**不可回写**（见 core/postedit.py）。
    Format(".pdf", "doc", "PDF 文档", kind="pdf"),
    Format(".pptx", "doc", "PPT 演示", kind="pptx", writable=True),
    Format(".epub", "doc", "EPUB 电子书", kind="epub", writable=True),
    Format(".srt", "script", "Galgame 脚本", kind="srt", writable=True),
    Format(".vtt", "script", "Galgame 脚本", kind="vtt", writable=True),
    Format(".json", "script", "Galgame 脚本", kind="json", writable=True),
    Format(".xlsx", "script", "Excel / T++ 表格", kind="tpp", writable=True),
    Format(".png", "image", "图片（OCR 翻译）"),
    Format(".jpg", "image", "图片（OCR 翻译）"),
    Format(".jpeg", "image", "图片（OCR 翻译）"),
    Format(".bmp", "image", "图片（OCR 翻译）"),
    Format(".webp", "image", "图片（OCR 翻译）"),
)

#: 界面上「图片」一类的简称，用于卡片角标。
CATEGORY_BADGE = {"doc": "", "script": "脚本", "image": "图片"}

_BY_SUFFIX: dict[str, Format] = {item.suffix: item for item in FORMATS}


def get(suffix: str) -> Format | None:
    """按后缀取登记信息（大小写不敏感）；不认识的格式返回 None。"""
    return _BY_SUFFIX.get((suffix or "").lower())


def of(category: str) -> tuple[Format, ...]:
    """某一粗类下的全部格式，顺序同 ``FORMATS``。"""
    return tuple(item for item in FORMATS if item.category == category)


def suffixes(category: str | None = None) -> tuple[str, ...]:
    """某一粗类（或全部）的后缀，顺序同 ``FORMATS``。"""
    items = FORMATS if category is None else of(category)
    return tuple(item.suffix for item in items)


def doc_suffixes() -> tuple[str, ...]:
    return suffixes("doc")


def script_suffixes() -> tuple[str, ...]:
    return suffixes("script")


def image_suffixes() -> tuple[str, ...]:
    return suffixes("image")


def all_suffixes() -> tuple[str, ...]:
    return suffixes()


def kind_by_suffix() -> dict[str, str]:
    """后缀 → 译后编辑清单里的 kind（不出清单的格式不在里面）。"""
    return {item.suffix: item.kind for item in FORMATS if item.kind}


def writable_suffixes() -> frozenset[str]:
    """``postedit.apply_edits`` 能回写的后缀集合。"""
    return frozenset(item.suffix for item in FORMATS if item.writable)


def category_text(
    category: str, *, limit: int | None = None, ellipsis: bool = False
) -> str:
    """把某一类的后缀写成「.a / .b / .c」，给界面与报错文案用。

    ``limit`` 只取前几个；``ellipsis`` 表示后面还有被省略的（念「等」），
    与旧文案「.png / .jpg 等图片文件」逐字对齐。
    """
    items = [item.suffix for item in of(category)]
    if limit is not None:
        hidden = len(items) > limit
        items = items[:limit]
    else:
        hidden = False
    text = " / ".join(items)
    return f"{text} 等" if ellipsis and hidden else text


def dialog_groups() -> list[tuple[str, str]]:
    """文件对话框的过滤分组：粗类分组 + 文档逐项，生成自下表。"""
    def patterns(category: str) -> str:
        return " ".join(f"*{item.suffix}" for item in of(category))

    groups = [
        ("全部支持", " ".join(f"*{item.suffix}" for item in FORMATS)),
        ("文档（Word/PDF/PPT/EPUB）", patterns("doc")),
        ("脚本（字幕/JSON/T++表格）", patterns("script")),
        ("图片（OCR 翻译）", patterns("image")),
    ]
    groups.extend((item.name, f"*{item.suffix}") for item in of("doc"))
    groups.append(("所有文件", "*.*"))
    return groups
