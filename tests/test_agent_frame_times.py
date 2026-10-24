"""sample_frames 抽帧时间点的性质测试。"""

from __future__ import annotations

import itertools

from hypothesis import given, settings
from hypothesis import strategies as st

from mindex.agent.tools.search import MAX_FRAMES, frame_times, frame_url

# 素材时长范围内的秒数（最长 10 小时）；区间长度包含 0（单帧镜头 / 退化区间）。
SECONDS = st.floats(min_value=0, max_value=36_000, allow_nan=False, allow_infinity=False)
LENGTHS = st.one_of(st.just(0.0), st.floats(min_value=0, max_value=7_200, allow_nan=False, allow_infinity=False))


# Feature: agent-capability-expansion, Property 9: 抽帧时间点
@settings(max_examples=100, deadline=None)
@given(start=SECONDS, length=LENGTHS, count=st.integers(min_value=1, max_value=50))
def test_frame_times_count_monotonic_and_in_range(start: float, length: float, count: int) -> None:
    """**Validates: Requirements 8.4**"""
    end = start + length
    times = frame_times(start, end, count)

    assert len(times) == min(count, MAX_FRAMES) == min(count, 12)
    assert all(a <= b for a, b in itertools.pairwise(times))
    assert all(start <= t <= end for t in times)
    # URL 中实际提交的时间点同样落在区间内（不越过终点，服务端越界会返回 422）。
    for t in times:
        sent = float(frame_url("src", t, 768).split("t=", 1)[1].split("&", 1)[0])
        assert sent <= end
        assert sent >= 0
