"""分片：把一大串段落按"场景边界"切开，而不是按固定行数。

为什么不能按固定行数切批次
--------------------------
对话是有场景的：角色在场关系、正在讨论的事、彼此的情绪，都随场景连续。
按"每 20 行一批"硬切，会把一段对峙切到两个批次里，后一个批次看不到
前因，模型只能盲猜代词指代——这就是"叙事断裂"的直接来源。

边界判定（两种前端共用一套骨架，只有标记不同）：

* **章节标记**（``第3章`` / ``Chapter 3`` / ``▼`` / ``#``）——硬边界；
* **空行分隔**——源文里的空行本身就是作者的分段意图；
* **超长保护**——一个场景超过 ``max_segments`` 也必须切，
  否则本地模型的上下文装不下，反而更差。

对话场景里说话人来回切换**不**构成边界：A→B→A→B 本来就是同一场对话。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 章节/场景标记。西式、日式、中文常见写法都覆盖。
CHAPTER_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*#{1,3}\s*\S"),
    re.compile(r"^\s*(?:第\s*[0-9零一二三四五六七八九十百千万]+\s*[章回節节])"),
    re.compile(r"^\s*(?:chapter|scene|act)\s*[0-9ivxlc]+\b", re.I),
    re.compile(r"^\s*[▼△▲■◆◇★☆※]"),
    re.compile(r"^\s*\[scene[^\]]*\]", re.I),
)

#: 场景 id 的前缀。故意用可读前缀而不是纯数字，日志里一眼能看出是哪个场景。
SCENE_PREFIX = "sc"


@dataclass
class Segment:
    """一个待译单元。文档模式只有 source；脚本模式带说话人。"""

    source: str
    segment_id: str = ""
    speaker: str = ""
    order: int = 0

    def __post_init__(self) -> None:
        if not self.segment_id:
            self.segment_id = f"s{self.order:05d}"


@dataclass
class Scene:
    """一批连续段落 + 它在场景序列里的位置。"""

    scene_id: str
    segments: list[Segment] = field(default_factory=list)
    index: int = 0
    speakers: tuple[str, ...] = ()

    @property
    def sources(self) -> list[str]:
        return [segment.source for segment in self.segments]

    def speaker_set(self) -> set[str]:
        names = set(self.speakers)
        names.update(segment.speaker for segment in self.segments if segment.speaker)
        return names


class SceneSegmenter:
    """按场景边界分片。``marker_res`` 可替换以适配不同源格式。"""

    def __init__(
        self,
        max_segments: int = 40,
        marker_res: tuple[re.Pattern[str], ...] = CHAPTER_RES,
        blank_line_breaks: bool = True,
    ) -> None:
        self.max_segments = max(1, max_segments)
        self.marker_res = marker_res
        self.blank_line_breaks = blank_line_breaks

    # ---------------------------------------------------------------- 判定

    def is_boundary(self, previous: str | None, current: str) -> bool:
        """previous→current 之间是否要切开。"""
        if previous is None:
            return False
        if not current.strip():
            return False  # 空行本身不是内容，交给 blank 判定
        if any(pattern.match(current) for pattern in self.marker_res):
            return True
        if self.blank_line_breaks and not previous.strip():
            # previous 是空行而 current 有内容：作者在这里分了段
            return True
        return False

    # ---------------------------------------------------------------- 分片

    def segment(self, segments: list[Segment]) -> list[Scene]:
        scenes: list[Scene] = []
        current: list[Segment] = []
        previous_text: str | None = None

        def flush() -> None:
            if not current:
                return
            index = len(scenes)
            speakers = sorted(
                {seg.speaker for seg in current if seg.speaker}
            )
            scenes.append(
                Scene(
                    scene_id=f"{SCENE_PREFIX}{index:04d}",
                    segments=list(current),
                    index=index,
                    speakers=tuple(speakers),
                )
            )
            current.clear()

        for segment in segments:
            boundary = self.is_boundary(previous_text, segment.source)
            if boundary and current:
                flush()
            elif current and len(current) >= self.max_segments:
                # 超长保护：上下文装不下比切错边界更糟
                flush()
            current.append(segment)
            previous_text = segment.source
        flush()
        return scenes


def segments_from_texts(
    texts: list[str], speakers: list[str] | None = None
) -> list[Segment]:
    """便捷构造：把纯文本列表变成 Segment 列表（保持顺序与下标）。"""
    result: list[Segment] = []
    for index, text in enumerate(texts):
        speaker = ""
        if speakers and index < len(speakers):
            speaker = (speakers[index] or "").strip()
        result.append(Segment(source=text, order=index, speaker=speaker))
    return result
