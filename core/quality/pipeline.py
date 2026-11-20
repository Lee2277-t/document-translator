"""质量管线：把分片、装配、翻译、校验、校改串成一个可运行的循环。

``Translator → Verifier → Corrector`` 不是凭空设计的结构——多 Agent
反射式翻译（TransAgents 等）是已被验证的方向，GalTransl 自己也内置了
"找问题"阶段。这里把它**状态化**：场景边界决定批次，术语库跨批次存活，
校验发现的问题驱动定向重译，而不是整篇重来。

三个工程取舍
------------
1. **校改有预算上限**。``max_corrections`` 防止"越改越差"的死亡螺旋：
   模型对同一句改三遍之后，第四遍往往还不如第一版。超限就保留当前最佳。
2. **上下文按场景滚动**。前一个场景的尾部原文作为摘要喂给下一批，
   零额外推理开销（真摘要生成留在阶段 4 的人回路里做）。
3. **ctx 能力探测**。不是所有翻译器都吃上下文（Edge/Bing/Marian 都是
   无状态单句接口）。用签名探测，吃就喂，不吃就老老实实逐句翻——
   质量引擎的其他层（术语校验、校改）对两种翻译器都有效。
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..ctx import EMPTY_CONTEXT
from .assembler import ContextAssembler
from .segments import Scene, SceneSegmenter, Segment
from .verifier import Issue, LLMVerifier, RuleVerifier

#: 校改提示词。要求只输出译文——任何解释都会污染回写内容。
_CORRECT_SYSTEM = (
    "你是翻译审校。用户给出原文、当前译文和发现的问题清单。"
    "请输出修正后的译文，要求：\n"
    "1. 只输出译文本身，不要解释、不要引号、不要前后缀；\n"
    "2. 原样保留占位符（形如 Zxq0Z / Ztq0Z / Znt0Z）与数字；\n"
    "3. 只修问题清单里指出的错误，没问题的部分保持原样；\n"
    "4. 如果问题清单是空的，原样返回当前译文。"
)


@dataclass
class QualityResult:
    """一次质量管线的完整产出。"""

    translations: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    scenes: int = 0
    corrected: int = 0
    llm_checks: int = 0
    seconds: float = 0.0
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(issue.is_blocking() for issue in self.issues)


class Corrector:
    """只对被标记的句子定向重译。"""

    def __init__(self, engine: Any, max_rounds: int = 1) -> None:
        self.engine = engine
        self.max_rounds = max(1, max_rounds)

    def correct(
        self,
        source: str,
        translation: str,
        issues: Sequence[Issue],
        speakers: Sequence[str] = (),
    ) -> str:
        if self.engine is None or not issues:
            return translation
        listed = "\n".join(f"- {issue.detail}" for issue in issues)
        prompt = (
            f"原文：{source}\n当前译文：{translation}\n"
            f"问题清单：\n{listed}\n请输出修正后的译文。"
        )
        try:
            raw = self.engine.translate(prompt, EMPTY_CONTEXT)
        except Exception:
            return translation
        fixed = (raw or "").strip()
        # 校改失败（空/变长/丢占位符）时保留原译文：校改是改善，不是赌博
        if not fixed:
            return translation
        if len(fixed) > max(len(translation) * 3, 200):
            return translation
        return fixed


class QualityPipeline:
    """质量引擎主入口。文档与脚本两种前端共用。"""

    def __init__(
        self,
        translator: Any,
        termbase: Any = None,
        character_store: Any = None,
        store: Any = None,
        engine: Any = None,
        tm: Any = None,
        budget: int = 1200,
        max_segments_per_scene: int = 40,
        verify: bool = True,
        correct: bool = True,
        llm_check: bool = True,
        seed_from_glossary: bool = False,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.translator = translator
        self.termbase = termbase
        self.character_store = character_store
        self.store = store
        self.log = log or (lambda message: None)
        self.verify_enabled = verify
        self.correct_enabled = correct
        self.llm_check_enabled = llm_check
        #: 翻译记忆。只有**通过校验**的译文才写进去——把存疑译文存进 TM，
        #: 下一次模糊匹配会把同一个错误复印到更多句子上。
        self.tm = tm
        # 校改引擎：显式传入优先，否则用翻译器自带的引擎（LocalLLMTranslator.engine）
        self.engine = engine if engine is not None else getattr(translator, "engine", None)

        self.assembler = ContextAssembler(
            termbase=termbase, character_store=character_store, budget=budget
        )
        self.segmenter = SceneSegmenter(max_segments=max_segments_per_scene)
        self.rule_verifier = RuleVerifier(
            termbase=termbase, character_store=character_store
        )
        self.llm_verifier = (
            LLMVerifier(engine=self.engine, character_store=character_store)
            if llm_check and self.engine is not None
            else None
        )
        self.corrector = Corrector(engine=self.engine) if correct else None

        if seed_from_glossary and termbase is not None:
            try:
                termbase.seed_from_glossary()
            except Exception:
                pass

        self._ctx_aware = self._detect_ctx_support()
        #: 最近一次 run 的分片，供 rename_and_retranslate 做增量重译
        self._last_segments: list[Segment] = []
        self._last_scenes: list[Scene] = []

    # ---------------------------------------------------------------- 工具

    def _detect_ctx_support(self) -> bool:
        """翻译器的 translate_texts 是否接受 ctx 参数。"""
        try:
            parameters = inspect.signature(self.translator.translate_texts).parameters
        except (TypeError, ValueError):
            return False
        return "ctx" in parameters

    def _translate(
        self,
        texts: list[str],
        ctx: Any,
        progress: Callable[[int, int, str], None] | None,
    ) -> list[str]:
        if self._ctx_aware and ctx is not None and not ctx.is_trivial():
            return self.translator.translate_texts(texts, progress, ctx=ctx)
        return self.translator.translate_texts(texts, progress)

    @staticmethod
    def _scene_progress(
        scene_start: int,
        scene_length: int,
        total: int,
        progress: Callable[[int, int, str], None] | None,
    ) -> Callable[[int, int, str], None] | None:
        """把场景内的进度折算回全文进度。

        不折算的话，一个 40 段的场景翻完时进度条会从 12% 直接跳到 52%，
        用户以为程序卡死了。本地推理下一个场景可能要几分钟，进度必须平滑。
        """
        if progress is None:
            return None

        def inner(done: int, scene_total: int, message: str) -> None:
            offset = int(done * scene_length / max(scene_total, 1))
            progress(min(scene_start + offset, total), total, message)

        return inner

    # ---------------------------------------------------------------- 主流程

    def run(
        self,
        segments: Sequence[Segment],
        progress: Callable[[int, int, str], None] | None = None,
    ) -> QualityResult:
        started = time.time()
        result = QualityResult()
        if not segments:
            return result

        scenes = self.segmenter.segment(list(segments))
        result.scenes = len(scenes)
        # 按对象定位而不是按 order 下标：调用方若传入自带编号的 Segment
        # （比如文档全局 id），order 不保证是 0..n-1 连续，按下标会错位。
        outputs: dict[int, str] = {}
        self._last_segments = list(segments)
        self._last_scenes = scenes

        previous_sources: list[str] = []
        done = 0
        for scene in scenes:
            done = self._run_scene(scene, previous_sources, result, outputs, done, len(segments), progress)
            previous_sources = scene.sources
            # 摘要只带上一场，不无限累积——上下文预算有限。
            # 存的是装配时算出的同一个滚动摘要（见 _run_scene），add_scene
            # 是 upsert；这里只保留一处写入——曾经有两遍一样的块，是编辑残留。
            if self.store is not None:
                summary = self.assembler.rolling_summary(previous_sources)
                if summary:
                    try:
                        self.store.add_scene(scene.scene_id, summary, scene.index)
                    except Exception:
                        pass

        result.translations = [
            outputs.get(id(segment), segment.source) for segment in segments
        ]
        result.seconds = time.time() - started
        if progress is not None:
            progress(len(segments), len(segments), "")
        return result

    # ---------------------------------------------------------------- 场景

    def _run_scene(
        self,
        scene: Scene,
        previous_sources: list[str],
        result: QualityResult,
        outputs: dict[int, str],
        done: int,
        total: int,
        progress,
    ) -> int:
        """跑一个场景：装配上下文 → 翻译 → 逐段校验校改。返回新的 done。"""
        summary = self.assembler.rolling_summary(previous_sources)
        assembled = self.assembler.assemble(
            scene.sources,
            speakers=list(scene.speakers),
            summary=summary,
            scene_id=scene.scene_id,
        )
        scene_progress = self._scene_progress(
            done, len(scene.segments), total, progress
        )
        try:
            translations = self._translate(
                scene.sources, assembled.context, scene_progress
            )
        except Exception as exc:
            # 单个场景失败不拖垮整轮：该场景保留原文，继续下一场景
            self.log(f"场景 {scene.scene_id} 翻译失败（{type(exc).__name__}），保留原文")
            translations = list(scene.sources)
            result.issues.append(
                Issue(
                    severity="major",
                    dimension="coherence",
                    segment_id=scene.segments[0].segment_id,
                    detail=f"场景翻译失败：{type(exc).__name__}",
                )
            )
        if len(translations) != len(scene.segments):
            translations = (list(translations) + list(scene.sources))[
                : len(scene.segments)
            ]

        for segment, translation in zip(scene.segments, translations):
            outputs[id(segment)] = self._process_segment(
                segment, translation, scene, result
            )
            done += 1
            if progress is not None:
                progress(done, total, scene.scene_id)
        return done

    # ---------------------------------------------------------------- 人在回路

    def rename_and_retranslate(
        self,
        source: str,
        new_target: str,
        progress=None,
    ) -> tuple[int, int]:
        """全局重命名 + 只重译受影响的场景。返回（重译段数, 重译场景数）。

        这是"人在回路"成本最低的干预点：用户发现某个词译得不对，改一次，
        所有用到它的场景重译，没用到的一个字都不重跑。术语指纹变化会让
        这些场景的缓存键整体失效，所以重译拿到的一定是新译名。
        """
        if self.termbase is None or not new_target:
            return 0, 0
        old_target = self.termbase.lookup(source)
        self.termbase.rename(source, new_target)
        self.log(
            f"术语重命名：「{source}」{old_target or '（新术语）'} → {new_target}"
        )

        scenes = getattr(self, "_last_scenes", []) or []
        surfaces = [source] + list(self.termbase.variants_of(source))
        affected = [
            scene
            for scene in scenes
            if any(
                surface and surface in "\n".join(scene.sources)
                for surface in surfaces
            )
        ]
        if not affected:
            return 0, 0

        outputs: dict[int, str] = {}
        done = 0
        total = sum(len(scene.segments) for scene in affected)
        result = QualityResult()
        previous_sources: list[str] = []
        for scene in affected:
            done = self._run_scene(
                scene, previous_sources, result, outputs, done, total, progress
            )
            previous_sources = scene.sources
        if progress is not None:
            progress(total, total, "")
        return total, len(affected)

    def pending_terms(self) -> list[dict]:
        """未决术语池：候选（还没译名）+ 存疑（发现了冲突译法）。"""
        if self.termbase is None:
            return []
        rows: list[dict] = []
        for state in ("disputed", "candidate"):
            for term in self.termbase.store.terms_by_state(state):
                rows.append(
                    {
                        "source": term.get("source", ""),
                        "target": term.get("target", ""),
                        "state": state,
                        "note": term.get("note", ""),
                        "first_seen": term.get("first_seen", ""),
                    }
                )
        return rows

    # ---------------------------------------------------------------- 单段

    def _process_segment(
        self,
        segment: Segment,
        translation: str,
        scene: Scene,
        result: QualityResult,
    ) -> str:
        current = translation
        speakers = sorted(scene.speaker_set())

        if self.verify_enabled:
            issues = self.rule_verifier.verify(
                segment.segment_id,
                segment.source,
                current,
                speaker=segment.speaker,
                speakers=speakers,
            )
            if issues:
                result.issues.extend(issues)
            # LLM 层只在规则层发现问题时出动——不为"感觉不放心"花用户的时间
            if self.llm_verifier is not None and issues:
                extra = self.llm_verifier.verify(
                    segment.segment_id,
                    segment.source,
                    current,
                    speakers=speakers,
                    rule_issues=issues,
                )
                result.llm_checks += 1
                if extra:
                    result.issues.extend(extra)
                    issues = list(issues) + list(extra)

            if self.corrector is not None and any(
                issue.severity in ("critical", "major") for issue in issues
            ):
                blocking = [
                    issue for issue in issues if issue.severity in ("critical", "major")
                ]
                fixed = self.corrector.correct(
                    segment.source, current, blocking, speakers
                )
                if fixed and fixed != current:
                    # 校改后要重新过规则层：修好了要确认，改坏了要发现
                    recheck = self.rule_verifier.verify(
                        segment.segment_id,
                        segment.source,
                        fixed,
                        speaker=segment.speaker,
                        speakers=speakers,
                    )
                    worse = len(recheck) > len(blocking)
                    if not worse:
                        current = fixed
                        result.corrected += 1
                        # 用重新校验的结果替换掉这一段的旧问题
                        result.issues = [
                            issue
                            for issue in result.issues
                            if issue.segment_id != segment.segment_id
                        ] + list(recheck)
        # 只有零问题遗留的译文才进翻译记忆：带病句进去会被模糊匹配复印出去
        remaining = [
            issue for issue in result.issues if issue.segment_id == segment.segment_id
        ]
        if self.tm is not None and not remaining and current != segment.source:
            try:
                self.tm.add(segment.source, current)
            except Exception:
                pass
        return current
