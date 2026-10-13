from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from conftest import FixtureSpeaker, FixtureStoryboarder

from mindex import studio, studio_steps
from mindex.adapters import ModelError
from mindex.media import audio_info


def test_fixture_speaker_wav_is_probeable(config):
    speaker = FixtureSpeaker(config)
    text = "四个字的文本"  # 6 个字符 -> 0.3 + 0.12*6 = 1.02 秒
    payload = speaker.speak(text)
    assert speaker.calls == 1 and payload[:4] == b"RIFF"
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "shot.wav"
        path.write_bytes(payload)
        info = audio_info(path)
    assert abs(info["duration"] - (0.3 + 0.12 * len(text))) < 0.05
    assert info["sample_rate"] == 16000 and info["channels"] == 1


def prepare_job(config, text):
    job_id = studio.create_job(config, text)["id"]
    studio_steps.run_step(config, job_id, "split", storyboarder=FixtureStoryboarder(config))
    return job_id


def test_run_tts_writes_manifest_with_exact_durations(config):
    job_id = prepare_job(config, "第一句较长的配音文本。第二句较长的配音文本。")
    speaker = FixtureSpeaker(config)
    studio_steps.run_step(config, job_id, "tts", speaker=speaker)
    assert speaker.calls == 2
    manifest = json.loads((studio.job_dir(config, job_id) / "audio.json").read_text())
    assert manifest["voice"] == config.studio.tts.voice
    assert set(manifest["shots"]) == {"shot-01", "shot-02"}
    for shot_id, item in manifest["shots"].items():
        assert item["duration"] > 0 and item["file"] == f"audio/{shot_id}.wav"
    # 逐镜检查点：重跑不重复调用 TTS
    studio_steps.run_step(config, job_id, "tts", speaker=speaker)
    assert speaker.calls == 2


def test_run_tts_cleans_orphan_audio_after_shot_removal(config):
    job_id = prepare_job(config, "第一句配音文本。第二句配音文本。")
    studio_steps.run_step(config, job_id, "tts", speaker=FixtureSpeaker(config))
    directory = studio.audio_dir(config, job_id)
    assert {path.stem for path in directory.glob("*.wav")} == {"shot-01", "shot-02"}
    # 人工删镜后再重跑：孤儿音频被清理
    storyboard = studio.load_storyboard(config, job_id)
    studio.save_storyboard(
        config,
        job_id,
        studio.Storyboard(job_id=job_id, shots=storyboard.shots[:1]),
    )
    speaker = FixtureSpeaker(config)
    studio_steps.run_step(config, job_id, "tts", speaker=speaker)
    assert {path.stem for path in directory.glob("*.wav")} == {"shot-01"}
    manifest = json.loads((studio.job_dir(config, job_id) / "audio.json").read_text())
    assert set(manifest["shots"]) == {"shot-01"}


def test_run_tts_rejects_empty_audio(config):
    job_id = prepare_job(config, "空音频的文本。")

    class EmptySpeaker:
        def speak(self, text, voice=None):
            return b""

    with pytest.raises(ModelError, match="空音频"):
        studio_steps.run_step(config, job_id, "tts", speaker=EmptySpeaker())
    assert studio.load_job(config, job_id)["steps"]["tts"]["status"] == "failed"
    # 失败后可换正常 speaker 重试
    studio_steps.run_step(config, job_id, "tts", speaker=FixtureSpeaker(config))
    assert studio.load_job(config, job_id)["steps"]["tts"]["status"] == "complete"

