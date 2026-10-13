from __future__ import annotations

import math
import struct
import wave

import pytest

from mindex import studio, studio_render, studio_steps
from mindex.media import MediaError, audio_info, command, probe
from mindex.pipeline import Pipeline


@pytest.fixture
def video_with_audio(tmp_path):
    path = tmp_path / "带音轨素材.mp4"
    command(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=red:s=160x120:r=25:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(path),
        ]
    )
    return path


def write_tone(path, duration: float, rate: int = 16000, frequency: int = 440):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        for index in range(int(rate * duration)):
            handle.writeframes(struct.pack("<h", int(8000 * math.sin(2 * math.pi * frequency * index / rate))))


def build_job(config, video_file, events_spec, *, burn=False, audio_mode="tts_only"):
    """events_spec: [(shot_id, source_start, source_end, audio_seconds)]"""
    pipeline = Pipeline(config)
    source = pipeline.prepare(video_file)
    job_id = studio.create_job(config, "渲染测试文案。", options={"burn": burn, "audio_mode": audio_mode})["id"]
    for step in ("split", "recall", "tts", "select"):
        studio.begin_step(config, job_id, step)
        studio.complete_step(config, job_id, step, {})
    audio_dir = studio.audio_dir(config, job_id)
    audio_dir.mkdir(parents=True, exist_ok=True)
    events = []
    for order, (shot_id, start, end, seconds) in enumerate(events_spec, 1):
        wav = audio_dir / f"{shot_id}.wav"
        write_tone(wav, seconds)
        duration = audio_info(wav)["duration"]
        events.append(
            studio.TimelineEvent(
                shot_id=shot_id,
                order=order,
                audio=studio.TimelineAudio(
                    file=f"audio/{shot_id}.wav", duration=duration, atempo=1.0, render_duration=duration
                ),
                video=studio.TimelineVideo(
                    source_id=source,
                    source_name="测试素材",
                    segment_id=f"{shot_id}-anchor",
                    segment_ids=[f"{shot_id}-anchor"],
                    start=start,
                    end=end,
                    alignment="manual",
                ),
                subtitle=studio.TimelineSubtitle(start=0.0, end=duration, text=f"{shot_id} 的字幕"),
            )
        )
    timeline = studio.Timeline(
        job_id=job_id, mode="manual", audio_mode=audio_mode, burn=burn, events=events
    )
    studio.write_timeline(config, job_id, timeline)
    return job_id, timeline


def test_render_soft_subtitle_outputs_video_srt_and_audio(config, video_file):
    job_id, _timeline = build_job(
        config,
        video_file,
        [("shot-01", 0.0, 1.0, 1.0), ("shot-02", 2.0, 4.0, 2.0)],
        burn=False,
    )
    result = studio_steps.run_step(config, job_id, "render")
    assert result["events"] == 2 and result["final"] == "render/final.mp4"
    final = studio.render_dir(config, job_id) / "final.mp4"
    srt = studio.render_dir(config, job_id) / "final.srt"
    assert final.is_file() and srt.is_file()
    info = probe(final)
    assert abs(info["duration"] - 3.0) < 0.4  # 两镜配音时长之和
    assert info["audio"]  # TTS 音轨存在
    raw = probe_streams(final)
    assert any(stream["codec_type"] == "subtitle" for stream in raw)  # mov_text 软字幕轨
    content = srt.read_text()
    assert "shot-01 的字幕" in content and "shot-02 的字幕" in content
    assert "00:00:01,000 --> 00:00:03,000" in content  # 第二镜字幕偏移 = 第一镜时长


def probe_streams(path):
    import json as json_module

    from mindex.media import command

    return json_module.loads(
        command(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)], timeout=30)
    )["streams"]


def test_render_checkpoints_skip_existing_clips(config, video_file):
    job_id, _ = build_job(config, video_file, [("shot-01", 0.0, 1.0, 1.0)], burn=False)
    studio_steps.run_step(config, job_id, "render")
    clips = list((studio.render_dir(config, job_id) / "clips").glob("*.mp4"))
    assert len(clips) == 1
    first_mtime = clips[0].stat().st_mtime_ns
    # 删除最终产物重渲染：逐镜 clip 检查点复用（mtime 不变）
    (studio.render_dir(config, job_id) / "final.mp4").unlink()
    studio_steps.run_step(config, job_id, "render")
    assert clips[0].stat().st_mtime_ns == first_mtime
    assert (studio.render_dir(config, job_id) / "final.mp4").is_file()


def test_render_ducking_mixes_ambient_audio(config, video_with_audio):
    job_id, _timeline = build_job(
        config,
        video_with_audio,
        [("shot-01", 0.0, 1.0, 1.0)],
        burn=False,
        audio_mode="ducking",
    )
    studio_steps.run_step(config, job_id, "render")
    final = studio.render_dir(config, job_id) / "final.mp4"
    assert final.is_file()
    assert (studio.render_dir(config, job_id) / "orig" / "shot-01.wav").is_file()  # 原声已提取
    assert abs(probe(final)["duration"] - 1.0) < 0.4


def test_render_ducking_degrades_gracefully_without_audio_track(config, video_file):
    """无音轨素材在 ducking 模式下自动退化为纯配音，不失败。"""
    job_id, _timeline = build_job(
        config,
        video_file,
        [("shot-01", 0.0, 1.0, 1.0)],
        burn=False,
        audio_mode="ducking",
    )
    studio_steps.run_step(config, job_id, "render")
    final = studio.render_dir(config, job_id) / "final.mp4"
    assert final.is_file() and probe(final)["audio"]
    assert not (studio.render_dir(config, job_id) / "orig" / "shot-01.wav").is_file()


def test_render_burned_subtitle_or_font_hint(config, video_file):
    if not studio_render.has_subtitles_filter():
        pytest.skip("当前 ffmpeg 未编译 subtitles 滤镜（缺 libass），烧录路径不可测")
    job_id, _timeline = build_job(
        config,
        video_file,
        [("shot-01", 0.0, 1.0, 1.0)],
        burn=True,
    )
    try:
        studio_steps.run_step(config, job_id, "render")
    except MediaError as exc:
        if "字体" not in str(exc):
            raise
        pytest.skip("当前环境缺少可用的 CJK 字体，跳过烧录断言")
    final = studio.render_dir(config, job_id) / "final.mp4"
    assert final.is_file()
    raw = probe_streams(final)
    assert not any(stream["codec_type"] == "subtitle" for stream in raw)  # 烧录无软字幕轨
