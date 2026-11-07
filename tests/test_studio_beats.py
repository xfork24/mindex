(
    """曲目节奏分析（spec m3-post-production 需求 16.2）：合成节拍器音频的 BPM 误差 ≤ 2%，节拍时间误差 ≤ """
    """30 """
    """ms。"""
)

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from studio import beats

RATE = 22050


def metronome(bpm: float, seconds: float = 12.0, *, offset: float = 0.0, rate: int = RATE) -> bytes:
    """短促的 1 kHz 衰减咔嗒声，从 ``offset`` 秒起每拍一下；返回 16 位单声道 WAV。"""
    samples = np.zeros(int(rate * seconds), dtype=np.float32)
    length = int(rate * 0.02)
    click = np.sin(2 * np.pi * 1000 * np.arange(length) / rate) * np.exp(-np.arange(length) / (rate * 0.004))
    time = offset
    while time < seconds:
        start = int(time * rate)
        piece = samples[start : start + length]
        piece += click[: piece.size].astype(np.float32)
        time += 60 / bpm
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((samples * 20000).astype("<i2").tobytes())
    return buffer.getvalue()


@pytest.mark.parametrize("bpm", [90, 120, 150])
def test_metronome_bpm_and_beats(tmp_path, bpm):
    path = tmp_path / f"m{bpm}.wav"
    path.write_bytes(metronome(bpm, offset=0.25))
    result = beats.analyze(path)
    assert result["bpm"] == pytest.approx(bpm, rel=0.02)
    assert result["analysis_version"] == beats.ANALYSIS_VERSION
    assert result["duration"] == pytest.approx(12.0, abs=0.01)
    truth = np.arange(0.25, 12.0, 60 / bpm)
    found = np.array(result["beats"])
    assert len(found) >= len(truth) - 2  # 首尾各允许漏检一拍
    # 每个检出节拍都靠近某个真实节拍。
    errors = np.abs(found[:, None] - truth[None, :]).min(axis=1)
    assert errors.max() <= 0.03, errors.max()
    assert 0 <= result["energy"] <= 1 and 0 <= result["brightness"] <= 1


def test_analysis_is_deterministic(tmp_path):
    path = tmp_path / "m.wav"
    path.write_bytes(metronome(120, seconds=6))
    assert beats.analyze(path) == beats.analyze(path)


def test_too_short_audio_is_rejected(tmp_path):
    path = tmp_path / "short.wav"
    path.write_bytes(metronome(120, seconds=0.5))
    with pytest.raises(ValueError, match="太短"):
        beats.analyze(path)
