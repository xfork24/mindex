"""渲染合成：逐镜统一切割 → concat 视频轨 → 逐镜音频处理 → 三轨合成。

所有 ffmpeg 输出走 media.atomic_media（.partial 原子替换）；
逐镜 clip 与音频是检查点，失败重跑只补缺；concat 与合成阶段幂等重执行。
"""

from __future__ import annotations

import math
import os
import re
import wave
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

from shared.config import Config, SubtitleConfig, fingerprint, portable_parameters
from shared.media import MediaError, atomic_media, command, probe
from shared.trace import current
from studio.artifacts import digest
from studio.studio import Timeline, save, save_bytes


def has_subtitles_filter() -> bool:
    output = command(["ffmpeg", "-hide_banner", "-filters"], timeout=30).decode(errors="replace")
    return re.search(r"\bsubtitles\b", output) is not None


def _escape_filter_path(path: Path) -> str:
    escaped = str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    # Windows 盘符的冒号即使转义（C\:），滤镜图解析器仍会在 C: 处截断文件名（avformat_open_input 'C'）；
    # 单引号包裹后转义形式才生效（实测 ffmpeg 9.0.2）。POSIX 保持原形式不变。
    if os.name == "nt":
        return f"'{escaped}'"
    return escaped


# 竖屏字幕抬高到画面下方约 1/8 处，避开短视频平台底部的按钮与文案区（单位为 libass PlayResY=288）。
PORTRAIT_MARGIN_V = 36


def canvas_subtitle_style(canvas: str, width: int, height: int, subtitle: SubtitleConfig) -> dict | None:
    """按画幅缩放字幕：16:9 返回 None（样式与旧版逐字节一致）。

    libass 的 FontSize 以 PlayResY（SRT 转换缺省 288）为基准、随画面高度缩放；16:9 下画面高度即短边。
    其他画幅把字号乘以 ``短边 / 高``，使字幕像素大小相对短边与 16:9 一致——全屏播放时（横屏看 16:9、
    竖屏看 9:16）字幕的物理大小相同；1:1 系数为 1。竖屏另把 MarginV 提高到 ``PORTRAIT_MARGIN_V``。
    """
    if canvas == "16:9":
        return None
    scaled = round(subtitle.font_size * min(width, height) / height, 2)
    style = {"FontSize": f"{scaled:g}"}
    if height > width:
        style["MarginV"] = str(PORTRAIT_MARGIN_V)
    return style


def subtitle_filter(srt_path: Path, subtitle: SubtitleConfig, *, style: dict | None = None) -> str:
    parts = f"subtitles={_escape_filter_path(srt_path)}"
    if subtitle.font_file:
        parts += f":fontsdir={_escape_filter_path(Path(subtitle.font_file).parent)}"
    values = {"FontName": subtitle.font_family, "FontSize": str(subtitle.font_size), **(style or {})}
    return f"{parts}:force_style='{','.join(f'{k}={v}' for k, v in values.items())}'"


def check_font(srt_path: Path, subtitle: SubtitleConfig):
    """烧录前环境烟测：滤镜存在性 + 1 帧渲染，尽早失败并给出精确提示。"""
    if subtitle.font_file and not Path(subtitle.font_file).is_file():
        raise MediaError(f"字幕字体文件不存在：{subtitle.font_file}")
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


def build_srt(timeline: Timeline, boundaries: list[dict] | None = None) -> str:
    lines: list[str] = []
    offset = 0.0
    for index, event in enumerate(timeline.events):
        start, end = offset + event.subtitle.start, offset + event.subtitle.end
        if boundaries is not None:
            start, end = boundaries[index]["start"], boundaries[index]["end"]
        lines += [
            str(event.order),
            f"{_srt_time(start)} --> {_srt_time(end)}",
            event.subtitle.text,
            "",
        ]
        offset += event.audio.render_duration
    return "\n".join(lines)


def _concat_list(paths: list[Path], listing: Path):
    # concat demuxer 文件名用单引号包裹，内部单引号转义为 '\''
    quote = "'"
    escaped = quote + "\\" + quote + quote
    entries = [f"file '{str(path.absolute()).replace(quote, escaped)}'" for path in paths]
    listing.write_text("\n".join(entries) + "\n", encoding="utf-8")


def concat_media(paths: list[Path], output: Path, *, timeout: float = 600):
    listing = output.parent / f"{output.stem}.txt"
    _concat_list(paths, listing)
    atomic_media(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy"], output, timeout=timeout)


def fit_filters(width: int, height: int, fit: str = "pad") -> list[str]:
    """pad：完整保留画面并补黑边；crop：铺满画布并居中裁掉多余部分。"""
    if fit == "crop":
        return [f"scale={width}:{height}:force_original_aspect_ratio=increase", f"crop={width}:{height}"]
    return [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
    ]


def cut_shot(source: Path, event, render, output: Path, *, frames: int | None = None, fit: str = "pad"):
    """先裁所选区间，再按画幅适配（pad/crop）并补末帧，输出确定帧数。"""
    fps = Fraction(str(render.fps))
    frames = frames or math.ceil(event.audio.render_duration * fps)
    duration = float(frames / fps)
    filters = [
        f"trim=duration={event.video.end - event.video.start:.9f}",
        "setpts=PTS-STARTPTS",
        f"fps={fps.numerator}/{fps.denominator}",
        *fit_filters(render.width, render.height, fit),
        "setsar=1",
        f"tpad=stop_mode=clone:stop_duration={duration + 1:.9f}",
    ]
    atomic_media(
        [
            "-ss",
            str(event.video.start),
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            ",".join(filters),
            "-frames:v",
            str(frames),
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


# 静态图片解码上限（像素）：先归一化为 PNG（方向、透明底铺白），再按画幅超采样做推拉。
STILL_MAX_PIXELS = 16_000_000
STILL_SUPERSAMPLE = 2  # zoompan 的取景坐标按整数像素取整：在 2 倍画布上取景，减轻推拉抖动


def still_motion(motion: str, strength: float, frames: int) -> tuple[str, str, str]:
    """zoompan 的 z/x/y 表达式（``on`` 为输出帧序号，从 0 开始）。

    最大放大倍数 ``1 + strength/2``：zoom-in 由 1 推到最大，zoom-out 反向；pan-left/pan-right 在最大
    放大倍数下把取景框从右到左 / 从左到右水平移过整幅画面；none（或 strength=0）为静止画面。"""
    peak = 1 + strength * 0.5 if motion != "none" else 1.0
    k = peak - 1
    t = f"(on/{max(frames - 1, 1)})"
    center_x, center_y = "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
    if motion == "zoom-in":
        return f"1+{k:.6f}*{t}", center_x, center_y
    if motion == "zoom-out":
        return f"{peak:.6f}-{k:.6f}*{t}", center_x, center_y
    if motion == "pan-left":
        return f"{peak:.6f}", f"(iw-iw/zoom)*(1-{t})", center_y
    if motion == "pan-right":
        return f"{peak:.6f}", f"(iw-iw/zoom)*{t}", center_y
    return f"{peak:.6f}", center_x, center_y


def still_shot(
    image: Path,
    render,
    output: Path,
    *,
    frames: int,
    motion: str = "zoom-in",
    strength: float = 0.3,
    fit: str = "pad",
):
    """静态图片 → 恰好 ``frames`` 帧、画幅尺寸的推拉/平移画面（无音轨）。

    图片先经 ``shared/images.image_png`` 归一化（EXIF 方向、透明铺白、像素上限），再按 pad/crop 适配到
    超采样画布，由 zoompan 逐帧取景后缩放到 ``render.width × render.height``；同一输入输出确定。"""
    from shared.images import image_png

    fps = Fraction(str(render.fps))
    width, height = render.width, render.height
    still = output.with_name(output.stem + ".still.png")
    save_bytes(still, image_png(image, STILL_MAX_PIXELS))
    zoom, x, y = still_motion(motion, strength, frames)
    filters = [
        *fit_filters(width * STILL_SUPERSAMPLE, height * STILL_SUPERSAMPLE, fit),
        "setsar=1",
        f"zoompan=z='{zoom}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={fps.numerator}/{fps.denominator}",
        "setsar=1",
    ]
    duration = float(frames / fps)
    atomic_media(
        [
            "-i",
            str(still),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            ",".join(filters),
            "-frames:v",
            str(frames),
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
    still.unlink(missing_ok=True)


EDGE_ZOOM = 0.2  # 缩放转场的幅度：入场从 1.2 倍推到 1 倍，出场从 1 倍推到 1.2 倍


def edge_filter(
    width: int, height: int, fps: Fraction, frames: int, enter: dict | None, leave: dict | None
) -> str:
    """镜内转场的 filter_complex（输入 ``[0:v]``，输出 ``[v]``），帧数与时长都不变。

        ``enter``/``leave`` 为 ``{"kind", "duration"}``（时长已截到镜头的一半以内）。fade 黑场淡入淡出；white
    白场；
        zoom 缩放并伴随黑场淡入淡出；slide 在黑底上从右滑入 / 向左滑出。"""
    total = float(frames / fps)
    rate = f"{fps.numerator}/{fps.denominator}"
    chain, fades = [], []
    kinds = {"enter": (enter or {}).get("kind"), "leave": (leave or {}).get("kind")}
    if "zoom" in kinds.values():
        terms = []
        if kinds["enter"] == "zoom":
            n = max(1, round(enter["duration"] * fps))
            terms.append(f"if(lt(in,{n}),{1 + EDGE_ZOOM}-{EDGE_ZOOM}*in/{n},1)")
        if kinds["leave"] == "zoom":
            n = max(1, round(leave["duration"] * fps))
            start = frames - 1 - n
            terms.append(f"if(gt(in,{start}),1+{EDGE_ZOOM}*(in-{start})/{n},1)")
        chain += [
            f"scale={width * 2}:{height * 2}",
            "setsar=1",
            f"zoompan=z='{'*'.join(terms)}':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d=1:s={width}x{height}"
            f":fps={rate}",
            "setsar=1",
        ]
    for side, edge in (("enter", enter), ("leave", leave)):
        if not edge or edge["kind"] == "slide":
            continue
        color = "white" if edge["kind"] == "white" else "black"
        start = 0.0 if side == "enter" else max(0.0, total - edge["duration"])
        fades.append(
            f"fade=t={'in' if side == 'enter' else 'out'}:st={start:.6f}:d={edge['duration']:.6f}:c={color}"
        )
    body = ",".join([*chain, "setpts=PTS-STARTPTS"])
    if "slide" not in kinds.values():
        return f"[0:v]{','.join([body, *fades, 'format=yuv420p'])}[v]"
    a = enter["duration"] if kinds["enter"] == "slide" else 0
    b = leave["duration"] if kinds["leave"] == "slide" else 0
    x = "0"
    if b:
        x = f"if(gt(t,{total - b:.6f}),-{width}*(t-{total - b:.6f})/{b:.6f},0)"
    if a:
        x = f"if(lt(t,{a:.6f}),{width}*(1-t/{a:.6f}),{x})"
    tail = ",".join([*fades, "format=yuv420p"])
    return (
        f"[0:v]{body}[fg];color=c=black:s={width}x{height}:r={rate}:d={total + 1:.6f}[bg];"
        f"[bg][fg]overlay=x='{x}':y=0:eof_action=repeat,{tail}[v]"
    )


def apply_edges(source: Path, output: Path, render, *, frames: int, enter: dict | None, leave: dict | None):
    """在确定帧数的分镜画面上叠加镜内转场，输出同帧数、同尺寸的画面（无音轨）。"""
    fps = Fraction(str(render.fps))
    graph = edge_filter(render.width, render.height, fps, frames, enter, leave)
    atomic_media(
        [
            "-i",
            str(source),
            "-filter_complex",
            graph,
            "-map",
            "[v]",
            "-an",
            "-frames:v",
            str(frames),
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
        timeout=max(600, float(frames / fps) * 10),
    )


def process_shot_audio(voice_source: Path, ambient_source: Path | None, event, audio_cfg, output: Path):
    """配音 atempo→loudnorm→48k；ducking 模式叠加压低后的原声（duration=first 以配音为准）。"""
    voice = output.with_name(f"{event.shot_id}.voice.wav")
    filters = []
    if event.audio.atempo != 1:
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
            (
                f"[1:a]volume={audio_cfg.duck_gain},apad[bg];"
                "[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0"
            ),
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
            f"loudnorm={audio_cfg.loudnorm},aresample=48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
        ],
        output,
    )


def watermark_graph(
    image: Path, mark: dict, width: int, height: int, *, window: tuple[float, float] | None = None
) -> str:
    """水印滤镜片段（用在 -vf 链首）：图片按短边比例缩放、乘以不透明度，叠加在画面一角；
    ``window`` 给出时只在该时间段显示（例如不含片头片尾）。"""
    short = min(width, height)
    size = max(2, round(mark.get("scale", 0.12) * short / 2) * 2)
    margin = round(mark.get("margin", 0.03) * short)
    position = mark.get("position", "top-right")
    x = f"{margin}" if position.endswith("left") else f"main_w-overlay_w-{margin}"
    y = f"{margin}" if position.startswith("top") else f"main_h-overlay_h-{margin}"
    enable = f":enable='between(t,{window[0]:.6f},{window[1]:.6f})'" if window else ""
    return (
        f"movie={_escape_filter_path(image)}:loop=0,setpts=N/FRAME_RATE/TB,format=rgba,scale={size}:-2,"
        f"colorchannelmixer=aa={float(mark.get('opacity', 0.8)):.3f}[wm];"
        f"[in][wm]overlay={x}:{y}:shortest=1:format=auto{enable}"
    )


def ass_filter(ass_path: Path, subtitle: SubtitleConfig) -> str:
    """烧录 ASS（样式写在文件里，不再 force_style）；字体目录与 SRT 烧录一致。"""
    parts = f"subtitles={_escape_filter_path(ass_path)}"
    if subtitle.font_file:
        parts += f":fontsdir={_escape_filter_path(Path(subtitle.font_file).parent)}"
    return parts


def mux(
    video: Path,
    audio_track: Path,
    srt: Path,
    output: Path,
    config: Config,
    *,
    burn: bool,
    style: dict | None = None,
    ass: Path | None = None,
    ass_subtitles: bool = True,
    extra_filters: list[str] | None = None,
):
    """封装成片。``ass`` 给出时烧录该 ASS（字幕样式与文字层）；``ass_subtitles=False`` 表示 ASS 只有文字层，
    烧录字幕仍按 SRT + force_style（旧版样式）叠在其上。``burn=False`` 时另带软字幕轨。
    ``extra_filters`` 为烧录前的画面滤镜（水印等）。都没有时与旧版完全相同。"""
    render, subtitle = config.studio.render, config.studio.subtitle
    args = ["-i", str(video), "-i", str(audio_track)]
    filters = list(extra_filters or [])
    if ass is not None:
        filters.append(ass_filter(ass, subtitle))
    if burn and (ass is None or not ass_subtitles):
        filters.append(subtitle_filter(srt, subtitle, style=style))
    if filters:
        args += ["-map", "0:v:0", "-map", "1:a:0"]
        if not burn:
            args[4:4] = ["-i", str(srt)]
            args += ["-map", "2:s:0"]
        args += [
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
        ]
        if not burn:
            args += ["-c:s", "mov_text", "-metadata:s:s:0", "language=chi"]
    else:
        args += [
            "-i",
            str(srt),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map",
            "2:s:0",
            "-c:v",
            "copy",
            "-c:s",
            "mov_text",
            "-metadata:s:s:0",
            "language=chi",
        ]
    args += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    atomic_media(args, output, timeout=max(600, probe(video)["duration"] * 10))


def render_timeline(
    config: Config,
    timeline: Timeline,
    *,
    voice_lookup: Callable[[str], Path],
    source_lookup: Callable[[str], Path],
    workdir: Path,
    interval_lookup: Callable | None = None,
    audio_lookup: Callable | None = None,
) -> dict:
    render, audio_cfg = config.studio.render, config.studio.audio
    session = current.get()
    workdir.mkdir(parents=True, exist_ok=True)
    fps = Fraction(str(render.fps))
    if timeline.burn:
        smoke = session.target("render/font-check.srt") if session else workdir / "font-check.srt"
        save_bytes(smoke, "1\n00:00:00,000 --> 00:00:00,200\n中文字幕字体检查\n".encode())
        check_font(smoke, config.studio.subtitle)

    def produce(relative, key, function):
        logical = f"render/{relative}"
        path = session.reuse(logical, key) if session else None
        if path is None:
            path = session.target(logical) if session else workdir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            function(path)
            if session:
                session.publish(logical, path, key)
        return path

    clips, tracks, boundaries = [], [], []
    total_frames, total_samples = 0, 0
    source_keys = {}
    for event in timeline.events:
        absolute_start, absolute_end = event.video.start, event.video.end
        voice = voice_lookup(event.shot_id)
        if interval_lookup is not None:
            source = interval_lookup(event.video.source_id, absolute_start, absolute_end)
            event = event.model_copy(deep=True)
            event.video.start, event.video.end = 0, absolute_end - absolute_start
        else:
            source = source_lookup(event.video.source_id)
        if source not in source_keys:
            source_keys[source] = {
                "id": event.video.source_id,
                "media_version": "shot-media-v1" if interval_lookup is not None else "legacy",
                "bytes": source.stat().st_size,
                "mtime_ns": source.stat().st_mtime_ns,
            }
        source_key = source_keys[source]
        ambient = None

        def render_ambient(path, source=source, event=event, start=absolute_start, end=absolute_end):
            if audio_lookup is not None:
                raw = path.with_name(path.stem + ".raw.wav")
                audio_lookup(event.video.source_id, start, end, raw)
                extract_ambient(raw, event, audio_cfg, path)
            else:
                extract_ambient(source, event, audio_cfg, path)

        if timeline.audio_mode == "ducking" and probe(source)["audio"]:
            key = fingerprint(
                {
                    "source": source_key,
                    "start": event.video.start,
                    "end": event.video.end,
                    "audio": audio_cfg.model_dump(),
                    "version": 2,
                }
            )
            ambient = produce(
                f"orig/{event.shot_id}.wav",
                key,
                render_ambient,
            )
        voice_key = fingerprint(
            {
                "voice": digest(voice),
                "atempo": event.audio.atempo,
                "mode": timeline.audio_mode,
                "ambient": digest(ambient) if ambient else None,
                "audio": audio_cfg.model_dump(),
                "version": 2,
            }
        )
        processed = produce(
            f"voice/{event.shot_id}.wav",
            voice_key,
            lambda path, voice=voice, ambient=ambient, event=event: process_shot_audio(
                voice, ambient, event, audio_cfg, path
            ),
        )
        with wave.open(str(processed), "rb") as handle:
            samples = handle.getnframes()
            if handle.getframerate() != 48000 or samples <= 0:
                raise MediaError("处理后配音必须为有效的 48kHz PCM")
        frames = math.ceil(Fraction(samples, 48000) * fps)
        end_sample = round(Fraction(total_frames + frames, 1) / fps * 48000)
        if end_sample - total_samples < samples:
            frames += 1
            end_sample = round(Fraction(total_frames + frames, 1) / fps * 48000)
        padded_samples = end_sample - total_samples
        track_key = fingerprint({"voice": digest(processed), "samples": padded_samples, "version": 2})
        track = produce(
            f"audio/{event.shot_id}.wav",
            track_key,
            lambda path, processed=processed, padded_samples=padded_samples: atomic_media(
                [
                    "-i",
                    str(processed),
                    "-af",
                    f"apad=whole_len={padded_samples},atrim=end_sample={padded_samples},asetpts=PTS-STARTPTS",
                    "-c:a",
                    "pcm_s16le",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                ],
                path,
            ),
        )
        video_key = fingerprint(
            {
                "source": source_key,
                "start": event.video.start,
                "end": event.video.end,
                "frames": frames,
                "render": render.model_dump(),
                "version": 2,
            }
        )
        clip = produce(
            f"clips/{event.shot_id}.mp4",
            video_key,
            lambda path, source=source, event=event, frames=frames: cut_shot(
                source, event, render, path, frames=frames
            ),
        )
        boundaries.append(
            {
                "shot_id": event.shot_id,
                "start": float(total_frames / fps),
                "end": float((total_frames + frames) / fps),
                "frames": frames,
                "start_sample": total_samples,
                "end_sample": end_sample,
                "voice_samples": samples,
                "source_id": event.video.source_id,
                "source_path": str(source),
                "source_start": absolute_start,
                "source_end": absolute_end,
            }
        )
        total_frames += frames
        total_samples = end_sample
        clips.append(clip)
        tracks.append(track)
    duration = float(total_frames / fps)
    listing = {
        "fps": str(fps),
        "sample_rate": 48000,
        "total_frames": total_frames,
        "total_samples": total_samples,
        "total_seconds": duration,
        "events": boundaries,
    }
    if session:
        session.json("render/timeline.json", listing)
    else:
        save(workdir / "timeline.json", listing)
    video_track = produce(
        "video.mp4",
        fingerprint([digest(p) for p in clips]),
        lambda path: concat_media(clips, path, timeout=max(600, duration * 10)),
    )
    audio_track = produce(
        "audio_track.wav",
        fingerprint([digest(p) for p in tracks]),
        lambda path: concat_media(tracks, path, timeout=max(600, duration * 10)),
    )
    content = build_srt(timeline, boundaries)
    srt = session.target("render/final.srt") if session else workdir / "final.srt"
    save_bytes(srt, content.encode("utf-8"))
    vtt_content = "WEBVTT\n\n" + re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", content)
    vtt = session.target("render/final.vtt") if session else workdir / "final.vtt"
    save_bytes(vtt, vtt_content.encode("utf-8"))
    if timeline.burn:
        check_font(srt, config.studio.subtitle)
    final = session.target("render/final.mp4") if session else workdir / "final.next.mp4"
    mux(video_track, audio_track, srt, final, config, burn=timeline.burn)
    validate_final(final, duration, timeline.burn, render.fps)
    if session:
        for logical, path in (
            ("render/final.mp4", final),
            ("render/final.srt", srt),
            ("render/final.vtt", vtt),
        ):
            session.publish(
                logical,
                path,
                fingerprint(
                    {
                        "timeline": listing,
                        "subtitle": portable_parameters(config, config.studio.subtitle.model_dump()),
                        "burn": timeline.burn,
                        "sha256": digest(path),
                    }
                ),
            )
    else:
        final.replace(workdir / "final.mp4")
        final = workdir / "final.mp4"
    return {"final": final, "srt": srt, "vtt": vtt, "events": len(timeline.events), "total_seconds": duration}


def validate_final(
    path: Path, duration: float, burn: bool, fps: float, *, size: tuple[int, int] | None = None
):
    import json

    raw = json.loads(command(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)]))
    streams = raw.get("streams", [])
    video = next((s for s in streams if s["codec_type"] == "video"), None)
    audio = next((s for s in streams if s["codec_type"] == "audio"), None)
    if video is None or audio is None:
        raise MediaError("成片缺少视频或配音轨道")
    if not burn and not any(s["codec_type"] == "subtitle" for s in streams):
        raise MediaError("成片缺少软字幕轨道")
    if size is not None and (int(video["width"]), int(video["height"])) != tuple(size):
        raise MediaError(f"成片分辨率 {video['width']}x{video['height']} 与画幅 {size[0]}x{size[1]} 不一致")
    if abs(float(video["duration"]) - duration) > 1 / fps + 0.002:
        raise MediaError("成片视频时长与渲染清单不一致")
    if abs(float(audio["duration"]) - duration) > 0.05:
        raise MediaError("成片配音时长与渲染清单不一致")
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ],
        timeout=max(600, duration * 10),
    )
