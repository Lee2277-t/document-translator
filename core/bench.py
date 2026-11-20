"""质量量化：锚定测试集 + MQM 打分 + DeepL 对照基准（v1.5.2）。

为什么需要这一层
----------------
v1.5.0 之前，「哪个引擎质量更好」只能靠人读几段译文感觉——无法复盘、
无法回归、无法回答「换引擎到底提升了多少」。本模块把质量变成数字：

1. **锚定测试集**（``samples/quality_anchors.json``，44 条）：固定的一组
   外→中句子，按考察点分类（术语 / 数字 / 结构 / 专名 / 口语 / 多语种），
   每条带**自动判定依据**。固定 = 任何机器、任何时间跑出来的数字可对比。
2. **规则层打分**（零依赖、零网络、确定性）：术语一致（空白归一后包含
   判定——v1.4.1 的教训：``_tidy`` 在中英边界插空格，``AI技术``/`AI 技术`
   必须判同一个）、数字保真（``check_numbers`` 双口径 + 期望数字显式命中）、
   格式完整（占位符残留 / 漏译闸门 / 回声检测）。能用规则判的绝不麻烦模型，
   与 ``core/quality/verifier.py`` 同一原则。
3. **DeepL 对照基准**：DeepL 是业界顶尖商用引擎，把它当「质量上限」的
   参照系——其他引擎报「与基准的差距」，用户立刻知道自己在什么位置。
   没配 key 不静默跳过，报告里给出申请引导（复用 ``--paid=deepl`` 的文案）。
4. **LLM judge 层（可选）**：规则层判不了的流畅度 / 忠实度，用本机 Ollama
   打 1-5 分（``--judge``）。解析失败**不计分**也不抛异常——judge 挂了
   只是少一个维度，不能拖垮基准测量（verifier LLM 层的防御性解析同款）。

输出：控制台摘要 + 落盘 ``质量对比报告_YYYY-MM-DD.txt``。
入口：``app.py --bench``。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .api_keys import load_key
from .common import app_root
from .number_check import extract_numbers
from .quality.verifier import _find_placeholders

#: 打分维度 → 中文名。顺序即报告展示顺序。
DIMENSION_LABELS: tuple[tuple[str, str], ...] = (
    ("terminology", "术语一致"),
    ("numbers", "数字保真"),
    ("format", "格式完整"),
)

#: 默认锚定集位置（随项目走，不写死绝对路径）。
ANCHORS_PATH: Path = app_root() / "samples" / "quality_anchors.json"

#: 报告文件名前缀（按天一份，历史可对比）。
REPORT_PREFIX = "质量对比报告"

#: LLM judge 的系统提示。要求结构化输出，解析失败不计分。
JUDGE_SYSTEM = (
    "你是翻译质量审校。用户给出原文与译文，按两个维度各打 1-5 的整数分：\n"
    "fluency：译文是否流畅自然，像人话；\n"
    "fidelity：译文是否忠实于原文，无遗漏、无妄增。\n"
    "严格按 JSON 输出：{\"fluency\": n, \"fidelity\": n}，不要输出任何其他内容。"
)

#: judge 单条超时（秒）。1.7b 本地模型打分慢，给足。
JUDGE_TIMEOUT = 90.0


# ---------------------------------------------------------------- 锚定集


@dataclass
class Anchor:
    """一条锚点：原文 + 自动判定依据。"""

    id: str
    focus: list[str]
    source: str
    expect_terms: list[dict] = field(default_factory=list)
    expect_numbers: list[str] = field(default_factory=list)
    no_leftover: bool = True
    min_chars: int = 0
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Anchor":
        terms = []
        for item in data.get("expect_terms") or []:
            if isinstance(item, dict) and item.get("src") and item.get("dst"):
                terms.append({"src": str(item["src"]), "dst": str(item["dst"])})
        return cls(
            id=str(data.get("id") or "").strip(),
            focus=[str(name) for name in (data.get("focus") or [])],
            source=str(data.get("source") or "").strip(),
            expect_terms=terms,
            expect_numbers=[str(v) for v in (data.get("expect_numbers") or [])],
            no_leftover=bool(data.get("no_leftover", True)),
            min_chars=int(data.get("min_chars") or 0),
            notes=str(data.get("notes") or ""),
        )


def load_anchors(path: Path | str | None = None) -> list[Anchor]:
    """读锚定集。文件缺失 / 结构损坏直接抛——基准测量没有锚点就跑不了，
    静默返回空列表只会让用户以为测过了。"""
    target = Path(path) if path else ANCHORS_PATH
    data = json.loads(target.read_text(encoding="utf-8"))
    raw = data.get("anchors") if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"锚定集为空或格式不对：{target}")
    anchors = [Anchor.from_dict(item) for item in raw]
    ids = [anchor.id for anchor in anchors]
    if len(ids) != len(set(ids)):
        raise ValueError(f"锚定集 id 重复：{target}")
    missing = [anchor.id for anchor in anchors if not anchor.source]
    if missing:
        raise ValueError(f"锚点缺少原文：{missing}")
    return anchors


# ---------------------------------------------------------------- 打分


def _squash(text: str) -> str:
    """空白归一。v1.4.1 决策 49④：``_tidy`` 会在中英/中数字边界插空格，
    ``AI技术`` 与 `AI 技术` 是同一个东西，不归一就会批量误报术语未生效。
    """
    return re.sub(r"\s+", "", text or "")


def _num_key(value: str) -> str:
    """数字键：消除形态差（``3000.00``/``3000``、``3.50``/``3.5``、``-03``/``3``）。

    三件事都归一：
    1. **单位符号**：``extract_numbers`` 会把 ``4.6%`` 整串提出来（百分号吃进
       数字），期望值 ``4.6`` 必须能匹配上——剥掉尾部 ``%°€$£¥`` 再比；
    2. **负号**：日期 ``2024-03-15`` 会被拆成 ``-03``/``-15``（分隔符吃进
       数字），中文译文拆出 ``3``/``15``；摄氏「-5°C」与「零下 5°C」同理。
       真正的正负一致性由 ``check_numbers`` 的多重集合比对负责，本层只问
       「数字在不在」；
    3. **小数尾零**：``3.50`` 与 ``3.5`` 是同一个数。
    非数字串（版本号 2.1.0 之类）原样返回，让同口径提取自然匹配。
    """
    text = (value or "").strip()
    stripped = text.rstrip("%°€$£¥ \t")
    try:
        number = abs(float(stripped))
    except ValueError:
        return text
    if number == int(number):
        return str(int(number))
    return f"{number:.6g}"


def _expect_numbers_present(expect: Sequence[str], translation: str) -> bool:
    """期望数字是否都按原文形态保留在译文里。

    期望值也过 ``extract_numbers``——「2024-03-15」会拆成 2024/3/15，
    译文「2024 年 3 月 15 日」同样拆出这三个，同口径才谈得上命中。
    """
    want: set[str] = set()
    for value in expect:
        want.update(_num_key(item) for item in extract_numbers(value))
    if not want:
        return True
    got = {_num_key(item) for item in extract_numbers(translation)}
    return want <= got


@dataclass
class AnchorResult:
    """一条锚点在一个引擎上的结果。checks 的值：True 过 / False 挂 /
    None 不适用（该锚点没有这类期望）。"""

    anchor_id: str
    focus: list[str]
    translation: str
    checks: dict[str, bool | None] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def run_anchor(translator: Any, anchor: Anchor) -> AnchorResult:
    """用单个引擎翻一条锚点并逐维度判定。翻译器抛异常时该条记失败，
    不让整个基准测量中断——一个引擎翻不动某条本身就是质量信息。"""
    checks: dict[str, bool | None] = {name: None for name, _ in DIMENSION_LABELS}
    failures: list[str] = []
    translation = ""
    try:
        outputs = translator.translate_texts([anchor.source])
        translation = outputs[0] if outputs else ""
    except Exception as exc:
        failures.append(f"引擎调用失败：{type(exc).__name__}: {exc}")
        return AnchorResult(anchor.id, anchor.focus, "", checks, failures)

    # 术语一致：指定译法空白归一后出现在译文里
    if anchor.expect_terms:
        missing_terms = [
            term
            for term in anchor.expect_terms
            if _squash(term["dst"]) not in _squash(translation)
        ]
        checks["terminology"] = not missing_terms
        for term in missing_terms:
            failures.append(f"术语未生效：{term['src']} → {term['dst']}")

    # 数字保真：数值多重集合相等（_num_key 归一后）+ 期望数字显式命中。
    # 为什么不直接复用 check_numbers 的精确比对：它按字符串比对
    # 「3000.00」与「3000」、按 token 拆解「$4.2 billion」与「42亿美元」，
    # 数值相等却报缺失/新增（number_check 的货币+数量级联动是已知 TODO，
    # 见该模块 docstring）。bench 问的是「数值对不对」，口径随核心问题走；
    # 拼写数字（twenty three）两端都由 spelled_to_digits 归一，天然对齐。
    if anchor.expect_numbers:
        source_keys = Counter(_num_key(n) for n in extract_numbers(anchor.source))
        got_keys = Counter(_num_key(n) for n in extract_numbers(translation))
        present = _expect_numbers_present(anchor.expect_numbers, translation)
        checks["numbers"] = present and source_keys == got_keys
        if source_keys != got_keys:
            for key in list(source_keys - got_keys)[:3]:
                failures.append(f"数字丢失：{key}")
            for key in list(got_keys - source_keys)[:3]:
                failures.append(f"数字妄增：{key}")
        if not present:
            for value in anchor.expect_numbers:
                if _num_key(value) not in got_keys:
                    failures.append(f"数字未保留：{value}")

    # 格式完整：占位符残留 / 漏译闸门 / 回声（引擎没翻）
    cjk = len(re.findall(r"[\u4e00-\u9fff]", translation))
    placeholders = _find_placeholders(translation) if anchor.no_leftover else []
    echo = translation.strip() == anchor.source.strip()
    format_ok = (
        bool(translation.strip())
        and not placeholders
        and cjk >= anchor.min_chars
        and not echo
    )
    checks["format"] = format_ok
    if not translation.strip():
        failures.append("译文为空")
    if echo:
        failures.append("译文与原文相同（引擎未翻译）")
    if placeholders:
        failures.append(f"占位符残留：{'、'.join(placeholders[:3])}")
    if translation.strip() and cjk < anchor.min_chars:
        failures.append(f"译文过短（中文 {cjk} 字 < {anchor.min_chars}），疑漏译")

    return AnchorResult(anchor.id, anchor.focus, translation, checks, failures)


@dataclass
class EngineScore:
    """一个引擎的得分卡。rate = 通过数 / 适用数（None 的维度不计入）。"""

    name: str
    label: str
    passed: dict[str, int] = field(default_factory=dict)
    applicable: dict[str, int] = field(default_factory=dict)
    errors: int = 0

    def rate(self, dimension: str) -> float | None:
        total = self.applicable.get(dimension, 0)
        if not total:
            return None
        return self.passed.get(dimension, 0) / total

    @property
    def overall(self) -> float:
        rates = [
            rate
            for rate in (self.rate(name) for name, _ in DIMENSION_LABELS)
            if rate is not None
        ]
        return sum(rates) / len(rates) if rates else 0.0


def score_engine(name: str, label: str, results: Sequence[AnchorResult]) -> EngineScore:
    score = EngineScore(name=name, label=label)
    for result in results:
        if not result.translation and any(result.failures):
            score.errors += 1
        for dimension, _ in DIMENSION_LABELS:
            value = result.checks.get(dimension)
            if value is None:
                continue
            score.applicable[dimension] = score.applicable.get(dimension, 0) + 1
            if value:
                score.passed[dimension] = score.passed.get(dimension, 0) + 1
    return score


@dataclass
class BenchRun:
    """一次基准测量的完整结果。"""

    scores: dict[str, EngineScore] = field(default_factory=dict)
    details: dict[str, list[AnchorResult]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def run_bench(
    engines: Sequence[tuple[str, str, Any]],
    anchors: Sequence[Anchor],
    progress: Callable[[str, int, int], None] | None = None,
) -> BenchRun:
    """对每个引擎跑完整锚定集。engines 形如 [(name, label, translator)]。"""
    run = BenchRun()
    total = len(anchors)
    for name, label, translator in engines:
        results: list[AnchorResult] = []
        for index, anchor in enumerate(anchors, start=1):
            results.append(run_anchor(translator, anchor))
            if progress:
                progress(label, index, total)
        run.scores[name] = score_engine(name, label, results)
        run.details[name] = results
    return run


# ---------------------------------------------------------------- LLM judge


def _extract_json(raw: str) -> str | None:
    """从模型输出里抓第一个 JSON 对象。模型爱裹 ```json 或加前后废话。"""
    text = (raw or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    return text[start : end + 1]


def make_ollama_judge(
    model: str, base_url: str = "http://localhost:11434/v1"
) -> Callable[[str, str], dict | None]:
    """造一个 judge_fn(source, translation) -> {"fluency": n, "fidelity": n}。

    防御性解析：任何失败（网络 / 格式 / 越界）都返回 None——该条不计分。
    judge 是基准测量的选修层，它的失败不能污染必考层。
    """
    from .local_llm import _http_post_json

    def judge(source: str, translation: str) -> dict | None:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM},
                {
                    "role": "user",
                    "content": f"原文：{source}\n译文：{translation}",
                },
            ],
            "stream": False,
            "temperature": 0.0,
        }
        try:
            data = _http_post_json(
                f"{base_url.rstrip('/')}/chat/completions", payload, JUDGE_TIMEOUT
            )
            raw = data["choices"][0]["message"]["content"]
            parsed = json.loads(_extract_json(raw) or "")
            fluency = int(parsed["fluency"])
            fidelity = int(parsed["fidelity"])
        except Exception:
            return None
        if not (1 <= fluency <= 5 and 1 <= fidelity <= 5):
            return None
        return {"fluency": fluency, "fidelity": fidelity}

    return judge


def run_judge(
    judge_fn: Callable[[str, str], dict | None],
    run: BenchRun,
    anchors: Sequence[Anchor],
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, dict[str, float]]:
    """对每个引擎的每条译文打分，返回 {engine: {"fluency": 均分, "fidelity": 均分,
    "scored": 条数}}。解析失败的条目不进均分。"""
    source_by_id = {anchor.id: anchor.source for anchor in anchors}
    judged: dict[str, dict[str, float]] = {}
    total = sum(len(run.details.get(name, [])) for name in run.scores)
    done = 0
    for name in run.scores:
        fluency_sum = fidelity_sum = scored = 0
        for result in run.details.get(name, []):
            done += 1
            if progress:
                progress("judge", done, total)
            if not result.translation:
                continue
            verdict = judge_fn(source_by_id.get(result.anchor_id, ""), result.translation)
            if not verdict:
                continue
            fluency_sum += verdict["fluency"]
            fidelity_sum += verdict["fidelity"]
            scored += 1
        if scored:
            judged[name] = {
                "fluency": round(fluency_sum / scored, 2),
                "fidelity": round(fidelity_sum / scored, 2),
                "scored": scored,
            }
    return judged


# ---------------------------------------------------------------- 报告


def _pad(text: str, width: int) -> str:
    """按东亚宽度填充——中文占两列，不然表头永远对不齐。"""
    actual = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(1, width - actual)


def _pct(rate: float | None) -> str:
    return "  —  " if rate is None else f"{rate * 100:5.1f}%"


def render_report(
    run: BenchRun,
    anchors: Sequence[Anchor],
    judge: dict[str, dict[str, float]] | None = None,
    baseline: str = "deepl",
) -> str:
    """纯文本对比报告。基准行单独一块，差距列让人一眼看到位置。"""
    lines: list[str] = []
    lines.append("═" * 64)
    lines.append(f" 质量对比报告 · 锚定测试集（{len(anchors)} 条）· v1.5.2")
    lines.append("═" * 64)
    lines.append("")

    header = (
        _pad("引擎", 22)
        + "".join(_pad(label, 10) for _, label in DIMENSION_LABELS)
        + _pad("综合", 9)
    )
    lines.append(header)
    lines.append("─" * 64)

    baseline_score = run.scores.get(baseline)
    for name, score in run.scores.items():
        row = _pad(score.label, 22)
        for dimension, _ in DIMENSION_LABELS:
            row += _pad(_pct(score.rate(dimension)), 10)
        row += _pct(score.overall)
        lines.append(row)

    if baseline_score is not None:
        lines.append("─" * 64)
        lines.append(
            _pad(f"与 {baseline_score.label} 基准差距", 22) + "（百分点，负 = 不如基准）"
        )
        for name, score in run.scores.items():
            if name == baseline:
                continue
            row = _pad(f"  {score.label}", 22)
            for dimension, _ in DIMENSION_LABELS:
                mine = score.rate(dimension)
                base = baseline_score.rate(dimension)
                if mine is None or base is None:
                    row += _pad("—", 10)
                else:
                    row += _pad(f"{(mine - base) * 100:+6.1f}", 10)
            row += f"{(score.overall - baseline_score.overall) * 100:+6.1f}"
            lines.append(row)
    else:
        lines.append("─" * 64)
        lines.append("对照基准缺失：未配置 DeepL key（或未指定 deepl 引擎）。")
        lines.append("配置方法：在 api_keys.local.json 填 deepl.key，申请地址")
        lines.append("https://www.deepl.com/pro-api （Free 版每月 50 万字符免费额度）。")

    if judge:
        lines.append("")
        lines.append("─" * 64)
        lines.append(_pad("LLM judge（本地大模型，1-5 分）", 22) + _pad("流畅", 8) + "忠实")
        for name, verdict in judge.items():
            label = run.scores[name].label if name in run.scores else name
            lines.append(
                _pad(label, 22)
                + _pad(f"{verdict['fluency']:.1f}", 8)
                + f"{verdict['fidelity']:.1f}"
                + f"（{verdict['scored']} 条参评）"
            )

    # 失败明细：每个引擎最多列 12 条，按锚点顺序（编号即重要度）
    detail_shown = False
    for name, score in run.scores.items():
        failures = [r for r in run.details.get(name, []) if r.failures]
        if not failures:
            continue
        if not detail_shown:
            lines.append("")
            lines.append("─" * 64)
            lines.append("失败明细（每引擎最多 12 条）")
            detail_shown = True
        lines.append(f"【{score.label}】")
        for result in failures[:12]:
            lines.append(
                f"  {result.anchor_id}（{'/'.join(result.focus)}）："
                + "；".join(result.failures)
            )
        hidden = len(failures) - 12
        if hidden > 0:
            lines.append(f"  ……另有 {hidden} 条未列出")

    for note in run.notes:
        lines.append(f"备注：{note}")
    lines.append("")
    lines.append("口径说明：术语一致=指定译法空白归一后命中；数字保真=自动比对无")
    lines.append("缺失新增且期望数字显式命中；格式完整=无占位符残留、过长度闸门、")
    lines.append("非回声。综合=三维度等权平均（不适用维度不计入）。")
    lines.append("═" * 64)
    return "\n".join(lines)


# ---------------------------------------------------------------- 引擎装配


def build_translators(
    requested: Sequence[str],
    log: Callable[[str], None] | None = None,
    model: str | None = None,
) -> tuple[list[tuple[str, str, Any]], list[str]]:
    """按名字构造翻译器。返回 ([(name, label, translator)], 跳过原因)。

    不可用的引擎**不静默跳过**——原因收集进 notes 写进报告，用户知道
    谁没参跑、为什么（本地大模型没拉模型 / DeepL 没配 key）。
    """
    engines: list[tuple[str, str, Any]] = []
    notes: list[str] = []
    for name in requested:
        try:
            if name == "offline":
                from .local_translator import LocalTranslator

                engines.append(("offline", "离线小模型", LocalTranslator(log=log)))
            elif name == "cloud":
                from .translator import Translator

                engines.append(
                    ("cloud", "免费云端", Translator(cache_path=None, log=log))
                )
            elif name == "local-llm":
                from .local_llm import LocalLLMTranslator, probe_local_llm

                engine, reason = probe_local_llm(model=model, log=log)
                if engine is None:
                    notes.append(
                        f"本地大模型未参跑：{reason}。"
                        f"可用 --model= 指定本机已拉的模型。"
                    )
                    continue
                engines.append(
                    (
                        "local-llm",
                        f"本地大模型（{engine.model}）",
                        LocalLLMTranslator(
                            model=model,
                            cache_path=None,
                            quality=False,
                            log=log,
                        ),
                    )
                )
            elif name == "deepl":
                from .deepl import DeepLTranslator

                key_config = load_key("deepl")
                if not key_config:
                    notes.append(
                        "DeepL 未参跑：未配置 key。申请地址 "
                        "https://www.deepl.com/pro-api ，填进 "
                        "api_keys.local.json 的 deepl.key。"
                    )
                    continue
                engines.append(
                    ("deepl", "DeepL（基准）", DeepLTranslator(key_config, cache=False, log=log))
                )
            else:
                notes.append(f"未知引擎：{name}（可选 offline/cloud/local-llm/deepl）")
        except Exception as exc:
            notes.append(f"引擎 {name} 构造失败：{type(exc).__name__}: {exc}")
    return engines, notes
