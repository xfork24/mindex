"""成片后期管线：逐镜对齐 → 画面拼接 → 配音拼接与背景音乐 → 字幕 → 封装与校验。

``compose`` 是 compose 阶段的执行入口。每一步只在用到时参与：未使用任何后期能力的方案，
ffmpeg 参数、产物与签名都与拆分前逐字节一致（``compose_payload`` 返回空）。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from shared.media import atomic_media
from shared.work_budget import copy_file
from studio import studio

from .plans import canvas_of, canvas_render
from .versions import file_info, file_path, ordered_shots, version

MUSIC_FADE_IN = 0.5
MUSIC_FADE_OUT = 1.5
# 配音出现时背景音乐的侧链压低：配音已归一到约 -16 LUFS（RMS 约 0.12），阈值取 0.06、压缩比 3，
# 说话时背景音乐约压低 3–6 dB 但仍清晰可闻。旧参数（阈值 0.03、比 6）会把整段配音下的背景音乐压到近乎无声，
# 改动混音参数时递增 MUSIC_MIX_VERSION（进入有背景音乐的成片签名，使旧成片需要重新合成）。
MUSIC_MIX_VERSION = 2
MUSIC_DUCKING = "threshold=0.06:ratio=3:attack=50:release=500"


@dataclass
class Assembly:
    """按分镜顺序对齐后的成片素材：每镜画面、补齐到帧边界的配音音轨、时间线事件与边界。"""

    fps: Fraction
    clips: list[Path] = field(default_factory=list)
    handles: list[int] = field(default_factory=list)  # 每镜画面末尾多出的余量帧（重叠转场）
    tracks: list[Path] = field(default_factory=list)
    events: list = field(default_factory=list)
    boundaries: list[dict] = field(default_factory=list)
    total_frames: int = 0
    total_samples: int = 0

    @property
    def seconds(self) -> float:
        return float(self.total_frames / self.fps)


def brand_effect(config, p, store=None) -> dict | None:
    """方案生效的品牌内容（``brands.resolve``）；没选品牌也没有覆盖时不访问数据库，返回 None。"""
    from studio import brands

    options = p.get("options") or {}
    if not options.get("brand_id") and not options.get("brand_overrides"):
        return None
    if callable(store):  # 签名资源的惰性数据库
        store = store()
    if store is None:
        from shared.storage import Store

        store = Store.open(config)
    return brands.resolve(store, options)


def subtitle_plan(config, w, p, store=None) -> tuple[dict | None, dict[str, list[str]]]:
    """烧录 ASS 字幕的样式与各镜高亮词。只有烧录字幕且（有字幕样式或有高亮词）时样式非 None；
    否则成片沿用 SRT + force_style（与旧版一致）。

    样式优先级：方案覆盖里的完整样式 > 方案参数 ``subtitle_style`` 预设 > 品牌模板样式 >
    classic（只有高亮词时）。"""
    from . import ass

    if not p["options"].get("burn"):
        return None, {}
    highlights = {}
    for sid in ordered_shots(w, p):
        words = version(w, p["shots"][sid]["content"])["data"].get("highlights")
        if words:
            highlights[sid] = list(words)
    name = p["options"].get("subtitle_style")
    override = (p["options"].get("brand_overrides") or {}).get("subtitle")
    brand = brand_effect(config, p, store)
    style = (
        (brand or {}).get("subtitle")
        if override
        else ass.preset(name)
        if name
        else (brand or {}).get("subtitle")
    )
    if style is None and not highlights:
        return None, {}
    return style or ass.preset("classic"), highlights


def overlay_plan(w, p) -> list[tuple[str, list[dict]]]:
    """各镜用字幕引擎（engine=ass）烧录的文字层；HTML 文字层在分镜渲染时叠加，不在这里。"""
    found = []
    for sid in ordered_shots(w, p):
        layers = version(w, p["shots"][sid]["content"])["data"].get("overlays") or []
        layers = [layer for layer in layers if layer.get("engine", "ass") == "ass"]
        if layers:
            found.append((sid, layers))
    return found


def overlay_style(config, w, p, store=None) -> dict:
    """文字层样式：品牌模板（含方案覆盖）给出时用它，否则为缺省样式。"""
    from . import ass

    return (brand_effect(config, p, store) or {}).get("overlay") or dict(ass.DEFAULT_OVERLAY_STYLE)


def compose_payload(config, w, p, store=None) -> dict:
    """后期能力进入 compose 签名的键；都未参与时为空（签名与旧版一致）。"""
    from . import ass, transitions

    payload = {}
    if found := transitions.overlaps(w, p):
        payload["transitions"] = found
        payload["transition_version"] = transitions.TRANSITION_VERSION
    style, highlights = subtitle_plan(config, w, p, store)
    if style is not None:
        payload["subtitle_style"] = style
        if highlights:
            payload["highlights"] = [[sid, words] for sid, words in highlights.items()]
    if layers := overlay_plan(w, p):
        payload["overlays"] = [[sid, items] for sid, items in layers]
        payload["overlay_style"] = overlay_style(config, w, p, store)
    if style is not None or layers:
        payload["ass_version"] = ass.ASS_VERSION
    if brand := brand_assets(config, p, store):
        payload["brand_assets"] = brand
        payload["bookend_version"] = BOOKEND_VERSION
    if needs_clean(p):
        payload["clean"] = True
    from . import beat_align

    if aligned := beat_align.compose_payload(p):
        payload["beat_align"] = aligned
    return payload


def needs_clean(p) -> bool:
    """有快速派生的多画幅导出时，成片另存一份无字幕、无水印的画面（``clean.mp4``）供导出阶段重新排版。"""
    return any(item.get("mode", "derive") == "derive" for item in p["options"].get("exports") or [])


# 改动片头片尾的规范化或水印叠加规则时递增：进入有片头片尾或水印的成片签名。
BOOKEND_VERSION = 1


def brand_assets(config, p, store=None) -> dict:
    """方案生效的片头、片尾与水印（品牌模板经方案覆盖后）；都没有时为空。"""
    effect = brand_effect(config, p, store) or {}
    return {key: effect[key] for key in ("intro", "outro", "watermark") if effect.get(key)}


def bookend_clip(config, p, clip: dict, directory: Path, name: str) -> tuple[Path, Path, int, int]:
    """片头或片尾视频 → 方案画幅与帧率的画面（确定帧数）与等长的 48k 立体声（无原声时静音）。"""
    from shared.media import probe
    from studio import brands
    from studio.render import fit_filters

    source = brands.require_asset(config, clip["asset"])
    render, fit = canvas_render(config, p)
    fps = Fraction(str(render.fps))
    info = probe(source)
    frames = max(1, round(Fraction(str(info["duration"])) * fps))
    samples = round(Fraction(frames, 1) / fps * 48000)
    video, audio = directory / f"{name}.mp4", directory / f"{name}.wav"
    atomic_media(
        [
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            ",".join(
                [
                    f"fps={fps.numerator}/{fps.denominator}",
                    *fit_filters(render.width, render.height, fit),
                    "setsar=1",
                    "tpad=stop_mode=clone:stop_duration=1",
                ]
            ),
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
        video,
    )
    if info["audio"]:
        args = [
            "-i",
            str(source),
            "-vn",
            "-af",
            f"aresample=48000,volume={float(clip.get('volume', 1.0)):.4f},apad=whole_len={samples},"
            f"atrim=end_sample={samples},asetpts=PTS-STARTPTS",
        ]
    else:
        args = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-af", f"atrim=end_sample={samples}"]
    atomic_media([*args, "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"], audio)
    return video, audio, frames, samples


def add_bookends(
    config, w, p, assembly: Assembly, video: Path, audio: Path, directory: Path
) -> tuple[Path, Path, dict]:
    """把品牌片头片尾接在正文前后：画面与音轨拼接，正文边界整体后移；背景音乐只在正文里（已先混好）。"""
    from studio.render import concat_media

    brand = brand_assets(config, p)
    if not brand.get("intro") and not brand.get("outro"):
        return video, audio, {}
    report, videos, audios = {}, [video], [audio]
    fps = assembly.fps
    for name in ("intro", "outro"):
        if not brand.get(name):
            continue
        clip_video, clip_audio, frames, samples = bookend_clip(config, p, brand[name], directory, name)
        report[name] = {
            "frames": frames,
            "samples": samples,
            "seconds": float(frames / fps),
            "asset": brand[name]["asset"],
        }
        if name == "intro":
            videos.insert(0, clip_video)
            audios.insert(0, clip_audio)
        else:
            videos.append(clip_video)
            audios.append(clip_audio)
    if intro := report.get("intro"):
        for item in assembly.boundaries:
            item["start"] += intro["seconds"]
            item["end"] += intro["seconds"]
            item["start_sample"] += intro["samples"]
            item["end_sample"] += intro["samples"]
    body_frames, body_samples = assembly.total_frames, assembly.total_samples
    assembly.total_frames += sum(item["frames"] for item in report.values())
    assembly.total_samples += sum(item["samples"] for item in report.values())
    report["body"] = {
        "start": report.get("intro", {}).get("seconds", 0.0),
        "seconds": float(body_frames / fps),
        "frames": body_frames,
        "samples": body_samples,
    }
    joined_video, joined_audio = directory / "video-bookends.mp4", directory / "audio-bookends.wav"
    concat_media(videos, joined_video)
    concat_media(audios, joined_audio)
    return joined_video, joined_audio, report


def watermark_filter(config, p, assembly: Assembly, bookends: dict) -> list[str]:
    """水印叠加滤镜（烧录之前）；没有水印时为空。不含片头片尾时只在正文时间段显示。"""
    from studio import brands
    from studio.render import watermark_graph

    mark = brand_assets(config, p).get("watermark")
    if not mark:
        return []
    render, _ = canvas_render(config, p)
    window = None
    if bookends and not mark.get("include_bookends"):
        body = bookends["body"]
        window = (body["start"], body["start"] + body["seconds"])
    return [
        watermark_graph(
            brands.require_asset(config, mark["asset"]), mark, render.width, render.height, window=window
        )
    ]


def beat_delays(config, job_id, w, p, items, fps) -> list[int]:
    """开启节拍对齐时每个切点的推迟帧数（见 ``beat_align.delays``）；关闭或没有背景音乐时全为 0。"""
    from studio import beats

    from . import beat_align, music, transitions

    found = beat_align.tolerance(p)
    segments = music.segments(w, p) if found is not None else []
    if not segments:
        return [0] * len(items)
    analyses = {}
    for segment in segments:
        item = version(w, p["shots"][segment["cue"]]["music"])
        if item["status"] != "complete":
            raise studio.StudioError("背景音乐尚未成功生成，请先执行背景音乐")
        try:
            result = beats.analyze(file_path(config, job_id, item, "music"))
        except ValueError:
            continue  # 太短无法分析的音乐不参与对齐
        analyses[segment["cue"]] = (result["beats"], result["duration"])
    skip = {sid for sid, _ in transitions.overlaps(w, p)}
    sids = [sid for sid, _ in items]
    return beat_align.delays(
        [item["data"]["frames"] for _, item in items],
        [int(item["data"].get("handle_frames") or 0) for _, item in items],
        sids,
        segments,
        analyses,
        fps,
        found,
        skip,
    )


def assemble(config, job_id, w, p, directory) -> Assembly:
    """逐镜读取渲染版本：校验帧率与帧数，把配音补齐到帧边界对应的采样数，并计算成片时间边界。
    开启节拍对齐时切点推迟到节拍：本镜画面多取余量帧、配音补静音（``beat_delays``）。"""
    fps = Fraction(str(config.studio.render.fps))
    result = Assembly(fps=fps)
    items = [(sid, version(w, p["shots"][sid]["render"])) for sid in ordered_shots(w, p)]
    delays = beat_delays(config, job_id, w, p, items, fps)
    for index, (sid, item) in enumerate(items, 1):
        if item["data"]["fps"] != str(fps):
            raise studio.StudioError("分镜帧率与成片帧率不一致")
        event = studio.TimelineEvent.model_validate(item["data"]["event"])
        event.order = index
        delay = delays[index - 1]
        frames = item["data"]["frames"] + delay
        end_sample = round(Fraction(result.total_frames + frames, 1) / fps * 48000)
        samples = end_sample - result.total_samples
        if samples < item["data"]["samples"]:
            raise studio.StudioError("分镜帧数不足以容纳配音，请重新渲染")
        track = directory / f"track-{index}.wav"
        atomic_media(
            [
                "-i",
                str(file_path(config, job_id, item, "audio")),
                "-af",
                f"apad=whole_len={samples},atrim=end_sample={samples},asetpts=PTS-STARTPTS",
                "-c:a",
                "pcm_s16le",
                "-ar",
                "48000",
                "-ac",
                "2",
            ],
            track,
        )
        result.clips.append(file_path(config, job_id, item, "video"))
        result.handles.append(int(item["data"].get("handle_frames") or 0) - delay)
        result.tracks.append(track)
        result.events.append(event)
        boundary = {
            "shot_id": sid,
            "start": float(result.total_frames / fps),
            "end": float((result.total_frames + frames) / fps),
            "frames": frames,
            "start_sample": result.total_samples,
            "end_sample": end_sample,
        }
        if delay:  # 只在切点被推迟时加键：旧成片数据形态不变
            boundary["beat_delay"] = delay
        result.boundaries.append(boundary)
        result.total_frames += frames
        result.total_samples = end_sample
    return result


def join_video(config, w, p, assembly: Assembly, output: Path) -> Path:
    """拼接逐镜画面。硬切处直接复制码流；重叠转场相连的一组镜头先用 xfade 合成一段（吃掉尾部余量），
    再与其余镜头一起复制拼接，所以只有带转场的那几镜被重新编码。"""
    from studio.render import concat_media

    from . import transitions

    kinds = dict((sid, item) for sid, item in transitions.overlaps(w, p))
    sids = [item["shot_id"] for item in assembly.boundaries]
    if not kinds and not any(assembly.handles):
        concat_media(assembly.clips, output)
        return output
    pieces, index, group = [], 0, 0
    while index < len(sids):
        end = index
        while end < len(sids) - 1 and sids[end] in kinds and assembly.handles[end]:
            end += 1
        if end == index and assembly.handles[index]:
            # 节拍对齐留下的余量：只取到（推迟后的）切点，重新编码这一镜。
            target = output.with_name(f"trim-{index + 1}.mp4")
            trim_clip(config, p, assembly.clips[index], assembly.boundaries[index]["frames"], target)
            pieces.append(target)
        elif end == index:
            pieces.append(assembly.clips[index])
        else:
            group += 1
            target = output.with_name(f"xfade-{group}.mp4")
            xfade_group(config, p, assembly, list(range(index, end + 1)), kinds, sids, target)
            pieces.append(target)
        index = end + 1
    concat_media(pieces, output)
    for piece in pieces:
        if piece.name.startswith(("xfade-", "trim-")):
            piece.unlink(missing_ok=True)
    return output


def trim_clip(config, p, clip: Path, frames: int, output: Path):
    """取分镜画面的前 ``frames`` 帧（去掉没用到的尾部余量）。"""
    render, _ = canvas_render(config, p)
    atomic_media(
        [
            "-i",
            str(clip),
            "-map",
            "0:v:0",
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
        timeout=max(600, frames),
    )


def xfade_group(
    config, p, assembly: Assembly, members: list[int], kinds: dict, sids: list[str], output: Path
):
    """一组以重叠转场相连的镜头 → 一段画面：第 i 镜与第 i+1 镜在第 i 镜的余量上交叉过渡，
    输出帧数 = 各镜 frames 之和（最后一镜没有余量）。"""
    fps = assembly.fps
    render, _ = canvas_render(config, p)
    rate = f"{fps.numerator}/{fps.denominator}"
    args, graph = [], []
    for position, index in enumerate(members):
        args += ["-i", str(assembly.clips[index])]
        graph.append(f"[{position}:v]fps={rate},settb=AVTB,setpts=PTS-STARTPTS,format=yuv420p[v{position}]")
    current, elapsed = "v0", 0
    for position, index in enumerate(members[:-1]):
        elapsed += assembly.boundaries[index]["frames"]
        # 过渡时长取转场设置（帧对齐）：余量可能因节拍对齐而更长，多出的余量不参与过渡。
        overlap = min(assembly.handles[index], round(Fraction(str(kinds[sids[index]]["duration"])) * fps))
        kind = kinds[sids[index]].get("kind", "fade")
        label = f"x{position}"
        graph.append(
            f"[{current}][v{position + 1}]xfade=transition={kind}:duration={float(overlap / fps):.6f}"
            f":offset={float(elapsed / fps):.6f}[{label}]"
        )
        current = label
    frames = sum(assembly.boundaries[index]["frames"] for index in members)
    atomic_media(
        [
            *args,
            "-filter_complex",
            ";".join(graph),
            "-map",
            f"[{current}]",
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
    return output


def music_report(w, p, boundaries) -> list[dict]:
    """成片里实际混入的背景音乐段落（写进成片版本数据，供页面列出素材）：
    起始分镜、覆盖分镜、音乐版本、音量，以及在成片时间线上的起止秒。没有背景音乐时为空。"""
    from . import music

    spans = {item["shot_id"]: item for item in boundaries}
    report = []
    for segment in music.segments(w, p):
        vid = p["shots"][segment["cue"]].get("music")
        entry = {
            "cue": segment["cue"],
            "shots": segment["shots"],
            "version": vid,
            "volume": float(segment["music"].get("volume", 0.25)),
            "kind": segment["music"].get("kind"),
            "prompt": segment["music"].get("prompt"),
            "start": spans[segment["shots"][0]]["start"],
            "end": spans[segment["shots"][-1]]["end"],
        }
        if (
            segment["music"].get("kind") == "library"
        ):  # 曲库音乐带曲名与署名（发布元数据的音乐署名）；生成音乐形态不变
            data = (w["versions"].get(vid or "") or {}).get("data") or {}
            entry.update(
                {key: data.get(key) for key in ("track_id", "title", "artist", "license", "attribution")}
            )
        report.append(entry)
    return report


def mix_music(config, job_id, w, p, directory, voice, boundaries, total_samples):
    """背景音乐段落 → 与配音等长的背景音轨（段内循环/截断、淡入淡出、按音量缩放，段外静音），
    再以配音为侧链自动压低后与配音混合。没有背景音乐段落时返回 None（成片音轨与旧版完全相同）。"""
    from . import music

    segments = music.segments(w, p)
    if not segments:
        return None
    spans = {item["shot_id"]: (item["start_sample"], item["end_sample"]) for item in boundaries}
    pieces, cursor = [], 0

    def silence(samples, index):
        target = directory / f"bgm-gap-{index}.wav"
        atomic_media(
            [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-af",
                f"atrim=end_sample={samples}",
                "-c:a",
                "pcm_s16le",
                "-ar",
                "48000",
                "-ac",
                "2",
            ],
            target,
        )
        return target

    for index, segment in enumerate(segments, 1):
        start = spans[segment["shots"][0]][0]
        end = spans[segment["shots"][-1]][1]
        if start > cursor:
            pieces.append(silence(start - cursor, index))
        samples = end - start
        seconds = samples / 48000
        item = version(w, p["shots"][segment["cue"]]["music"])
        if item["status"] != "complete":
            raise studio.StudioError("背景音乐尚未成功生成，请先执行背景音乐")
        source = file_path(config, job_id, item, "music")
        volume = float(segment["music"].get("volume", 0.25))
        fade_out = min(MUSIC_FADE_OUT, seconds / 3)
        target = directory / f"bgm-{index}.wav"
        atomic_media(
            [
                "-stream_loop",
                "-1",
                "-i",
                str(source),
                "-vn",
                "-af",
                f"aresample=48000,volume={volume:.4f},afade=t=in:d={min(MUSIC_FADE_IN, seconds / 3):.3f},"
                f"apad=whole_len={samples},atrim=end_sample={samples},"
                f"afade=t=out:st={max(0.0, seconds - fade_out):.3f}:d={fade_out:.3f},asetpts=PTS-STARTPTS",
                "-c:a",
                "pcm_s16le",
                "-ar",
                "48000",
                "-ac",
                "2",
            ],
            target,
        )
        pieces.append(target)
        cursor = end
    if total_samples > cursor:
        pieces.append(silence(total_samples - cursor, len(segments) + 1))
    from studio.render import concat_media

    bed = directory / "bgm.wav"
    concat_media(pieces, bed)
    mixed = directory / "audio-mixed.wav"
    atomic_media(
        [
            "-i",
            str(voice),
            "-i",
            str(bed),
            "-filter_complex",
            "[0:a]asplit=2[voice][key];"
            f"[1:a][key]sidechaincompress={MUSIC_DUCKING}[bed];"
            "[voice][bed]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            f"atrim=end_sample={total_samples}",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
        ],
        mixed,
    )
    for piece in pieces:
        piece.unlink(missing_ok=True)
    return mixed


def join_audio(config, job_id, w, p, assembly: Assembly, directory) -> Path:
    """拼接逐镜配音，有背景音乐段落时铺在配音下面。"""
    from studio.render import concat_media

    audio = directory / "audio.wav"
    concat_media(assembly.tracks, audio)
    bgm = mix_music(config, job_id, w, p, directory, audio, assembly.boundaries, assembly.total_samples)
    return bgm if bgm is not None else audio


def write_subtitles(p, timeline, assembly: Assembly, directory) -> tuple[Path, Path]:
    """按成片边界写 SRT 与 VTT。"""
    from studio.render import build_srt

    content = build_srt(timeline, assembly.boundaries)
    srt, vtt = directory / "final.srt", directory / "final.vtt"
    studio.save_bytes(srt, content.encode())
    studio.save_bytes(
        vtt, ("WEBVTT\n\n" + re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", content)).encode()
    )
    return srt, vtt


def write_ass(config, w, p, assembly: Assembly, directory) -> tuple[Path | None, bool]:
    """需要 ASS 时写 ``final.ass``：样式化字幕与高亮词（烧录且选了样式或有高亮词）、字幕引擎文字层。
    返回 ``(路径或 None, ASS 是否含字幕)``；只有文字层时字幕仍按旧版 SRT 样式烧录（或保持软字幕）。"""
    cues = [
        {"shot_id": item["shot_id"], "start": item["start"], "end": item["end"], "text": event.subtitle.text}
        for item, event in zip(assembly.boundaries, assembly.events, strict=True)
    ]
    return write_ass_cues(config, w, p, cues, directory)


def subtitle_cues(w, p, boundaries) -> list[dict]:
    """由成片边界与分镜旁白重建字幕时间（多画幅派生导出用，与成片字幕一致）。"""
    return [
        {
            "shot_id": item["shot_id"],
            "start": item["start"],
            "end": item["end"],
            "text": version(w, p["shots"][item["shot_id"]]["content"])["data"]["text"],
        }
        for item in boundaries
    ]


def write_ass_cues(config, w, p, cues, directory, name: str = "final.ass") -> tuple[Path | None, bool]:
    """按 ``cues``（成片时间的字幕与镜头边界）写 ASS；方案画幅决定 PlayRes 与字号换算。"""
    from . import ass

    style, highlights = subtitle_plan(config, w, p)
    layers = overlay_plan(w, p)
    if style is None and not layers:
        return None, False
    render, _ = canvas_render(config, p)
    spans = {item["shot_id"]: (item["start"], item["end"]) for item in cues}
    font = config.studio.subtitle.font_family
    look = overlay_style(config, w, p)
    events = ass.layer_events(
        [(sid, *spans[sid], items) for sid, items in layers], look, width=render.width, height=render.height
    )
    styles = ass.layer_styles(look, font=font, width=render.width, height=render.height) if layers else []
    target = directory / name
    studio.save_bytes(
        target,
        ass.build_ass(
            cues,
            width=render.width,
            height=render.height,
            font=font,
            font_size=config.studio.subtitle.font_size,
            style=style,
            highlights=highlights,
            layers=events,
            layer_styles=styles,
        ).encode(),
    )
    return target, style is not None


def finish(
    config,
    p,
    video,
    audio,
    srt,
    timeline,
    assembly: Assembly,
    output: Path,
    *,
    ass_path: Path | None = None,
    ass_subtitles: bool = True,
    extra_filters: list[str] | None = None,
) -> Path:
    """封装成片（烧录或软字幕）并校验尺寸与时长。"""
    from studio.render import canvas_subtitle_style, check_font, mux, validate_final

    if timeline.burn or ass_path is not None:
        check_font(srt, config.studio.subtitle)
    render, _ = canvas_render(config, p)
    style = canvas_subtitle_style(canvas_of(p)[0], render.width, render.height, config.studio.subtitle)
    mux(
        video,
        audio,
        srt,
        output,
        config,
        burn=timeline.burn,
        style=style,
        ass=ass_path,
        ass_subtitles=ass_subtitles,
        extra_filters=extra_filters,
    )
    validate_final(
        output, assembly.seconds, timeline.burn, config.studio.render.fps, size=(render.width, render.height)
    )
    return output


def provenance(w, p) -> dict:
    """成片的依赖闭包：分镜工作台与全部分镜渲染版本递归引用的版本快照。"""
    graph_ids = set()

    def collect(vid):
        if vid in graph_ids:
            return
        graph_ids.add(vid)
        for dep in version(w, vid)["inputs"].values():
            if dep:
                collect(dep)

    collect(p["board"])
    for sid in ordered_shots(w, p):
        collect(p["shots"][sid]["render"])
    return {vid: copy.deepcopy(version(w, vid)) for vid in graph_ids}


def compose(config, job_id, w, p, directory):
    assembly = assemble(config, job_id, w, p, directory)
    timeline = studio.Timeline(
        job_id=job_id,
        mode="manual",
        audio_mode=p["options"]["audio_mode"],
        burn=p["options"]["burn"],
        events=assembly.events,
    )
    video = join_video(config, w, p, assembly, directory / "video.mp4")
    audio = join_audio(config, job_id, w, p, assembly, directory)
    video, audio, bookends = add_bookends(config, w, p, assembly, video, audio, directory)
    clean = None
    if needs_clean(p):
        clean = directory / "clean.mp4"
        copy_file(video, clean)
    srt, vtt = write_subtitles(p, timeline, assembly, directory)
    ass_path, ass_subtitles = write_ass(config, w, p, assembly, directory)
    final = finish(
        config,
        p,
        video,
        audio,
        srt,
        timeline,
        assembly,
        directory / "final.mp4",
        ass_path=ass_path,
        ass_subtitles=ass_subtitles,
        extra_filters=watermark_filter(config, p, assembly, bookends),
    )
    data = {
        "plan": copy.deepcopy(p),
        "graph": provenance(w, p),
        "total_frames": assembly.total_frames,
        "total_samples": assembly.total_samples,
        "total_seconds": assembly.seconds,
        "boundaries": assembly.boundaries,
        "burn": timeline.burn,
    }
    if report := music_report(w, p, assembly.boundaries):  # 只在有背景音乐时加键，旧成片数据形态不变
        data["music"] = report
    if bookends:  # 片头片尾：正文边界已整体后移
        data["bookends"] = bookends
    if mark := brand_assets(config, p).get("watermark"):
        data["watermark"] = mark
    studio.save(directory / "timeline.json", data)
    return data, {
        name: file_info(config, job_id, target)
        for name, target in (
            ("mp4", final),
            ("srt", srt),
            ("vtt", vtt),
            ("timeline", directory / "timeline.json"),
            *([("ass", ass_path)] if ass_path is not None else []),
            *([("clean", clean)] if clean is not None else []),
        )
    }
