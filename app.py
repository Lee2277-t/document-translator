from __future__ import annotations

import ctypes
import difflib
import logging
import os
import queue
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core.common import (
    CancelledError,
    TranslationError,
    app_root,
    check_cancel,
    data_root,
    human_size,
    shown_path,
)
from core import common, direction, engine_registry, formats, glossary, page_range, segment_rules, tuning
from core.pdf_translator import translate_pdf
from core.style import CONCISE, FORMAL, STANDARD, STYLES, label as style_label
from core.translator import Translator
from core.word_translator import translate_word

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:
    DND_FILES = None
    TkinterDnD = None


APP_DIR = app_root()
CACHE_DIR = data_root() / "cache"
LOG_DIR = data_root() / "logs"
ICON_PATH = APP_DIR / "assets" / "icon.ico"

#: 输出编码分两条路走，两条都不许把已经成功的翻译拖下水：
#: ① **连着控制台**（`isatty()` 真）保持本机代码页——cmd 下眼看中文的用户照旧看到中文，只是
#:    GBK 装不下的那几个字符（`›`、`≈`、emoji）降级成 \uXXXX 写法。不降级就会抛
#:    `UnicodeEncodeError`：成品已经生成好了，用户看到的却是一屏 traceback、退码非 0
#:    （v1.8.0 冻结包验收第 5 项就是这么红的）。
#: ② **被重定向或走管道**（`isatty()` 假）整条换成 UTF-8——脚本侧不必再「先试 UTF-8、解不动退 GBK」，
#:    那是 v1.6.8 起欠了四轮的老债。这一路看的是 `PYTHONIOENCODING` 的**值**而不是「有没有设」
#:    （v1.9.2）：用户点的是别的编码（`gbk`、`cp936`…）才一个字都不改，显式意图抢过来没有道理；
#:    点的本来就是 UTF 系时照样设成 UTF-8——**冻结包里那个环境变量根本落不到 stdio 编码上**
#:    （v1.9.1 封版实测：同条件下源树给 UTF-8、免安装版给 GBK），弃权等于把用户要的 UTF-8
#:    换成他的本机代码页。连着控制台的那一路不受这条影响，`PYTHONIOENCODING=gbk` 仍是第 ① 条。
def _env_wants_utf8() -> bool:
    """`PYTHONIOENCODING` 里写的是不是 UTF 系编码（`utf-8`、`UTF8:replace`、`cp65001`、`u8`）。"""
    value = (os.environ.get("PYTHONIOENCODING") or "").split(":")[0]
    return value.strip().lower().replace("-", "").replace("_", "") in ("utf8", "u8", "cp65001")


def _reconfigure_streams(streams: tuple) -> None:
    """按「有没有连着控制台」与「用户点的是哪种编码」决定流编码；任何一步失败都只是不改，不抛。"""
    for stream in streams:
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            override = bool(os.environ.get("PYTHONIOENCODING")) and not _env_wants_utf8()
            if stream.isatty() or override:
                stream.reconfigure(errors="backslashreplace")
            else:
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError):
            pass


_reconfigure_streams((sys.stdout, sys.stderr))

# 保密模式：--offline 启动，全程使用本地模型，不写应用日志（logs\app.log）、不落翻译缓存
# （cache\）。另有一份只记时间/阶段/段号/异常类型的按天元信息日志（core/file_log.py），
# 里面没有任何原文或译文——这条口径与《保密模式说明.txt》第 3 条一致。
OFFLINE = "--offline" in sys.argv[1:]
#: 把同一个事实交给翻译器：成品旁边那些《自检报告》《译后编辑清单》的回显走
#: ``core.common.shown_path``，保密模式下只念文件名（《保密模式说明.txt》四·4）。
common.CONFIDENTIAL = OFFLINE

#: 版本号。散出去的免安装包没有更新通道，对方报问题时先问这个号。
VERSION = "1.9.13"

#: 文档格式（保版式翻译）、脚本格式（Galgame/字幕，逐行翻译 + 质量引擎）、
#: 图片格式（OCR 识别后叠中文，对标拍照翻译）。三组后缀与文件对话框的分组、
#: 角标文案统一来自 ``core/formats.py`` 的登记表（v1.9.11，X4）；这里保留
#: 三个别名，是因为分发链与既有用例都按这几个名字取。
DOC_SUFFIXES = formats.doc_suffixes()
SCRIPT_SUFFIXES = formats.script_suffixes()
IMAGE_SUFFIXES = formats.image_suffixes()
ALL_SUFFIXES = formats.all_suffixes()

FONT = "Microsoft YaHei UI"
BG = "#f2f5f9"
CARD = "#ffffff"
BORDER = "#dbe4ef"
TEXT = "#16233b"
MUTED = "#6b7a90"
ACCENT = "#1e4976"
ACCENT_HOVER = "#16385c"
ACCENT_SOFT = "#e8f0f9"
SUCCESS = "#067647"
DANGER = "#b42318"

#: 卡片角标：脚本/图片按粗类念，文档按 kind 念（v1.9.11 起由登记表驱动；
#: 表里新增的文档格式没登记就用 kind 全大写兜底，不会退回成「文档」两个字）。
_BADGE_TEXT = {"docx": "WORD", "pdf": "PDF", "epub": "EPUB", "pptx": "PPT"}
#: 角标配色（底色, 字色）。没有专门配色的格式按粗类兜底。
_BADGE_COLORS = {
    ".docx": ("#e8f0fb", "#1d4ed8"),
    ".pdf": ("#fdecea", DANGER),
    ".epub": ("#eef7f0", "#0f766e"),
    ".pptx": ("#fef3e2", "#b45309"),
}
_BADGE_COLORS_BY_CATEGORY = {
    "doc": ("#e8f0fb", "#1d4ed8"),
    "script": ("#eef7f0", SUCCESS),
    "image": ("#f3effc", "#7c3aed"),
}


def _badge_text(fmt: formats.Format) -> str:
    if fmt.category == "doc":
        return _BADGE_TEXT.get(fmt.kind or "", (fmt.kind or "文档").upper())
    return formats.CATEGORY_BADGE[fmt.category]


def _badge_colors(fmt: formats.Format) -> tuple[str, str]:
    return _BADGE_COLORS.get(
        fmt.suffix, _BADGE_COLORS_BY_CATEGORY.get(fmt.category, ("#e8f0fb", "#1d4ed8"))
    )


def _format_hint(
    prefix: str = "只支持 ", *, ellipsis: bool = True, image_tail: str = "图片文件。"
) -> str:
    """「只支持 …」类提示：清单从登记表生成，格式增减时文案跟着变。

    图片只说前两种再念「等」——五张图片后缀逐个列出来，一眼看过去全是扩展名。
    """
    images = formats.category_text("image", limit=2, ellipsis=ellipsis)
    # 「等」后面直接接名词（「等图片文件」），没有「等」时补一个空格（「图片，」）。
    gap = "" if images.endswith("等") else " "
    return (
        f"{prefix}{formats.category_text('doc')} 文档，"
        f"{formats.category_text('script')} 脚本，"
        f"或 {images}{gap}{image_tail}"
    )


logger = logging.getLogger("translation_tool")
if OFFLINE:
    logger.addHandler(logging.NullHandler())
    # 硬断网：本进程内任何联网尝试都会直接失败
    import socket

    def _block_network(*_args, **_kwargs):
        raise OSError("保密模式已禁用网络连接")

    socket.socket.connect = _block_network
    socket.socket.connect_ex = _block_network
    socket.create_connection = _block_network
    socket.getaddrinfo = _block_network
else:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE = LOG_DIR / "app.log"
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2 * 1024 * 1024:
            LOG_FILE.replace(LOG_DIR / "app.log.1")
    except OSError:
        pass
    logging.basicConfig(
        filename=str(LOG_FILE),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )


def _enable_dpi_awareness() -> float:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
    except Exception:
        dpi = 96
    return max(1.0, dpi / 96.0)


def _ellipsize(text: str, max_columns: int = 64) -> str:
    columns = 0
    for index, char in enumerate(text):
        columns += 2 if ord(char) > 0x2E7F else 1
        if columns > max_columns:
            return text[:index].rstrip() + "…"
    return text


def _expand_inputs(items: list[str]) -> list[str]:
    """把拖进来的东西摊平成文件列表：文件夹展开一层，不支持的后缀丢掉。

    摊平后按原顺序去重，让用户看到的队列顺序和拖入顺序一致。
    """
    paths: list[str] = []
    seen: set[str] = set()
    for item in items:
        candidate = os.path.abspath(os.path.expanduser(str(item).strip()))
        if os.path.isdir(candidate):
            try:
                children = sorted(
                    os.path.join(candidate, name)
                    for name in os.listdir(candidate)
                )
            except OSError:
                continue
            for child in children:
                if not os.path.isfile(child):
                    continue
                if os.path.splitext(child)[1].lower() not in CONVERT_SUFFIXES:
                    continue
                key = os.path.normcase(child)
                if key not in seen:
                    seen.add(key)
                    paths.append(child)
            continue
        if not os.path.isfile(candidate):
            continue
        if os.path.splitext(candidate)[1].lower() not in CONVERT_SUFFIXES:
            continue
        key = os.path.normcase(candidate)
        if key not in seen:
            seen.add(key)
            paths.append(candidate)
    return paths


def _canvas_round_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class ToggleSwitch(tk.Frame):
    def __init__(
        self,
        master,
        text: str,
        value: bool,
        px,
        bg: str,
        command=None,
    ) -> None:
        super().__init__(master, bg=bg)
        self._px = px
        self._value = value
        self._enabled = True
        self._command = command
        self._track_w = px(38)
        self._track_h = px(22)
        self._canvas = tk.Canvas(
            self,
            width=self._track_w,
            height=self._track_h,
            bg=bg,
            highlightthickness=0,
            cursor="hand2",
        )
        self._canvas.pack(side="left")
        self._label = tk.Label(
            self,
            text=text,
            font=(FONT, 9),
            fg=TEXT,
            bg=bg,
            cursor="hand2",
        )
        self._label.pack(side="left", padx=(px(8), 0))
        for widget in (self, self._canvas, self._label):
            widget.bind("<Button-1>", self._on_click)
        self._draw()

    def get(self) -> bool:
        return self._value

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        cursor = "hand2" if enabled else "arrow"
        self.configure(cursor=cursor)
        self._canvas.configure(cursor=cursor)
        self._label.configure(
            cursor=cursor, fg=TEXT if enabled else "#9aa8ba"
        )
        self._draw()

    def _on_click(self, _event=None) -> None:
        if not self._enabled:
            return
        self._value = not self._value
        self._draw()
        if self._command is not None:
            self._command(self._value)

    def _draw(self) -> None:
        canvas = self._canvas
        canvas.delete("all")
        width = self._track_w
        height = self._track_h
        if not self._enabled:
            track = "#e2e8f0"
            knob = "#f8fafc"
        elif self._value:
            track = ACCENT
            knob = "#ffffff"
        else:
            track = "#c3cfdd"
            knob = "#ffffff"
        _canvas_round_rect(
            canvas, 0, 0, width, height, height / 2, fill=track, outline=track
        )
        pad = self._px(3)
        diameter = height - pad * 2
        left = width - pad - diameter if self._value else pad
        canvas.create_oval(
            left,
            pad,
            left + diameter,
            pad + diameter,
            fill=knob,
            outline="",
        )


class TranslatorApp:
    def __init__(self) -> None:
        self.scale = _enable_dpi_awareness()
        self.dnd_ready = False
        if TkinterDnD is not None:
            try:
                self.root = TkinterDnD.Tk()
                self.dnd_ready = True
            except Exception:
                self.root = tk.Tk()
        else:
            self.root = tk.Tk()
        self.root.tk.call("tk", "scaling", self.scale * 96 / 72)

        self.events: queue.Queue = queue.Queue()
        self.stop_event: threading.Event | None = None
        self.worker_thread: threading.Thread | None = None
        self.translator: Translator | None = None
        self.src_path: str | None = None
        self.output_path: str | None = None
        self.busy = False
        self.drop_hover = False
        # 批量队列。src_path 仍然指向「当前/唯一」那个文件，单个文件时队列为空，
        # 这样 CLI 与老的自检脚本都不用改。
        self.queue: list[str] = []
        self.batch_index = 0
        self.batch_total = 0
        self.results: list[tuple[str, str | None]] = []
        self.pdf_layout = "side"
        self.pdf_pages = CLI_PAGES
        self.style = CLI_STYLE
        self.pdf_passwords: dict[str, str] = {}

        self._setup_window()
        self._setup_style()
        self._build_ui()
        self._bind_events()
        self._pump_events()

        logger.info("应用启动，Python %s", sys.version.split()[0])
        if OFFLINE:
            self._append_log("欢迎使用保密模式。把 Word（.docx）、PDF 或 EPUB 拖进来即可开始。", "muted")
            self._append_log(
                "文档全程在本机处理，翻译不联网、不存缓存。运行日志仅记录进度等元信息。",
                "muted",
            )
            self._append_log(
                "英文文档用小型模型，日、俄、法、德等语言用多语种模型，都在本机完成。",
                "muted",
            )
        else:
            self._append_log("欢迎使用。把 Word（.docx）、PDF 或 EPUB 拖进来即可开始。", "muted")
            self._append_log("翻译需要联网，相同内容会自动缓存；请勿用于涉密文档。", "muted")

    def px(self, value: float) -> int:
        return max(1, int(round(value * self.scale)))

    def _setup_window(self) -> None:
        self.root.title(
            "文档翻译工具 · 保密模式" if OFFLINE else "文档翻译工具 · 外文译中文"
        )
        self.root.configure(bg=BG)
        if ICON_PATH.exists():
            try:
                self.root.iconbitmap(default=str(ICON_PATH))
            except tk.TclError:
                pass

        width = self.px(660)
        height = self.px(660)
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(width, screen_w - self.px(40))
        height = min(height, screen_h - self.px(80))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2 - self.px(12))
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(self.px(600), self.px(620))

    def _setup_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "Accent.TButton",
            background=ACCENT,
            foreground="#ffffff",
            bordercolor=ACCENT,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            focuscolor=ACCENT,
            font=(FONT, 10, "bold"),
            padding=(self.px(18), self.px(9)),
            relief="flat",
        )
        style.map(
            "Accent.TButton",
            background=[
                ("disabled", "#b7c5d6"),
                ("pressed", "#102c4a"),
                ("active", ACCENT_HOVER),
            ],
            foreground=[("disabled", "#f4f7fb")],
            bordercolor=[("disabled", "#b7c5d6")],
            lightcolor=[("disabled", "#b7c5d6")],
            darkcolor=[("disabled", "#b7c5d6")],
        )
        style.configure(
            "Soft.TButton",
            background="#e8eef7",
            foreground=ACCENT,
            bordercolor="#e8eef7",
            lightcolor="#e8eef7",
            darkcolor="#e8eef7",
            focuscolor="#e8eef7",
            font=(FONT, 10),
            padding=(self.px(14), self.px(9)),
            relief="flat",
        )
        style.map(
            "Soft.TButton",
            background=[
                ("disabled", "#f2f5f9"),
                ("pressed", "#ccdaec"),
                ("active", "#dbe6f3"),
            ],
            foreground=[("disabled", "#aebdd0")],
            bordercolor=[
                ("disabled", "#f2f5f9"),
                ("active", "#dbe6f3"),
                ("pressed", "#ccdaec"),
            ],
            lightcolor=[("disabled", "#f2f5f9")],
            darkcolor=[("disabled", "#f2f5f9")],
        )
        style.configure(
            "Tool.TCheckbutton",
            background=BG,
            foreground=TEXT,
            font=(FONT, 9),
            padding=(0, self.px(2)),
            focuscolor=BG,
        )
        style.map(
            "Tool.TCheckbutton",
            background=[("active", BG)],
            foreground=[("disabled", "#9aa8ba")],
        )
        style.configure(
            "Blue.Horizontal.TProgressbar",
            troughcolor="#e3eaf3",
            background=ACCENT,
            bordercolor="#e3eaf3",
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            thickness=self.px(8),
        )
        style.configure(
            "Vertical.TScrollbar",
            background="#d6dfea",
            troughcolor=CARD,
            bordercolor=CARD,
            arrowcolor=MUTED,
            relief="flat",
        )

    def _build_ui(self) -> None:
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(
            fill="both",
            expand=True,
            padx=self.px(22),
            pady=(self.px(18), self.px(14)),
        )
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(6, weight=1)

        header = tk.Frame(outer, bg=BG)
        header.grid(row=0, column=0, sticky="ew")
        title_row = tk.Frame(header, bg=BG)
        title_row.pack(anchor="w")
        tk.Label(
            title_row,
            text="文档翻译工具",
            font=(FONT, 19, "bold"),
            fg=TEXT,
            bg=BG,
            anchor="w",
        ).pack(side="left")
        if OFFLINE:
            tk.Label(
                title_row,
                text="保密模式 · 全程离线",
                font=(FONT, 8, "bold"),
                fg=SUCCESS,
                bg="#e7f6ee",
                padx=self.px(8),
                pady=self.px(3),
            ).pack(side="left", padx=(self.px(10), 0), pady=(self.px(4), 0))
        tk.Label(
            header,
            text=(
                "Word / PDF 外语文档 → 中文双语版，文档只在本机处理"
                if OFFLINE
                else "Word / PDF 外文文档 → 中文双语版，自动保留原排版"
            ),
            font=(FONT, 9),
            fg=MUTED,
            bg=BG,
            anchor="w",
        ).pack(anchor="w", pady=(self.px(1), 0))

        self.drop = tk.Canvas(
            outer,
            height=self.px(134),
            bg=BG,
            highlightthickness=0,
            cursor="hand2",
        )
        self.drop.grid(row=1, column=0, sticky="ew", pady=(self.px(13), self.px(10)))

        self.file_card = tk.Frame(
            outer,
            bg=CARD,
            highlightbackground=BORDER,
            highlightthickness=self.px(1),
        )
        self.file_card.grid(row=2, column=0, sticky="ew")
        self.file_card.grid_columnconfigure(1, weight=1)

        self.badge = tk.Label(
            self.file_card,
            text="文件",
            font=(FONT, 8, "bold"),
            bg="#eef2f8",
            fg=MUTED,
            width=5,
            pady=self.px(4),
        )
        self.badge.grid(
            row=0, column=0, padx=(self.px(12), self.px(10)), pady=self.px(9)
        )

        info = tk.Frame(self.file_card, bg=CARD)
        info.grid(row=0, column=1, sticky="ew", pady=self.px(8))
        info.grid_columnconfigure(0, weight=1)
        self.name_label = tk.Label(
            info,
            text="尚未选择文件",
            font=(FONT, 10, "bold"),
            fg=TEXT,
            bg=CARD,
            anchor="w",
        )
        self.name_label.grid(row=0, column=0, sticky="ew")
        self.meta_label = tk.Label(
            info,
            text="拖入或点击上方区域选择要翻译的文档",
            font=(FONT, 8),
            fg=MUTED,
            bg=CARD,
            anchor="w",
        )
        self.meta_label.grid(row=1, column=0, sticky="ew", pady=(self.px(1), 0))

        self.change_label = tk.Label(
            self.file_card,
            text="更换",
            font=(FONT, 9),
            fg=ACCENT,
            bg=CARD,
            cursor="hand2",
        )
        self.change_label.grid(
            row=0, column=2, padx=(self.px(10), self.px(14)), pady=self.px(9)
        )
        self.change_label.bind("<Button-1>", lambda event: self.choose_file())
        self.change_label.bind(
            "<Enter>", lambda event: self.change_label.configure(fg=ACCENT_HOVER)
        )
        self.change_label.bind(
            "<Leave>", lambda event: self.change_label.configure(fg=ACCENT)
        )
        self.change_label.grid_remove()

        options = tk.Frame(outer, bg=BG)
        options.grid(row=3, column=0, sticky="ew", pady=(self.px(8), 0))
        options.grid_columnconfigure(1, weight=1)
        self.ocr_switch = ToggleSwitch(
            options,
            "识别扫描版 PDF（OCR，速度较慢）",
            value=True,
            px=self.px,
            bg=BG,
        )
        self.ocr_switch.grid(row=0, column=0, sticky="w")
        self.ocr_switch.set_enabled(False)
        tk.Label(
            options,
            text="文字版 PDF 无需开启",
            font=(FONT, 8),
            fg=MUTED,
            bg=BG,
        ).grid(row=0, column=1, sticky="e")

        if not OFFLINE:
            # 联网模式会把原文和译文写进本地缓存库，这件事必须让用户看得见、
            # 关得掉。保密模式本来就不落盘，不显示这个开关免得引起误解。
            self.cache_switch = ToggleSwitch(
                options,
                "不保存翻译缓存（原文不落盘）",
                value=False,
                px=self.px,
                bg=BG,
                command=self._on_cache_toggle,
            )
            self.cache_switch.grid(row=1, column=0, sticky="w", pady=(self.px(6), 0))
            tk.Label(
                options,
                text="涉密文档请勿用联网模式",
                font=(FONT, 8),
                fg=MUTED,
                bg=BG,
            ).grid(row=1, column=1, sticky="e", pady=(self.px(6), 0))

        if not OFFLINE:
            # 引擎档位：免费云端 / 本地大模型 / 付费 API（DeepL）。
            # 本地大模型数据仍然不出本机（走本机 Ollama），质量明显高于免费
            # 云端的小通道，适合日常文档；服务不可用时仅回退本机离线小模型。
            # 付费档需要自备 key 并按量计费，选中时要先过闸门
            # （见 _on_engine_change 的 paid 分支 / _confirm_paid_engine）。
            # 涉密防线第一层：整个 engine_frame 只在非保密模式下渲染，
            # 保密模式看不到任何联网与付费入口。
            engine_frame = tk.Frame(options, bg=BG)
            engine_frame.grid(
                row=2, column=0, columnspan=2, sticky="w", pady=(self.px(6), 0)
            )
            tk.Label(
                engine_frame,
                text="翻译引擎：",
                font=(FONT, 9),
                fg=TEXT,
                bg=BG,
            ).pack(side="left")
            self.engine_var = tk.StringVar(value="cloud")
            # 档位清单来自 core.engine_registry——界面、CLI、构造共用一份真相，
            # 加一档只改那张表，不再改这里的 if 链。标签刻意压到六个字以内
            # （全名与数据去向在 _on_engine_change 的日志里念出来）：四个档位
            # 在全角字体下要排进同一行，不加宽窗口。
            for value, radio_text in engine_registry.radio_choices(OFFLINE):
                tk.Radiobutton(
                    engine_frame,
                    text=radio_text,
                    value=value,
                    variable=self.engine_var,
                    font=(FONT, 9),
                    fg=TEXT,
                    bg=BG,
                    activebackground=BG,
                    selectcolor=CARD,
                    highlightthickness=0,
                    command=self._on_engine_change,
                ).pack(side="left", padx=(self.px(6), 0))
            # 自托管档的地址是用户填的，给一个显眼的入口；用 Label 而不是
            # Button，是为了和单选的字号、行高对齐（同 version_label 的写法）。
            self.custom_config_link = tk.Label(
                engine_frame,
                text="配置自托管…",
                font=(FONT, 9),
                fg=ACCENT,
                bg=BG,
                cursor="hand2",
            )
            self.custom_config_link.pack(side="left", padx=(self.px(10), 0))
            self.custom_config_link.bind(
                "<Button-1>", lambda event: self.open_custom_config()
            )

        if not OFFLINE:
            # 质量引擎开关：只对本地大模型档有意义（云端引擎不吃上下文，
            # 开了也没有校改引擎可用）。默认开——这一档本来就卖质量。
            self.quality_switch = ToggleSwitch(
                options,
                "质量引擎（术语一致 + 上下文连贯 + 自动校改）",
                value=True,
                px=self.px,
                bg=BG,
            )
            self.quality_switch.grid(row=3, column=0, sticky="w", pady=(self.px(6), 0))
            self.quality_switch.grid_remove()

        layout_row = 4 if not OFFLINE else 1
        layout_frame = tk.Frame(options, bg=BG)
        layout_frame.grid(
            row=layout_row, column=0, columnspan=2, sticky="w", pady=(self.px(6), 0)
        )
        tk.Label(
            layout_frame,
            text="PDF 成品：",
            font=(FONT, 9),
            fg=TEXT,
            bg=BG,
        ).pack(side="left")
        self.layout_var = tk.StringVar(value="side")
        #: zh 版式的按钮文字跟随目标语言（译成英文时叫「仅英文」，
        #: 标签写死「仅中文」会让反向用户以为选错了版式）。
        self._only_target_radio = None
        for value, label in (
            ("side", "左右对照"),
            ("zh", self._only_target_label()),
            ("inplace", "原位重排"),
        ):
            radio = tk.Radiobutton(
                layout_frame,
                text=label,
                value=value,
                variable=self.layout_var,
                font=(FONT, 9),
                fg=TEXT,
                bg=BG,
                activebackground=BG,
                selectcolor=CARD,
                highlightthickness=0,
                command=self._on_layout_change,
            )
            radio.pack(side="left", padx=(self.px(6), 0))
            if value == "zh":
                self._only_target_radio = radio

        # 范围（v1.6.8 只有 PDF，v1.7.0 起 PPT/EPUB 也吃）：跟「PDF 成品」放同一行，
        # 因为它只影响按序号切分的三种格式，单起一行会让界面多出一个像「全局设置」的假象。
        # 标签只能写中性的「范围：」——同一串数字对 PDF 是页、对 PPT 是张、对 EPUB 是章。
        # 留空 = 整份文档。
        tk.Label(
            layout_frame,
            text="范围：",
            font=(FONT, 9),
            fg=TEXT,
            bg=BG,
        ).pack(side="left", padx=(self.px(16), 0))
        self.pages_var = tk.StringVar(value=CLI_PAGES)
        pages_entry = tk.Entry(
            layout_frame,
            textvariable=self.pages_var,
            font=(FONT, 9),
            bg=CARD,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            width=12,
        )
        pages_entry.pack(side="left", padx=(self.px(4), 0), ipady=self.px(2))
        pages_entry.bind("<FocusOut>", lambda _event: self._on_pages_change())
        pages_entry.bind("<Return>", lambda _event: self._on_pages_change())

        style_frame = tk.Frame(options, bg=BG)
        style_frame.grid(
            row=layout_row + 1, column=0, columnspan=2, sticky="w", pady=(self.px(6), 0)
        )
        tk.Label(
            style_frame,
            text="译文风格：",
            font=(FONT, 9),
            fg=TEXT,
            bg=BG,
        ).pack(side="left")
        self.style_var = tk.StringVar(value=STANDARD)
        for value, label in (
            (STANDARD, "标准"),
            (FORMAL, "正式商务"),
            (CONCISE, "简洁"),
        ):
            tk.Radiobutton(
                style_frame,
                text=label,
                value=value,
                variable=self.style_var,
                font=(FONT, 9),
                fg=TEXT,
                bg=BG,
                activebackground=BG,
                selectcolor=CARD,
                highlightthickness=0,
                command=self._on_style_change,
            ).pack(side="left", padx=(self.px(6), 0))

        # 目标语言（v1.5.8）：默认中文，与 v1.5.7 行为一致；选英文时抽段判定、
        # 术语方向、缓存键、后处理全部跟着换（见 core/direction.py）。
        target_frame = tk.Frame(options, bg=BG)
        target_frame.grid(
            row=layout_row + 2, column=0, columnspan=2, sticky="w", pady=(self.px(6), 0)
        )
        tk.Label(
            target_frame,
            text="目标语言：",
            font=(FONT, 9),
            fg=TEXT,
            bg=BG,
        ).pack(side="left")
        self.target_var = tk.StringVar(value=direction.ZH)
        for value in (direction.ZH, direction.EN):
            tk.Radiobutton(
                target_frame,
                text=direction.LABELS[value],
                value=value,
                variable=self.target_var,
                font=(FONT, 9),
                fg=TEXT,
                bg=BG,
                activebackground=BG,
                selectcolor=CARD,
                highlightthickness=0,
                command=self._on_target_change,
            ).pack(side="left", padx=(self.px(6), 0))

        actions = tk.Frame(outer, bg=BG)
        actions.grid(row=4, column=0, sticky="ew", pady=(self.px(12), 0))
        self.start_btn = ttk.Button(
            actions,
            text="开始翻译",
            style="Accent.TButton",
            command=self.start_translation,
            state="disabled",
        )
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(
            actions,
            text="取消",
            style="Soft.TButton",
            command=self.cancel_translation,
            state="disabled",
        )
        self.cancel_btn.pack(side="left", padx=(self.px(8), 0))
        self.folder_btn = ttk.Button(
            actions,
            text="打开文件夹",
            style="Soft.TButton",
            command=self.open_folder,
            state="disabled",
        )
        self.folder_btn.pack(side="right")
        self.open_btn = ttk.Button(
            actions,
            text="打开译文",
            style="Soft.TButton",
            command=self.open_output,
            state="disabled",
        )
        self.open_btn.pack(side="right", padx=(0, self.px(8)))
        self.postedit_btn = ttk.Button(
            actions,
            text="译后编辑",
            style="Soft.TButton",
            command=self.open_postedit,
            state="disabled",
        )
        self.postedit_btn.pack(side="right", padx=(0, self.px(8)))
        # 术语库按钮放在译后编辑旁边：两者都是「人在回路」的入口，但术语库
        # 跨批次积累、不依赖某次翻译的成品，所以常态可用（不像译后编辑要等
        # 翻译成功才有清单）。pack 在 postedit 之后 → 视觉上排在它左边。
        self.termbase_btn = ttk.Button(
            actions,
            text="术语库",
            style="Soft.TButton",
            command=self.open_termbase,
        )
        self.termbase_btn.pack(side="right", padx=(0, self.px(8)))

        progress_frame = tk.Frame(outer, bg=BG)
        progress_frame.grid(row=5, column=0, sticky="ew", pady=(self.px(14), 0))
        progress_frame.grid_columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(
            progress_frame,
            style="Blue.Horizontal.TProgressbar",
            mode="determinate",
            maximum=100,
            value=0,
        )
        self.progress.grid(row=0, column=0, sticky="ew")
        self.percent_label = tk.Label(
            progress_frame,
            text="",
            font=(FONT, 9, "bold"),
            fg=ACCENT,
            bg=BG,
            width=5,
            anchor="e",
        )
        self.percent_label.grid(row=0, column=1, padx=(self.px(8), 0))
        self.status_label = tk.Label(
            progress_frame,
            text="准备就绪",
            font=(FONT, 9),
            fg=MUTED,
            bg=BG,
            anchor="w",
            justify="left",
            wraplength=self.px(540),
        )
        self.status_label.grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(self.px(5), 0)
        )

        log_card = tk.Frame(
            outer,
            bg=CARD,
            highlightbackground=BORDER,
            highlightthickness=self.px(1),
        )
        log_card.grid(row=6, column=0, sticky="nsew", pady=(self.px(14), 0))
        log_card.grid_columnconfigure(0, weight=1)
        log_card.grid_rowconfigure(1, weight=1)
        log_head = tk.Frame(log_card, bg=CARD)
        log_head.grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=self.px(13),
            pady=(self.px(8), 0),
        )
        tk.Label(
            log_head,
            text="运行日志",
            font=(FONT, 9, "bold"),
            fg=TEXT,
            bg=CARD,
        ).pack(side="left")
        self.clear_label = tk.Label(
            log_head,
            text="清空",
            font=(FONT, 8),
            fg=MUTED,
            bg=CARD,
            cursor="hand2",
        )
        self.clear_label.pack(side="right")
        self.clear_label.bind("<Button-1>", lambda event: self.clear_log())
        self.clear_label.bind(
            "<Enter>", lambda event: self.clear_label.configure(fg=ACCENT)
        )
        self.clear_label.bind(
            "<Leave>", lambda event: self.clear_label.configure(fg=MUTED)
        )

        self.log_text = tk.Text(
            log_card,
            height=6,
            wrap="word",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            bg=CARD,
            fg="#4a5a70",
            font=(FONT, 9),
            padx=self.px(12),
            pady=self.px(3),
            spacing1=self.px(1),
            spacing3=self.px(1),
            cursor="arrow",
            state="disabled",
        )
        self.log_text.grid(
            row=1,
            column=0,
            sticky="nsew",
            padx=(self.px(6), 0),
            pady=(self.px(2), self.px(6)),
        )
        scroll = ttk.Scrollbar(
            log_card,
            orient="vertical",
            command=self.log_text.yview,
            style="Vertical.TScrollbar",
        )
        scroll.grid(
            row=1,
            column=1,
            sticky="ns",
            padx=(0, self.px(6)),
            pady=(self.px(2), self.px(6)),
        )
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.tag_configure("time", foreground="#a3b1c4")
        self.log_text.tag_configure("info", foreground="#4a5a70")
        self.log_text.tag_configure("muted", foreground=MUTED)
        self.log_text.tag_configure("success", foreground=SUCCESS)
        self.log_text.tag_configure("error", foreground=DANGER)

        footer = tk.Frame(outer, bg=BG)
        footer.grid(row=7, column=0, sticky="ew", pady=(self.px(8), 0))
        tk.Label(
            footer,
            text="译文保存在原文件旁边，文件名带「_双语」",
            font=(FONT, 8),
            fg=MUTED,
            bg=BG,
        ).pack(side="left")
        if not OFFLINE:
            # 联网模式把原文落进了本地缓存库，必须给一个看得见的清除入口。
            cache_link = tk.Label(
                footer,
                text="清除翻译缓存",
                font=(FONT, 8),
                fg=ACCENT,
                bg=BG,
                cursor="hand2",
            )
            cache_link.pack(side="left", padx=(self.px(10), 0))
            cache_link.bind("<Button-1>", lambda event: self.clear_cache())
            cache_link.bind("<Enter>", lambda e: cache_link.configure(fg=ACCENT_HOVER))
            cache_link.bind("<Leave>", lambda e: cache_link.configure(fg=ACCENT))
        self.version_label = tk.Label(
            footer,
            text=f"v{VERSION}",
            font=(FONT, 8),
            fg="#98a5b6",
            bg=BG,
            cursor="hand2",
        )
        self.version_label.pack(side="right")
        self.version_label.bind("<Button-1>", lambda event: self.show_about())
        self.mode_label = tk.Label(
            footer,
            text=(
                "本地离线模型 · 处理过程不联网"
                if OFFLINE
                else "免费翻译 · 无需 API Key"
            ),
            font=(FONT, 8),
            fg="#98a5b6",
            bg=BG,
        )
        self.mode_label.pack(side="right", padx=(0, self.px(8)))

        self._draw_dropzone()

    def _on_engine_change(self) -> None:
        """切换引擎档位：档位说明、合规闸门、质量开关的显隐都问登记表。

        三件事的口径与 CLI 一一对应，不搞第二套：
        本地大模型需要 Ollama，探测失败由 ``LocalLLMTranslator`` 自己回退
        免费云端；付费 API（DeepL）走 ``_confirm_paid_engine``（=CLI
        ``_paid_gate``）；自托管服务走 ``_confirm_custom_engine``（=CLI
        ``_custom_gate``）。闸门没过就弹回免费云端。
        """
        key = getattr(self, "engine_var", None) and self.engine_var.get()
        if not key:
            return
        spec = engine_registry.get(key)
        if spec.provider == "deepl" and not self._confirm_paid_engine():
            # 闸门没过：弹回免费云端。StringVar.set 不触发 command 回调，
            # 显式刷新一次日志与文案（不会递归——免费云端没有闸门）。
            self._revert_to_cloud()
            return
        if spec.key == engine_registry.CUSTOM and not self._confirm_custom_engine():
            self._revert_to_cloud()
            return
        self._append_log(self._engine_hint(spec), "muted")
        if getattr(self, "mode_label", None):
            self.mode_label.configure(text=spec.footer)
        if getattr(self, "quality_switch", None):
            # 质量管线要逐段带上下文，只有本地大模型/自托管这两档吃这套；
            # 云端与 DeepL 是逐句接口，开着也没有校改引擎可用。
            if spec.shows_quality:
                self.quality_switch.grid()
            else:
                self.quality_switch.grid_remove()

    def _revert_to_cloud(self) -> None:
        self.engine_var.set(engine_registry.CLOUD)
        self._on_engine_change()

    def _engine_hint(self, spec) -> str:
        """切档时念给用户的那句说明。自托管档的地址是用户配的，
        登记表里的静态文案没法说清「这次到底发去哪儿」，现拼。"""
        if spec.key == engine_registry.CUSTOM:
            from core import custom_engine

            config = custom_engine.load_config()
            base = custom_engine.describe(config)
            # 回退方向由地址在哪决定，这句必须跟着变：内网服务连不上时改发
            # 免费云端，等于把这一档存在的理由废掉，所以那句话不能说错。
            if not config.base_url:
                rescue = (
                    "配好地址后，本机/内网的服务连不上时只改走本机离线小模型，"
                    "不会把原文发给联网翻译服务"
                )
            elif custom_engine.endpoint_scope(config.base_url) == "public":
                rescue = "服务连不上时改走免费云端通道（这台地址本来就在公网）"
            else:
                rescue = (
                    "服务连不上时只改走本机离线小模型，"
                    "不会把原文发给联网翻译服务（没装离线模型就明确报失败）"
                )
            return (
                f"{base}。这一档走质量引擎；{rescue}。"
                f"涉密文档请用保密模式。配置写在 {custom_engine.config_path()}。"
            )
        return spec.hint

    def _confirm_custom_engine(self) -> bool:
        """自托管档 GUI 三道闸门，口径与 CLI ``_custom_gate`` 逐条一致。

        1. 没配置（或配置里地址为空/没启用）→ 弹引导，告诉他点「配置自托管…」
           或直接改 ``自定义引擎.json``，然后弹回免费云端；
        2. 地址不合法，或**公网 + 明文 http** → 弹错误说明为什么不能用
           （明文公网等于文档裸奔，这条没有「知道了就用」的选项）；
        3. 地址在公网 → 确认「文档原文会发往这台服务器」，确认标记落在
           ``api_keys.local.json`` 的 ``custom.acknowledged``（与 deepl 同一
           套标记文件，永不进 git）；内网/本机地址不问，因为数据不出边界。
        """
        from core import custom_engine
        from core.api_keys import is_acknowledged, save_acknowledgement

        if OFFLINE:
            return False
        config = custom_engine.load_config()
        if not config.usable():
            messagebox.showinfo(
                "需要先配置自托管服务",
                "自托管档要指向一个已经在跑的服务（vLLM、llama.cpp 的 "
                "llama-server、LM Studio、或另一台机器上的 Ollama），"
                "并且要提供 OpenAI 兼容的 /v1/chat/completions。\n\n"
                "点「配置自托管…」填地址；也可以直接编辑：\n"
                f"  {custom_engine.config_path()}\n"
                '     {"enabled": true, "name": "单位GPU服务器",'
                ' "base_url": "http://192.168.1.20:8000/v1", "model": ""}',
            )
            return False
        try:
            custom_engine.require_usable(config.base_url)
        except TranslationError as exc:
            messagebox.showerror("这个地址不能用", str(exc))
            return False
        if custom_engine.endpoint_scope(config.base_url) == "public":
            if not is_acknowledged(custom_engine.CUSTOM_PROVIDER):
                if not messagebox.askyesno(
                    "涉密警示：自托管服务在公网",
                    f"这个地址在公网：\n\n  {config.base_url}\n\n"
                    "文档原文会离开本机/本单位的网络边界。涉密文档请用保密模式"
                    "（启动翻译工具-保密模式.bat），全程只在本机处理。\n\n"
                    "确认已知悉上述风险并仍要使用吗？",
                    icon="warning",
                ):
                    return False
                save_acknowledgement(custom_engine.CUSTOM_PROVIDER)
        return True

    def open_custom_config(self) -> None:
        """打开自托管服务配置窗口（「配置自托管…」入口）。"""
        if OFFLINE:
            messagebox.showinfo(
                "保密模式不使用自托管服务",
                "保密模式只用本机 models/ 里的离线模型，全程不联网。\n"
                "自托管服务的地址是用户填的网络端点，无法保证它不出本机，"
                "所以这一档在保密模式里不可用。\n\n"
                "要用自托管服务：双击「启动翻译工具.bat」。"
            )
            return
        CustomEngineDialog(self.root, self)

    def _confirm_paid_engine(self) -> bool:
        """付费档 GUI 三道闸门。返回 True 放行；False 表示应弹回免费云端。

        判断口径与 CLI ``_paid_gate`` 逐条对应，确认标记复用
        ``core.api_keys`` 的 ``is_acknowledged`` / ``save_acknowledgement``
        （v1.5.0 落盘的那一个，CLI 与 GUI 共用）：

        1. 没配 key（或形态不对）→ 弹窗引导去哪申请、填哪个文件、怎么填，
           然后弹回免费云端；
        2. 配了 key 但没确认过涉密警示 → 弹窗问「已知悉原文会发送至
           DeepL（境外服务器）」，选否弹回免费云端，选是落确认标记；
        3. 已确认 → 放行。

        与保密模式的硬互斥不在这里判断：保密模式下付费单选整个不渲染
        （``_on_engine_change`` 的 paid 分支根本到不了），第一层防线
        在渲染时就成立了；下面的 ``OFFLINE`` 兜底只是双保险。
        """
        if OFFLINE:
            return False
        from core.api_keys import (
            is_acknowledged,
            keys_file,
            load_key,
            looks_like_key,
            save_acknowledgement,
        )

        key_config = load_key("deepl")
        key = (key_config or {}).get("key", "")
        if not key_config or not looks_like_key(key):
            messagebox.showinfo(
                "需要 DeepL API key",
                "付费档需要你自己申请 DeepL API key：\n\n"
                "1. 打开 https://www.deepl.com/pro-api 注册 DeepL API"
                "（Free 版每月 50 万字符免费额度，够日常文档用）；\n"
                f"2. 在程序目录新建 {keys_file()}（不会进 git），"
                "内容例如：\n"
                '     {"deepl": {"key": '
                '"xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx:fx"}}\n'
                "3. 重新选择「付费 API」即可生效。\n\n"
                "key 有两种形态：Pro 是一串 UUID；Free 在 UUID 后面加 "
                ":fx 结尾。",
                parent=self.root,
            )
            return False
        if not is_acknowledged("deepl"):
            answered = messagebox.askyesno(
                "涉密警示",
                "付费档会把文档原文发送至 DeepL（境外服务器）。\n\n"
                "涉密文档请使用保密模式（启动翻译工具-保密模式.bat），"
                "不要用付费档。\n\n"
                "确认已知悉上述风险，继续使用付费 API？",
                parent=self.root,
            )
            if not answered:
                return False
            save_acknowledgement("deepl")
        return True

    def _on_cache_toggle(self, enabled: bool) -> None:
        if enabled:
            self._append_log(
                "已开启「不保存缓存」：本次翻译的原文与译文都不会写入本地缓存库。",
                "muted",
            )
        else:
            self._append_log(
                "已关闭「不保存缓存」：原文与译文会写入 cache\\translations.sqlite，"
                "下次遇到相同内容会直接复用。涉密文档请改用保密模式。",
                "muted",
            )

    def _on_style_change(self) -> None:
        """切换译文风格。术语表与风格相互独立，改风格不影响术语锁定。"""
        self.style = self.style_var.get()
        if self.style == STANDARD:
            return
        hint = {
            FORMAL: "对外函件与合同的语气：你→您、我们→我方、请→敬请。",
            CONCISE: "压掉冗余表述：对于…来说、…的原因、…的情况下。",
        }[self.style]
        self._append_log(f"译文风格：{style_label(self.style)}。{hint}", "muted")

    def _on_target_change(self) -> None:
        """切换目标语言。方向是全局开关，改完立即生效，不必重启。"""
        if self.busy:
            # 译后处理是运行时读全局方向的，中途改会让同一份文档出现两种
            # 目标语言；直接拒绝并回显当前方向，不做「下轮生效」这种暗账。
            self.target_var.set(direction.target())
            self._append_log(
                "翻译进行中，暂不能切换目标语言；请等本次完成后再改。", "muted"
            )
            return
        code = self.target_var.get()
        try:
            direction.set_target(code)
        except ValueError as exc:
            # set_target 拒绝时方向保持原值，单选按钮也必须跟着回显原值，
            # 否则界面显示「中文」而引擎还在译英文。
            self.target_var.set(direction.target())
            self._append_log(str(exc), "muted")
            return
        if self._only_target_radio is not None:
            self._only_target_radio.configure(text=self._only_target_label())
        if direction.is_english():
            self._append_log(
                "目标语言：英文。已经是英文的段落会自动跳过；术语表按反向使用"
                "（中文 = 外文）。",
                "muted",
            )
        else:
            self._append_log("目标语言：中文。", "muted")

    @staticmethod
    def _only_target_label() -> str:
        return "仅英文" if direction.is_english() else "仅中文"

    def _on_layout_change(self) -> None:
        self.pdf_layout = self.layout_var.get()
        if self.pdf_layout == "zh":
            self._append_log(
                f"PDF 成品改为「{self._only_target_label()}」：页面尺寸与源文件一致，"
                "方便打印与传阅；原文里的链接热区一并带到成品页上。",
                "muted",
            )
        elif self.pdf_layout == "inplace":
            self._append_log(
                "PDF 成品改为「原位重排」：在原件上直接把原文替换成中文，页面尺寸、"
                "链接、书签、表格与矢量图原样保留；只处理文字版页面，扫描页会整页"
                "保留不动。",
                "muted",
            )
        else:
            self._append_log(
                "PDF 成品改为「左右对照」：左原文、右中文；"
                "原文里的链接热区仍在左半边可点。",
                "muted",
            )
        # OCR 开关的可用性跟版式走：原位重排不做 OCR（扫描页整页保留），
        # 其余版式按「有没有排队 PDF / 是否在忙」决定，交给 _refresh_buttons
        # 统一口径，避免两处各写一套状态机。
        self._refresh_buttons()

    def _on_pages_change(self) -> None:
        """范围写完当场回显，别等翻译跑起来才发现写错。

        这里只能验写法：排队的多个文件页数/张数/章数各不相同，界面上没有统一的上界可查，
        「第 40」超不超出由各自的 translate_* 拿自己那份文件的数量去判。

        回显用量词中性的一句（`SCOPE_NOTE`）：写完这一栏时还不知道它最终落在 PDF、
        PPT 还是 EPUB 上，念「第 2 页」对 PPT 是假话。精确的「张 / 章」由每个文件
        开翻时按自己的 `Unit` 念出来。
        """
        spec = self.pages_var.get().strip()
        self.pdf_pages = spec
        if not spec:
            self._append_log("文档范围：整份翻译。", "muted")
            return
        try:
            page_range.describe(spec, page_range.NEUTRAL)
        except TranslationError as exc:
            self._append_log(f"文档范围写错了：{exc}", "error")
            return
        self._append_log(f"文档范围：{spec}（{page_range.SCOPE_NOTE}）。", "muted")

    def clear_cache(self) -> None:
        """清空在线模式的译文缓存——只动 translations 表。

        注意：cache\\translations.sqlite 现在是**统一状态库**（阶段 0 起，
        译文缓存 / 术语库 / 角色卡 / 翻译记忆同库不同表）。早年这里直接删文件，
        那会把用户积累的术语和翻译记忆一起毁掉——清缓存是日常操作，
        毁术语库是不可恢复的损失。现在只 DELETE translations 表 + VACUUM。
        """
        from core.state import StateStore

        target = CACHE_DIR / "translations.sqlite"
        if not target.exists():
            messagebox.showinfo("清除缓存", "当前没有缓存文件。", parent=self.root)
            return
        try:
            size = human_size(target.stat().st_size)
        except OSError:
            size = ""
        if not messagebox.askyesno(
            "清除翻译缓存",
            f"将清空 {target}"
            + (f"（{size}）" if size else "")
            + " 里的译文缓存。\n\n"
            "缓存存着以往翻译过的原文与译文，清除后下次遇到相同内容会重新翻译。\n"
            "术语库、角色卡与翻译记忆不受影响，会保留。\n\n确定清除吗？",
            parent=self.root,
        ):
            return
        try:
            store = StateStore(str(target))
            store.clear_translations()
            store.close()
        except Exception as exc:
            messagebox.showerror("清除缓存失败", str(exc), parent=self.root)
            return
        for suffix in ("-wal", "-shm"):
            try:
                Path(str(target) + suffix).unlink()
            except OSError:
                pass
        self._append_log("翻译缓存已清除（术语库与翻译记忆已保留）。", "success")

    def show_about(self) -> None:
        if OFFLINE:
            mode_line = "保密模式（本机离线、已断网）"
            engine_lines = "翻译引擎：离线小模型（数据不出本机）"
        else:
            mode_line = "联网模式"
            engine_lines = (
                "翻译引擎档位：\n"
                "  免费云端——默认，无需安装；\n"
                "  本地大模型——本机 Ollama（如 qwen2.5:7b），不可用时仅回退到本机离线小模型；\n"
                "  付费 API——DeepL，质量最强、按量计费，需自备 key，原文会发送至 DeepL；\n"
                "  自托管服务——你单位自己的机器（vLLM / llama-server / 另一台机器上的 "
                "Ollama），地址与模型名自己配，数据去向由地址决定。"
            )
        messagebox.showinfo(
            "关于",
            f"文档翻译工具 v{VERSION}\n\n"
            f"模式：{mode_line}\n"
            f"{engine_lines}\n"
            f"程序目录：{APP_DIR}\n\n"
            f"{_format_hint('支持 ', ellipsis=False, image_tail='图片，译文保存在原文件旁边。')}\n"
            "涉密文档请用「启动翻译工具-保密模式.bat」。",
            parent=self.root,
        )

    def _bind_events(self) -> None:
        self.drop.bind("<Configure>", self._draw_dropzone)
        self.drop.bind("<Button-1>", lambda event: self.choose_file())
        self.drop.bind("<Enter>", self._on_drop_hover)
        self.drop.bind("<Leave>", self._on_drop_leave)

        if self.dnd_ready:
            try:
                self.drop.drop_target_register(DND_FILES)
                self.drop.dnd_bind("<<DropEnter>>", self._on_drop_hover)
                self.drop.dnd_bind("<<DropLeave>>", self._on_drop_leave)
                self.drop.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                self.dnd_ready = False
                logger.exception("拖放功能初始化失败")

        self.root.bind("<Control-o>", lambda event: self.choose_file())
        self.root.bind("<Escape>", lambda event: self.cancel_translation())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _draw_dropzone(self, _event=None) -> None:
        canvas = self.drop
        canvas.delete("all")
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width <= 1 or height <= 1:
            return
        edge = self.px(2)
        hover = self.drop_hover and not self.busy
        fill = "#f8fbff" if hover else CARD
        outline = ACCENT if hover else BORDER
        _canvas_round_rect(
            canvas,
            edge,
            edge,
            width - edge,
            height - edge,
            self.px(14),
            fill=fill,
            outline=outline,
            width=self.px(1),
        )

        cx = width / 2
        icon_y = height * 0.29
        radius = self.px(21)
        canvas.create_oval(
            cx - radius,
            icon_y - radius,
            cx + radius,
            icon_y + radius,
            fill=ACCENT_SOFT,
            outline="",
        )
        line_width = self.px(2)
        canvas.create_line(
            cx,
            icon_y - self.px(9),
            cx,
            icon_y + self.px(4),
            fill=ACCENT,
            width=line_width,
            arrow=tk.LAST,
            arrowshape=(self.px(8), self.px(10), self.px(4)),
        )
        canvas.create_line(
            cx - self.px(9),
            icon_y + self.px(2),
            cx - self.px(9),
            icon_y + self.px(9),
            cx + self.px(9),
            icon_y + self.px(9),
            cx + self.px(9),
            icon_y + self.px(2),
            fill=ACCENT,
            width=line_width,
            capstyle=tk.ROUND,
        )

        if self.busy:
            title = "正在翻译，请稍候"
            subtitle = "完成后译文会保存到原文件旁边"
        else:
            title = "把 Word / PDF / PPT / EPUB 拖到这里"
            subtitle = (
                "也可以点击选择文件 · 支持 .docx / .pdf / .pptx / .epub / 脚本 / 图片"
                if self.dnd_ready
                else "点击选择要翻译的文件 · 支持 .docx / .pdf / .pptx / .epub / 脚本 / 图片"
            )
        canvas.create_text(
            cx,
            height * 0.58,
            text=title,
            fill=TEXT,
            font=(FONT, 12, "bold"),
        )
        canvas.create_text(
            cx,
            height * 0.80,
            text=subtitle,
            fill=MUTED,
            font=(FONT, 9),
        )

    def _on_drop_hover(self, _event=None) -> None:
        self.drop_hover = True
        self._draw_dropzone()

    def _on_drop_leave(self, _event=None) -> None:
        self.drop_hover = False
        self._draw_dropzone()

    def _on_drop(self, event) -> None:
        self.drop_hover = False
        self._draw_dropzone()
        if self.busy:
            self._set_status("正在翻译，请等当前任务结束后再添加文件。", MUTED)
            return
        try:
            items = list(self.root.tk.splitlist(event.data))
        except Exception:
            items = [str(event.data)]
        if not items:
            return
        paths = _expand_inputs(items)
        if not paths:
            self._show_failure(_format_hint())
            return
        self.set_files(paths)

    def choose_file(self) -> None:
        if self.busy:
            self._set_status("正在翻译，请稍候。", MUTED)
            return
        initial = (
            os.path.dirname(self.src_path)
            if self.src_path
            else str(Path.home() / "Desktop")
        )
        paths = filedialog.askopenfilenames(
            parent=self.root,
            title="选择要翻译的文档或脚本（可按住 Ctrl 多选）",
            initialdir=initial,
            filetypes=formats.dialog_groups(),
        )
        if paths:
            self.set_files(list(paths))

    def set_files(self, paths: list[str]) -> None:
        """一次拖进多个文件（或整个文件夹）时排队处理。"""
        if not paths:
            return
        if len(paths) == 1:
            self.queue = []
            self.set_file(paths[0])
            return
        self.queue = list(paths)
        self.src_path = paths[0]
        self.output_path = None
        self.results = []
        self.badge.configure(text=f"{len(paths)} 个", bg=ACCENT_SOFT, fg=ACCENT)
        self.name_label.configure(text=f"已排队 {len(paths)} 个文件")
        self.meta_label.configure(
            text=_ellipsize(
                "、".join(os.path.basename(item) for item in paths), 64
            )
        )
        self.change_label.grid()
        self.ocr_switch.set_enabled(not self.busy)
        self.progress.configure(mode="determinate", maximum=100, value=0)
        self.percent_label.configure(text="")
        self._set_status(f"已排队 {len(paths)} 个文件，点击「开始翻译」", MUTED)
        self._append_log(f"已排队 {len(paths)} 个文件：", "info")
        for item in paths:
            self._append_log(f"  · {_ellipsize(os.path.basename(item), 48)}", "muted")
        self._refresh_buttons()

    def set_file(self, path: str) -> None:
        path = os.path.abspath(os.path.expanduser(str(path).strip()))
        name = os.path.basename(path)
        if not os.path.isfile(path):
            self._show_failure("找不到这个文件，请确认文件还在原位置。")
            return
        ext = os.path.splitext(path)[1].lower()
        if ext == ".doc":
            self._show_failure(
                "暂不支持旧版 .doc 文件。请在 Word 中另存为 .docx 后再拖进来。"
            )
            return
        if ext not in ALL_SUFFIXES:
            self._show_failure(_format_hint("只支持 "))
            return
        fmt = formats.get(ext)
        self.src_path = path
        self.output_path = None
        badge_bg, badge_fg = _badge_colors(fmt)
        self.badge.configure(
            text=_badge_text(fmt), bg=badge_bg, fg=badge_fg
        )
        self.name_label.configure(text=_ellipsize(name))
        kind = fmt.name
        try:
            size_text = human_size(os.path.getsize(path))
        except OSError:
            size_text = ""
        self.meta_label.configure(
            text=f"{size_text} · {kind}" if size_text else kind
        )
        self.change_label.grid()
        self.ocr_switch.set_enabled(
            ext == ".pdf" and not self.busy and self.pdf_layout != "inplace"
        )
        self.progress.configure(mode="determinate", maximum=100, value=0)
        self.percent_label.configure(text="")
        self._set_status("准备就绪，点击「开始翻译」", MUTED)
        if OFFLINE:
            self._append_log(f"已选择：{name}", "info")
        else:
            self._append_log(f"已选择：{path}", "info")
        self._refresh_buttons()

    def start_translation(self) -> None:
        if self.busy or not self.src_path:
            return
        targets = list(self.queue) if self.queue else [self.src_path]
        targets = [item for item in targets if os.path.isfile(item)]
        if not targets:
            self._show_failure("文件不存在，请重新选择。")
            return
        if not self._ensure_pdf_passwords(targets):
            return
        ocr_enabled = self.ocr_switch.get()
        if any(item.lower().endswith(".pdf") for item in targets) and not ocr_enabled:
            self.emit_log("已关闭 OCR：扫描版页面会被跳过。")

        self.busy = True
        self.output_path = None
        self.results = []
        self.batch_total = len(targets)
        self.batch_index = 0
        self.stop_event = threading.Event()
        self.progress.stop()
        self.progress.configure(mode="indeterminate")
        self.progress.start(14)
        self.percent_label.configure(text="")
        self._set_status(
            "正在准备文档，首次翻译需加载本地模型…" if OFFLINE else "正在准备文档…",
            TEXT,
        )
        self._draw_dropzone()
        self._refresh_buttons()
        if len(targets) > 1:
            self._append_log(f"开始批量翻译 {len(targets)} 个文件…", "info")
        else:
            self._append_log(
                f"开始翻译：{os.path.basename(targets[0])}", "info"
            )

        self.worker_thread = threading.Thread(
            target=self._run_translation,
            args=(targets, ocr_enabled, self.stop_event, self._ui_options()),
            daemon=True,
        )
        self.worker_thread.start()

    def _ensure_pdf_passwords(self, targets: list[str]) -> bool:
        """加密的 PDF 在开始前就要到密码——翻译跑在后台线程里，那里不能弹 Tk 对话框。"""
        import pymupdf
        from tkinter import simpledialog

        for path in targets:
            if not path.lower().endswith(".pdf"):
                continue
            try:
                probe = pymupdf.open(path)
            except Exception as exc:
                self._show_failure(f"打不开 {os.path.basename(path)}：{exc}")
                return False
            try:
                locked = bool(probe.needs_pass)
            finally:
                probe.close()
            if not locked:
                continue
            entered = simpledialog.askstring(
                "PDF 已加密",
                f"「{_ellipsize(os.path.basename(path), 40)}」有密码保护。\n"
                "请输入打开密码：",
                show="*",
                parent=self.root,
            )
            if not entered:
                self._show_failure(
                    f"已取消：{os.path.basename(path)} 需要密码才能打开。"
                )
                return False
            self.pdf_passwords[path] = entered
            self._append_log(
                f"已记录 {os.path.basename(path)} 的打开密码（仅本次使用，不保存）。",
                "muted",
            )
        return True

    def _ui_options(self) -> dict:
        """界面线程专用：把档位与两个开关快照成普通值，交给后台线程用。

        工作线程里读 tk 变量是崩溃级问题（跨线程取 Tcl）：主线程正在 ``mainloop`` 时它常常
        「碰巧能用」，一旦不在（无人值守驱动、个别 Tcl 构建线程检查更严）就直接
        ``RuntimeError: main thread is not in main loop``，而这份异常会被 ``_run_translation``
        的兜底 ``except Exception`` 吞成一句「程序出现意外问题」——v1.9.4 的 GUI 端到端验收
        就是在联网档实测到的（保密档恰好每个分支都在 ``OFFLINE`` 处短路，才一直没炸）。
        与 ``pdf_pages`` 同一套路：选项在界面线程快照，后台只读快照。
        """
        return {
            "engine_key": self._engine_key(),
            "no_cache": bool(
                getattr(self, "cache_switch", None) and self.cache_switch.get()
            ),
            "quality": (
                not getattr(self, "quality_switch", None) or self.quality_switch.get()
            ),
        }

    def _make_translator(
        self, stop_event: threading.Event | None, options: dict | None = None
    ) -> Translator:
        """按当前界面档位构造翻译器。

        档位清单与构造入口都在 ``core.engine_registry``（唯一真相），本方法只做
        一件事：把界面上的开关、缓存与翻译记忆换算成 ``EngineParams``。
        翻译与「译后编辑」的重译本段共用这一条路径——同一套档位口径，不搞第二条路。

        ``options`` 是界面线程拍好的快照：**从工作线程调用必须传**；留空表示就在界面线程上
        （译后编辑的重译/备选走这一条），现读控件、拿到的是用户刚拨过的那一档。
        """
        opts = options if options is not None else self._ui_options()
        key = engine_registry.OFFLINE if OFFLINE else opts["engine_key"]
        # 四档共用同一个记忆实例：同一句原文在任意档位必须得到同一份答复。
        # 保密模式零落盘，所以不注入（由登记表里的保密档负责说这句话）。
        tm = None if OFFLINE else _shared_tm(self.emit_log)
        cache_path = None if (OFFLINE or opts["no_cache"]) else str(
            CACHE_DIR / "translations.sqlite"
        )
        params = engine_registry.EngineParams(
            log=self.emit_log,
            style=self.style,
            cache_path=cache_path,
            tm=tm,
            jobs=CLI_JOBS,
            quality=opts["quality"],
            stop_event=stop_event,
            **_cli_tuning(),
        )
        return engine_registry.build(key, params)

    def _engine_key(self) -> str:
        """界面当前选中的档位。没有单选控件（保密模式）时返回免费云端，
        调用方在保密分支不会用到它——返回可构造的 key 比抛错好排查。"""
        key = getattr(self, "engine_var", None) and self.engine_var.get()
        return key or engine_registry.CLOUD

    def _run_translation(
        self,
        targets: list[str],
        ocr_enabled: bool,
        stop_event: threading.Event,
        options: dict,
    ) -> None:
        # try 覆盖到构造期：付费档 key 缺失这类「构造期 TranslationError」
        # 也要走统一的错误通道（events → _on_error 弹窗 + 恢复界面），
        # 不能让后台线程裸抛之后界面卡在「忙碌」状态。
        outputs: list[str] = []
        try:
            translator = self._make_translator(stop_event, options)
            self.translator = translator
            for index, src_path in enumerate(targets):
                check_cancel(stop_event)
                self.batch_index = index
                if len(targets) > 1:
                    self.emit_log(
                        f"[{index + 1}/{len(targets)}] "
                        f"{os.path.basename(src_path)}"
                    )
                try:
                    output = self._translate_one(
                        src_path, translator, ocr_enabled, stop_event
                    )
                except CancelledError:
                    raise
                except TranslationError as exc:
                    if len(targets) > 1:
                        self.emit_log(f"  跳过：{exc}")
                        self.results.append((src_path, None))
                        continue
                    raise
                outputs.append(output)
                self.results.append((src_path, output))
                if os.path.exists(output):
                    shown = shown_path(output)
                    self.emit_log(
                        f"已生成：{shown}（{human_size(os.path.getsize(output))}）"
                    )
            self.batch_index = 0
            self.batch_total = 0
            if not outputs:
                raise TranslationError("这批文件都没有生成译文，请检查文件内容。")
            self.events.put(("done", outputs[-1], list(self.results)))
        except CancelledError:
            self.events.put(("cancelled",))
        except TranslationError as exc:
            self.events.put(("error", str(exc)))
        except Exception as exc:
            logger.exception("翻译过程出现异常")
            detail = "".join(
                traceback.format_exception_only(type(exc), exc)
            ).strip()
            self.events.put(("error", f"程序出现意外问题：{detail}"))
        finally:
            logger.info("翻译任务结束")

    def _translate_one(
        self,
        src_path: str,
        translator: Translator,
        ocr_enabled: bool,
        stop_event: threading.Event,
    ) -> str:
        # 用量流水的 file 字段记当前文件名（只有 DeepLTranslator 带这个
        # 属性；hasattr 守卫让在线/本地引擎不受影响）。与 CLI _convert
        # 逐个文件设置 current_file 的行为对齐。
        if hasattr(translator, "current_file"):
            translator.current_file = os.path.basename(src_path)
        ext = os.path.splitext(src_path)[1].lower()
        if ext == ".pdf":
            return translate_pdf(
                src_path,
                translator,
                progress=self._queue_progress,
                log=self.emit_log,
                ocr_enabled=ocr_enabled,
                layout=self.pdf_layout,
                password=self.pdf_passwords.get(src_path),
                pages=self.pdf_pages or None,
            )
        if ext == ".pptx":
            from core.ppt_translator import translate_ppt

            return translate_ppt(
                src_path,
                translator,
                progress=self._queue_progress,
                log=self.emit_log,
                pages=self.pdf_pages or None,
            )
        if ext == ".epub":
            from core.epub_translator import translate_epub

            return translate_epub(
                src_path,
                translator,
                progress=self._queue_progress,
                log=self.emit_log,
                pages=self.pdf_pages or None,
            )
        if ext in SCRIPT_SUFFIXES:
            from core.script_translator import translate_script

            return translate_script(
                src_path,
                translator,
                log=self.emit_log,
                progress=self._queue_progress,
            )
        if ext in IMAGE_SUFFIXES:
            from core.image_translator import translate_image

            return translate_image(
                src_path,
                translator,
                log=self.emit_log,
                progress=self._queue_progress,
            )
        return translate_word(
            src_path,
            translator,
            progress=self._queue_progress,
            log=self.emit_log,
        )

    def _queue_progress(self, done, total, message: str) -> None:
        if self.batch_total > 1:
            # 批量时把单文件进度折算成整批进度，进度条才不会每换一个文件就跳回 0。
            fraction = done / max(total, 1)
            overall = (self.batch_index + fraction) / self.batch_total * 100
            self.events.put(
                (
                    "progress",
                    overall,
                    100,
                    f"[{self.batch_index + 1}/{self.batch_total}] {message}",
                )
            )
        else:
            self.events.put(("progress", done, total, message))

    def cancel_translation(self) -> None:
        if not self.busy or self.stop_event is None:
            return
        self.stop_event.set()
        self.cancel_btn.configure(state="disabled")
        self._set_status("正在取消，请稍候…", MUTED)
        self._append_log("已请求取消，将在当前段落或页面结束后停止。", "muted")

    def _pump_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self._append_log(event[1], event[2] if len(event) > 2 else "info")
                elif kind == "progress":
                    self._on_progress(event[1], event[2], event[3])
                elif kind == "done":
                    self._on_done(event[1], event[2] if len(event) > 2 else None)
                elif kind == "error":
                    self._on_error(event[1])
                elif kind == "cancelled":
                    self._on_cancelled()
        except queue.Empty:
            pass
        try:
            if self.root.winfo_exists():
                self.root.after(80, self._pump_events)
        except tk.TclError:
            pass

    def _on_progress(self, done, total, message: str) -> None:
        if total:
            mode = str(self.progress.cget("mode"))
            if mode != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate")
            self.progress.configure(maximum=total, value=done)
            self.percent_label.configure(text=f"{int(done / total * 100)}%")
        if message:
            self._set_status(message, TEXT)

    def _on_done(self, output: str, results=None) -> None:
        self.output_path = output
        self.busy = False
        self.stop_event = None
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100, value=100)
        self.percent_label.configure(text="100%")
        self._draw_dropzone()
        if results and len(results) > 1:
            done = [item for item in results if item[1]]
            failed = len(results) - len(done)
            self._append_log(
                f"批量完成：成功 {len(done)} 个"
                + (f"，失败 {failed} 个" if failed else "")
                + "。",
                "success",
            )
            for name, _output in results:
                if not _output:
                    self._append_log(f"  未生成：{os.path.basename(name)}", "error")
            self._set_status(
                f"批量完成：成功 {len(done)} 个"
                + (f"，失败 {failed} 个" if failed else ""),
                SUCCESS if not failed else DANGER,
            )
            if failed:
                try:
                    messagebox.showwarning(
                        "批量翻译完成（有失败）",
                        f"成功 {len(done)} 个，失败 {failed} 个。\n"
                        "失败的文件已在下方日志里列出，可单独重试。",
                        parent=self.root,
                    )
                except tk.TclError:
                    pass
        else:
            self._set_status(f"翻译完成：{os.path.basename(output)}", SUCCESS)
            self._append_log("翻译完成，可以点击「打开译文」查看。", "success")
            self._hint_postedit()
        self._refresh_buttons()

    def _hint_postedit(self) -> None:
        """翻译完成后若有疑点，提示走译后编辑闭环（v1.5.4）。"""
        if not self._edit_list_ready():
            return
        try:
            from core.postedit import edit_list_path, load_edit_list

            items = load_edit_list(edit_list_path(self.output_path))
        except Exception:
            return
        with_issues = sum(1 for item in items if item.issues)
        if with_issues:
            self._append_log(
                f"自检发现 {with_issues} 处疑点，可点击「译后编辑」逐条定位处理。",
                "info",
            )
        elif items:
            self._append_log(
                f"已生成译后编辑清单（{len(items)} 段可改），"
                "可点击「译后编辑」逐段校对。",
                "muted",
            )

    def _on_error(self, message: str) -> None:
        self.busy = False
        self.stop_event = None
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.percent_label.configure(text="")
        self._set_status(f"翻译失败：{message}", DANGER)
        self._append_log(f"翻译失败：{message}", "error")
        self._draw_dropzone()
        self._refresh_buttons()
        try:
            messagebox.showerror("翻译失败", message, parent=self.root)
        except tk.TclError:
            pass

    def _on_cancelled(self) -> None:
        self.busy = False
        self.stop_event = None
        self.batch_index = 0
        self.batch_total = 0
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.percent_label.configure(text="")
        completed = [output for _source, output in self.results if output]
        self.output_path = completed[-1] if completed else None
        if completed:
            self._set_status(f"已取消翻译，已保留 {len(completed)} 个成品", MUTED)
            self._append_log(
                f"已取消，已完成的 {len(completed)} 个译文文件已保留，"
                "可点击「打开译文」查看最后一个成品。",
                "muted",
            )
            for output in completed:
                self._append_log(f"  已保留：{shown_path(output)}", "success")
            self._hint_postedit()
        else:
            self._set_status("已取消翻译", MUTED)
            self._append_log(
                "已取消，本次没有已完成的译文文件。"
                if self.results else "已取消，未生成译文文件。",
                "muted",
            )
        self._draw_dropzone()
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        ext = (
            os.path.splitext(self.src_path)[1].lower() if self.src_path else ""
        )
        has_file = bool(self.src_path)
        has_output = bool(self.output_path and os.path.exists(self.output_path))
        if self.busy:
            self.start_btn.configure(state="disabled")
            self.cancel_btn.configure(state="normal")
            self.open_btn.configure(state="disabled")
            self.folder_btn.configure(state="disabled")
            self.postedit_btn.configure(state="disabled")
            self.ocr_switch.set_enabled(False)
            self.change_label.configure(fg="#aebdd0", cursor="arrow")
        else:
            self.start_btn.configure(state="normal" if has_file else "disabled")
            self.cancel_btn.configure(state="disabled")
            self.open_btn.configure(state="normal" if has_output else "disabled")
            self.folder_btn.configure(state="normal" if has_file else "disabled")
            self.postedit_btn.configure(
                state="normal" if has_output and self._edit_list_ready() else "disabled"
            )
            self.ocr_switch.set_enabled(
                ext == ".pdf" and self.pdf_layout != "inplace"
            )
            self.change_label.configure(fg=ACCENT, cursor="hand2")

    def _edit_list_ready(self) -> bool:
        """成品旁边有没有译后编辑清单（v1.5.4 翻译成功才会落）。"""
        if not self.output_path or not os.path.exists(self.output_path):
            return False
        from core.postedit import edit_list_path

        return os.path.isfile(edit_list_path(self.output_path))

    def open_output(self) -> None:
        if not self.output_path or not os.path.exists(self.output_path):
            messagebox.showinfo(
                "尚未生成译文",
                "请先完成一次翻译，再打开译文。",
                parent=self.root,
            )
            return
        try:
            os.startfile(self.output_path)
        except OSError as exc:
            messagebox.showerror(
                "无法打开文件", f"{exc}\n\n文件位置：{self.output_path}", parent=self.root
            )

    def open_folder(self) -> None:
        target = self.output_path if self.output_path and os.path.exists(
            self.output_path
        ) else self.src_path
        if not target:
            return
        folder = os.path.dirname(target)
        try:
            os.startfile(folder)
        except OSError as exc:
            messagebox.showerror("无法打开文件夹", str(exc), parent=self.root)

    def open_postedit(self) -> None:
        if not self._edit_list_ready():
            messagebox.showinfo(
                "尚无译后编辑清单",
                "请先完成一次翻译——翻译成功后，成品旁边会生成一份译后编辑清单。",
                parent=self.root,
            )
            return
        PostEditWindow(self)

    def open_termbase(self) -> None:
        """打开术语库窗口（v1.5.5）：未决术语池 + 人工裁决 + TBX 导入导出。

        与「译后编辑」不同，术语库是跨批次积累的，不依赖某次翻译的成品，
        所以这个按钮常态可用——翻译前就能查、能改、能导入外部 TBX。
        """
        TermbaseWindow(self)

    def _show_failure(self, message: str) -> None:
        self._set_status(message, DANGER)
        self._append_log(message, "error")

    def _set_status(self, text: str, color: str = MUTED) -> None:
        self.status_label.configure(text=text, fg=color)

    def _append_log(self, message: str, level: str = "info") -> None:
        if not hasattr(self, "log_text"):
            return
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{timestamp}  ", "time")
        self.log_text.insert("end", f"{message}\n", level)
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 400:
            self.log_text.delete("1.0", "80.0")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._append_log("日志已清空。", "muted")

    def emit_log(self, message: str) -> None:
        logger.info(message)
        self.events.put(("log", message))

    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno(
                "退出",
                "翻译还在进行中，现在退出会丢失本次结果。确定退出吗？",
                parent=self.root,
            ):
                return
            if self.stop_event is not None:
                self.stop_event.set()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        self.root.mainloop()


class CustomEngineDialog:
    """自托管翻译服务配置窗口：服务名、地址、模型名，加一次连通性测试。

    三个字段各有各的用处：地址决定「数据发去哪儿」（本机 / 内网 / 公网，
    直接关系要不要过涉密闸门），模型名决定「调哪个模型」，服务名只出现在
    日志与确认弹窗里。**密钥不在这里填**——需要 bearer key 的服务读
    ``api_keys.local.json`` 的 ``custom.key``，与 DeepL 共用同一个永不进 git
    的文件；配置窗口里存 key 等于把秘密写进一个会被拷来拷去的中文 json。

    「保存并使用」不自己判断闸门：它只把档位拨到自托管并调
    ``TranslatorApp._on_engine_change``，公网地址该弹的确认框由那一道闸门弹，
    避免同一个合规判断在这里再写一遍（写两遍必然会写歪）。
    """

    def __init__(self, root: tk.Misc, app: "TranslatorApp") -> None:
        from core import custom_engine

        self.app = app
        self.px = getattr(app, "px", lambda value: value)
        self.config = custom_engine.load_config()

        self.window = tk.Toplevel(root)
        self.window.title("配置自托管翻译服务")
        self.window.geometry("620x420")
        self.window.configure(bg=BG)
        self.window.transient(root)

        outer = tk.Frame(self.window, bg=BG, padx=self.px(16), pady=self.px(14))
        outer.pack(fill="both", expand=True)
        outer.grid_columnconfigure(1, weight=1)

        tk.Label(
            outer,
            text="把翻译请求发到你单位自己的服务上（vLLM / llama-server / "
            "LM Studio / 另一台机器上的 Ollama），需要 OpenAI 兼容的 "
            "/v1/chat/completions 接口。",
            bg=BG,
            fg=MUTED,
            font=(FONT, 9),
            justify="left",
            anchor="w",
            wraplength=self.px(560),
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        self.name_var = tk.StringVar(value=self.config.name)
        self.url_var = tk.StringVar(value=self.config.base_url)
        self.model_var = tk.StringVar(value=self.config.model)
        rows = (
            ("服务名称", self.name_var, "只为让日志看得出当前在调哪台机器"),
            ("服务地址", self.url_var, "例：http://192.168.1.20:8000/v1"),
            ("模型名", self.model_var, "留空＝自动读服务上的模型（服务只挂一个时直接用它）"),
        )
        for offset, (caption, variable, example) in enumerate(rows):
            row = 1 + offset
            tk.Label(outer, text=caption, bg=BG, fg=TEXT, font=(FONT, 9)).grid(
                row=row, column=0, sticky="w", pady=self.px(4)
            )
            entry = tk.Entry(
                outer,
                textvariable=variable,
                font=(FONT, 9),
                bg=CARD,
                fg=TEXT,
                insertbackground=TEXT,
                relief="flat",
                highlightthickness=1,
                highlightbackground=BORDER,
            )
            entry.grid(row=row, column=1, sticky="ew", padx=(self.px(8), 0))
            tk.Label(outer, text=example, bg=BG, fg=MUTED, font=(FONT, 8)).grid(
                row=row, column=1, sticky="w", pady=(self.px(20), 0)
            )
            if variable is self.url_var:
                entry.bind("<KeyRelease>", lambda event: self._refresh_hint())

        self.scope_label = tk.Label(
            outer, text="", bg=BG, fg=TEXT, font=(FONT, 9), anchor="w", justify="left"
        )
        self.scope_label.grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(self.px(12), 0)
        )
        self.status_label = tk.Label(
            outer, text="", bg=BG, fg=MUTED, font=(FONT, 8), anchor="w", justify="left"
        )
        self.status_label.grid(row=5, column=0, columnspan=2, sticky="w")

        buttons = tk.Frame(outer, bg=BG)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(self.px(14), 0))
        for text, command in (
            ("测试连接", self._test),
            ("保存并使用", self._save),
            ("关闭", self.window.destroy),
        ):
            ttk.Button(buttons, text=text, command=command).pack(
                side="left", padx=(self.px(6), 0)
            )
        self._refresh_hint()

    def _refresh_hint(self) -> None:
        """地址一改就念出「数据去哪儿」，让用户在保存前就看到自己填的东西意味着什么。"""
        from core import custom_engine

        raw = self.url_var.get().strip()
        if not raw:
            self.scope_label.configure(text="还没填地址。", fg=MUTED)
            return
        try:
            url = custom_engine.normalize_base_url(raw)
        except TranslationError as exc:
            self.scope_label.configure(text=str(exc), fg=DANGER)
            return
        scope = custom_engine.endpoint_scope(url)
        if scope == "public" and url.startswith("http://"):
            self.scope_label.configure(
                text=f"{url} —— 地址在公网且是明文 HTTP，不允许保存（请改 https://）。",
                fg=DANGER,
            )
            return
        text = custom_engine.SCOPE_TEXT[scope]
        extra = (
            ""
            if scope != "public"
            else "（公网端点：保存后首次使用会要求确认涉密警示）"
        )
        self.scope_label.configure(
            text=f"{url} —— 文档原文{text}。" + extra,
            fg=TEXT if scope != "public" else DANGER,
        )

    def _test(self) -> None:
        """连一次 ``/v1/models``，把服务上真实存在的模型名列出来。

        这一步是配置自托管时最容易省的、也最不该省的一步：模型名写错了
        不会报错，只会静默回退免费云端——用户以为在用单位的 GPU 服务器。
        """
        from core import custom_engine

        try:
            url = custom_engine.normalize_base_url(self.url_var.get())
        except TranslationError as exc:
            self.status_label.configure(text=str(exc), fg=DANGER)
            return
        models = custom_engine.discover_models(url)
        if not models:
            self.status_label.configure(
                text=f"没连上 {url} 的 /v1/models（服务没起、地址不对或不提供模型列表）。"
                "可以先保存，模型名自己填准。",
                fg=DANGER,
            )
            return
        self.status_label.configure(
            text="服务上有 " + str(len(models)) + " 个模型：" + "、".join(models[:5]),
            fg=SUCCESS,
        )
        if not self.model_var.get().strip() and len(models) == 1:
            self.model_var.set(models[0])

    def _save(self) -> bool:
        from core import custom_engine

        raw = self.url_var.get().strip()
        if raw:
            try:
                url = custom_engine.require_usable(raw)
            except TranslationError as exc:
                self.status_label.configure(text=str(exc), fg=DANGER)
                return False
        else:
            url = ""
        config = custom_engine.CustomEngineConfig(
            enabled=bool(url),
            name=self.name_var.get().strip() or "单位自托管服务",
            base_url=url,
            model=self.model_var.get().strip(),
        )
        try:
            saved = custom_engine.save_config(config)
        except OSError as exc:
            self.status_label.configure(text=f"配置写不进去：{exc}", fg=DANGER)
            return False
        self.config = config
        self.status_label.configure(text=f"已保存：{saved}", fg=SUCCESS)
        if url:
            # 闸门交给 _on_engine_change：公网地址该弹的确认框在这里弹一次就够。
            self.app.engine_var.set(engine_registry.CUSTOM)
            self.app._on_engine_change()
        self.app.emit_log(custom_engine.describe(config))
        return True


class PostEditWindow:
    """译后编辑窗口（v1.5.4，E2 最小闭环）：疑点定位 + 改段重译 + 写回不坏版式。

    左列是疑点优先的段落清单（默认只看疑点），右列原文/疑点只读、译文可编。
    「重译本段」用与翻译时同档位的引擎重摇一段；「保存写回」走
    ``core/postedit.apply_edits``——只改这段文字、不动格式，源文件不碰。
    定位漂移（成品被人动过）时跳过并出声，不盲写。
    """

    def __init__(self, app: "TranslatorApp") -> None:
        from core.postedit import edit_list_path, load_edit_list

        self.app = app
        self.output_path = app.output_path
        self.items = load_edit_list(edit_list_path(self.output_path))
        self.current = None
        self.visible: list = []
        self.only_issues = tk.BooleanVar(value=True)

        self.window = tk.Toplevel(app.root)
        self.window.title("译后编辑")
        self.window.geometry("920x600")
        self.window.configure(bg=BG)
        self.window.transient(app.root)

        outer = tk.Frame(self.window, bg=BG, padx=16, pady=14)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x")
        issue_count = sum(1 for item in self.items if item.issues)
        tk.Label(
            header,
            text=f"{os.path.basename(self.output_path)}",
            bg=BG,
            fg=TEXT,
            font=("Microsoft YaHei", 11, "bold"),
        ).pack(side="left")
        self.summary_label = tk.Label(
            header,
            text=f"共 {len(self.items)} 段 · 疑点 {issue_count} 处",
            bg=BG,
            fg=MUTED,
        )
        self.summary_label.pack(side="left", padx=(10, 0))
        self.only_check = tk.Checkbutton(
            header,
            text="只看疑点",
            variable=self.only_issues,
            bg=BG,
            fg=TEXT,
            activebackground=BG,
            command=self._refresh_list,
        )
        self.only_check.pack(side="right")

        body = tk.Frame(outer, bg=BG)
        body.pack(fill="both", expand=True, pady=(10, 0))
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        left = tk.Frame(body, bg=BG, width=300)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)
        self.listbox = tk.Listbox(left, bg="#ffffff", fg=TEXT, bd=0, highlightthickness=0)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar = tk.Scrollbar(left, command=self.listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(4, weight=1)
        right.grid_columnconfigure(0, weight=1)
        self.loc_label = tk.Label(
            right, text="", bg=BG, fg=TEXT, font=("Microsoft YaHei", 11, "bold")
        )
        self.loc_label.grid(row=0, column=0, sticky="w")
        self._field(right, 1, "原文", readonly=True)
        self._field(right, 2, "疑点", readonly=True)
        self._field(right, 3, "译文（可改）", readonly=False)
        self.trans_text = self.fields[2]  # 译文框（前两个是只读的原文/疑点）

        # 备选区（v1.6.2，E4）：双击一条候选即填进译文框，**不自动写回**——
        # 选候选只是换个说法，落盘仍要人点「保存写回」。
        tk.Label(right, text="备选译文（双击选用）", bg=BG, fg=MUTED).grid(
            row=7, column=0, sticky="w", pady=(8, 0)
        )
        self.cand_list = tk.Listbox(
            right, height=3, bg="#ffffff", fg=TEXT, bd=0, highlightthickness=0,
            activestyle="none",
        )
        self.cand_list.grid(row=8, column=0, sticky="ew", pady=(2, 0))
        self.cand_list.bind("<Double-Button-1>", self._use_candidate)

        buttons = tk.Frame(right, bg=BG)
        buttons.grid(row=9, column=0, sticky="ew", pady=(10, 0))
        self.retranslate_btn = ttk.Button(
            buttons, text="重译本段", style="Soft.TButton", command=self._retranslate
        )
        self.retranslate_btn.pack(side="left")
        self.alternative_btn = ttk.Button(
            buttons, text="要备选译文", style="Soft.TButton", command=self._request_alternative
        )
        self.alternative_btn.pack(side="left", padx=(8, 0))
        self.restore_btn = ttk.Button(
            buttons, text="恢复初译", style="Soft.TButton", command=self._restore
        )
        self.restore_btn.pack(side="left", padx=(8, 0))
        self.save_btn = ttk.Button(
            buttons, text="保存写回", style="Accent.TButton", command=self._save
        )
        self.save_btn.pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons, text="关闭", style="Soft.TButton", command=self.window.destroy
        ).pack(side="right")
        self.status_label = tk.Label(
            buttons, text="", bg=BG, fg=MUTED
        )
        self.status_label.pack(side="right", padx=(0, 12))

        self._refresh_list()
        if self.visible:
            self.listbox.selection_set(0)
            self._on_select()

    def _field(self, parent, row: int, label: str, readonly: bool) -> None:
        tk.Label(parent, text=label, bg=BG, fg=MUTED).grid(
            row=row * 2 - 1, column=0, sticky="w", pady=(8, 0)
        )
        widget = tk.Text(
            parent,
            height=4,
            wrap="word",
            bg="#ffffff" if not readonly else "#f4f6f9",
            fg=TEXT,
            bd=0,
            highlightthickness=0,
            padx=8,
            pady=6,
        )
        widget.grid(row=row * 2, column=0, sticky="ew", pady=(2, 0))
        if readonly:
            widget.configure(state="disabled")
        if not hasattr(self, "fields"):
            self.fields: list = []
        self.fields.append(widget)

    def _fill(self, widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        if widget is not self.trans_text:
            widget.configure(state="disabled")

    def _refresh_list(self) -> None:
        self.listbox.delete(0, "end")
        if self.only_issues.get():
            self.visible = [item for item in self.items if item.issues]
        else:
            self.visible = list(self.items)
        self.visible.sort(key=lambda item: (not item.issues, item.id))
        for item in self.visible:
            mark = "⚠ " if item.issues else "  "
            star = "  *待写回" if item.pending else ""
            snippet = item.source.replace("\n", " ")[:14]
            self.listbox.insert("end", f"{mark}{item.location}｜{snippet}{star}")

    def _on_select(self, _event=None) -> None:
        if not self.listbox.curselection():
            return
        self.current = self.visible[self.listbox.curselection()[0]]
        self.loc_label.configure(text=f"{self.current.location}（{self.current.id}）")
        self._fill(self.fields[0], self.current.source)
        self._fill(
            self.fields[1], "\n".join(self.current.issues) or "（这段没有疑点）"
        )
        self._fill(self.trans_text, self.current.translation)
        self._refresh_candidates()

    def _refresh_candidates(self) -> None:
        """备选区跟着选中段走：把候选列出来，和现译相同的那条标「当前」。"""
        self.cand_list.delete(0, "end")
        if not self.current:
            return
        from core.translator import normalize_source

        current = normalize_source(self.current.translation)
        for position, candidate in enumerate(self.current.candidates, 1):
            suffix = "   ← 当前译文" if normalize_source(candidate) == current else ""
            snippet = " ".join(candidate.split())
            if len(snippet) > 46:
                snippet = snippet[:45] + "…"
            self.cand_list.insert("end", f"{position}. {snippet}{suffix}")
        if not self.current.candidates:
            self.cand_list.insert("end", "（还没有备选，点「要备选译文」让引擎再摇一条）")

    def _set_busy(self, busy: bool, text: str = "") -> None:
        state = "disabled" if busy else "normal"
        self.retranslate_btn.configure(state=state)
        self.alternative_btn.configure(state=state)
        self.restore_btn.configure(state=state)
        self.save_btn.configure(state=state)
        self.status_label.configure(text=text, fg=MUTED if busy else TEXT)

    def _retranslate(self) -> None:
        if not self.current:
            return
        from core.postedit import retranslate_segment

        self._set_busy(True, "重译中…（本地大模型可能要几十秒）")
        self.window.update_idletasks()
        try:
            translator = self.app._make_translator(stop_event=None)
            fresh = retranslate_segment(translator, self.current.source)
            self.current.translation = fresh
            self._fill(self.trans_text, fresh)
            self.app._append_log(f"已重译 {self.current.location}：{fresh}", "info")
            self._set_busy(False, "重译完成，确认后点「保存写回」")
        except TranslationError as exc:
            self._set_busy(False)
            messagebox.showerror("重译失败", str(exc), parent=self.window)
        except Exception as exc:  # 引擎层的意外也不该把窗口留在忙态
            logger.exception("译后编辑重译出现异常")
            self._set_busy(False)
            messagebox.showerror("重译失败", f"{type(exc).__name__}: {exc}", parent=self.window)

    def _restore(self) -> None:
        if not self.current:
            return
        self._fill(self.trans_text, self.current.applied or self.current.source)

    def _request_alternative(self) -> None:
        """让引擎把当前段重摇一条不同说法，存进备选区（不写成品）。"""
        if not self.current:
            return
        from core.alternatives import add_candidate, generate_alternative
        from core.postedit import dump_edit_list

        self._set_busy(True, "正在要备选…（本地大模型可能要几十秒）")
        self.window.update_idletasks()
        try:
            translator = self.app._make_translator(stop_event=None)
            fresh = generate_alternative(translator, self.current)
            if add_candidate(self.current, fresh):
                dump_edit_list(self.output_path, self.items)
                self.app._append_log(
                    f"{self.current.location} 收到一条备选译文：{fresh}", "info"
                )
                self._set_busy(False, "双击备选即可选用（仍要点「保存写回」才落盘）")
            else:
                self._set_busy(False, "这条和已有候选重复，没再加")
            self._refresh_candidates()
        except TranslationError as exc:
            self._set_busy(False)
            messagebox.showinfo("没有拿到备选", str(exc), parent=self.window)
        except Exception as exc:  # 引擎层的意外也不该把窗口留在忙态
            logger.exception("译后编辑取备选出现异常")
            self._set_busy(False)
            messagebox.showerror(
                "取备选失败", f"{type(exc).__name__}: {exc}", parent=self.window
            )

    def _use_candidate(self, _event=None) -> None:
        """双击一条候选 → 填进译文框。只是换个说法，不替你写回成品。"""
        if not self.current or not self.current.candidates:
            return
        selection = self.cand_list.curselection()
        if not selection:
            return
        from core.alternatives import pick_candidate

        try:
            chosen = pick_candidate(self.current, selection[0])
        except TranslationError as exc:
            messagebox.showwarning("选不上", str(exc), parent=self.window)
            return
        self._fill(self.trans_text, chosen)
        self._refresh_list()
        self._refresh_candidates()
        self.status_label.configure(
            text="已选用这条备选 —— 点「保存写回」才写进成品", fg=TEXT
        )

    def _save(self) -> None:
        if not self.current:
            return
        from core.postedit import apply_edits, dump_edit_list

        self.current.translation = self.trans_text.get("1.0", "end-1c").strip()
        if not self.current.translation:
            messagebox.showwarning(
                "译文是空的", "请先填写译文，或点「恢复初译」。", parent=self.window
            )
            return
        result = apply_edits(
            self.output_path,
            [self.current],
            log=lambda message: self.app._append_log(message, "success"),
            # 传播：手改过的段写进翻译记忆（保密模式 _shared_tm 返回 None，
            # 于是这里自动不写——不需要第二处判断）。
            tm=_shared_tm(self.app.emit_log),
        )
        if self.current.id in result.applied:
            self.current.applied = self.current.translation
            dump_edit_list(self.output_path, self.items)
            self.app._append_log(
                f"已写回 {self.current.location}：只改了这段文字，格式与源文件未动。",
                "success",
            )
            messagebox.showinfo(
                "已写回",
                f"{self.current.location} 的译文已更新。\n只改了这段文字，格式未动。",
                parent=self.window,
            )
            self._refresh_list()
        else:
            messagebox.showwarning(
                "未能写回",
                "\n".join(result.notes) or "在成品里定位不到这段，未改动成品。",
                parent=self.window,
            )


class TermbaseWindow:
    """术语库窗口（v1.5.5）：未决术语池可视化 + 人工裁决 + TBX 导入导出。

    结构照 ``PostEditWindow``（Toplevel + 左清单右详情 + 操作按钮 + 底部
    状态栏），但盯的是另一件事——**术语一致性的人回路口**：

    * 清单排序「未决优先」：disputed（模型自报译法冲突 / 人工标疑）排最前，
      candidate（候选，还没有译名）次之；adopted / rejected 默认折叠，
      取消勾选「只看未决」才浏览。人要回路先看需要自己拍板的地方；
    * 裁决三动作（采用 / 改名 / 拒绝）直接调 ``Termbase`` 的对应方法，
      不自己写第二套状态机——术语状态流转全项目只有 termbase.py 一处
      实现（见该文件顶部注释）；
    * TBX 导入复用 ``core/tbx.import_entries`` 的去重规则：已存在条目一律
      保留库内原值，外部文件不覆盖用户既有裁决，冲突在主窗口日志与状态栏
      出声，绝不静默；
    * TBX 导出只带「已采用」条款（``core/tbx.export_entries`` 的默认口径）：
      存疑/候选是未决状态，导出给别的工具等于把半成品决策推出去。

    同一个窗口里还有「翻译记忆」分区（两个按钮 + 条数）：术语库与记忆库
    同属**语言资产**、心智一致（都是"攒下来、要维护、要能带走"的东西），
    给它们各开一个窗口反而让人记不住去哪儿维护。刻意不新建窗口类——在这
    个 127KB 的文件上再叠一个类，改动的成本和风险都不划算。

    TMX 导出比 TBX 多一道闸门：它导出的是**原文句子**（明文 XML），属于
    保密红线行为，所以除了界面的二次确认对话框，保密模式直接拒绝。
    """

    #: 状态中文名与清单行首标记。disputed 用 ⚠——它对应「模型发现同一源词
    #: 出现了两种译法」，是最需要人看的一档。
    _STATE_LABELS = {
        "candidate": "候选",
        "adopted": "已采用",
        "disputed": "存疑",
        "rejected": "已拒绝",
    }
    _STATE_MARKS = {"candidate": "·", "adopted": "✓", "disputed": "⚠", "rejected": "×"}

    def __init__(self, app: "TranslatorApp", store: "StateStore | None" = None) -> None:
        from core.state import StateStore
        from core.termbase import Termbase
        from core.tm import TranslationMemory

        self.app = app
        # store 可注入（测试传临时 SQLite）；默认走与翻译管线同一个库文件
        self.store = store or StateStore(str(CACHE_DIR / "translations.sqlite"))
        self.termbase = Termbase(self.store, log=self._log)
        # 翻译记忆与术语库同一份库、不同表：在这里一并维护。
        self.memory = TranslationMemory(self.store, log=self._log)
        self.rows: list[dict[str, Any]] = []
        self.visible: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.only_pending = tk.BooleanVar(value=True)

        self.window = tk.Toplevel(app.root)
        self.window.title("术语库")
        self.window.geometry("900x560")
        self.window.configure(bg=BG)
        self.window.transient(app.root)

        outer = tk.Frame(self.window, bg=BG, padx=self.px(16), pady=self.px(14))
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x")
        tk.Label(
            header,
            text="术语库",
            bg=BG,
            fg=TEXT,
            font=("Microsoft YaHei", 11, "bold"),
        ).pack(side="left")
        self.summary_label = tk.Label(header, text="", bg=BG, fg=MUTED)
        self.summary_label.pack(side="left", padx=(self.px(10), 0))
        tk.Checkbutton(
            header,
            text="只看未决",
            variable=self.only_pending,
            bg=BG,
            fg=TEXT,
            activebackground=BG,
            command=self._refresh,
        ).pack(side="right")

        body = tk.Frame(outer, bg=BG)
        body.pack(fill="both", expand=True, pady=(self.px(10), 0))
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        left = tk.Frame(body, bg=BG, width=self.px(320))
        left.grid(row=0, column=0, sticky="nsew", padx=(0, self.px(12)))
        left.grid_rowconfigure(0, weight=1)
        left.grid_columnconfigure(0, weight=1)
        self.listbox = tk.Listbox(left, bg="#ffffff", fg=TEXT, bd=0, highlightthickness=0)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar = tk.Scrollbar(left, command=self.listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        self.state_label = tk.Label(
            right, text="", bg=BG, fg=ACCENT, font=("Microsoft YaHei", 10, "bold")
        )
        self.state_label.grid(row=0, column=0, sticky="w")
        self._field(right, 1, "源词", readonly=True)
        self._field(right, 2, "当前译名", readonly=True)
        self._field(right, 3, "备注", readonly=True)
        self.source_text = self.fields[0]
        self.target_text = self.fields[1]
        self.note_text = self.fields[2]

        tk.Label(
            right, text="新译名（「拒绝」时填原因）", bg=BG, fg=MUTED
        ).grid(row=7, column=0, sticky="w", pady=(self.px(8), 0))
        self.target_entry = tk.Entry(
            right,
            bg="#ffffff",
            fg=TEXT,
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
        )
        self.target_entry.grid(row=8, column=0, sticky="ew", pady=(self.px(2), 0))

        buttons = tk.Frame(right, bg=BG)
        buttons.grid(row=9, column=0, sticky="ew", pady=(self.px(10), 0))
        self.adopt_btn = ttk.Button(
            buttons, text="采用", style="Accent.TButton", command=self._adopt
        )
        self.adopt_btn.pack(side="left")
        self.rename_btn = ttk.Button(
            buttons, text="改名", style="Soft.TButton", command=self._rename
        )
        self.rename_btn.pack(side="left", padx=(self.px(8), 0))
        self.reject_btn = ttk.Button(
            buttons, text="拒绝", style="Soft.TButton", command=self._reject
        )
        self.reject_btn.pack(side="left", padx=(self.px(8), 0))
        ttk.Button(
            buttons, text="导入 TBX", style="Soft.TButton", command=self._import_tbx
        ).pack(side="right")
        ttk.Button(
            buttons, text="导出 TBX", style="Soft.TButton", command=self._export_tbx
        ).pack(side="right", padx=(0, self.px(8)))
        ttk.Button(
            buttons, text="关闭", style="Soft.TButton", command=self.window.destroy
        ).pack(side="right", padx=(0, self.px(8)))
        self.status_label = tk.Label(buttons, text="", bg=BG, fg=MUTED)
        self.status_label.pack(side="right", padx=(0, self.px(12)))

        # ---- 翻译记忆分区 ----
        # 与术语库同一个窗口：两者都是「攒下来的语言资产」，心智一致。
        # 只有两个按钮和一个条数——记忆是按原文自动匹配的，不需要像术语
        # 那样逐条裁决，给它一整个列表窗口是浪费。
        memory = tk.Frame(outer, bg=BG)
        memory.pack(fill="x", pady=(self.px(10), 0))
        tk.Label(
            memory,
            text="翻译记忆",
            bg=BG,
            fg=TEXT,
            font=("Microsoft YaHei", 11, "bold"),
        ).pack(side="left")
        self.memory_label = tk.Label(memory, text="", bg=BG, fg=MUTED)
        self.memory_label.pack(side="left", padx=(self.px(10), 0))
        ttk.Button(
            memory, text="导入 TMX", style="Soft.TButton", command=self._import_tmx
        ).pack(side="right")
        ttk.Button(
            memory, text="导出 TMX", style="Soft.TButton", command=self._export_tmx
        ).pack(side="right", padx=(0, self.px(8)))

        self._refresh()
        if self.visible:
            self.listbox.selection_set(0)
            self._on_select()
        else:
            # 数据库不存在/为空都不崩（StateStore 会自建空库）：给一条出路，
            # 告诉用户术语从哪来——翻译时自动积累，或从 TBX 导入。
            self._set_status(
                "术语库还是空的：翻译时会自动积累候选术语，"
                "也可用「导入 TBX」灌入外部术语库。"
            )

    def px(self, value: float) -> int:
        """DPI 适配与主窗口同一把尺子（窗口是自己的 Toplevel，借 app 的）。"""
        return self.app.px(value)

    def _field(self, parent, row: int, label: str, readonly: bool) -> None:
        tk.Label(parent, text=label, bg=BG, fg=MUTED).grid(
            row=row * 2 - 1, column=0, sticky="w", pady=(self.px(8), 0)
        )
        widget = tk.Text(
            parent,
            height=3,
            wrap="word",
            bg="#ffffff" if not readonly else "#f4f6f9",
            fg=TEXT,
            bd=0,
            highlightthickness=0,
            padx=8,
            pady=6,
        )
        widget.grid(row=row * 2, column=0, sticky="ew", pady=(2, 0))
        if readonly:
            widget.configure(state="disabled")
        if not hasattr(self, "fields"):
            self.fields: list = []
        self.fields.append(widget)

    def _fill(self, widget: tk.Text, text: str) -> None:
        """三个详情框都是只读的，填完照旧锁回去。"""
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _log(self, message: str) -> None:
        """术语库的每个动作都往主窗口日志里写一份——GUI 可查、可复制。"""
        self.app._append_log(message, "info")

    def _set_status(self, message: str) -> None:
        """底部状态栏出声 + 主窗口日志留痕。"""
        self.status_label.configure(text=message, fg=TEXT)
        self._log(message)

    def _refresh(self) -> None:
        """按「存疑 → 候选 → 已采用 → 已拒绝」重拉清单；只看未决时折叠后两档。"""
        self.listbox.delete(0, "end")
        self.rows = []
        for state in ("disputed", "candidate", "adopted", "rejected"):
            self.rows.extend(self.store.terms_by_state(state))
        if self.only_pending.get():
            self.visible = [
                row for row in self.rows if row["state"] in ("disputed", "candidate")
            ]
        else:
            self.visible = list(self.rows)
        for row in self.visible:
            mark = self._STATE_MARKS.get(row["state"], "·")
            target = (row.get("target") or "").strip() or "（待定）"
            line = f"{mark} {row['source']} → {target}"
            note = (row.get("note") or "").strip()
            if note:
                line += f"  # {_ellipsize(note, 20)}"
            self.listbox.insert("end", line)
        stats = self.termbase.stats()
        self.summary_label.configure(
            text=(
                f"已采用 {stats.get('adopted', 0)} · 存疑 {stats.get('disputed', 0)} · "
                f"候选 {stats.get('candidate', 0)} · 已拒绝 {stats.get('rejected', 0)}"
            )
        )
        self._refresh_memory_label()
        if not self.visible:
            # 提示行也进 listbox（用户看得见），但 visible 保持空——
            # 选中它没有意义，_on_select 会拦。
            self.listbox.insert("end", "（没有可显示的术语）")

    def _clear_selection(self) -> None:
        self.current = None
        self._fill(self.source_text, "")
        self._fill(self.target_text, "")
        self._fill(self.note_text, "")
        self.target_entry.delete(0, "end")
        self.state_label.configure(text="")

    def _on_select(self, _event=None) -> None:
        selection = self.listbox.curselection()
        if not selection or not self.visible or selection[0] >= len(self.visible):
            self._clear_selection()
            return
        self.current = self.visible[selection[0]]
        row = self.current
        self._fill(self.source_text, row["source"])
        self._fill(self.target_text, (row.get("target") or "").strip() or "（待定）")
        self._fill(self.note_text, (row.get("note") or "").strip() or "（无备注）")
        # 输入框预填当前译名：改名只改末尾几个字，采用只需确认或微调
        self.target_entry.delete(0, "end")
        self.target_entry.insert(0, (row.get("target") or "").strip())
        self.state_label.configure(
            text=f"状态：{self._STATE_LABELS.get(row['state'], row['state'])}"
        )

    def _after_action(self, message: str) -> None:
        """裁决动作之后：刷新清单、尽量保住原选中、状态栏与日志出声。"""
        source = self.current["source"] if self.current else ""
        self._refresh()
        if source:
            for index, row in enumerate(self.visible):
                if row["source"] == source:
                    self.listbox.selection_set(index)
                    self._on_select()
                    break
            else:
                # 该条目从当前视图里消失了（如拒绝后勾着「只看未决」）
                self._clear_selection()
        else:
            self._clear_selection()
        self._set_status(message)

    def _adopt(self) -> None:
        if not self.current:
            return
        target = self.target_entry.get().strip()
        if not target:
            messagebox.showwarning(
                "译名是空的",
                "请先在上方输入框填写译名，再点「采用」。",
                parent=self.window,
            )
            return
        source = self.current["source"]
        # 已有备注（如存疑原因）保留下来，采用动作本身不抹历史
        note = (self.current.get("note") or "").strip() or "界面采用"
        self.termbase.adopt(source, target, note=note)
        self._after_action(f"已采用：{source} → {target}")

    def _rename(self) -> None:
        if not self.current:
            return
        target = self.target_entry.get().strip()
        if not target:
            messagebox.showwarning(
                "新译名是空的",
                "请先在上方输入框填写新译名，再点「改名」。",
                parent=self.window,
            )
            return
        source = self.current["source"]
        self.termbase.rename(source, target)
        self._after_action(
            f"已改名：{source} → {target}（术语指纹已变，存量译文缓存会随之重译）"
        )

    def _reject(self) -> None:
        if not self.current:
            return
        # 拒绝原因走同一个输入框：用户多数时候只想标记「这不是术语」，
        # 不值得为它单开一个对话框；想写原因就写，不写用默认值。
        reason = self.target_entry.get().strip() or "界面拒绝"
        source = self.current["source"]
        self.termbase.reject(source, reason)
        self._after_action(f"已拒绝：{source}（{reason}）")

    def _refresh_memory_label(self) -> None:
        """翻译记忆分区的条数。

        条数不是装饰：用户点「导出 TMX」之前要知道导出的是不是空表，
        点完回来要知道有没有真的灌进去。写在这里而不是两个按钮里各写一
        遍——一处更新，两处都对。
        """
        try:
            count = self.store.count_tm()
        except Exception:
            count = 0
        if count:
            self.memory_label.configure(
                text=f"共 {count} 条记忆（翻译与译后编辑自动积累）"
            )
        else:
            self.memory_label.configure(text="还没有记忆，可从 TMX 导入")

    def _import_tmx(self) -> None:
        from core.tmx import import_segments, parse_tmx

        path = filedialog.askopenfilename(
            title="选择要导入的 TMX 文件",
            filetypes=[("TMX 翻译记忆", "*.tmx"), ("所有文件", "*.*")],
            parent=self.window,
        )
        if not path:
            return
        try:
            segments = parse_tmx(path)
        except ValueError as exc:
            messagebox.showerror("导入失败", str(exc), parent=self.window)
            return
        result = import_segments(self.memory, segments, log=self._log)
        self._refresh_memory_label()
        summary = (
            f"TMX 导入完成：新增 {result['added']} 条 · "
            f"冲突保留 {result['kept']} 条 · "
            f"跳过 {result['skipped']} 条 · "
            f"无中文译文 {result['unpaired']} 条"
        )
        self._set_status(summary)
        messagebox.showinfo(
            "TMX 导入",
            f"{summary}\n（已存在原文一律保留库内译文）",
            parent=self.window,
        )

    def _export_tmx(self) -> None:
        """导出翻译记忆为 TMX。

        比 TBX 多一道确认：TMX 里写的是**原文句子**（明文 XML 落盘），属于
        保密红线行为，所以要让用户当场确认一次；保密模式直接不给出这个口子。
        理由完整版见 ``_run_tmx_export`` 与 ``core/tmx.py`` 的「保密提醒」。
        """
        from core.tmx import export_segments, generate_tmx

        if OFFLINE:
            self._set_status(
                "保密模式不导出翻译记忆：TMX 会把原文句子写成明文 XML 落盘。"
            )
            messagebox.showerror(
                "保密模式不支持导出",
                "TMX 导出会把原文句子写成明文 XML 文件，保密模式不允许。\n"
                "需要导出请退出保密模式再操作。",
                parent=self.window,
            )
            return
        segments = export_segments(self.store)
        if not segments:
            self._set_status("翻译记忆还是空的，没有可导出的内容。")
            messagebox.showinfo(
                "TMX 导出",
                "翻译记忆还是空的：翻译（或译后编辑）之后会自动积累。",
                parent=self.window,
            )
            return
        ok = messagebox.askyesno(
            "导出翻译记忆",
            f"将把 {len(segments)} 条记忆导出为 TMX。\n\n"
            "注意：TMX 文件里包含**原文句子**的明文，拿到文件的人能看到原文。\n"
            "确认导出吗？",
            parent=self.window,
        )
        if not ok:
            return
        path = filedialog.asksaveasfilename(
            title="导出翻译记忆为 TMX",
            defaultextension=".tmx",
            filetypes=[("TMX 翻译记忆", "*.tmx")],
            parent=self.window,
        )
        if not path:
            return
        target = Path(path)
        try:
            if str(target.parent) not in ("", "."):
                target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(generate_tmx(segments), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror(
                "导出失败", f"写不进 {path}：{exc}", parent=self.window
            )
            return
        summary = f"TMX 导出完成：{len(segments)} 条记忆 → {path}"
        self._set_status(summary)
        messagebox.showinfo("TMX 导出", summary, parent=self.window)

    def _import_tbx(self) -> None:
        from core.tbx import import_entries, parse_tbx

        path = filedialog.askopenfilename(
            title="选择要导入的 TBX 文件",
            filetypes=[("TBX 术语库", "*.tbx"), ("所有文件", "*.*")],
            parent=self.window,
        )
        if not path:
            return
        try:
            entries = parse_tbx(path)
        except ValueError as exc:
            # 解析层给的就是人话错误（不是有效的 TBX：……），直接转述
            messagebox.showerror("导入失败", str(exc), parent=self.window)
            return
        result = import_entries(self.termbase, entries, log=self._log)
        self._refresh()
        summary = (
            f"TBX 导入完成：新增 {result['added']} 条 · "
            f"冲突保留 {result['kept']} 条 · 跳过 {result['skipped']} 条"
        )
        self._set_status(summary)
        messagebox.showinfo("TBX 导入", summary, parent=self.window)

    def _export_tbx(self) -> None:
        from core.tbx import export_entries, generate_tbx

        path = filedialog.asksaveasfilename(
            title="导出术语库为 TBX",
            defaultextension=".tbx",
            filetypes=[("TBX 术语库", "*.tbx")],
            parent=self.window,
        )
        if not path:
            return
        entries = export_entries(self.store)
        xml = generate_tbx(entries)
        target = Path(path)
        try:
            if str(target.parent) not in ("", "."):
                target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(xml, encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("导出失败", f"写不进 {path}：{exc}", parent=self.window)
            return
        summary = f"TBX 导出完成：{len(entries)} 条已采用术语 → {path}"
        self._set_status(summary)
        messagebox.showinfo("TBX 导出", f"{summary}\n（只导出已采用条款）", parent=self.window)


#: 命令行批量（--convert）与拖文件夹时收的文件类型。脚本格式也走同一条路，
#: 由 _translate_one / translate_script 按扩展名分派。
CONVERT_SUFFIXES = ALL_SUFFIXES

#: 命令行开关。--layout=side|zh|inplace 选 PDF 成品形态（默认 side 左右对照；
#: zh 仅中文，页面尺寸与源一致；inplace 原位重排——在原件上直接替换文字，
#: 页面尺寸/链接/书签/表格底纹原样保留，只处理文字版页面）；--no-cache 不写本地缓存库；
#: --password=xxx 打开加密 PDF（批量时所有 PDF 共用这一个密码）；
#: --style=formal|concise 选译文风格档位；--to=zh|en 选目标语言（v1.5.8，
#: 默认 zh；英文模式下已是英文的段落自动跳过、术语表反向使用）；--local-llm 用本地大模型档
#: （需本机 Ollama，不可用时仅回退本机离线小模型），--model=xxx 指定模型名；
#: --no-quality 关掉质量引擎；--terms 查看术语库（未决术语池）。
#: --paid=deepl 选中付费档（DeepL，需自备 key，与 --offline 互斥）；
#: --yes-i-know 确认已知悉付费档涉密警示（文档原文会发送至 DeepL）。
#: --postedit=译文文件 对成品做译后编辑（v1.5.4）：--list 打印疑点清单；
#: 不加 --list 则把清单里手改过的译文写回成品；--retranslate=id,id 用当前
#: 命令行档位的引擎重摇指定段落后一起写回；--alternatives=id,id 给指定段
#: 各取一条备选译文（v1.6.2，绕开缓存重摇，只进清单不进成品）。
#: --tbx-import=文件.tbx 把外部 TBX 术语库导进本地术语库（v1.5.5，纯本地
#: 文件操作，保密模式同样可用；已存在条目不覆盖用户既有裁决）；
#: --tbx-export=路径.tbx 把术语库写成 TBX（只导已采用条款）。
#: --tmx-import=文件.tmx 把外部 TMX 灌进本地翻译记忆（导入不受限：这是往
#: 里灌，不外发；去重规则与 TBX 一致——不覆盖既有译文）；
#: --tmx-export=路径.tmx 把翻译记忆写成 TMX（默认全导）。**导出会把原文
#: 句子写成明文 XML 落盘**，所以要独立确认 --yes-i-know-tmx（不复用
#: --yes-i-know：那是确认内容出境给 DeepL，本项是确认内容落盘），保密模式
#: （--offline）下一律拒绝导出。
#: --no-translate=default|+latex,+ident|off 免翻区保护：URL / 邮箱 /
#: 版本号 / 编号 / 代码这类片段翻译前换成占位符、译完原样换回（默认五条
#: 规则；+latex / +ident 追加启发式规则；off 全关）。
#: --no-term-inflection（v1.6.4）：术语只锁表里的原形。默认认词形变化——
#: 术语表写 transformer = 变压器，正文里的 transformers 也一起锁；当某条术语的
#: 变形撞上另一个词、想退回旧行为时用这个开关。
CLI_LAYOUT = "side"
#: --pages=1-3,5（v1.6.8 只有 PDF，v1.7.0 起 PPT/EPUB 也吃）：只翻译选中的那几页，
#: 页码从 1 数，与阅读器显示一致；`-3` 是前 3 页，`8-` 是第 8 页到最后一页；留空 = 整份文档。
#: 同一个数字在三种容器里数的是不同的东西：PDF 的页、PPT 的幻灯片张、EPUB 的 spine 章节，
#: 上界各按自己那份文件的数量校验，所以报错的量词由各自的 translate_* 按 Unit 念。
#: PDF 左右对照/仅中文是重建页面，成品里就只有这些页；PDF 原位重排与 PPT/EPUB 都不重建，
#: 未选中的原样留在成品里——这条口径日志会各自说清。Word/Excel/字幕/图片不吃范围。
CLI_PAGES = ""
#: --segment-rules=路径（v1.8.0，差距清单 C5 剩下的「规则表可编辑」）：指定用哪一张分段规则表。
#: 不写这个开关也会读工具目录下的 分段规则.json——改完即生效，与「阈值四旋钮」同口径**只在
#: 命令行/文件层调，界面不加输入框**（会编辑规则的人本来就编辑文件）。两处都没有 = 内置规则，
#: 一声不吭。CLI_SEGMENT_NOTE 是「生效了什么」那句话，由 --convert 与起界面两条入口各自回显。
CLI_SEGMENT_RULES = ""
CLI_DUMP_SEGMENT_RULES = False
CLI_SEGMENT_NOTE = ""
CLI_NO_CACHE = False
CLI_STYLE = STANDARD
CLI_PASSWORD: str | None = None
CLI_ENGINE = "cloud"
CLI_MODEL: str | None = None
CLI_QUALITY = True
CLI_PAID = ""
CLI_YES_I_KNOW = False
#: --custom：用「配置自托管…」里填的那台服务（vLLM / llama-server / 另一台机器
#: 上的 Ollama，都要 OpenAI 兼容接口）。这一档的地址是用户自己填的，所以闸门
#: 按地址落在哪决定：本机/内网不拦，公网要 --yes-i-know-custom。
CLI_CUSTOM = False
#: --custom-url=：本次运行临时指定地址，不读也不写配置文件（试别的机器用）。
CLI_CUSTOM_URL = ""
CLI_YES_I_KNOW_CUSTOM = False
CLI_POSTEDIT: str | None = None
CLI_POSTEDIT_LIST = False
CLI_RETRANSLATE = ""
#: --alternatives=id,id：给指定段各摇一条备选译文（v1.6.2，E4），只存清单、
#: 不写成品——和界面的「要备选译文」是同一件事。
CLI_ALTERNATIVES = ""
CLI_TBX_IMPORT: str | None = None
CLI_TBX_EXPORT: str | None = None
CLI_TMX_IMPORT: str | None = None
CLI_TMX_EXPORT: str | None = None
#: --yes-i-know-tmx：确认已知悉「TMX 导出会把原文句子写成明文 XML 落盘」。
#: 刻意**不复用** --yes-i-know：后者确认的是「内容出境给 DeepL」，本项确认
#: 的是「内容落盘」，共用标记会让为付费档点过确认的用户在导出时被静默放行。
CLI_TMX_ACK = False
#: --no-translate=default|+latex,+ident|off：免翻区规则（见 core/no_translate.py）。
CLI_NO_TRANSLATE = "default"
#: --jobs=N：并发度。**默认 1**——不写这个开关时行为与串行逐字节等价。
#: 免费云端档走的是别人的免费接口，默认并发等于默认加速触发限流与封禁，
#: 所以只有用户显式要求才放开，且按档位夹上限（见 core/concurrency.py）。
CLI_JOBS = 1
#: --to=zh|en：目标语言（v1.5.8）。默认 zh，即 v1.5.7 的既有行为。
CLI_TARGET = "zh"
#: 请求尺寸与退避旋钮（v1.6.9，差距清单 C5+E2 的「阈值可配置」形态）。
#: 四个都是 **None = 没写**，各引擎按自己的硬限走；写了也**只允许收紧**，
#: 写大了夹回硬限并出声（口径与判据都在 core/tuning.py）。界面刻意不给面板：
#: 会调请求尺寸的人在写批处理，桌面双击用户不需要界面上多四个输入框——
#: 与 --jobs= 完全同一套做法（界面复用命令行传进来的值）。
CLI_BATCH_ITEMS: int | None = None
CLI_BATCH_CHARS: int | None = None
CLI_CHUNK_CHARS: int | None = None
CLI_RETRY_DELAYS: tuple[float, ...] | None = None
#: --tm-threshold=0.95：翻译记忆「近似复用」的相似度门槛（v1.9.10，差距清单 C3 最后一条）。
#: None = 没写，按 core.tm.DEFAULT_THRESHOLD 走。**只许往上调**：调低等于让更不像的
#: 旧译文顶替新内容，比不翻更危险（口径见 core/tuning.py）。门槛挂在记忆实例上而不是
#: 各引擎上——一条记忆四档共用，门槛也只能有一处。保密模式不接记忆，这一项在那边无意义。
CLI_TM_THRESHOLD: float | None = None


def _cli_tuning() -> dict:
    """把四个旋钮摊成构造参数，GUI / CLI / 自检三条入口共用一份。

    放在这里而不是逐个写四行，是为了让「新增一个旋钮只改一处」成立——
    三处 EngineParams 与三处直接构造点都要跟着改，漏一处的症状是
    「命令行写了、界面里没生效」，与 v1.6.3 之前档位开关散在三处同款。
    """
    return {
        "batch_items": CLI_BATCH_ITEMS,
        "batch_chars": CLI_BATCH_CHARS,
        "chunk_chars": CLI_CHUNK_CHARS,
        "retry_delays": CLI_RETRY_DELAYS,
    }


def _parse_jobs_argument(raw: str) -> int:
    """解析 ``--jobs=N``。认不出来就退回 1 并**说出声**——用户写了一个
    不存在的并发度却「什么都没发生」是最难排查的一类失效。"""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        print(f"--jobs 需要一个整数（收到「{raw}」），已按 1 执行。")
        return 1
    if value < 1:
        print(f"--jobs 不能小于 1（收到 {value}），已按 1 执行。")
        return 1
    return value


def _paid_gate() -> int | None:
    """``--paid=deepl`` 的三道闸门。返回 None 表示放行；否则返回退出码。

    三道闸门对应三类「不可行动」的失败，都必须在这里挡住、说人话：

    1. **与保密模式硬互斥**：文档出境是保密模式的红线，没有「提醒后放行」
       这个选项——同时给 ``--offline`` 与 ``--paid=deepl`` 直接报错；
    2. **没配 key**：打印去哪申请（deepl.com/pro-api）、填哪个文件、
       给一段 json 样例，退出码非零；
    3. **未确认涉密警示**：付费档会把文档原文发送至 DeepL（境外服务器），
       要求显式 ``--yes-i-know`` 才继续；确认过就落标记
       （``save_acknowledgement``，v1.5.1 的 GUI 确认对话框复用）。
    """
    from core.api_keys import (
        is_acknowledged,
        keys_file,
        load_key,
        looks_like_key,
        save_acknowledgement,
    )

    if not CLI_PAID:
        return None
    if CLI_PAID != "deepl":
        print(f"未知的付费引擎：--paid={CLI_PAID}。当前只支持 --paid=deepl（DeepL）。")
        return 5
    if OFFLINE:
        print("保密模式不能用付费 API：文档会发送到 DeepL 服务器。请二选一：")
        print("  · 涉密文档：用「启动翻译工具-保密模式.bat」（--offline，全程本机）")
        print("  · 要付费质量：去掉 --offline，用 --paid=deepl")
        return 2
    key_config = load_key("deepl")
    key = (key_config or {}).get("key", "")
    if not key_config or not looks_like_key(key):
        print("未配置可用的 DeepL API key。付费档需要你自己申请 key：")
        print("  1. 打开 https://www.deepl.com/pro-api 注册 DeepL API")
        print("     （Free 版每月 50 万字符免费额度，够日常文档用）；")
        print(f"  2. 在程序目录新建 {keys_file()}（不会进 git），内容例如：")
        print(
            '     {"deepl": {"key": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx:fx",'
            ' "monthly_char_budget": 500000}}'
        )
        print("  3. 重新运行本命令。key 有两种形态：Pro 是一串 UUID；")
        print("     Free 在 UUID 后面加 :fx 结尾。monthly_char_budget 可省略（0 = 不设上限）。")
        return 3
    if not is_acknowledged("deepl"):
        if not CLI_YES_I_KNOW:
            print("涉密警示：付费档会把文档原文发送至 DeepL（境外服务器）。")
            print("涉密文档请使用保密模式（启动翻译工具-保密模式.bat），不要用付费档。")
            print("确认已知悉上述风险，请加 --yes-i-know 重新运行。")
            return 4
        save_acknowledgement("deepl")
    return None


def _custom_gate() -> int | None:
    """``--custom`` 的三道闸门。返回 None 表示放行；否则返回退出码。

    与 ``_paid_gate`` 同构，但判断依据不是「有没有 key」而是「用户填的地址
    落在哪儿」——自托管服务可能在本机、在单位内网、也可能在公网上，三者的
    合规后果完全不同，所以不能照抄付费档那句「原文出境」的固定文案：

    1. **与保密模式硬互斥**（退出码 2）：地址是用户填的任意网络端点，
       没法保证它不出本机，而 ``--offline`` 的承诺是只用 ``models/``；
    2. **没配置或地址不能用**（3）：没填、没启用、写不出主机名，或者
       **公网 + 明文 HTTP**（文档原文会在互联网上裸奔，这条没有放行选项）；
    3. **公网未确认**（4）：要求显式 ``--yes-i-know-custom``，确认过就落
       ``api_keys.local.json`` 的 ``custom.acknowledged``。本机与内网地址
       不问——数据不出边界，每次都问只会让人乱按。

    只给 ``--custom-url=`` 不给 ``--custom`` 也算选了这一档（与 ``--paid=``
    自带档位同理），但该地址只用于本次运行，不写进配置文件。
    """
    global CLI_CUSTOM
    from core import custom_engine
    from core.api_keys import is_acknowledged, save_acknowledgement

    if not (CLI_CUSTOM or CLI_CUSTOM_URL):
        return None
    if OFFLINE:
        print("保密模式不能用自托管服务：那是一台网络端点，无法保证文档不出本机。请二选一：")
        print("  · 涉密文档：用「启动翻译工具-保密模式.bat」（--offline，全程本机）")
        print("  · 要调单位的服务：去掉 --offline，加 --custom")
        return 2
    config = custom_engine.load_config()
    url = CLI_CUSTOM_URL or config.base_url
    if not url:
        print("还没配置自托管服务，--custom 没有可用地址。两种配法：")
        print("  1. 界面点「配置自托管…」填服务地址；")
        print(f"  2. 直接编辑（或新建）{custom_engine.config_path()}，内容例如：")
        print(
            '     {"enabled": true, "name": "单位GPU服务器",'
            ' "base_url": "http://192.168.1.20:8000/v1", "model": ""}'
        )
        print("     服务要提供 OpenAI 兼容的 /v1/chat/completions"
              "（vLLM、llama.cpp 的 llama-server、LM Studio、另一台机器上的 Ollama 都行）。")
        return 3
    try:
        url = custom_engine.require_usable(url)
    except TranslationError as exc:
        print(str(exc))
        return 3
    if custom_engine.endpoint_scope(url) == "public":
        if not is_acknowledged(custom_engine.CUSTOM_PROVIDER):
            if not CLI_YES_I_KNOW_CUSTOM:
                print(f"涉密警示：这个自托管地址在公网，文档原文会发往：{url}")
                print("涉密文档请使用保密模式（启动翻译工具-保密模式.bat），全程只在本机处理。")
                print("确认已知悉上述风险，请加 --yes-i-know-custom 重新运行。")
                return 4
            save_acknowledgement(custom_engine.CUSTOM_PROVIDER)
    CLI_CUSTOM = True
    return None


def _collect_convert_files(arguments: list[str]) -> list[str]:
    """接受文件、文件夹或两者的混合，只收支持的文档与脚本格式。"""
    files: list[str] = []
    for item in arguments:
        path = Path(item)
        if path.is_dir():
            files.extend(
                str(child)
                for child in sorted(path.iterdir())
                if child.is_file() and child.suffix.lower() in CONVERT_SUFFIXES
            )
        elif path.is_file() and path.suffix.lower() in CONVERT_SUFFIXES:
            files.append(str(path))
    return files


def _notify(text: str) -> None:
    root = tk.Tk()
    root.withdraw()
    messagebox.showinfo("批量翻译", text)
    root.destroy()


def _build_cli_translator(log: Callable[[str], None]):
    """按命令行开关构造翻译器（``_convert`` 与 ``--postedit --retranslate`` 共用）。

    档位由 ``core.engine_registry.resolve_cli`` 从 ``--offline`` / ``--paid`` /
    ``--local-llm`` / ``--custom`` 四个开关里合成，构造走 ``build``——与界面
    ``_make_translator`` 用的是同一张表，所以「命令行说一档、实际跑另一档」
    这种事故没有第二条路可以产生。付费档 key 与自托管档闸门的兜底也在表里：
    ``main()`` 的 ``_paid_gate`` / ``_custom_gate`` 已经拦过一次，但
    ``_convert`` / ``_run_postedit`` 也可能被直接调用。
    """
    key = engine_registry.resolve_cli(
        offline=OFFLINE,
        paid=CLI_PAID,
        engine=CLI_ENGINE,
        custom=CLI_CUSTOM,
        notice=log,
    )
    cache = None if (CLI_NO_CACHE or OFFLINE) else str(CACHE_DIR / "translations.sqlite")
    return engine_registry.build(
        key,
        engine_registry.EngineParams(
            log=log,
            style=CLI_STYLE,
            cache_path=cache,
            # 保密模式零落盘：翻译记忆要写 SQLite，所以不注入（这句话由登记表
            # 里的保密档负责念出来）。
            tm=None if OFFLINE else _shared_tm(log),
            jobs=CLI_JOBS,
            quality=CLI_QUALITY,
            model=CLI_MODEL,
            base_url=CLI_CUSTOM_URL or None,
            **_cli_tuning(),
        ),
    )


def _convert(arguments: list[str]) -> int:
    """不进界面直接出对照成品，供「拖到批处理图标上」这类用法调用。"""
    files = _collect_convert_files(arguments)
    # 第一行的「模式」文字问登记表：加一档不用回来改这行 if 链。
    try:
        convert_tier = engine_registry.resolve_cli(
            offline=OFFLINE, paid=CLI_PAID, engine=CLI_ENGINE, custom=CLI_CUSTOM
        )
    except TranslationError as exc:
        print(str(exc))
        return 2
    mode_text = engine_registry.SHORT_MODES[convert_tier]
    lines: list[str] = [
        f"版本 v{VERSION}",
        f"{mode_text}模式，待处理 {len(files)} 个文件",
        f"目标语言：{direction.label()}",
    ]
    if CLI_LAYOUT == "zh":
        lines.append("PDF 成品形态：仅中文")
    elif CLI_LAYOUT == "inplace":
        lines.append("PDF 成品形态：原位重排")
    if CLI_PAGES:
        lines.append(f"文档范围：{CLI_PAGES}（{page_range.SCOPE_NOTE}）")
        if any(not path.lower().endswith(page_range.RANGE_SUFFIXES) for path in files):
            lines.append("Word、Excel、字幕、图片没有可挑的序号界，整份翻译")
    if CLI_SEGMENT_NOTE:
        lines.append(CLI_SEGMENT_NOTE)
    if CLI_NO_CACHE and not OFFLINE:
        lines.append("本次不写本地缓存库")
    succeeded = 0
    try:
        translator = _build_cli_translator(lines.append)
        for path in files:
            name = os.path.basename(path)
            # 用量流水的 file 字段记当前文件名（DeepLTranslator 才带这个属性）
            if hasattr(translator, "current_file"):
                translator.current_file = name
            try:
                lowered = path.lower()
                if lowered.endswith(".pdf"):
                    output = translate_pdf(
                        path,
                        translator,
                        log=lines.append,
                        layout=CLI_LAYOUT,
                        password=CLI_PASSWORD,
                        pages=CLI_PAGES or None,
                    )
                elif lowered.endswith(".pptx"):
                    from core.ppt_translator import translate_ppt

                    output = translate_ppt(
                        path, translator, log=lines.append, pages=CLI_PAGES or None
                    )
                elif lowered.endswith(".epub"):
                    from core.epub_translator import translate_epub

                    output = translate_epub(
                        path, translator, log=lines.append, pages=CLI_PAGES or None
                    )
                elif lowered.endswith(SCRIPT_SUFFIXES):
                    from core.script_translator import translate_script

                    output = translate_script(path, translator, log=lines.append)
                elif lowered.endswith(IMAGE_SUFFIXES):
                    from core.image_translator import translate_image

                    output = translate_image(path, translator, log=lines.append)
                else:
                    output = translate_word(path, translator, log=lines.append)
                succeeded += 1
                lines.append(f"已生成：{shown_path(output)}")
            except Exception as exc:
                lines.append(f"失败：{name} → {type(exc).__name__}: {exc}")
    finally:
        report = "\n".join(lines)
        print(report)
        if OFFLINE:
            # 保密模式不往磁盘写运行记录；打包版没有控制台，只能弹窗回报
            if sys.stdout is None:
                _notify("\n".join(lines[:1] + lines[-1:]))
        else:
            (APP_DIR / "转换日志.txt").write_text(report, encoding="utf-8")
    return 0 if succeeded == len(files) else 1


def _run_postedit(target: str) -> int:
    """译后编辑闭环（v1.5.4）：对成品应用清单里的手改译文，或重摇段落后写回。

    三种用法：

    - ``--postedit=成品 --list``：只打印疑点清单（疑点优先、待写回标星），
      不动任何文件；
    - ``--postedit=成品``：把清单里 ``translation`` 被改过的段写回成品
      （直接编辑成品旁边那份 JSON 即可，批量工作流不用开界面）；
    - ``--postedit=成品 --retranslate=w3,b5``：先用当前命令行档位的引擎
      重摇这些段（覆盖手改值），再一起写回。
    - ``--postedit=成品 --alternatives=w3``：给这些段各摇一条备选译文
      （v1.6.2），**只存进清单的 candidates 字段、不写成品**——把想用的
      那条填进 translation 再跑一次 --postedit 才落盘。
    """
    from core.postedit import (
        apply_edits,
        dump_edit_list,
        edit_list_path,
        format_edit_list,
        load_edit_list,
        retranslate_segment,
    )

    if not os.path.isfile(target):
        print(f"文件不存在：{target}")
        return 1
    list_path = edit_list_path(target)
    if not os.path.isfile(list_path):
        print(
            f"没有找到译后编辑清单：{shown_path(list_path)}\n"
            "请先翻译生成成品——翻译结束时清单会自动落在成品旁边。"
        )
        return 1
    try:
        items = load_edit_list(list_path)
    except TranslationError as exc:
        print(str(exc))
        return 1

    if CLI_POSTEDIT_LIST:
        print(f"译后编辑清单：{shown_path(list_path)}")
        print(format_edit_list(items))
        pending = [item.id for item in items if item.pending]
        if pending:
            print(f"\n有待写回的修改：{'、'.join(pending)}")
        return 0

    lines: list[str] = []

    def _parse_ids(raw: str) -> list[str] | None:
        wanted = [part.strip() for part in raw.split(",") if part.strip()]
        known = {item.id for item in items}
        missing = [part for part in wanted if part not in known]
        if missing:
            print(f"清单里没有这些段：{'、'.join(missing)}")
            print(f"可用段号：{'、'.join(sorted(known)) or '（空）'}")
            return None
        return wanted

    # 两个开关共用同一个翻译器：构造要探测本机模型，别为一次调用建两回。
    translator = (
        _build_cli_translator(lines.append)
        if (CLI_RETRANSLATE or CLI_ALTERNATIVES)
        else None
    )
    if CLI_RETRANSLATE:
        wanted = _parse_ids(CLI_RETRANSLATE)
        if wanted is None:
            return 1
        for item in items:
            if item.id not in wanted:
                continue
            try:
                item.translation = retranslate_segment(translator, item.source)
                print(f"已重译 {item.location}：{item.translation}")
            except TranslationError as exc:
                print(f"重译失败 {item.location}：{exc}")

    if CLI_ALTERNATIVES:
        from core.alternatives import add_candidate, generate_alternative

        wanted = _parse_ids(CLI_ALTERNATIVES)
        if wanted is None:
            return 1
        for item in items:
            if item.id not in wanted:
                continue
            try:
                fresh = generate_alternative(translator, item)
            except TranslationError as exc:
                print(f"没拿到备选 {item.location}：{exc}")
                continue
            if add_candidate(item, fresh):
                print(f"{item.location} 备选：{fresh}")
            else:
                print(f"{item.location} 的这条备选与已有译文重复，未加入")
        # 候选只进清单，不进成品：挑哪条由人决定（见 core/alternatives 模块说明）。
        dump_edit_list(target, items)
        print(f"备选已存进清单：{os.path.basename(list_path)}")

    pending = [item for item in items if item.pending]
    if not pending:
        print("清单里没有待写回的修改（译文与上次写回值一致）。")
        print(
            "可加 --retranslate=段号 让引擎重摇、--alternatives=段号 取备选，"
            "或直接编辑 "
            f"{os.path.basename(list_path)} 里的 translation 字段。"
        )
        return 0

    # tm 非空时，写回成功的人工译文会以 origin="human" 进入记忆库
    # （「确认一次、全文生效」）；保密模式下 _shared_tm 返回 None，不写不碰。
    result = apply_edits(target, pending, log=lines.append, tm=_shared_tm(lines.append))
    if result.applied:
        for item in pending:
            if item.id in result.applied:
                item.applied = item.translation
        dump_edit_list(target, items)
    print()
    summary = f"已写回 {len(result.applied)} 段"
    if result.skipped:
        summary += f"，跳过 {len(result.skipped)} 段"
    print(summary)
    for note in result.notes:
        print(f"  注意：{note}")
    if result.applied:
        print(f"成品已更新：{target}")
    return 0 if result.applied else 1


#: 进程级共享缓存的两个状态位（见 ``_shared_tm``）："没建过"与"建了但失败"
#: 必须能区分，否则每次都会重试一遍构造。
_SHARED_TM = None
_SHARED_TM_BUILT = False


def _shared_tm(log: Callable[[str], None] | None = None):
    """四档共用的翻译记忆实例。保密模式返回 None（见下）。

    为什么是进程级共享而不是每个翻译器各建一个：四档的**命中口径必须一致**
    （同一句原文在任意档位得到同一份记忆答复），多个对象各装一份全表也白白
    翻几倍内存。

    保密模式（``--offline``）返回 None，而不是"读不到就算了"：记忆读写落在
    SQLite 里，那是"敏感内容不落盘"红线所在。翻译器拿到 None 会整段跳过
    （见各档位的 ``if self.tm is not None``）， TMX 是另一条出口、单独闸门。
    """
    global _SHARED_TM, _SHARED_TM_BUILT
    if OFFLINE:
        return None
    if _SHARED_TM_BUILT:
        return _SHARED_TM
    _SHARED_TM_BUILT = True
    try:
        from core.state import StateStore
        from core.tm import DEFAULT_THRESHOLD, TranslationMemory

        _SHARED_TM = TranslationMemory(
            StateStore(str(CACHE_DIR / "translations.sqlite")),
            log=log or (lambda message: None),
            threshold=CLI_TM_THRESHOLD or DEFAULT_THRESHOLD,
        )
    except Exception as exc:  # 记忆是增强，不可用不能拖垮翻译
        _SHARED_TM = None
        if log is not None:
            log(f"翻译记忆不可用（{type(exc).__name__}），本次不复用历史译文。")
    return _SHARED_TM


def glossary_intro() -> str | None:
    from core.glossary import intro

    try:
        return intro()
    except Exception:
        return None


def _run_tbx_import(path: str) -> int:
    """``--tbx-import=文件.tbx``：把外部 TBX 术语库灌进本地术语库。

    纯本地文件操作：不构造翻译器、不联网，保密模式（``--offline``）同样
    可用，因此不走 ``_paid_gate``。去重规则与 GUI 共用
    ``core/tbx.import_entries``（全项目只此一处实现）：已存在条目一律
    保留库内原值，外部 TBX 不覆盖用户既有裁决；冲突逐条出声，摘要给
    「新增 / 冲突保留 / 跳过」。解析失败（不是有效的 TBX）打印人话错误、
    返回非零退出码。
    """
    from core.state import StateStore
    from core.termbase import Termbase
    from core.tbx import import_entries, parse_tbx

    if not os.path.isfile(path):
        print(f"文件不存在：{path}")
        return 1
    try:
        entries = parse_tbx(path)
    except ValueError as exc:
        print(str(exc))
        return 1

    store = StateStore(str(CACHE_DIR / "translations.sqlite"))
    termbase = Termbase(store)
    result = import_entries(termbase, entries, log=print)
    print(
        f"TBX 导入完成：新增 {result['added']} 条 · "
        f"冲突保留 {result['kept']} 条 · 跳过 {result['skipped']} 条"
    )
    if result["kept"]:
        print("（已存在条目一律保留库内原值，外部 TBX 不覆盖用户既有裁决）")
    return 0


def _run_tbx_export(path: str) -> int:
    """``--tbx-export=路径.tbx``：把术语库写成 TBX 文件。

    只导「已采用」条款——disputed / candidate 是未决状态，导出去等于把
    半成品决策推给下游（完整理由见 ``core/tbx.export_entries`` 的
    docstring）。空术语库也写一个结构合法的空文件，不让调用方猜。
    """
    from core.state import StateStore
    from core.tbx import export_entries, generate_tbx

    store = StateStore(str(CACHE_DIR / "translations.sqlite"))
    entries = export_entries(store)
    xml = generate_tbx(entries)
    target = Path(path)
    try:
        if str(target.parent) not in ("", "."):
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(xml, encoding="utf-8")
    except OSError as exc:
        print(f"写入失败：{path}（{exc}）")
        return 1
    print(f"TBX 导出完成：{len(entries)} 条已采用术语 → {path}")
    if not entries:
        print("术语库还没有已采用术语，导出的是一个空术语库文件（结构合法）。")
    return 0


def _run_tmx_import(path: str) -> int:
    """``--tmx-import=文件.tmx``：把外部 TMX 灌进本地翻译记忆。

    **导入不受保密闸门约束**：这是往里灌（inbound），不是外发也不涉及
    「把用户的敏感内容写到别处」，所以保密模式下也放行。去重写规则与 GUI
    共用 ``core/tmx.import_segments``（那份实现刻意与 TBX 一字不差）：已存在
    原文一律保留库内译值，冲突逐条出声；**没配中文译文的原文计进 unpaired
    并逐条出声**——静默丢弃会让用户以为全部导进来了。

    解析失败（不是有效的 TMX）打印人话错误并返回非零退出码。
    """
    from core.state import StateStore
    from core.tm import TranslationMemory
    from core.tmx import import_segments, parse_tmx

    if not os.path.isfile(path):
        print(f"文件不存在：{path}")
        return 1
    try:
        segments = parse_tmx(path)
    except ValueError as exc:
        print(str(exc))
        return 1

    store = StateStore(str(CACHE_DIR / "translations.sqlite"))
    memory = TranslationMemory(store)
    result = import_segments(memory, segments, log=print)
    print(
        f"TMX 导入完成：新增 {result['added']} 条 · "
        f"冲突保留 {result['kept']} 条 · "
        f"跳过 {result['skipped']} 条 · "
        f"无中文译文 {result['unpaired']} 条"
    )
    if result["kept"]:
        print("（已存在原文一律保留库内译文，外部 TMX 不覆盖用户既有资产）")
    if result["unpaired"]:
        print("（这些原文在导入文件里没有配中文译文，未入库）")
    return 0


def _run_tmx_export(path: str) -> int:
    """``--tmx-export=路径.tmx``：把翻译记忆写成 TMX 文件。

    两道闸门，都不可跳过：

    1. **保密模式拒绝导出**——TMX 是把「原文句子」写成明文 XML 落盘，正是
       保密模式不许做的事。宁可让用户在非保密模式下多跑一次，也不能在
       保密模式里开这个口子；
    2. **独立确认 ``--yes-i-know-tmx``**——刻意不复用 ``--yes-i-know``：
       后者确认的是「内容出境给 DeepL」，本项确认的是「内容落盘」，共用标记
       会让为付费档点过确认的用户在 TMX 导出上被静默放行。

    默认全导（记忆的入库门槛本来就是"零问题遗留"），过滤口径的解释见
    ``core/tmx.export_segments``。空记忆库也写一个结构合法的空文件。
    """
    from core.state import StateStore
    from core.tmx import export_segments, generate_tmx

    if OFFLINE:
        print("保密模式不导出翻译记忆：TMX 会把原文句子写成明文 XML 落盘。")
        print("  涉密内容：保持 --offline 不导出（管线内的记忆也已全程不参与读写）")
        print("  需要导出：去掉 --offline，用 --tmx-export=路径.tmx --yes-i-know-tmx")
        return 2
    if not CLI_TMX_ACK:
        print("TMX 导出会把本机的原文句子写成明文 XML 文件（含原文全文）。")
        print(f"  确认已知悉请加开关重跑：--tmx-export={path} --yes-i-know-tmx")
        return 2

    store = StateStore(str(CACHE_DIR / "translations.sqlite"))
    segments = export_segments(store)
    xml = generate_tmx(segments)
    target = Path(path)
    try:
        if str(target.parent) not in ("", "."):
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(xml, encoding="utf-8")
    except OSError as exc:
        print(f"写入失败：{path}（{exc}）")
        return 1
    print(f"TMX 导出完成：{len(segments)} 条翻译记忆 → {path}")
    if not segments:
        print("翻译记忆还是空的，导出的是一个空记忆库文件（结构合法）。")
    return 0


def _list_terms() -> int:
    """``--terms``：查看术语库状态（已采用 / 存疑 / 候选）。

    人在回路的第一步是"知道哪里需要我裁决"。命令行工作流（Galgame 批量）
    里没有界面，这条命令把未决术语池直接打到 stdout。
    """
    from core.state import StateStore
    from core.termbase import Termbase

    store = StateStore(str(CACHE_DIR / "translations.sqlite"))
    termbase = Termbase(store)
    stats = termbase.stats()
    lines = [
        f"术语库：已采用 {stats.get('adopted', 0)} · "
        f"存疑 {stats.get('disputed', 0)} · 候选 {stats.get('candidate', 0)} · "
        f"已拒绝 {stats.get('rejected', 0)}",
    ]
    for state, label in (("disputed", "存疑"), ("candidate", "候选")):
        rows = termbase.store.terms_by_state(state)
        if not rows:
            continue
        lines.append(f"── {label}（{len(rows)}）──")
        for row in rows[:60]:
            target = row.get("target") or "（待定）"
            note = row.get("note") or ""
            suffix = f"  # {note}" if note else ""
            lines.append(f"  {row.get('source', '')} → {target}{suffix}")
    adopted = termbase.adopted_terms()
    if adopted:
        lines.append(f"── 已采用（{len(adopted)}，前 20 条）──")
        for source, target in adopted[:20]:
            lines.append(f"  {source} → {target}")
    text = "\n".join(lines)
    print(text)
    if not OFFLINE:
        try:
            (APP_DIR / "术语库状态.txt").write_text(text + "\n", encoding="utf-8")
        except OSError:
            pass
    return 0


def _selftest() -> int:
    """免安装版自检：不开界面，试译一句固定文本，结果写到程序目录的「自检结果.txt」。
    分发给同事后打不开或翻不了，先跑自检，把这份文件发回来就能定位。"""
    from core.heading_terms import USER_FILE

    # 模式文字与试译用的档位都问登记表：--offline / --paid / --local-llm /
    # --custom 四个开关先合成一个档位，组合冲突（如 --paid 配 --local-llm）
    # 在这里就说清楚，而不是让自检悄悄挑一个赢的。
    try:
        selftest_tier = engine_registry.resolve_cli(
            offline=OFFLINE, paid=CLI_PAID, engine=CLI_ENGINE, custom=CLI_CUSTOM
        )
    except TranslationError as exc:
        print(str(exc))
        return 2
    mode_text = engine_registry.mode_text(selftest_tier)
    lines = [
        f"版本: v{VERSION}",
        f"模式: {mode_text}",
        f"目录: {APP_DIR}",
        f"术语表: {'已找到' if USER_FILE.is_file() else '缺失'}",
        f"   └ {glossary_intro() or '无条目'}",
        "离线模型: "
        + ("已找到" if (APP_DIR / "models" / "opus-mt-en-zh-ct2" / "model.bin").is_file() else "缺失"),
    ]
    for module in (
        "pymupdf",
        "docx",
        "pptx",
        "openpyxl",
        "wordninja",
        "tkinterdnd2",
        "rapidocr_onnxruntime",
        "requests",
    ):
        try:
            __import__(module)
            lines.append(f"组件 {module}: 正常")
        except Exception as exc:
            lines.append(f"组件 {module}: 失败（{type(exc).__name__}）")
    # 付费档探测（联网模式）：有 key 才测，没 key 跳过并说明。
    paid_translator = None
    # 自托管档探测（联网模式）：配置里有地址才测，没配就一句「未配置」。
    custom_translator = None
    if not OFFLINE:
        # 本地大模型档探测：3 秒超时， Ollama 没装也只是"未启用"，不算故障
        try:
            from core.local_llm import probe_local_llm

            _engine, reason = probe_local_llm()
            lines.append(f"本地大模型: {reason}")
        except Exception as exc:
            lines.append(f"本地大模型: 探测失败（{type(exc).__name__}）")
        # 付费档探测：发一次 Hello 验证 key 与域名；自检不该因为用户
        # 没买付费档就报红，没 key 就跳过并说明去哪配。
        try:
            from core.api_keys import load_key

            key_config = load_key("deepl")
        except Exception:
            key_config = None
        if key_config and key_config.get("key"):
            try:
                from core.deepl import DeepLTranslator

                paid_translator = DeepLTranslator(
                    key_config, log=lines.append, style=CLI_STYLE
                )
                paid_translator._ensure_probed()
                lines.append("付费引擎 DeepL: 探测通过")
            except Exception as exc:
                paid_translator = None
                lines.append(
                    f"付费引擎 DeepL: 探测失败（{type(exc).__name__}: {exc}）"
                )
        else:
            lines.append(
                "付费引擎 DeepL: 未配置 key（跳过；申请与配置方法见 "
                "--paid=deepl 的引导文案）"
            )
        # 自托管档探测：配置里有地址才测。测的是 /v1/models（服务在不在、
        # 挂了哪些模型）+ 地址落在本机/内网/公网，后者直接决定要不要过闸门。
        try:
            from core import custom_engine

            custom_config = custom_engine.load_config()
            if custom_config.usable():
                models = custom_engine.discover_models(custom_config.base_url)
                scope = custom_engine.SCOPE_TEXT[
                    custom_engine.endpoint_scope(custom_config.base_url)
                ]
                lines.append(
                    f"自托管服务: {'可达' if models else '未连上'}"
                    f"（{custom_config.base_url}，文档原文{scope}）"
                )
                lines.append(
                    "   └ 模型: "
                    + ("、".join(models[:5]) if models else "服务未列出模型")
                )
                if models and selftest_tier == engine_registry.CUSTOM:
                    custom_translator = engine_registry.build(
                        engine_registry.CUSTOM,
                        engine_registry.EngineParams(log=lines.append, style=CLI_STYLE),
                    )
            else:
                lines.append(
                    "自托管服务: 未配置（跳过；界面点「配置自托管…」，"
                    "或用 --custom-url=地址 临时指定）"
                )
        except Exception as exc:
            lines.append(f"自托管服务: 探测失败（{type(exc).__name__}）")
    probe = "Chapter 1. The future of artificial intelligence in education."
    try:
        if OFFLINE:
            from core.local_translator import LocalTranslator

            translator = LocalTranslator(log=lines.append, style=CLI_STYLE)
        elif paid_translator is not None:
            translator = paid_translator
        elif custom_translator is not None:
            translator = custom_translator
        else:
            translator = Translator(log=lines.append, style=CLI_STYLE)
        lines.append(f"试译: {probe}")
        lines.append(f"结果: {translator.translate_texts([probe])[0]}")
    except Exception as exc:
        lines.append(f"翻译失败: {type(exc).__name__}: {exc}")
    (APP_DIR / "自检结果.txt").write_text("\n".join(lines), encoding="utf-8")
    return 0


def _run_bench(arguments: list[str]) -> int:
    """质量量化基准（--bench）：锚定测试集 + MQM 打分 + DeepL 对照（v1.5.2）。

    子参数：``--engine=offline,cloud,local-llm,deepl``（默认全部可用引擎）、
    ``--judge``（追加 LLM judge 层，需本机 Ollama）、``--model=xxx``
    （本地大模型与 judge 共用的模型名）。
    """
    from core.api_keys import is_acknowledged, save_acknowledgement
    from core.bench import (
        ANCHORS_PATH,
        build_translators,
        load_anchors,
        make_ollama_judge,
        render_report,
        run_bench,
        run_judge,
    )
    from core.local_llm import RECOMMENDED_MODELS, probe_local_llm

    requested = ["offline", "cloud", "local-llm", "deepl"]
    model = None
    judge = False
    for item in arguments:
        if item.startswith("--engine="):
            names = [
                name.strip()
                for name in item.split("=", 1)[1].split(",")
                if name.strip()
            ]
            if names:
                requested = names
        elif item == "--judge":
            judge = True
        elif item.startswith("--model="):
            model = item.split("=", 1)[1].strip() or None
    # main() 的解析循环先跑，--model= 已被它消费成 CLI_MODEL 全局量、
    # 不会出现在这里的 arguments 里；bench 与 CLI 共用同一个模型名。
    model = model or CLI_MODEL

    # DeepL 对照会把锚点原文发去境外服务器：与 --paid=deepl 同一道涉密闸门，
    # 没确认过就必须显式 --yes-i-know，不因为"只是测几条句子"而放宽。
    if "deepl" in requested and not is_acknowledged("deepl"):
        if not CLI_YES_I_KNOW:
            print("涉密警示：基准测量含 DeepL 对照，锚点原文会发送至 DeepL（境外服务器）。")
            print("确认已知悉请加 --yes-i-know 重新运行；不含对照可去掉 deepl：")
            print("  --engine=offline,cloud,local-llm")
            return 4
        save_acknowledgement("deepl")

    print(f"锚定测试集：{ANCHORS_PATH}")
    anchors = load_anchors()
    print(f"共 {len(anchors)} 条锚点；参跑引擎：{'、'.join(requested)}")
    engines, notes = build_translators(requested, log=print, model=model)
    for name, label, _ in engines:
        print(f"  · {label}")
    for note in notes:
        print(f"  · 跳过：{note}")
    if not engines:
        print("没有可用的引擎，基准测量无法进行。")
        return 1

    run = run_bench(engines, anchors)
    for name, score in run.scores.items():
        print(
            f"  [{score.label}] 综合 {score.overall * 100:.1f}%"
            f"（术语 {score.rate('terminology') or 0:.0%} /"
            f" 数字 {score.rate('numbers') or 0:.0%} /"
            f" 格式 {score.rate('format') or 0:.0%}）"
        )

    judge_scores = {}
    if judge:
        engine, reason = probe_local_llm(model=model or RECOMMENDED_MODELS["general"][0])
        if engine is None:
            run.notes.append(f"LLM judge 未启用：{reason}。")
            print(f"LLM judge 未启用：{reason}")
        else:
            print(f"LLM judge 打分中（{engine.model}，逐条 1-5 分，请稍候）……")
            judge_scores = run_judge(
                make_ollama_judge(engine.model, engine.base_url), run, anchors
            )
            for name, verdict in judge_scores.items():
                label = run.scores[name].label if name in run.scores else name
                print(
                    f"  [{label}] judge：流畅 {verdict['fluency']:.1f} /"
                    f" 忠实 {verdict['fidelity']:.1f}（{verdict['scored']} 条参评）"
                )

    run.notes.extend(notes)
    report = render_report(run, anchors, judge=judge_scores or None)
    print()
    print(report)
    today = datetime.now().strftime("%Y-%m-%d")
    target = APP_DIR / f"质量对比报告_{today}.txt"
    target.write_text(report, encoding="utf-8")
    print(f"报告已存盘：{target}")
    return 0


#: 靠 ``rest`` 生效的开关：这些不写在参数循环里，而是分发时按 ``"--xx" in rest``
#: 现查（见 ``main()`` 后半段的 --version / --selftest / --convert 分支）。
#: **新增一个「留在 rest 里」的开关必须同时登记在这里**，否则会被下面的未知参数
#: 拦截当成拼错——这条比看起来更咬人，因为 --offline 也走这条路。
_PASSTHROUGH_FLAGS = frozenset(
    {"--offline", "--version", "--terms", "--bench", "--selftest", "--convert"}
)

#: 全部命令行开关，供「最接近的写法」提示与报错时的清单用。这张表**不参与**能不能
#: 识别的判定（判定靠参数循环本身：任何匹配得上的分支都走不到 else），所以漏登记
#: 只会让提示少一项，不会误拦合法参数。带 ``=`` 的写法表示「要接值」。
_CLI_FLAGS = (
    "--offline",
    "--version",
    "--help",
    "--terms",
    "--selftest",
    "--bench",
    "--convert",
    "--to=",
    "--jobs=",
    "--batch-items=",
    "--batch-chars=",
    "--chunk-chars=",
    "--retry-delays=",
    "--tm-threshold=",
    "--layout=",
    "--pages=",
    "--segment-rules=",
    "--dump-segment-rules",
    "--style=",
    "--model=",
    "--password=",
    "--no-cache",
    "--no-quality",
    "--no-term-inflection",
    "--local-llm",
    "--paid=",
    "--yes-i-know",
    "--custom",
    "--custom-url=",
    "--yes-i-know-custom",
    "--postedit=",
    "--retranslate=",
    "--list",
    "--alternatives=",
    "--tbx-import=",
    "--tbx-export=",
    "--tmx-import=",
    "--tmx-export=",
    "--yes-i-know-tmx",
    "--no-translate=",
)


#: `--help` 那一屏的分组表（v1.9.1）。**每个开关都必须在这里出现且只出现一次**，
#: 漏登记由 `tests/test_cli_help.py` 钉住：加了开关忘了写说明，测试当场红。
#: 左栏是「写法 + 一个例子」，右栏说清它干什么、默认值是什么、什么时候会出声。
_CLI_HELP_GROUPS = (
    (
        "模式与入口",
        (
            ("--offline", "保密模式：翻译不联网、不存缓存；日志仅记录运行元信息"),
            ("--convert 文件…", "命令行直接转换，成品写在原件旁边（名字带「_双语」）"),
            ("--selftest", "自检各组件是否可用，结果写「自检结果.txt」"),
            ("--bench", "跑质量基准（锚定测试集 + MQM 打分），可加 --engine=…"),
            ("--version", "打印版本号（也认 -v）"),
            ("--help", "就是这一屏（也认 -h）"),
        ),
    ),
    (
        "方向与版式",
        (
            ("--to=zh|en", "目标语言，默认 zh；写 en 做中译英，已是英文的段自动跳过"),
            ("--layout=side|zh|inplace", "PDF 成品形态：左右对照 / 仅中文 / 原位重排"),
            ("--pages=1-3,5", "只处理选中的序号：PDF 数页、PPT 数张、EPUB 数章；留空是整份"),
            ("--style=formal|concise", "译文风格档位"),
            ("--model=xxx", "指定模型名（本地大模型或单位自托管服务）"),
            ("--password=xxx", "打开加密 PDF；批量时所有 PDF 共用这一个密码"),
        ),
    ),
    (
        "引擎档位（联网版）",
        (
            ("--local-llm", "用本机 Ollama 大模型档，不可用时仅回退本机离线小模型"),
            ("--paid=deepl", "付费档 DeepL，需自备 api_keys.local.json；与 --offline 互斥"),
            ("--yes-i-know", "确认已知悉付费档会把文档原文发送至 DeepL"),
            ("--custom", "用单位自托管服务（OpenAI 兼容接口）"),
            ("--custom-url=xxx", "自托管服务地址；只给地址就等于选了 --custom"),
            ("--yes-i-know-custom", "确认已知悉「地址是公网域名时这一档按云端对待」"),
        ),
    ),
    (
        "批量与重试（只在命令行调，界面上没有这些输入框）",
        (
            ("--jobs=N", "并发度，默认 1；各档有硬上限，写大了夹回上限并念出来"),
            ("--batch-items=N", "单次请求送几段"),
            ("--batch-chars=N", "单次请求送多少字"),
            ("--chunk-chars=N", "单段最长多少字，超了先切再送"),
            ("--retry-delays=0.5,1,2", "被限流后逐次等多久（最多 6 档）；保密模式用不上这一档"),
            ("--tm-threshold=0.95", "翻译记忆近似复用的相似度门槛，只能调高（1.0 = 只认完全一致）"),
        ),
    ),
    (
        "断句规则",
        (
            ("--dump-segment-rules", "在工具目录生成可编辑的《分段规则.json》；已存在绝不覆盖"),
            ("--segment-rules=x.json", "临时指定某一份规则表，不动工具目录里那份"),
        ),
    ),
    (
        "术语与免翻",
        (
            ("--terms", "查看术语库状态（含未决术语池）"),
            ("--no-quality", "关掉质量引擎（术语一致性校验、上下文连贯与自动校改）"),
            ("--no-term-inflection", "关掉术语屈折匹配，只按字面命中"),
            ("--no-translate=default", "免翻区规则：URL / 邮箱 / 版本号等译完原样换回；"
             "可写 +latex,+ident 追加规则，off 全关"),
        ),
    ),
    (
        "术语库与翻译记忆进出",
        (
            ("--tbx-import=x.tbx", "导入外部术语库；纯本地操作，保密模式可用，不覆盖既有条目"),
            ("--tbx-export=x.tbx", "把术语库导出成 TBX，只导已采用的条款"),
            ("--tmx-import=x.tmx", "把外部 TMX 灌进本地翻译记忆；往里灌，不受保密闸门约束"),
            ("--tmx-export=x.tmx", "导出翻译记忆；原文会写成明文 XML 落盘，保密模式一律拒绝"),
            ("--yes-i-know-tmx", "确认已知悉上一条——刻意不复用 --yes-i-know（那是确认出境）"),
        ),
    ),
    (
        "成品复核",
        (
            ("--postedit=x.docx", "对成品做译后编辑：把清单里改过的译文写回成品"),
            ("--list", "与 --postedit= 连用，只打印疑点清单、不动任何文件"),
            ("--retranslate=id,id", "让当前档位的引擎重摇这些段（覆盖手改值）后一起写回"),
            ("--alternatives=id,id", "给这些段各摇一条备选译文，只进清单、不写成品"),
        ),
    ),
    (
        "缓存",
        (
            ("--no-cache", "不读写本地翻译缓存，看引擎本次给出的原样"),
        ),
    ),
)

_CLI_USAGE_LINES = (
    "用法：",
    "  翻译工具.exe                      打开图形界面，把文件拖进窗口",
    "  翻译工具.exe --offline             保密模式界面（内容不出本机）",
    "  翻译工具.exe --convert a.docx b.pdf 命令行直接转，成品写在原件旁边",
    "  把文件拖到「批量翻译-把文件拖到这里.bat」图标上  等价于上一行",
)

_CLI_HELP_FOOTER = (
    "几句要点：",
    "  · 涉密文件一律走 --offline（或双击「打开-保密模式.bat」），离线模型在本机跑，内容不出这台机器。",
    "  · 拼错的开关会当场报错并以退码 4 结束，不会静默当成文件路径忽略。",
    "  · 认不出的写法（超上限的阈值、坏正则、坏 JSON）逐条念出来并跳过那一条，不拦整份翻译。",
    "  · 更详细的口径写在包内《使用说明.txt》里，那份是给人读的。",
)


def print_help() -> None:
    """打印 `--help`：版本、用法、按功能分组的开关说明。"""
    print(f"翻译工具 v{VERSION} —— 文档翻译（Word / PDF / PPT / EPUB / 字幕 / 图片）")
    print()
    for line in _CLI_USAGE_LINES:
        print(line)
    print()
    for title, items in _CLI_HELP_GROUPS:
        print(title)
        for flag, desc in items:
            print(f"  {flag:<28} {desc}")
        print()
    for line in _CLI_HELP_FOOTER:
        print(line)


def _nearest_flag(item: str) -> str:
    """给认不出的开关挑一个最接近的写法，顺带说清它带不带值。

    只用于提示：拼得够近才给（cutoff 0.6），差太远宁可不给，免得把人带偏。
    """
    name = item.split("=", 1)[0]
    matches = difflib.get_close_matches(name, _CLI_FLAGS, n=1, cutoff=0.6)
    if not matches:
        return ""
    best = matches[0]
    if "=" in item and "=" not in best:
        return f"{best}（这个开关不带值）"
    if "=" not in item and best.endswith("="):
        return f"{best}…（这个开关要接值）"
    return best


def _apply_segment_rules() -> None:
    """装载分段规则表，并把「生效了什么」记成一句可回显的话（v1.8.0）。

    坏写法不拦翻译：文件读不动、JSON 语法错、正则编译不了，都由 `core/segment_rules`
    出声并把那一条丢掉，其余照常——用户拿到的是「这条规则没生效」，不是一个退出码。
    切句规则只影响断句好不好看，为它挡住整份翻译是本末倒置（与阈值旋钮同口径）。
    """
    global CLI_SEGMENT_NOTE
    if CLI_SEGMENT_RULES:
        path = Path(CLI_SEGMENT_RULES).expanduser()
        rules = segment_rules.load(path, print)
    else:
        rules, path = segment_rules.startup(print)
    segment_rules.set_active(rules)
    CLI_SEGMENT_NOTE = (
        f"分段规则：{rules.summary()}（来自 {path.name}）"
        if path is not None and not rules.is_default
        else ""
    )


def main() -> None:
    global CLI_LAYOUT, CLI_PAGES, CLI_NO_CACHE, CLI_PASSWORD, CLI_STYLE, CLI_ENGINE, CLI_MODEL, CLI_QUALITY, CLI_PAID, CLI_YES_I_KNOW, CLI_CUSTOM, CLI_CUSTOM_URL, CLI_YES_I_KNOW_CUSTOM, CLI_POSTEDIT, CLI_POSTEDIT_LIST, CLI_RETRANSLATE, CLI_ALTERNATIVES, CLI_TBX_IMPORT, CLI_TBX_EXPORT, CLI_TMX_IMPORT, CLI_TMX_EXPORT, CLI_TMX_ACK, CLI_NO_TRANSLATE, CLI_JOBS, CLI_TARGET, CLI_BATCH_ITEMS, CLI_BATCH_CHARS, CLI_CHUNK_CHARS, CLI_RETRY_DELAYS, CLI_SEGMENT_RULES, CLI_DUMP_SEGMENT_RULES, CLI_SEGMENT_NOTE
    argv = sys.argv[1:]
    rest: list[str] = []
    unknown: list[str] = []
    for item in argv:
        if item in ("--help", "-h"):
            # 帮助优先于一切校验与拦截：用户要的是「这一屏」，不该被同一行里
            # 拼错的别的开关或写错的 --to= 挡回去（与 argparse 同口径）。
            print_help()
            raise SystemExit(0)
        if item.startswith("--layout="):
            CLI_LAYOUT = item.split("=", 1)[1].strip()
            if CLI_LAYOUT not in ("side", "zh", "inplace"):
                CLI_LAYOUT = "side"
        elif item.startswith("--pages="):
            # 只存原文写法，写法本身在下面统一校验；上界留到读文件时按各自页数判。
            # 与 --layout= 不同，这里**不能**认不出就退回默认值：静默把
            # --pages=1-3 当成整份文档译，用户收到的是「全都翻了」的成品。
            CLI_PAGES = item.split("=", 1)[1].strip()
        elif item.startswith("--segment-rules="):
            CLI_SEGMENT_RULES = item.split("=", 1)[1].strip()
        elif item == "--dump-segment-rules":
            CLI_DUMP_SEGMENT_RULES = True
        elif item.startswith("--style="):
            requested = item.split("=", 1)[1].strip()
            if requested in STYLES:
                CLI_STYLE = requested
        elif item == "--no-cache":
            CLI_NO_CACHE = True
        elif item == "--no-term-inflection":
            # 不开一个 CLI_ 全局副本：开关的真实状态就在 glossary 里（它同时决定
            # 编译出的 pattern 长什么样），再存一份只会出现「两处说法不一致」。
            glossary.set_inflection(False)
        elif item.startswith("--password="):
            CLI_PASSWORD = item.split("=", 1)[1]
        elif item == "--local-llm":
            CLI_ENGINE = "local-llm"
        elif item.startswith("--paid="):
            CLI_PAID = item.split("=", 1)[1].strip().lower()
        elif item == "--yes-i-know":
            CLI_YES_I_KNOW = True
        elif item == "--custom":
            CLI_CUSTOM = True
        elif item.startswith("--custom-url="):
            CLI_CUSTOM_URL = item.split("=", 1)[1].strip()
            # 只给地址就算选了这一档（与 --paid= 自带档位同一个口径）
            CLI_CUSTOM = True
        elif item == "--yes-i-know-custom":
            CLI_YES_I_KNOW_CUSTOM = True
        elif item == "--no-quality":
            CLI_QUALITY = False
        elif item.startswith("--model="):
            CLI_MODEL = item.split("=", 1)[1].strip() or None
        elif item.startswith("--postedit="):
            CLI_POSTEDIT = item.split("=", 1)[1].strip() or None
        elif item == "--list":
            CLI_POSTEDIT_LIST = True
        elif item.startswith("--retranslate="):
            CLI_RETRANSLATE = item.split("=", 1)[1].strip()
        elif item.startswith("--alternatives="):
            CLI_ALTERNATIVES = item.split("=", 1)[1].strip()
        elif item.startswith("--tbx-import="):
            CLI_TBX_IMPORT = item.split("=", 1)[1].strip() or None
        elif item.startswith("--tbx-export="):
            CLI_TBX_EXPORT = item.split("=", 1)[1].strip() or None
        elif item.startswith("--tmx-import="):
            CLI_TMX_IMPORT = item.split("=", 1)[1].strip() or None
        elif item.startswith("--tmx-export="):
            CLI_TMX_EXPORT = item.split("=", 1)[1].strip() or None
        elif item == "--yes-i-know-tmx":
            CLI_TMX_ACK = True
        elif item.startswith("--no-translate="):
            CLI_NO_TRANSLATE = item.split("=", 1)[1].strip() or "default"
        elif item.startswith("--to="):
            CLI_TARGET = item.split("=", 1)[1].strip()
        elif item.startswith("--jobs="):
            CLI_JOBS = _parse_jobs_argument(item.split("=", 1)[1])
        elif item.startswith("--batch-items="):
            CLI_BATCH_ITEMS = tuning.parse_int(
                item.split("=", 1)[1],
                flag="--batch-items=",
                label="单次请求的段数",
                example="--batch-items=10",
                log=print,
            )
        elif item.startswith("--batch-chars="):
            CLI_BATCH_CHARS = tuning.parse_int(
                item.split("=", 1)[1],
                flag="--batch-chars=",
                label="单次请求的字符数",
                example="--batch-chars=2000",
                log=print,
            )
        elif item.startswith("--chunk-chars="):
            CLI_CHUNK_CHARS = tuning.parse_int(
                item.split("=", 1)[1],
                flag="--chunk-chars=",
                label="单段送译的字符数",
                example="--chunk-chars=1000",
                log=print,
            )
        elif item.startswith("--retry-delays="):
            CLI_RETRY_DELAYS = tuning.parse_delays(
                item.split("=", 1)[1], flag="--retry-delays=", log=print
            )
        elif item.startswith("--tm-threshold="):
            CLI_TM_THRESHOLD = tuning.parse_tm_threshold(
                item.split("=", 1)[1], flag="--tm-threshold=", log=print
            )
        else:
            # v1.6.7：认不出的 --xx 不再静默进 rest。rest 会被 _convert 当成文件路径，
            # 也会让闸门以为用户没选那一档——--custom=地址（应为 --custom-url=）就是这么
            # 跳过闸门的，而 --offlin 少一个字母会以**联网模式**起界面。
            if item.startswith("--") and item not in _PASSTHROUGH_FLAGS:
                unknown.append(item)
            else:
                rest.append(item)

    if unknown:
        print("未识别的参数：" + "、".join(unknown))
        for item in unknown:
            near = _nearest_flag(item)
            if near:
                print(f"  {item} 最接近的写法：{near}")
        print("可用的命令行开关（带 = 的要接值）：")
        print("  " + " ".join(_CLI_FLAGS))
        print("  每个开关干什么：翻译工具.exe --help")
        raise SystemExit(4)

    # 目标语言同样是「翻译行为的配置」，放在所有分发之前：--convert / 译后编辑
    # / 界面三条入口拿到的必须是同一个方向。写错语言码直接报错退出——
    # 用户写了 --to=en 却翻出中文，比报错难查得多。
    try:
        direction.set_target(CLI_TARGET)
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(4)
    if "--convert" in rest and direction.is_english():
        print("目标语言：英文（--to=en）")
    # 免翻区规则在所有分发之前解析：它是**翻译行为的配置**，不是某条命令的
    # 参数。放在这里（而不是某个 elif 分支里）才能保证任何翻译入口
    # （--convert / --postedit --retranslate / GUI）拿到的都是同一个规则表。
    # 认不出的写法抛人话 ValueError，打印后非零退出——用户写了一个不存在的
    # 规则名却「什么都没报错」是最难排查的一类失效。
    try:
        from core import no_translate
        from core.no_translate import parse_rules_argument

        no_translate.set_rules(parse_rules_argument(CLI_NO_TRANSLATE))
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(4)
    # 范围先验写法再放行，原因与 --to= / --no-translate= 一样：它是翻译行为的
    # 配置，--convert 与界面两条入口必须拿到同一个校验过的值。写错退 4，不能等翻译
    # 跑到一半才由某个文件报出来。「超出文档数量」这类错留到各 translate_* 用自己的
    # 总数判——一次排多个文件时这里根本不知道每个有几页/几张/几章。
    # 这里沿用默认 Unit（页范围）：命令行开关就叫 --pages=，报错念「页范围」跟开关名对得上。
    if CLI_PAGES:
        try:
            page_range.describe(CLI_PAGES)
        except TranslationError as exc:
            print(str(exc))
            raise SystemExit(4)
    _apply_segment_rules()
    if CLI_DUMP_SEGMENT_RULES:
        segment_rules.write_template(print)
        raise SystemExit(0)
    if "--version" in rest or "-v" in rest:
        print(f"文档翻译工具 v{VERSION}")
        raise SystemExit(0)
    if "--terms" in rest:
        raise SystemExit(_list_terms())
    # TBX 导入导出都是纯本地文件操作：不联网、不构造翻译器，保密模式可用，
    # 因此放在付费闸门之前——它们与「文档出不出境」没有任何关系。
    if CLI_TBX_IMPORT:
        raise SystemExit(_run_tbx_import(CLI_TBX_IMPORT))
    if CLI_TBX_EXPORT:
        raise SystemExit(_run_tbx_export(CLI_TBX_EXPORT))
    # TMX 同位置分发：纯本地文件操作，不联网、不构造翻译器。导入不受限，
    # 导出自带两道闸门（保密模式拒绝 + --yes-i-know-tmx），见 _run_tmx_export。
    if CLI_TMX_IMPORT:
        raise SystemExit(_run_tmx_import(CLI_TMX_IMPORT))
    if CLI_TMX_EXPORT:
        raise SystemExit(_run_tmx_export(CLI_TMX_EXPORT))
    if "--bench" in rest:
        raise SystemExit(_run_bench(rest))
    if "--selftest" in rest:
        raise SystemExit(_selftest())
    # --paid=deepl 的三道闸门：与保密模式互斥 / 没 key 给引导 /
    # 未确认涉密警示要求 --yes-i-know。自检不走闸门（它自己按 key 有无降级）。
    gate = _paid_gate()
    if gate is not None:
        raise SystemExit(gate)
    # --custom 的三道闸门：与 --paid 同一个位置、同一套退出码口径，判断依据换成
    # 「用户填的地址在本机 / 内网 / 公网」。自检同样不过闸（它只是报告连不连得上）。
    gate = _custom_gate()
    if gate is not None:
        raise SystemExit(gate)
    # 档位开关的互斥关系在登记表里判一次：--paid + --local-llm 这类组合
    # 以前是「写在后面的赢」，现在直接报错（交接稿记录的已知小缺口就此关掉）。
    try:
        cli_tier = engine_registry.resolve_cli(
            offline=OFFLINE,
            paid=CLI_PAID,
            engine=CLI_ENGINE,
            custom=CLI_CUSTOM,
        )
    except TranslationError as exc:
        print(str(exc))
        raise SystemExit(2)
    if CLI_POSTEDIT:
        # 译后编辑闭环：放在闸门之后——--retranslate 若选付费档，key 与
        # 涉密确认的口径与翻译时一致，不搞第二套。
        raise SystemExit(_run_postedit(CLI_POSTEDIT))
    if "--convert" in rest:
        raise SystemExit(_convert(rest[rest.index("--convert") + 1 :]))

    app = TranslatorApp()
    app.pdf_layout = CLI_LAYOUT
    if CLI_PAGES:
        # 输入框里的值本来就来自同一个全局（_build_ui 读的也是 CLI_PAGES），这里
        # 只补一句回显：命令行传进来的范围若在日志里不出声，用户没法确认它真生效了。
        app.pdf_pages = CLI_PAGES
        app._append_log(f"文档范围：{CLI_PAGES}（{page_range.SCOPE_NOTE}）。", "muted")
    if CLI_SEGMENT_NOTE:
        # 规则表是「这一份为什么跟上一份断句不一样」的唯一解释，起界面时不说一句，
        # 用户只能靠猜——与 --pages= 的回显同一处理。
        app._append_log(f"{CLI_SEGMENT_NOTE}。", "muted")
    # --to= 也可能用来起界面：把方向同步到界面上的「目标语言」单选，
    # 否则界面显示中文、实际在译英文，是最容易让人误判的一类错位。
    if getattr(app, "target_var", None) is not None:
        app.target_var.set(direction.target())
    if cli_tier != engine_registry.CLOUD and getattr(app, "engine_var", None):
        # 命令行选了档位（闸门已过）：把界面单选也拨过去，用户看得见当前用的是
        # 哪档。_on_engine_change 会再跑一遍 GUI 闸门，此时 key/确认都已就位，
        # 不会再弹窗。保密模式下根本没有 engine_var（档位不可切换），getattr 兜住。
        app.engine_var.set(cli_tier)
        app._on_engine_change()
    startup = [
        item
        for item in rest
        if not item.startswith("--") and os.path.exists(item)
    ]
    if len(startup) > 1:
        app.set_files([os.path.abspath(item) for item in startup])
    else:
        for item in startup:
            if os.path.isfile(item):
                app.set_file(item)
                break
    app.run()


if __name__ == "__main__":
    main()
