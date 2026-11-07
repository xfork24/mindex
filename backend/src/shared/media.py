"""媒体工具:ffmpeg/ffprobe 调用封装、探测、切片与原子写入。"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import subprocess
import threading
from collections import OrderedDict
from pathlib import Path

from shared.query_budget import remaining
from shared.trace import record
from shared.work_budget import commit_file, current, stream_command


class MediaError(ValueError):
    pass


def ffmpeg_bin() -> str:
    """ffmpeg 可执行文件：``MINDEX_FFMPEG``（桌面版指向随包构建），缺省从 PATH 查找 ``ffmpeg``。

    路径只决定用哪个二进制，不进入任何签名或缓存键。新增的 ffmpeg 调用一律经此函数或 ``command()``。"""
    return os.environ.get("MINDEX_FFMPEG", "").strip() or "ffmpeg"


def ffprobe_bin() -> str:
    """ffprobe 可执行文件：``MINDEX_FFPROBE``，缺省 ``ffprobe``。"""
    return os.environ.get("MINDEX_FFPROBE", "").strip() or "ffprobe"


def resolve_tool(args: list[str]) -> list[str]:
    """把首参数 ``ffmpeg``/``ffprobe`` 换成配置的可执行文件；其他命令原样返回。"""
    if args and args[0] in ("ffmpeg", "ffprobe"):
        return [ffmpeg_bin() if args[0] == "ffmpeg" else ffprobe_bin(), *args[1:]]
    return list(args)


def command(args: list[str], *, timeout: float = 600) -> bytes:

    timeout = remaining(timeout)
    args = resolve_tool(args)
    record("command", {"args": args, "timeout": timeout})

    budget = current()
    if (
        budget is not None
        and args
        and Path(args[0]).name.startswith("ffmpeg")
        and args[-1] not in {"-", "pipe:1"}
        and not args[-1].startswith("-")
        and budget.owns(Path(args[-1]))
    ):
        return stream_command(args, Path(args[-1]), timeout=timeout)
    try:
        result = subprocess.run(args, check=True, capture_output=True, timeout=timeout)
        record(
            "command-result",
            {"returncode": result.returncode, "stderr": result.stderr.decode(errors="replace")},
        )
        return result.stdout
    except FileNotFoundError as exc:
        raise MediaError(f"Missing executable: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        # 延迟导入：保持异常捕获范围，依赖加载失败时仍走原有降级路径。
        from shared.query_budget import check

        check()
        raise MediaError(f"{args[0]} timed out") from exc
    except subprocess.CalledProcessError as exc:
        record("command-error", {"returncode": exc.returncode, "stderr": exc.stderr.decode(errors="replace")})
        raise MediaError(exc.stderr.decode(errors="replace")[-2000:]) from exc


_PROBE_CACHE_LIMIT = 32
_probe_cache: OrderedDict[tuple, dict] = OrderedDict()
_probe_lock = threading.Lock()


def _probe_key(path: Path) -> tuple | None:
    """缓存身份沿用库内素材指纹约定（字节 + mtime）；所有媒体写入均为原子替换，mtime 必变。"""
    try:
        stat = path.stat()
    except OSError:
        return None  # 文件缺失时不缓存，交给 ffprobe 抛出原有 MediaError。
    return (str(path), stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino, stat.st_ctime_ns)


def probe(path: Path) -> dict:
    """ffprobe 结果的有界缓存：同一（路径、大小、mtime）只探测一次；失败不缓存，返回副本。"""
    key = _probe_key(path)
    if key is not None:
        with _probe_lock:
            cached = _probe_cache.get(key)
            if cached is not None:
                _probe_cache.move_to_end(key)
                return dict(cached)
    info = _probe_uncached(path)
    if key is not None:
        with _probe_lock:
            _probe_cache[key] = dict(info)
            while len(_probe_cache) > _PROBE_CACHE_LIMIT:
                _probe_cache.popitem(last=False)
    return info


def _probe_uncached(path: Path) -> dict:
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
    partial = output.with_name("." + output.stem + ".partial" + output.suffix)

    budget = current()
    if budget is not None:
        budget.retain(output)
        budget.retain(partial)
    try:
        command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args, str(partial)], timeout=timeout)
        commit_file(partial, output)
        if budget is not None:
            budget.committed(partial)
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
