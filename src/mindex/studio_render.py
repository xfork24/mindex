"""渲染合成：逐镜统一切割 → concat 视频轨 → 逐镜音频处理 → 三轨合成。

所有 ffmpeg 输出走 media.atomic_media（.partial 原子替换）；
逐镜 clip 与音频是检查点，失败重跑只补缺；concat 与合成阶段幂等重执行。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from .config import Config, SubtitleConfig
from .media import MediaError, atomic_media, command, probe
from .studio import Timeline

_PAD_EPS = 0.02


def has_subtitles_filter() -> bool:
    output = command(["ffmpeg", "-hide_banner", "-filters"], timeout=30).decode(errors="replace")
    return re.search(r"\bsubtitles\b", output) is not None


def _escape_filter_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def subtitle_filter(srt_path: Path, subtitle: SubtitleConfig) -> str:
    parts = f"subtitles={_escape_filter_path(srt_path)}"
    if subtitle.font_file:
        parts += f":fonts_dir={_escape_filter_path(Path(subtitle.font_file).parent)}"
    style = f"FontName={subtitle.font_family},FontSize={subtitle.font_size}"
    return f"{parts}:force_style='{style}'"


def check_font(srt_path: Path, subtitle: SubtitleConfig):
    """烧录前环境烟测：滤镜存在性 + 1 帧渲染，尽早失败并给出精确提示。"""
    if not has_subtitles_filter():
        raise MediaError(
            "当前 ffmpeg 未编译 subtitles 滤镜（缺少 libass），无法烧录字幕；"
            "请创建任务时选择软字幕（burn=false），或安装带 libass 的 ffmpeg"
        )
    try:
        command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:d=0.2",
                "-vf",
                subtitle_filter(srt_path, subtitle),
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            timeout=30,
        )
    except MediaError as exc:
        raise MediaError(
            f"缺少可用的 CJK 字体或字幕滤镜不可用，请安装 fonts-noto-cjk "
            f"或配置 studio.subtitle.font_file：{exc}"
        ) from exc


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def build_srt(timeline: Timeline) -> str:
    lines: list[str] = []
    offset = 0.0
    for event in timeline.events:
        start, end = offset, offset + event.audio.render_duration
        lines += [
            str(event.order),
            f"{_srt_time(start)} --> {_srt_time(end)}",
            event.subtitle.text,
            "",
        ]
        offset = end
    return "\n".join(lines)


def _concat_list(paths: list[Path], listing: Path):
    # concat demuxer 文件名用单引号包裹，内部单引号转义为 '\''
    quote = "'"
    escaped = quote + "\\" + quote + quote
    entries = [f"file '{str(path.absolute()).replace(quote, escaped)}'" for path in paths]
    listing.write_text("\n".join(entries) + "\n")


def concat_media(paths: list[Path], output: Path, *, timeout: float = 600):
    listing = output.parent / f"{output.stem}.txt"
    _concat_list(paths, listing)
    atomic_media(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy"], output, timeout=timeout)


def cut_shot(source: Path, event, render, output: Path):
    """逐镜统一切割：统一画布/fps/SAR，音视频时长残差由 tpad/裁剪吸收。"""
    duration = event.audio.render_duration
    pad = duration - (event.video.end - event.video.start)
    filters = [
        "setpts=PTS-STARTPTS",
        f"fps={render.fps}",
        f"scale={render.width}:{render.height}:force_original_aspect_ratio=decrease",
        f"pad={render.width}:{render.height}:(ow-iw)/2:(oh-ih)/2",
        "setsar=1",
    ]
    if pad > _PAD_EPS:
        filters.append(f"tpad=stop_mode=clone:stop_duration={pad:.3f}")
    atomic_media(
        [
            "-ss",
            str(event.video.start),
            "-i",
            str(source),
            "-t",
            str(duration),
            "-map",
            "0:v:0",
            "-an",  # 音轨统一由配音提供，原声按需另行提取
            "-vf",
            ",".join(filters),
            "-c:v",
            "libx264",
            "-preset",
            render.preset,
            "-crf",
            str(render.crf),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ],
        output,
        timeout=max(600, duration * 10),
    )


def process_shot_audio(voice_source: Path, ambient_source: Path | None, event, audio_cfg, output: Path):
    """配音 atempo→loudnorm→48k；ducking 模式叠加压低后的原声（duration=first 以配音为准）。"""
    voice = output.with_name(f"{event.shot_id}.voice.wav")
    filters = []
    if abs(event.audio.atempo - 1) > 1e-3:
        filters.append(f"atempo={event.audio.atempo:.4f}")
    filters += [f"loudnorm={audio_cfg.loudnorm}", "aresample=48000"]
    atomic_media(
        ["-i", str(voice_source), "-af", ",".join(filters), "-ac", "2", "-c:a", "pcm_s16le"],
        voice,
    )
    if ambient_source is None:
        voice.replace(output)
        return
    atomic_media(
        [
            "-i",
            str(voice),
            "-i",
            str(ambient_source),
            "-filter_complex",
            (f"[1:a]volume={audio_cfg.duck_gain}[bg];"
            "[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0"),
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
        ],
        output,
    )


def extract_ambient(source: Path, event, audio_cfg, output: Path):
    atomic_media(
        [
            "-ss",
            str(event.video.start),
            "-i",
            str(source),
            "-t",
            str(event.video.end - event.video.start),
            "-vn",
            "-af",
            f"loudnorm={audio_cfg.loudnorm}",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
        ],
        output,
    )


def mux(
    video: Path, audio_track: Path, srt: Path, output: Path, config: Config, *, burn: bool
):
    render, subtitle = config.studio.render, config.studio.subtitle
    args = ["-i", str(video), "-i", str(audio_track)]
    if burn:
        args += [
            "-map", "0:v:0", "-map", "1:a:0",
            "-vf", subtitle_filter(srt, subtitle),
            "-c:v", "libx264", "-preset", render.preset, "-crf", str(render.crf),
            "-pix_fmt", "yuv420p",
        ]
    else:
        args += [
            "-i", str(srt),
            "-map", "0:v:0", "-map", "1:a:0", "-map", "2:s:0",
            "-c:v", "copy", "-c:s", "mov_text",
            "-metadata:s:s:0", "language=chi",
        ]
    args += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest"]
    atomic_media(args, output, timeout=600)


def render_timeline(
    config: Config,
    timeline: Timeline,
    *,
    voice_lookup: Callable[[str], Path],
    source_lookup: Callable[[str], Path],
    workdir: Path,
) -> dict:
    render_cfg, audio_cfg, subtitle_cfg = config.studio.render, config.studio.audio, config.studio.subtitle
    clips_dir = workdir / "clips"
    audio_dir = workdir / "audio"
    orig_dir = workdir / "orig"
    srt_path = workdir / "final.srt"
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.write_text(build_srt(timeline))
    if timeline.burn:
        check_font(srt_path, subtitle_cfg)

    clips: list[Path] = []
    for event in timeline.events:
        clip = clips_dir / f"{event.shot_id}.mp4"
        if not clip.is_file():  # 检查点：已切割的镜头不重切
            cut_shot(source_lookup(event.video.source_id), event, render_cfg, clip)
        clips.append(clip)
    video_track = workdir / "video.mp4"
    concat_media(clips, video_track)

    tracks: list[Path] = []
    for event in timeline.events:
        target = audio_dir / f"{event.shot_id}.wav"
        if not target.is_file():  # 检查点
            source = source_lookup(event.video.source_id)
            ambient = None
            if timeline.audio_mode == "ducking" and probe(source)["audio"]:
                # 无音轨素材自动退化为纯配音；ducking 只是氛围增强
                ambient = orig_dir / f"{event.shot_id}.wav"
                if not ambient.is_file():
                    extract_ambient(source, event, audio_cfg, ambient)
            process_shot_audio(voice_lookup(event.shot_id), ambient, event, audio_cfg, target)
        tracks.append(target)
    audio_track = workdir / "audio_track.wav"
    concat_media(tracks, audio_track)

    final = workdir / "final.mp4"
    final.unlink(missing_ok=True)
    mux(video_track, audio_track, srt_path, final, config, burn=timeline.burn)
    return {"final": final, "srt": srt_path, "events": len(timeline.events)}
