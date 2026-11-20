"""支持格式登记表（v1.9.11，X4）：一种格式只在 core/formats.py 登记一次。

这些用例守的不是「表里有什么」，而是「各处的表是不是同一张」——app 的三组
后缀、文件对话框、提示文案、角标，postedit 的 kind 与可回写集合，脚本前端，
全部派生自 formats。哪天有人图省事又写死一份，这里会先红。
"""
from __future__ import annotations

import app
from core import formats


def test_registry_rows_are_well_formed():
    suffixes = [item.suffix for item in formats.FORMATS]
    assert len(suffixes) == len(set(suffixes)), "同一个后缀被登记了两次"
    for item in formats.FORMATS:
        assert item.suffix.startswith(".")
        assert item.category in ("doc", "script", "image")
        assert item.name
        if item.writable:
            assert item.kind, f"{item.suffix} 说能回写，却没有编辑清单的 kind"


def test_app_suffix_groups_come_from_the_registry():
    assert app.DOC_SUFFIXES == (".docx", ".pdf", ".pptx", ".epub")
    assert app.SCRIPT_SUFFIXES == (".srt", ".vtt", ".json", ".xlsx")
    assert app.IMAGE_SUFFIXES == (".png", ".jpg", ".jpeg", ".bmp", ".webp")
    assert app.ALL_SUFFIXES == (
        app.DOC_SUFFIXES + app.SCRIPT_SUFFIXES + app.IMAGE_SUFFIXES
    )


def test_postedit_tables_come_from_the_registry():
    from core import postedit

    assert postedit._KIND_BY_SUFFIX == {
        ".docx": "docx",
        ".pdf": "pdf",
        ".pptx": "pptx",
        ".epub": "epub",
        ".srt": "srt",
        ".vtt": "vtt",
        ".json": "json",
        ".xlsx": "tpp",
    }
    assert postedit._SUPPORTED_SUFFIXES == formats.writable_suffixes()
    # 说能回写就得真有 writer：少一把钥匙，用户会在「保存」那一步才看到
    # 「暂不支持回写」，那时译文已经改完了。
    assert set(postedit._WRITERS) == set(postedit._SUPPORTED_SUFFIXES)


def test_script_frontend_shares_the_same_group():
    from core import script_translator

    assert script_translator.SUPPORTED_SUFFIXES == formats.script_suffixes()


def test_dialog_groups_are_generated_and_cover_every_suffix():
    groups = formats.dialog_groups()
    everything = dict(groups)["全部支持"].split()
    assert everything == [f"*{suffix}" for suffix in formats.all_suffixes()]
    # 回归：v1.9.10 之前「全部支持」与「图片」两组漏掉 .bmp / .webp——拖拽收、
    # 对话框里却选不到。
    assert "*.bmp" in everything and "*.webp" in everything
    assert dict(groups)["图片（OCR 翻译）"] == "*.png *.jpg *.jpeg *.bmp *.webp"
    assert groups[-1] == ("所有文件", "*.*")
    for item in formats.of("doc"):
        assert (item.name, f"*{item.suffix}") in groups


def test_hint_text_lists_registry_suffixes():
    assert app._format_hint() == (
        "只支持 .docx / .pdf / .pptx / .epub 文档，.srt / .vtt / .json / .xlsx 脚本，"
        "或 .png / .jpg 等图片文件。"
    )
    assert app._format_hint(
        "支持 ", ellipsis=False, image_tail="图片，译文保存在原文件旁边。"
    ) == (
        "支持 .docx / .pdf / .pptx / .epub 文档，.srt / .vtt / .json / .xlsx 脚本，"
        "或 .png / .jpg 图片，译文保存在原文件旁边。"
    )


def test_badges_and_colors_come_from_the_registry():
    assert app._badge_text(formats.get(".docx")) == "WORD"
    assert app._badge_text(formats.get(".pptx")) == "PPT"
    assert app._badge_text(formats.get(".xlsx")) == "脚本"
    assert app._badge_text(formats.get(".webp")) == "图片"
    assert app._badge_colors(formats.get(".pdf")) == ("#fdecea", app.DANGER)
    assert app._badge_colors(formats.get(".webp")) == ("#f3effc", "#7c3aed")


def test_unknown_suffix_is_ignored():
    assert formats.get(".doc") is None
    assert formats.get("") is None
    assert formats.get(".DOCX") is formats.get(".docx")
