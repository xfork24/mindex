"""Sample original PTS locally; only sampled JPEGs cross the model-service boundary."""

from __future__ import annotations

import io

from PIL import Image

from mindex.models.adapters import ModelError


def sample_frames(path, start, end, fps=None):
    try:
        import av
    except ImportError as exc:
        raise ModelError("人物处理需要 av；请执行 uv sync --extra person") from exc
    found = False
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        origin = float(stream.start_time * stream.time_base) if stream.start_time else 0
        container.seek(int((start + origin) / stream.time_base), stream=stream, backward=True)
        next_time = start
        for frame in container.decode(stream):
            if frame.pts is None:
                raise ModelError("原片帧缺少时间戳，拒绝猜测时间")
            timestamp = float(frame.pts * stream.time_base) - origin
            if timestamp >= end:
                break
            if timestamp + 1e-8 < next_time or timestamp < start:
                continue
            found = True
            buffer = io.BytesIO()
            Image.fromarray(frame.to_ndarray(format="rgb24")).save(buffer, format="JPEG", quality=90)
            yield timestamp, buffer.getvalue()
            next_time = timestamp + 1 / fps if fps else timestamp
    if not found:
        raise ModelError("此区间没有可解码的实际帧")
