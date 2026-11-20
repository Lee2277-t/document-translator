"""PDF 原位重排（v1.5.3 D1，谨慎版）的行为钉子。

背景：决策 24 曾以「重排丢表格底纹与矢量图」否决 PDF 原位重排。v1.5.3 的
「谨慎版」回应这条否决——只做**文本级替换**：用红action 擦除原文文字
（显式保留图像与线稿），在原位置排入中文译文，页面不重新拼合，其余一切
（表格底纹、矢量图、图片、链接、书签、批注、表单、页面尺寸）原样保留。

这里用 PyMuPDF 现场构造样例 PDF + 假翻译器（不联网、不加载模型）跑通整条
管线，断言落在用户能感知的结果上：译文在不在、原文丢没丢、链接/书签/矢量图/
扫描页保不保得住。套路沿用 tests/test_pdf.py；界面接线用子进程验（headless
约束：一个进程只构造一次 Tk 根，见 tests/test_paid_gui.py 头部说明）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.common import TranslationError  # noqa: E402
from core.pdf_translator import LAYOUTS, translate_pdf  # noqa: E402

PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

#: 子进程全部成功的标记；失败时子进程打印 "FAIL: ..." 并非零退出。
_OK_MARKER = "INPLACE_GUI_OK"

#: 假翻译器译文里的固定标记。断言「译文在」时数它的出现次数。
TRANSLATION_MARK = "中文译文序号"

SINGLE_COLUMN_LINES = [
    "Quarterly revenue rose 12 percent.",
    "Enterprise contracts signed in Q3.",
    "Costs remained flat overall.",
]


class _IndexedChineseFake:
    """假翻译器：不联网、不加载模型，按调用次序返回确定的中文译文。

    译文只含中文与序号、不含任何原文子串——原位重排的核心断言是「译文在、
    原文不在」，译文里夹带原文会让断言失去意义。``length`` 非零时每段返回
    超长中文，用来构造「空间不足保留原文」的场景。
    """

    stop_event = None

    def __init__(self, length: int = 0) -> None:
        self.length = length
        self.count = 0

    def translate_texts(self, texts, progress=None):
        self.count += len(texts)
        if self.length:
            return [f"{'超' * self.length}{index}。" for index, _ in enumerate(texts)]
        return [f"{TRANSLATION_MARK}{index}。" for index, _ in enumerate(texts)]

    def summary(self) -> str:
        return f"翻译 {self.count} 段"


def _text_page(doc, lines, *, x=72, y=72, dy=28, width=595, height=842):
    """造一页竖排文字（默认行距足够大，每行自成一组，互不合并）。"""
    page = doc.new_page(width=width, height=height)
    for line in lines:
        page.insert_text(pymupdf.Point(x, y), line, fontsize=11, fontname="helv")
        y += dy
    return page


def _scanned_page(doc):
    """造一页「扫描件」：整页一张栅格图像，没有任何可提取文字。"""
    page = doc.new_page(width=595, height=842)
    scratch = pymupdf.open()
    canvas = scratch.new_page(width=400, height=560)
    canvas.draw_rect(
        pymupdf.Rect(10, 10, 390, 550), color=None, fill=(0.96, 0.96, 0.96)
    )
    for row in range(8):
        top = 40 + row * 60
        canvas.draw_rect(
            pymupdf.Rect(30, top, 370, top + 14), color=None, fill=(0.25, 0.25, 0.25)
        )
    pixmap = canvas.get_pixmap(dpi=150, alpha=False)
    page.insert_image(pymupdf.Rect(40, 40, 555, 800), pixmap=pixmap)
    scratch.close()
    return page


def _save(doc, path: Path) -> Path:
    doc.save(path)
    doc.close()
    return path


# ---------------------------------------------------------------------------
# 1. 单栏文字页：译文在原位、原文被擦除、页面尺寸不变
# ---------------------------------------------------------------------------

def test_inplace_single_column_replaces_text_and_keeps_page_size(tmp_path):
    doc = pymupdf.open()
    _text_page(doc, SINGLE_COLUMN_LINES)
    src = _save(doc, tmp_path / "single.pdf")

    out = translate_pdf(
        src, _IndexedChineseFake(), layout="inplace", ocr_enabled=False
    )

    source_rect = pymupdf.open(src)[0].rect
    out_doc = pymupdf.open(out)
    try:
        assert out_doc[0].rect == source_rect, "原位重排不该改动页面尺寸"
        text = out_doc[0].get_text()
        for line in SINGLE_COLUMN_LINES:
            assert line not in text, f"原文应当被擦除：{line}"
        assert text.count(TRANSLATION_MARK) == 3, "三段译文都应当落在原位置"
    finally:
        out_doc.close()


# ---------------------------------------------------------------------------
# 2. 双栏文字页：两栏各自被原位替换（双栏由既有抽段链路自然覆盖）
# ---------------------------------------------------------------------------

def test_inplace_double_column_replaces_both_columns(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text(
        pymupdf.Point(72, 100),
        "Left column paragraph about quarterly revenue.",
        fontsize=11,
        fontname="helv",
    )
    page.insert_text(
        pymupdf.Point(320, 100),
        "Right column paragraph about operating costs.",
        fontsize=11,
        fontname="helv",
    )
    src = _save(doc, tmp_path / "two_column.pdf")

    out = translate_pdf(
        src, _IndexedChineseFake(), layout="inplace", ocr_enabled=False
    )

    out_doc = pymupdf.open(out)
    try:
        text = out_doc[0].get_text()
        assert "Left column" not in text, "左栏原文应当被擦除"
        assert "Right column" not in text, "右栏原文应当被擦除"
        assert text.count(TRANSLATION_MARK) >= 2, "两栏都应当排入译文"
    finally:
        out_doc.close()


# ---------------------------------------------------------------------------
# 3. 扫描页：整页保留（图像仍在、无译文），日志交代原因
# ---------------------------------------------------------------------------

def test_inplace_scanned_page_kept_untouched(tmp_path):
    doc = pymupdf.open()
    _text_page(doc, SINGLE_COLUMN_LINES)
    _scanned_page(doc)
    src = _save(doc, tmp_path / "mixed.pdf")

    logs: list[str] = []
    out = translate_pdf(
        src, _IndexedChineseFake(), log=logs.append, layout="inplace",
        ocr_enabled=False,
    )

    out_doc = pymupdf.open(out)
    try:
        scan_page = out_doc[1]
        assert scan_page.get_image_info(), "扫描页的图像应当原样保留"
        assert TRANSLATION_MARK not in scan_page.get_text(), "扫描页不该被排入译文"
        assert any(
            "扫描件" in line and "保留原页" in line and "原位重排" in line
            for line in logs
        ), "日志应当说明扫描页为何不动"
    finally:
        out_doc.close()


# ---------------------------------------------------------------------------
# 4. 混合文档：完成消息同时交代译文放置与扫描页保留
# ---------------------------------------------------------------------------

def test_inplace_mixed_document_message_accounts_for_both(tmp_path):
    doc = pymupdf.open()
    _text_page(doc, SINGLE_COLUMN_LINES)
    _scanned_page(doc)
    src = _save(doc, tmp_path / "mixed.pdf")

    logs: list[str] = []
    translate_pdf(
        src, _IndexedChineseFake(), log=logs.append, layout="inplace",
        ocr_enabled=False,
    )

    message = logs[-1]
    assert "原位重排" in message and "成功放置" in message and "段译文" in message
    assert "扫描页已原样保留" in message, "完成消息应当交代扫描页去了哪里"


# ---------------------------------------------------------------------------
# 5. 链接保留：链接压在文字上（红action 会误删链接，必须补回）
# ---------------------------------------------------------------------------

def test_inplace_keeps_links(tmp_path):
    doc = pymupdf.open()
    page = _text_page(doc, ["Linked paragraph with a reference to the report."])
    page.insert_link(
        {
            "kind": pymupdf.LINK_URI,
            "from": pymupdf.Rect(70, 58, 320, 78),
            "uri": "https://example.com/report",
        }
    )
    src = _save(doc, tmp_path / "linked.pdf")

    out = translate_pdf(
        src, _IndexedChineseFake(), layout="inplace", ocr_enabled=False
    )

    out_doc = pymupdf.open(out)
    try:
        links = out_doc[0].get_links()
        assert any(
            link.get("uri") == "https://example.com/report" for link in links
        ), "文字页上的链接应当在原位重排后保留"
        assert TRANSLATION_MARK in out_doc[0].get_text()
    finally:
        out_doc.close()


# ---------------------------------------------------------------------------
# 6. 书签保留：目录原样（页面不重建，书签天然过得来）
# ---------------------------------------------------------------------------

def test_inplace_keeps_toc(tmp_path):
    doc = pymupdf.open()
    _text_page(doc, SINGLE_COLUMN_LINES)
    _text_page(doc, ["Second page opening paragraph."])
    doc.set_toc([[1, "Chapter One", 1], [1, "Chapter Two", 2]])
    src = _save(doc, tmp_path / "toc.pdf")
    expected = pymupdf.open(src).get_toc()

    out = translate_pdf(
        src, _IndexedChineseFake(), layout="inplace", ocr_enabled=False
    )

    out_doc = pymupdf.open(out)
    try:
        assert out_doc.get_toc() == expected, "书签应当原样保留"
    finally:
        out_doc.close()


# ---------------------------------------------------------------------------
# 7. 表格底纹/矢量图保留：决策 24 担忧的直接反证
# ---------------------------------------------------------------------------

def test_inplace_keeps_table_shading_and_line_art(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    # 表格底纹（矢量填充）+ 表格线（矢量线稿）——当年否决重排怕丢的就是这两样
    page.draw_rect(
        pymupdf.Rect(60, 90, 535, 160), color=None, fill=(0.88, 0.88, 0.88)
    )
    page.draw_line(
        pymupdf.Point(60, 160), pymupdf.Point(535, 160), color=(0, 0, 0), width=0.8
    )
    page.insert_text(
        pymupdf.Point(72, 120),
        "Revenue by segment table header",
        fontsize=11,
        fontname="helv",
    )
    page.insert_text(
        pymupdf.Point(72, 145),
        "Second row of the same table cell.",
        fontsize=11,
        fontname="helv",
    )
    # 一个小矢量块，完整落在第一行文字的擦除框内：graphics=NONE 时原样保留，
    # 若退化成默认的 REMOVE_IF_COVERED 会被整块删掉——钉住这个参数，
    # 否则「表格底纹不丢」只对比擦除框大的底纹成立，框内小矢量（细规则线、
    # 小图标）丢了照样绿灯（QA 变异检验 M2 的发现，2026-10 补强）。
    page.draw_rect(
        pymupdf.Rect(90, 111, 120, 123), color=None, fill=(0.4, 0.4, 0.4)
    )
    src = _save(doc, tmp_path / "table.pdf")

    out = translate_pdf(
        src, _IndexedChineseFake(), layout="inplace", ocr_enabled=False
    )

    out_doc = pymupdf.open(out)
    try:
        page = out_doc[0]
        assert len(page.get_drawings()) >= 2, "表格底纹与线稿应当原样保留"
        # 擦除框内的小矢量块也必须幸存（pin graphics=PDF_REDACT_LINE_ART_NONE）
        assert any(
            abs(drawing["rect"].x0 - 90) < 2 and abs(drawing["rect"].y0 - 111) < 2
            for drawing in page.get_drawings()
        ), "擦除框内的小矢量图不应当被红action 删掉"
        text = page.get_text()
        assert "Revenue by segment" not in text
        assert "Second row" not in text
        assert text.count(TRANSLATION_MARK) >= 2, "表格里的文字同样被原位替换"
    finally:
        out_doc.close()


# ---------------------------------------------------------------------------
# 8. 空间不足保留原文：装不下的组不擦除，计入 fit_failures 并告知用户
# ---------------------------------------------------------------------------

def test_inplace_keeps_original_when_translation_cannot_fit(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=240)
    page.insert_text(pymupdf.Point(60, 100), "Tiny cell.", fontsize=11, fontname="helv")
    src = _save(doc, tmp_path / "tiny.pdf")

    logs: list[str] = []
    out = translate_pdf(
        src, _IndexedChineseFake(length=600), log=logs.append,
        layout="inplace", ocr_enabled=False,
    )

    out_doc = pymupdf.open(out)
    try:
        text = out_doc[0].get_text()
        assert "Tiny cell." in text, "装不下时应当保留原文"
        assert "超" not in text, "装不下的译文不该落到页面上"
    finally:
        out_doc.close()
    assert any("空间不足" in line for line in logs), "空间不足必须告知用户"


# ---------------------------------------------------------------------------
# 9. 全扫描文档：人话错误（开不开 OCR 都一样，原位重排不做扫描件）
# ---------------------------------------------------------------------------

def test_inplace_all_scanned_reports_human_error(tmp_path):
    doc = pymupdf.open()
    _scanned_page(doc)
    _scanned_page(doc)
    src = _save(doc, tmp_path / "all_scanned.pdf")

    for ocr_enabled in (False, True):
        with pytest.raises(TranslationError) as info:
            translate_pdf(
                src, _IndexedChineseFake(), layout="inplace",
                ocr_enabled=ocr_enabled,
            )
        assert "扫描" in str(info.value), (
            f"ocr_enabled={ocr_enabled} 时应报扫描件相关的人话错误"
        )


# ---------------------------------------------------------------------------
# 10. 版式校验：LAYOUTS 放行、translate_pdf 不回落、CLI 校验放行
# ---------------------------------------------------------------------------

def test_layout_validation_accepts_inplace(tmp_path):
    assert "inplace" in LAYOUTS

    # 功能面：layout="inplace" 不得回落到 side（回落后页宽翻倍、文件名变「双语」）
    doc = pymupdf.open()
    _text_page(doc, SINGLE_COLUMN_LINES)
    src = _save(doc, tmp_path / "sample.pdf")
    out = translate_pdf(
        src, _IndexedChineseFake(), layout="inplace", ocr_enabled=False
    )
    assert out.endswith("_原位.pdf"), "原位重排的输出文件名后缀应为「原位」"
    out_doc = pymupdf.open(out)
    try:
        source_width = pymupdf.open(src)[0].rect.width
        assert out_doc[0].rect.width == pytest.approx(source_width, abs=2)
    finally:
        out_doc.close()

    # CLI 校验：--layout=inplace 放行；非法值照旧回落 side
    _run_cli_child(["--layout=inplace"], "inplace")
    _run_cli_child(["--layout=zh"], "zh")
    _run_cli_child(["--layout=bogus"], "side")


# ---------------------------------------------------------------------------
# 11. GUI 接线：第三项单选、切换日志、OCR 开关随版式置灰
# ---------------------------------------------------------------------------

_GUI_BODY = """\
    gui = application
    labels = {}
    stack = [gui.root]
    while stack:
        widget = stack.pop()
        for child in widget.winfo_children():
            stack.append(child)
            if (
                isinstance(child, tk.Radiobutton)
                and str(child.cget("variable")) == str(gui.layout_var)
            ):
                labels[str(child.cget("value"))] = str(child.cget("text"))
    if labels != {"side": "左右对照", "zh": "仅中文", "inplace": "原位重排"}:
        problems.append("版式单选不是三项: %r" % (labels,))
    gui.layout_var.set("inplace")
    gui._on_layout_change()
    if gui.pdf_layout != "inplace":
        problems.append("选中后 pdf_layout 没跟上: %r" % gui.pdf_layout)
    log_text = gui.log_text.get("1.0", "end-1c")
    for needle in ("原位重排", "原样保留", "扫描页"):
        if needle not in log_text:
            problems.append("inplace 档日志缺关键信息: %s" % needle)
    # OCR 开关随版式走：排了 PDF 时 side 档可用，inplace 档置灰，切回来恢复
    gui.src_path = "C:/sample/报告.pdf"
    gui.layout_var.set("side")
    gui._on_layout_change()
    if not gui.ocr_switch._enabled:
        problems.append("排了 PDF 时 OCR 开关应可用")
    gui.layout_var.set("inplace")
    gui._on_layout_change()
    if gui.ocr_switch._enabled:
        problems.append("inplace 档 OCR 开关应置灰")
    gui.layout_var.set("zh")
    gui._on_layout_change()
    if not gui.ocr_switch._enabled:
        problems.append("切回 zh 档 OCR 开关应恢复可用")
"""


def test_gui_layout_radio_and_ocr_switch() -> None:
    _assert_ok(_run_gui_child(_GUI_BODY), _OK_MARKER)


def _run_gui_child(body: str) -> subprocess.CompletedProcess:
    """在子进程里构造一次界面并执行 body（headless：反复建 Tk 根会抖）。"""
    script = (
        "import sys\n"
        "import tkinter as tk\n"
        "sys.argv = ['app']\n"
        "import app\n"
        "app._enable_dpi_awareness()\n"
        "application = app.TranslatorApp()\n"
        "problems = []\n"
        "try:\n"
        f"{body}\n"
        "except Exception as exc:\n"
        "    problems.append('%s: %s' % (type(exc).__name__, exc))\n"
        "finally:\n"
        "    try:\n"
        "        application.root.destroy()\n"
        "    except Exception:\n"
        "        pass\n"
        "    if problems:\n"
        "        print('FAIL: ' + ' | '.join(problems))\n"
        "        sys.exit(1)\n"
        f"    print('{_OK_MARKER}')\n"
    )
    return subprocess.run(
        [str(PYTHON), "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


def _run_cli_child(argv: list[str], expected: str) -> None:
    """main() 的 --layout 校验：inplace 放行，非法值回落 side。"""
    script = f"""\
import sys
sys.argv = ['app'] + {argv!r}
import app
created = []
app.TranslatorApp.run = lambda self: created.append(self)
problems = []
try:
    app.main()
    if not created:
        problems.append("main() 没构造界面")
    elif created[0].pdf_layout != {expected!r}:
        problems.append(
            "%r 应放行为 %r，实得 %r"
            % ({argv!r}, {expected!r}, created[0].pdf_layout)
        )
except SystemExit as exc:
    problems.append("main() 不该退出: %r" % (exc.code,))
except Exception as exc:
    problems.append("异常 %s: %s" % (type(exc).__name__, exc))
finally:
    for gui in created:
        try:
            gui.root.destroy()
        except Exception:
            pass
if problems:
    print("FAIL: " + " | ".join(problems))
    sys.exit(1)
print("INPLACE_CLI_OK")
"""
    _assert_ok(
        subprocess.run(
            [str(PYTHON), "-c", script],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        ),
        "INPLACE_CLI_OK",
    )


def _assert_ok(child: subprocess.CompletedProcess, marker: str) -> None:
    assert child.returncode == 0, (
        f"stdout: {child.stdout}\nstderr: {child.stderr}"
    )
    assert marker in child.stdout, (
        f"stdout: {child.stdout}\nstderr: {child.stderr}"
    )


# ---------------------------------------------------------------------------
# 12. 既有版式不回归：共用链路只允许加 inplace 条件，side/zh 行为不变
# ---------------------------------------------------------------------------

def test_existing_layouts_not_regressed(tmp_path):
    doc = pymupdf.open()
    _text_page(doc, SINGLE_COLUMN_LINES)
    src = _save(doc, tmp_path / "sample.pdf")
    source_width = pymupdf.open(src)[0].rect.width

    side_out = translate_pdf(
        src, _IndexedChineseFake(), layout="side", ocr_enabled=False
    )
    side_doc = pymupdf.open(side_out)
    try:
        assert side_doc[0].rect.width > source_width * 1.6, "左右对照宽度应翻倍"
        text = side_doc[0].get_text()
        assert "Quarterly revenue" in text, "左半页应当保留原文"
        assert TRANSLATION_MARK in text, "右半页应当有译文"
    finally:
        side_doc.close()

    zh_out = translate_pdf(
        src, _IndexedChineseFake(), layout="zh", ocr_enabled=False
    )
    zh_doc = pymupdf.open(zh_out)
    try:
        assert zh_doc[0].rect.width == pytest.approx(source_width, abs=2)
        assert TRANSLATION_MARK in zh_doc[0].get_text(), "仅中文版应有译文"
    finally:
        zh_doc.close()


# ---------------------------------------------------------------------------
# 13. 放置失败诚实可见：段2（放译文）失败时日志/消息/报告都说实话
# ---------------------------------------------------------------------------

def test_inplace_placement_failure_is_reported_honestly(tmp_path, monkeypatch):
    """段2（放译文）失败：擦除不可逆，必须诚实交代内容损失。

    QA v1.5.3 复核定位的缺陷——旧写法一个 try 横跨擦除与插入两个阶段，
    except 只能清理未 apply 的擦除框；插入阶段逃逸的故障会让该页
    「擦了原文、空白收场」却仍打「该页保留原页」。修法是把 try 拆成
    擦除/放置两段，本钉子钉住段2 的诚实交代：日志、完成消息、自检报告
    三处都出声，其他页不受影响，源文件不被改动。
    """
    import core.pdf_translator as pdf_module

    doc = pymupdf.open()
    _text_page(doc, SINGLE_COLUMN_LINES)  # 第 1 页：放置阶段被探针炸掉
    _text_page(doc, ["Second page opening paragraph."])  # 第 2 页：正常
    src = _save(doc, tmp_path / "two_pages.pdf")

    original_insert = pdf_module._insert_translation_text
    state = {"calls": 0}

    def exploding_insert(page, group, rect, offset_x, text, size):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("boom")  # write_text 级 pymupdf 故障的替身
        return original_insert(page, group, rect, offset_x, text, size)

    monkeypatch.setattr(
        pdf_module, "_insert_translation_text", exploding_insert
    )

    logs: list[str] = []
    out = translate_pdf(
        src, _IndexedChineseFake(), log=logs.append, layout="inplace",
        ocr_enabled=False,
    )

    # 1. 诚实日志：说清「原文已擦除、译文没放上、建议重跑」，
    #    且绝不许出现与事实矛盾的「保留原页」
    assert any(
        "译文放置失败" in line and "已擦除" in line and "重跑" in line
        for line in logs
    ), "段2 失败必须诚实交代内容损失"
    assert all("保留原页" not in line for line in logs), (
        "擦了原文的页不许再说「保留原页」"
    )

    # 2. 失败计数进完成消息与自检报告（「高」级——页面内容受损）
    message = logs[-1]
    assert "3 处译文放置失败" in message, f"完成消息应交代放置失败：{message}"
    report_path = tmp_path / "two_pages_原位 自检报告.txt"
    assert report_path.exists(), "自检报告应当生成"
    report_text = report_path.read_text(encoding="utf-8")
    assert "[高] 译文放置失败 · 第 1 页" in report_text
    assert "3 处原文已擦除但译文未能放入" in report_text

    # 3. 失败页原文已擦、译文未落（成品如实反映事实）；其他页正常翻译
    out_doc = pymupdf.open(out)
    try:
        first_text = out_doc[0].get_text()
        for line in SINGLE_COLUMN_LINES:
            assert line not in first_text, "失败页原文应已被擦除"
        assert TRANSLATION_MARK not in first_text, "失败页不该落下译文"
        assert TRANSLATION_MARK in out_doc[1].get_text(), "其他页应正常翻译"
    finally:
        out_doc.close()

    # 4. 源文件不被改动（inplace 是改完另存，不碰原件）
    src_doc = pymupdf.open(src)
    try:
        src_text = src_doc[0].get_text()
        for line in SINGLE_COLUMN_LINES:
            assert line in src_text, "源文件不应当被改动"
    finally:
        src_doc.close()


# ---------------------------------------------------------------------------
# 14. inplace 跳过 toc 复制块：书签天然保留，不该被无关故障打出误导日志
# ---------------------------------------------------------------------------

def test_inplace_skips_toc_copy_block(tmp_path, monkeypatch):
    """inplace 的书签天然保留（页面一个都没重建），没有可复制的东西。

    旧写法在 inplace 下仍会调 get_toc，一个与 inplace 无关的故障会打出
    误导性的「书签复制失败」。修法是整个 toc 块在 inplace 下跳过；本钉子
    同时钉住 side/zh 的 toc 复制没有被这次改动碰坏（守卫按版式生效）。
    """
    doc = pymupdf.open()
    _text_page(doc, SINGLE_COLUMN_LINES)
    doc.set_toc([[1, "Chapter One", 1]])
    src = _save(doc, tmp_path / "toc.pdf")
    expected = pymupdf.open(src).get_toc()

    def exploding_get_toc(self, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pymupdf.Document, "get_toc", exploding_get_toc)

    logs: list[str] = []
    out = translate_pdf(
        src, _IndexedChineseFake(), log=logs.append, layout="inplace",
        ocr_enabled=False,
    )
    assert not any("书签复制失败" in line for line in logs), (
        "inplace 不该调 get_toc，更不该因此打出「书签复制失败」"
    )
    assert logs[-1].startswith("已生成原位重排 PDF"), "inplace 应当正常收尾"

    # 守卫按版式生效：zh 仍然走 toc 复制块（get_toc 故障时如实报错）
    zh_logs: list[str] = []
    translate_pdf(
        src, _IndexedChineseFake(), log=zh_logs.append, layout="zh",
        ocr_enabled=False,
    )
    assert any("书签复制失败" in line for line in zh_logs), (
        "zh 版式仍应尝试复制书签，不能被 inplace 的跳过逻辑带坏"
    )

    monkeypatch.undo()
    inplace_doc = pymupdf.open(out)
    try:
        assert inplace_doc.get_toc() == expected, "inplace 的书签天然保留"
    finally:
        inplace_doc.close()
