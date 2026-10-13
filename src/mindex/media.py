from __future__ import annotations

import base64
import hashlib
import json
import math
import subprocess
from pathlib import Path


class MediaError(ValueError):
    pass


def command(args: list[str], *, timeout: float = 600) -> bytes:
    try:
        result = subprocess.run(args, check=True, capture_output=True, timeout=timeout)
        return result.stdout
    except FileNotFoundError as exc:
        raise MediaError(f"Missing executable: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{args[0]} timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise MediaError(exc.stderr.decode(errors="replace")[-2000:]) from exc


def probe(path: Path) -> dict:
    raw = json.loads(
        command(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], timeout=30
        )
    )
    video = next(
        (
            s
            for s in raw["streams"]
            if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    if not video:
        raise MediaError("文件中没有可用的视频轨道")
    duration = float(video.get("duration") or raw["format"].get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise MediaError("无法确定视频时长")
    num, den = video.get("avg_frame_rate", "25/1").split("/")
    fps = float(num) / float(den) if float(den) else 25
    return {
        "duration": duration,
        "fps": fps or 25,
        "width": video["width"],
        "height": video["height"],
        "audio": any(s["codec_type"] == "audio" for s in raw["streams"]),
    }


def audio_info(path: Path) -> dict:
    """探测纯音频文件时长；probe() 要求视频轨，不能用于 TTS wav。"""
    raw = json.loads(
        command(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=sample_rate,channels,codec_name:format=duration",
                "-of",
                "json",
                str(path),
            ],
            timeout=30,
        )
    )
    streams = raw.get("streams") or []
    duration = float((raw.get("format") or {}).get("duration") or 0)
    if not streams or not math.isfinite(duration) or duration <= 0:
        raise MediaError("文件中没有可用的音频轨道或无法确定音频时长")
    stream = streams[0]
    return {
        "duration": duration,
        "sample_rate": int(stream.get("sample_rate") or 0),
        "channels": int(stream.get("channels") or 0),
    }


def content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def windows(start: float, end: float, size: float = 4, stride: float = 3) -> list[tuple[float, float]]:
    if not (0 <= start < end and 0 < stride <= size):
        raise ValueError("Invalid window bounds")
    if end - start <= size:
        return [(start, end)]
    result = []
    position = start
    while position + size < end - 1e-6:
        result.append((round(position, 6), round(position + size, 6)))
        position += stride
    tail = (round(max(start, end - size), 6), round(end, 6))
    if not result or tail != result[-1]:
        result.append(tail)
    return result


def atomic_media(args: list[str], output: Path, *, timeout: float = 600):
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + ".partial" + output.suffix)
    try:
        command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args, str(partial)], timeout=timeout)
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)


def cut(source: Path, start: float, end: float, output: Path):
    if output.exists():
        return
    atomic_media(
        [
            "-ss",
            str(start),
            "-i",
            str(source),
            "-t",
            str(end - start),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            "setpts=PTS-STARTPTS,pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-af",
            "asetpts=PTS-STARTPTS",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
        ],
        output,
        timeout=max(600, (end - start) * 10),
    )


def thumbnail(source: Path, output: Path):
    if output.exists():
        return
    # Avoid decoding an entire long shot just to choose one thumbnail timestamp.
    duration = probe(source)["duration"]
    args = [
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-vf",
        "scale=480:-2",
    ]
    try:
        atomic_media(["-ss", str(max(0, duration / 2 - 0.05)), *args], output)
    except (MediaError, FileNotFoundError):
        atomic_media(["-ss", "0", *args], output)


def extract_audio(source: Path, output: Path, start: float = 0, duration: float | None = None):
    args = ["-ss", str(start), "-i", str(source)]
    if duration is not None:
        args += ["-t", str(duration)]
    atomic_media([*args, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"], output)


def frame_data(source: Path, count: int = 12) -> list[tuple[float, str]]:
    timestamps = frame_timestamps(source)
    # Sample actual presentation timestamps, including one-frame clips.
    count = min(count, len(timestamps))
    frames = []
    for index in range(count):
        timestamp = timestamps[round(index * (len(timestamps) - 1) / max(1, count - 1))]
        raw = command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                str(timestamp),
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale=768:-2",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "pipe:1",
            ],
            timeout=30,
        )
        if raw:
            frames.append((timestamp, "data:image/jpeg;base64," + base64.b64encode(raw).decode()))
    if not frames:
        raise MediaError("视频帧解码失败")
    return frames


def frame_timestamps(source: Path) -> list[float]:
    raw = command(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "json",
            str(source),
        ],
        timeout=max(600, probe(source)["duration"] * 2),
    )
    values = [
        float(f["best_effort_timestamp_time"])
        for f in json.loads(raw)["frames"]
        if "best_effort_timestamp_time" in f
    ]
    if not values:
        raise MediaError("无法读取帧时间戳")
    origin = values[0]
    return [max(0.0, v - origin) for v in values]
