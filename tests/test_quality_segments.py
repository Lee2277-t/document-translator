"""分片器测试：场景边界判对，批次才不会切断叙事。"""

from __future__ import annotations

from core.quality.segments import (
    SceneSegmenter,
    Segment,
    segments_from_texts,
)


def _segments(*texts: str) -> list[Segment]:
    return segments_from_texts(list(texts))


def test_chapter_marker_starts_new_scene() -> None:
    scenes = SceneSegmenter().segment(_segments("opening line", "第2章", "next chapter"))
    assert len(scenes) == 2
    assert scenes[0].sources == ["opening line"]
    assert scenes[1].sources == ["第2章", "next chapter"]


def test_various_chapter_marker_styles() -> None:
    texts = [
        "a",
        "# heading",
        "b",
        "Chapter 3",
        "c",
        "▼ scene break",
        "d",
        "第十四回",
        "e",
    ]
    scenes = SceneSegmenter().segment(_segments(*texts))
    assert len(scenes) == 5, [scene.sources for scene in scenes]


def test_blank_line_is_boundary_but_speaker_change_is_not() -> None:
    """对话里 A→B→A 的说话人切换是同一场对话，不能切。"""
    segmenter = SceneSegmenter(blank_line_breaks=True)
    scenes = segmenter.segment(
        segments_from_texts(
            ["你好啊", "怎么啦", "没什么事"],
            speakers=["桜", "太郎", "桜"],
        )
    )
    assert len(scenes) == 1
    # speakers 按 Unicode 排序（太郎 U+592A < 桜 U+685C），保证输出稳定可复现
    assert scenes[0].speakers == ("太郎", "桜")

    # 空行是作者的分段意图，要切
    scenes = segmenter.segment(_segments("first block", "", "second block"))
    assert len(scenes) == 2


def test_blank_line_can_be_disabled() -> None:
    segmenter = SceneSegmenter(blank_line_breaks=False)
    scenes = segmenter.segment(_segments("first block", "", "second block"))
    assert len(scenes) == 1


def test_max_segments_protection() -> None:
    """超长场景必须切——上下文装不下比切错边界更糟。"""
    texts = [f"line {index}" for index in range(10)]
    scenes = SceneSegmenter(max_segments=4).segment(_segments(*texts))
    assert len(scenes) == 3
    assert [len(scene.segments) for scene in scenes] == [4, 4, 2]


def test_segment_ids_are_stable_and_unique() -> None:
    segments = segments_from_texts(["a", "b", "c"])
    ids = [segment.segment_id for segment in segments]
    assert ids == ["s00000", "s00001", "s00002"]
    assert len(set(ids)) == 3


def test_empty_input() -> None:
    assert SceneSegmenter().segment([]) == []


def test_scene_speakers_include_mentioned() -> None:
    scenes = SceneSegmenter().segment(
        segments_from_texts(["line one"], speakers=[""])
    )
    assert scenes[0].speaker_set() == set()
