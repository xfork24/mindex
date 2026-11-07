"""音乐节奏分析（纯 numpy，桌面版冻结环境同样可用）：BPM、节拍时间、能量与亮度。

步骤：ffmpeg 解码为 22050 Hz 单声道 → STFT（窗 2048、步长 512）→ 对数幅度谱的半波整流谱通量作为起音包络
→ 包络自相关在 60–200 BPM 范围内求峰（加对数高斯先验偏向 120 BPM，减少倍频/半频错判）→ 动态规划节拍跟踪
（Ellis 2007：以目标间隔为中心的对数高斯转移代价）。结果确定：同一输入同一输出。
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from shared.media import command, ffmpeg_bin

# 改动分析算法或参数时递增：曲库记录分析版本，过期的分析会提示重新分析。
ANALYSIS_VERSION = 1
RATE = 22050
WINDOW = 2048
HOP = 512
MIN_BPM, MAX_BPM = 60.0, 200.0
MAX_SECONDS = 600


def decode(path: Path, *, seconds: float = MAX_SECONDS) -> np.ndarray:
    raw = command(
        [
            ffmpeg_bin(),
            "-v",
            "error",
            "-i",
            str(path),
            "-t",
            f"{seconds:g}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(RATE),
            "-f",
            "f32le",
            "-",
        ],
        timeout=300,
    )
    return np.frombuffer(raw, dtype=np.float32).copy()


def onset_envelope(samples: np.ndarray) -> np.ndarray:
    if samples.size < WINDOW:
        return np.zeros(1)
    # 前补半个窗加一个步长：第 i 帧的时间即 i × HOP（谱通量在起音进入窗口中部、相邻两帧差分时达峰）。
    samples = np.concatenate([np.zeros(WINDOW // 2 + HOP, dtype=samples.dtype), samples])
    frames = 1 + (samples.size - WINDOW) // HOP
    index = np.arange(WINDOW)[None, :] + HOP * np.arange(frames)[:, None]
    spectrum = np.abs(np.fft.rfft(samples[index] * np.hanning(WINDOW), axis=1))
    log = np.log1p(1000 * spectrum)
    flux = np.maximum(0.0, np.diff(log, axis=0)).sum(axis=1)
    flux = np.concatenate([[0.0], flux])
    flux -= np.convolve(flux, np.ones(16) / 16, mode="same")  # 去掉缓慢变化的基线
    flux = np.maximum(flux, 0)
    peak = flux.max()
    return flux / peak if peak > 0 else flux


def estimate_bpm(envelope: np.ndarray) -> float:
    frame_rate = RATE / HOP
    centered = envelope - envelope.mean()
    size = 1 << (2 * centered.size - 1).bit_length()
    spectrum = np.fft.rfft(centered, size)
    correlation = np.fft.irfft(spectrum * np.conj(spectrum), size)[: centered.size]
    lags = np.arange(correlation.size)
    valid = (lags >= frame_rate * 60 / MAX_BPM) & (lags <= frame_rate * 60 / MIN_BPM)
    if not valid.any() or correlation[0] <= 0:
        return 0.0
    bpm = 60 * frame_rate / np.maximum(lags, 1)
    prior = np.exp(-0.5 * (np.log2(bpm / 120.0) / 0.9) ** 2)
    score = np.where(valid, correlation / correlation[0] * prior, -np.inf)
    lag = int(np.argmax(score))
    # 抛物线插值求亚帧精度的周期。
    if 0 < lag < correlation.size - 1:
        a, b, c = correlation[lag - 1], correlation[lag], correlation[lag + 1]
        denominator = a - 2 * b + c
        if denominator != 0:
            lag = lag + 0.5 * (a - c) / denominator
    return float(60 * frame_rate / lag)


def track_beats(envelope: np.ndarray, bpm: float, tightness: float = 100.0) -> np.ndarray:
    """动态规划节拍跟踪，返回节拍所在帧。"""
    if bpm <= 0 or envelope.size < 2:
        return np.array([], dtype=int)
    period = (RATE / HOP) * 60 / bpm
    score = envelope.astype(float).copy()
    backlink = np.full(envelope.size, -1)
    window = np.arange(-round(2 * period), -round(period / 2) + 1)
    cost = -tightness * (np.log(-window / period)) ** 2
    for index in range(envelope.size):
        candidates = index + window
        mask = candidates >= 0
        if not mask.any():
            continue
        values = score[candidates[mask]] + cost[mask]
        best = int(np.argmax(values))
        if values[best] > 0:
            score[index] += values[best]
            backlink[index] = candidates[mask][best]
    # 从末尾附近得分最高处回溯。
    tail = max(0, envelope.size - round(period) - 1)
    position = tail + int(np.argmax(score[tail:]))
    beats = []
    while position >= 0:
        beats.append(position)
        position = backlink[position]
    return np.array(beats[::-1], dtype=int)


def analyze(path: Path) -> dict:
    """返回 ``{bpm, beats, duration, energy, brightness, analysis_version}``；
    节拍时间单位为秒（保留 3 位）。
    """
    samples = decode(path)
    duration = samples.size / RATE
    if duration < 1:
        raise ValueError("音频太短，无法分析节奏")
    envelope = onset_envelope(samples)
    bpm = estimate_bpm(envelope)
    beats = track_beats(envelope, bpm) * HOP / RATE
    rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
    energy = round(min(1.0, max(0.0, (20 * math.log10(rms + 1e-9) + 40) / 34)), 3)  # -40 dBFS→0，-6 dBFS→1
    spectrum = np.abs(np.fft.rfft(samples[: RATE * 60] * 1.0))
    freqs = np.fft.rfftfreq(min(samples.size, RATE * 60), 1 / RATE)
    centroid = float((spectrum * freqs).sum() / spectrum.sum()) if spectrum.sum() > 0 else 0.0
    return {
        "bpm": round(bpm, 2),
        "beats": [round(float(b), 3) for b in beats],
        "duration": round(duration, 3),
        "energy": energy,
        "brightness": round(min(1.0, centroid / 4000), 3),
        "analysis_version": ANALYSIS_VERSION,
    }
