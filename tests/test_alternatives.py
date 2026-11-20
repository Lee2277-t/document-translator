"""备选译文（v1.6.2，E4）的行为钉子。

盯住四件事：

1. **重摇绕开缓存**：在线档与本地大模型档都先查 ``translations.sqlite``，
   不绕开的话「备选」永远等于现译。绕开只对这一次调用生效，缓存本体
   （``get``/``set``）与翻译器上的对象都要原样还回去；
2. **确定性引擎不伪造候选**：重摇结果与现译一致时抛 ``TranslationError``
   说人话，而不是把同一句存两遍或给个空壳；
3. **候选跟着段走**：进清单的 ``candidates`` 字段，重启界面还在；旧清单
   （没有这个字段）读回来是空列表；
4. **选了不算通过**：``pick_candidate`` 只改 ``translation``，成品与
   ``applied`` 不动，必须走「保存写回」才落盘、才进翻译记忆。

GUI 用例走子进程，理由同 tests/test_postedit.py 头注（同进程反复起 Tk 根
有 ``tcl_findLibrary`` 抖动）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from docx import Document

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from core.alternatives import (  # noqa: E402
    MAX_CANDIDATES,
    add_candidate,
    bypass_cache,
    generate_alternative,
    pick_candidate,
)
from core.common import TranslationError  # noqa: E402
from core.postedit import (  # noqa: E402
    EditItem,
    apply_edits,
    dump_edit_list,
    edit_list_path,
    format_edit_list,
    items_from_pairs,
    load_edit_list,
    retranslate_segment,
)
from core.word_translator import translate_word  # noqa: E402

PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

FIRST = "Revenue grew by 15.3 percent last quarter."


def _item(translation: str = "初译。", applied: str | None = None) -> EditItem:
    return EditItem(
        id="w1",
        location="段落 1",
        source=FIRST,
        translation=translation,
        kind="docx",
        applied=translation if applied is None else applied,
    )


class _Cache:
    """记下 get/set 调用：真正的「缓存读写」计数在这里。"""

    def __init__(self, value: str | None):
        self.value = value
        self.gets = 0
        self.sets = 0
        self._profile = "fake"

    def get(self, engine, text):
        self.gets += 1
        return self.value

    def set(self, engine, text, translation):
        self.sets += 1


class _CachedEngine:
    """模拟真实翻译器：先查缓存，命中就直接返回上次的译文。"""

    stop_event = None

    def __init__(self, cached: str | None, fresh: str = "另一种说法。"):
        self.cache = _Cache(cached)
        self.fresh = fresh
        self.calls = 0

    def translate_texts(self, texts, progress=None):
        self.calls += 1
        hit = self.cache.get("edge", texts[0])
        if hit is not None:
            return [hit]
        return [self.fresh]


# ---------------------------------------------------------------------------
# 一、绕开缓存
# ---------------------------------------------------------------------------


def test_generate_alternative_bypasses_cache() -> None:
    engine = _CachedEngine(cached="初译。")
    text = generate_alternative(engine, _item())
    assert text == "另一种说法。"
    assert engine.calls == 1
    # 空壳把读拦下了（否则会拿到缓存里的「初译。」），也没往缓存里写。
    assert engine.cache.gets == 0
    assert engine.cache.sets == 0


def test_bypass_cache_restores_translator_cache() -> None:
    engine = _CachedEngine(cached="初译。")
    original = engine.cache
    with bypass_cache(engine):
        assert engine.cache is not original
        assert engine.cache.get("edge", "x") is None
        engine.cache.set("edge", "x", "y")
    assert engine.cache is original
    assert original.sets == 0


def test_bypass_cache_restores_after_exception() -> None:
    engine = _CachedEngine(cached=None)
    original = engine.cache

    class _Boom:
        cache = original

        def translate_texts(self, texts, progress=None):
            raise RuntimeError("引擎炸了")

    boom = _Boom()
    with pytest.raises(RuntimeError):
        with bypass_cache(boom):
            boom.translate_texts(["x"])
    assert boom.cache is original


def test_retranslate_segment_also_bypasses_cache() -> None:
    """「重译本段」同一个坑：不绕缓存就永远返回上次那条。"""
    engine = _CachedEngine(cached="初译。")
    assert retranslate_segment(engine, FIRST) == "另一种说法。"
    assert engine.cache.gets == 0


# ---------------------------------------------------------------------------
# 二、确定性引擎不伪造候选
# ---------------------------------------------------------------------------


def test_same_as_current_raises_with_human_hint() -> None:
    engine = _CachedEngine(cached=None, fresh="初译。")
    with pytest.raises(TranslationError, match="给不出不同说法"):
        generate_alternative(engine, _item("初译。"))


def test_same_as_applied_also_counts_as_duplicate() -> None:
    """译文被改过、引擎摇回的恰是上次写回值：同样是「没有新说法」。"""
    engine = _CachedEngine(cached=None, fresh="上次写回的。")
    item = _item(translation="手改中的。", applied="上次写回的。")
    with pytest.raises(TranslationError, match="给不出不同说法"):
        generate_alternative(engine, item)


def test_empty_result_raises() -> None:
    engine = _CachedEngine(cached=None, fresh="   ")
    with pytest.raises(TranslationError, match="没有返回"):
        generate_alternative(engine, _item())


def test_empty_source_raises() -> None:
    engine = _CachedEngine(cached=None)
    item = _item()
    item.source = "   "
    with pytest.raises(TranslationError, match="没有原文"):
        generate_alternative(engine, item)


# ---------------------------------------------------------------------------
# 三、候选收纳与选用
# ---------------------------------------------------------------------------


def test_add_candidate_dedupes_and_caps() -> None:
    item = _item("初译。", applied="上次写回的。")
    assert not add_candidate(item, "初译。")      # 与现译相同
    assert not add_candidate(item, "上次写回的。")  # 与上次写回值相同
    assert not add_candidate(item, "   ")          # 空

    assert add_candidate(item, "备选一。")
    assert not add_candidate(item, " 备选一。 ")    # 归一化后重复
    for text in ("备选二。", "备选三。", "备选四。"):
        assert add_candidate(item, text)
    assert item.candidates == ["备选二。", "备选三。", "备选四。"]
    assert len(item.candidates) == MAX_CANDIDATES


def test_pick_candidate_only_moves_translation() -> None:
    item = _item("初译。")
    add_candidate(item, "备选一。")
    assert not item.pending

    assert pick_candidate(item, 0) == "备选一。"
    assert item.translation == "备选一。"
    assert item.applied == "初译。"   # 成品里还是初译
    assert item.pending               # 选中 → 待写回，但还没落盘


def test_pick_candidate_out_of_range_raises() -> None:
    item = _item()
    with pytest.raises(TranslationError, match="没有这条备选"):
        pick_candidate(item, 0)


# ---------------------------------------------------------------------------
# 四、清单持久化与「不选不算通过」
# ---------------------------------------------------------------------------


def test_candidates_survive_roundtrip(tmp_path: Path) -> None:
    items = items_from_pairs([(FIRST, "初译。")], ["段落 1"], [{}], "docx", "w")
    add_candidate(items[0], "备选一。")
    path = dump_edit_list(str(tmp_path / "x_双语.docx"), items)
    back = load_edit_list(path)
    assert back[0].candidates == ["备选一。"]
    assert back[0].to_dict() == items[0].to_dict()


def test_old_list_without_candidates_loads(tmp_path: Path) -> None:
    payload = {
        "version": 1,
        "output": "x_双语.docx",
        "items": [
            {
                "id": "w1",
                "location": "段落 1",
                "source": FIRST,
                "translation": "初译。",
                "kind": "docx",
                "locator": {},
                "issues": [],
                "applied": "初译。",
            }
        ],
    }
    path = tmp_path / "x_双语 译后编辑清单.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert load_edit_list(str(path))[0].candidates == []


def test_format_edit_list_shows_candidates() -> None:
    item = _item()
    add_candidate(item, "备选一。")
    assert "备选1：备选一。" in format_edit_list([item])


class _FakeTranslator:
    stop_event = None

    def translate_texts(self, texts, progress=None):
        return ["初译原文。"] * len(texts)

    def summary(self) -> str:
        return "假译"


class _RecordingTM:
    def __init__(self) -> None:
        self.confirmed: list[tuple[str, str]] = []

    def confirm(self, source: str, target: str) -> None:
        self.confirmed.append((source, target))


def test_unpicked_candidate_never_reaches_output_or_tm(tmp_path: Path) -> None:
    doc = Document()
    doc.add_paragraph(FIRST)
    src = tmp_path / "sample.docx"
    doc.save(str(src))
    out = translate_word(str(src), _FakeTranslator(), log=lambda m: None)

    items = load_edit_list(edit_list_path(out))
    target = items[0]
    add_candidate(target, "备选说法。")
    dump_edit_list(out, items)

    # 不选 = 没改：待写回为空、成品不动、记忆库没被写。
    pending = [item for item in items if item.pending]
    assert pending == []
    tm = _RecordingTM()
    assert apply_edits(out, pending, tm=tm).applied == []
    assert tm.confirmed == []
    assert "备选说法。" not in [p.text for p in Document(out).paragraphs]

    # 选了 + 保存写回：成品里出现候选文字，人工译文进记忆。
    pick_candidate(target, 0)
    result = apply_edits(out, [target], tm=tm)
    assert result.applied == [target.id]
    assert "备选说法。" in [p.text for p in Document(out).paragraphs]
    assert tm.confirmed and tm.confirmed[0][1] == "备选说法。"


# ---------------------------------------------------------------------------
# 五、CLI --alternatives
# ---------------------------------------------------------------------------


def _prepare_output(tmp_path: Path) -> str:
    doc = Document()
    doc.add_paragraph(FIRST)
    src = tmp_path / "sample.docx"
    doc.save(str(src))
    return translate_word(str(src), _FakeTranslator(), log=lambda m: None)


def test_cli_alternatives_saves_candidates_without_writing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    out = _prepare_output(tmp_path)
    monkeypatch.setattr(app, "CLI_POSTEDIT", out)
    monkeypatch.setattr(app, "CLI_POSTEDIT_LIST", False)
    monkeypatch.setattr(app, "CLI_RETRANSLATE", "")
    monkeypatch.setattr(app, "CLI_ALTERNATIVES", "w1")
    monkeypatch.setattr(
        app, "_build_cli_translator", lambda log=None: _CachedEngine(None, "候选说法。")
    )

    code = app._run_postedit(out)
    assert code == 0
    printed = capsys.readouterr().out
    assert "备选：候选说法。" in printed or "候选说法。" in printed

    items = load_edit_list(edit_list_path(out))
    assert items[0].candidates == ["候选说法。"]
    assert not items[0].pending                     # 存候选 ≠ 写成品
    assert "候选说法。" not in [p.text for p in Document(out).paragraphs]


def test_cli_alternatives_reports_deterministic_engine(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    out = _prepare_output(tmp_path)
    same = load_edit_list(edit_list_path(out))[0].translation
    monkeypatch.setattr(app, "CLI_POSTEDIT", out)
    monkeypatch.setattr(app, "CLI_POSTEDIT_LIST", False)
    monkeypatch.setattr(app, "CLI_RETRANSLATE", "")
    monkeypatch.setattr(app, "CLI_ALTERNATIVES", "w1")
    monkeypatch.setattr(
        app, "_build_cli_translator", lambda log=None: _CachedEngine(None, same)
    )

    assert app._run_postedit(out) == 0
    printed = capsys.readouterr().out
    assert "没拿到备选" in printed and "给不出不同说法" in printed
    assert load_edit_list(edit_list_path(out))[0].candidates == []


def test_cli_alternatives_unknown_id_errors(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    out = _prepare_output(tmp_path)
    monkeypatch.setattr(app, "CLI_POSTEDIT", out)
    monkeypatch.setattr(app, "CLI_POSTEDIT_LIST", False)
    monkeypatch.setattr(app, "CLI_RETRANSLATE", "")
    monkeypatch.setattr(app, "CLI_ALTERNATIVES", "zz")
    monkeypatch.setattr(app, "_build_cli_translator", lambda log=None: _CachedEngine(None))

    assert app._run_postedit(out) == 1
    printed = capsys.readouterr().out
    assert "清单里没有这些段：zz" in printed


# ---------------------------------------------------------------------------
# 六、GUI 闭环（子进程：要备选 → 双击选用 → 保存写回）
# ---------------------------------------------------------------------------


_CHILD_GUI = r"""
import sys, tempfile
from pathlib import Path

sys.path.insert(0, r"%s")

from docx import Document

import app
from core.postedit import edit_list_path, load_edit_list
from core.word_translator import translate_word

FIRST = %r


class Fake:
    stop_event = None

    def translate_texts(self, texts, progress=None):
        return ["初译甲", "初译乙"]

    def summary(self):
        return "假译2段"


class AltEngine:
    stop_event = None

    def __init__(self):
        self.cache = type("C", (), {"gets": 0, "sets": 0,
                                    "get": lambda s, *a: "不该被读到",
                                    "set": lambda s, *a: None})()

    def translate_texts(self, texts, progress=None):
        return ["备选甲"]


work = Path(tempfile.mkdtemp())
src = work / "s.docx"
doc = Document()
doc.add_paragraph(FIRST)
doc.add_paragraph("The board approved the merger unanimously.")
doc.save(str(src))
out = translate_word(str(src), Fake(), log=lambda m: None)

for name in ("showinfo", "showwarning", "showerror"):
    setattr(app.messagebox, name, lambda *a, **k: None)

app._enable_dpi_awareness()
application = app.TranslatorApp()
application._make_translator = lambda **kw: AltEngine()
application.output_path = out
window = app.PostEditWindow(application)
window.window.update()
problems = []

window.only_issues.set(False)
window._refresh_list()
window.listbox.selection_set(0)
window._on_select()
window.window.update()

# 1. 初始没有备选，备选区给提示而不是空白
if window.cand_list.size() != 1:
    problems.append(f"空备选区行数={window.cand_list.size()}")
elif "还没有备选" not in window.cand_list.get(0):
    problems.append("空备选区没有提示")

# 2. 要一条备选：进备选区、进清单、成品不动
window._request_alternative()
window.window.update()
if window.current.candidates != ["备选甲"]:
    problems.append(f"候选没进内存={window.current.candidates}")
if window.cand_list.size() != 1:
    problems.append(f"备选区行数={window.cand_list.size()}")
on_disk = load_edit_list(edit_list_path(out))
if on_disk[0].candidates != ["备选甲"]:
    problems.append("候选没进清单")
if "备选甲" in [p.text for p in Document(out).paragraphs]:
    problems.append("要备选就动了成品")

# 3. 双击选用：填进译文框、标待写回，但成品仍未动
window.cand_list.selection_set(0)
window._use_candidate()
window.window.update()
if window.current is None:
    problems.append("选中项丢了")
elif "备选甲" not in window.trans_text.get("1.0", "end"):
    problems.append("译文框没填候选")
elif not window.current.pending:
    problems.append("选候选后不是待写回")
if "备选甲" in [p.text for p in Document(out).paragraphs]:
    problems.append("选用候选就动了成品（该等保存写回）")

# 4. 保存写回：成品出现候选文字、清单 applied 同步
window._save()
window.window.update()
texts = [p.text for p in Document(out).paragraphs]
if "备选甲" not in texts:
    problems.append("保存写回没生效")
if FIRST not in texts:
    problems.append("原文段被动了")
saved = load_edit_list(edit_list_path(out))
if saved[0].applied != "备选甲":
    problems.append("清单 applied 没同步")
if saved[0].candidates != ["备选甲"]:
    problems.append("写回后候选丢了数据")

if problems:
    print("FAIL:" + ";".join(problems))
    sys.exit(1)
print("ALT_GUI_OK")
"""


def test_gui_alternative_roundtrip() -> None:
    script = _CHILD_GUI % (str(ROOT), FIRST)
    result = subprocess.run(
        [str(PYTHON), "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    assert "ALT_GUI_OK" in result.stdout, (
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_bypass_cache_also_masks_built_fallback_layer() -> None:
    """本地大模型档的已建回退档有自己的一份缓存，只换外层等于没换。"""

    class _Fallback:
        def __init__(self) -> None:
            self.cache = _Cache("回退档缓存值")

    class _LLM:
        def __init__(self) -> None:
            self.cache = _Cache("主档缓存值")
            self._fallback = _Fallback()

    engine = _LLM()
    main_cache, fallback_cache = engine.cache, engine._fallback.cache
    with bypass_cache(engine):
        assert engine.cache.get("edge", "x") is None
        assert engine._fallback.cache.get("edge", "x") is None
    assert engine.cache is main_cache
    assert engine._fallback.cache is fallback_cache
