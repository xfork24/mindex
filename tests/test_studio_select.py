from __future__ import annotations

import pytest

from mindex.config import SelectionConfig
from mindex.studio_select import SelectionError, select_events


def make_library(ranges: list[tuple[float, float]]):
    """合成单一原片的 shot 表：连续区间 + 相邻/覆盖查询回调。"""
    shots = [{"id": f"seg-{index}", "start": start, "end": end} for index, (start, end) in enumerate(ranges)]
    total = ranges[-1][1]

    def source_duration(_source_id):
        return total

    def extend_next(_source_id, end):
        later = [shot for shot in shots if shot["end"] > end + 1e-6]
        return min(later, key=lambda shot: shot["end"]) if later else None

    def extend_prev(_source_id, start):
        earlier = [shot for shot in shots if shot["start"] < start - 1e-6]
        return max(earlier, key=lambda shot: shot["start"]) if earlier else None

    def covered(_source_id, start, end):
        return [shot["id"] for shot in shots if shot["start"] < end - 1e-6 and shot["end"] > start + 1e-6]

    return source_duration, extend_next, extend_prev, covered


def candidate(source_id, start, end, rank, segment_id=None):
    return {
        "segment_id": segment_id or f"{source_id}-{start}-{end}",
        "source_id": source_id,
        "source_name": source_id,
        "start": start,
        "end": end,
        "duration": round(end - start, 3),
        "score": 0.01 / rank,
        "rank": rank,
    }


def run_select(shots, candidates, audio, ranges, selection=None):
    selection = selection or SelectionConfig()
    callbacks = make_library(ranges)
    return select_events(
        shots,
        candidates,
        audio,
        selection,
        source_duration=callbacks[0],
        extend_next=callbacks[1],
        extend_prev=callbacks[2],
        covered_shots=callbacks[3],
    )


def test_nearest_candidate_without_extension():
    events, warnings = run_select(
        [{"id": "shot-01", "text": "文本"}],
        {"shot-01": [candidate("src", 0, 4, 1)]},
        {"shot-01": 4.0},
        [(0, 4), (4, 8)],
    )
    event = events[0]
    assert event.video.alignment == "nearest"
    assert event.video.segment_ids == ["seg-0"]
    assert event.audio.atempo == 1.0 and event.audio.render_duration == 4.0
    assert warnings == []


def test_forward_extension_crosses_contiguous_shots():
    events, _ = run_select(
        [{"id": "shot-01", "text": "文本"}],
        {"shot-01": [candidate("src", 0, 2, 1)]},
        {"shot-01": 4.0},
        [(0, 2), (2, 6)],
    )
    event = events[0]
    assert event.video.alignment == "extended"
    assert (event.video.start, event.video.end) == (0.0, 4.0)  # 区间可落在 shot 中间
    assert event.video.segment_ids == ["seg-0", "seg-1"]
    assert event.audio.atempo == 1.0


def test_backward_extension_inside_anchor_shot():
    events, _ = run_select(
        [{"id": "shot-01", "text": "文本"}],
        {"shot-01": [candidate("src", 6, 8, 1)]},
        {"shot-01": 4.0},
        [(0, 4), (4, 8)],
    )
    event = events[0]
    assert (event.video.start, event.video.end) == (4.0, 8.0)
    assert event.video.segment_ids == ["seg-1"]
    assert event.video.alignment == "extended"


def test_trimmed_when_video_much_longer_than_audio():
    events, warnings = run_select(
        [{"id": "shot-01", "text": "文本"}],
        {"shot-01": [candidate("src", 0, 10, 1)]},
        {"shot-01": 5.0},
        [(0, 10)],
    )
    event = events[0]
    assert event.video.alignment == "trimmed"
    assert event.audio.atempo == pytest.approx(0.9)  # 钳制到 1-0.1
    assert event.audio.render_duration == pytest.approx(5.0 / 0.9, abs=1e-3)
    assert event.warning and warnings


def test_padded_when_source_too_short():
    events, warnings = run_select(
        [{"id": "shot-01", "text": "文本"}],
        {"shot-01": [candidate("src", 0, 3, 1)]},
        {"shot-01": 8.0},
        [(0, 3)],
    )
    event = events[0]
    assert event.video.alignment == "padded"
    assert event.audio.atempo == pytest.approx(1.1)
    assert event.audio.render_duration == pytest.approx(8.0 / 1.1, abs=1e-3)
    assert warnings


def test_missing_candidates_raise_aggregated_error():
    with pytest.raises(SelectionError, match="shot-02"):
        run_select(
            [{"id": "shot-01", "text": "甲"}, {"id": "shot-02", "text": "乙"}],
            {"shot-01": [candidate("src", 0, 4, 1)], "shot-02": []},
            {"shot-01": 4.0, "shot-02": 4.0},
            [(0, 4), (4, 8)],
        )


def test_degenerate_candidates_only():
    with pytest.raises(SelectionError, match="无法用于选片"):
        run_select(
            [{"id": "shot-01", "text": "文本"}],
            {"shot-01": [candidate("src", 2.0, 2.02, 1)]},  # 时长 0.02s 的退化候选
            {"shot-01": 4.0},
            [(0, 4)],
        )


def test_repeat_penalty_pushes_to_alternative_source():
    selection = SelectionConfig(
        weights={"semantic": 0.3, "duration": 0.3, "extension": 0.0, "repeat": 1.0, "overlap": 1.0}
    )
    shots = [{"id": "shot-01", "text": "甲"}, {"id": "shot-02", "text": "乙"}]
    candidates = {
        "shot-01": [candidate("s1", 0, 4, 1), candidate("s2", 0, 4, 2)],
        "shot-02": [candidate("s1", 0, 4, 1), candidate("s2", 0, 4, 2)],
    }
    events, _ = run_select(shots, candidates, {"shot-01": 4.0, "shot-02": 4.0}, [(0, 4)], selection)
    assert events[0].video.source_id == "s1"  # 第一镜选语义最优
    assert events[1].video.source_id == "s2"  # 来源重复惩罚使其换源


def test_overlap_penalty_avoids_same_source_range():
    selection = SelectionConfig(
        weights={"semantic": 0.3, "duration": 0.3, "extension": 0.0, "repeat": 0.0, "overlap": 1.0}
    )
    shots = [{"id": "shot-01", "text": "甲"}, {"id": "shot-02", "text": "乙"}]
    # shot-02 的 rank1 候选与 shot-01 已选区间重叠，rank2 候选不重叠
    candidates = {
        "shot-01": [candidate("s1", 0, 4, 1)],
        "shot-02": [candidate("s1", 2, 6, 1), candidate("s1", 4, 8, 2)],
    }
    events, _ = run_select(shots, candidates, {"shot-01": 4.0, "shot-02": 4.0}, [(0, 8)], selection)
    assert (events[1].video.start, events[1].video.end) == (4.0, 8.0)


def test_subtitle_spans_render_duration():
    events, _ = run_select(
        [{"id": "shot-01", "text": "字幕文本"}],
        {"shot-01": [candidate("src", 0, 4, 1)]},
        {"shot-01": 4.0},
        [(0, 4)],
    )
    assert events[0].subtitle.text == "字幕文本"
    assert events[0].subtitle.start == 0.0
    assert events[0].subtitle.end == events[0].audio.render_duration
