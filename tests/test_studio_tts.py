from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from conftest import FixtureSpeaker, FixtureStoryboarder, legacy_job

from mindex.models.adapters import ModelError
from mindex.shared.media import audio_info
from mindex.studio import studio, studio_steps


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
    job_id = legacy_job(config, text)["id"]
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


def test_run_tts_preserves_history_after_shot_removal(config):
    job_id = prepare_job(config, "第一句配音文本。第二句配音文本。")
    studio_steps.run_step(config, job_id, "tts", speaker=FixtureSpeaker(config))
    directory = studio.audio_dir(config, job_id)
    assert {path.stem for path in directory.glob("*.wav")} == {"shot-01", "shot-02"}
    # 人工删镜后再重跑：历史音频保留，但不进入当前清单
    storyboard = studio.load_storyboard(config, job_id)
    studio.save_storyboard(
        config,
        job_id,
        studio.Storyboard(job_id=job_id, shots=storyboard.shots[:1]),
    )
    speaker = FixtureSpeaker(config)
    studio_steps.run_step(config, job_id, "tts", speaker=speaker)
    assert {path.stem for path in directory.glob("*.wav")} == {"shot-01", "shot-02"}
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


def test_edit_text_regenerates_only_changed_audio_and_preserves_attempt(config):
    job_id = prepare_job(config, "第一句配音文本。第二句配音文本。")
    speaker = FixtureSpeaker(config)
    studio_steps.run_step(config, job_id, "tts", speaker=speaker)
    registry = json.loads((studio.job_dir(config, job_id) / "checkpoints.json").read_text())
    old = studio.job_dir(config, job_id) / registry["audio/shot-01.wav"]["path"]
    old_bytes = old.read_bytes()
    storyboard = studio.load_storyboard(config, job_id)
    storyboard.shots[0].text = "这是经过编辑后明显更长的第一句配音文本。"
    studio.save_storyboard(config, job_id, storyboard)
    studio_steps.run_step(config, job_id, "tts", speaker=speaker)
    assert speaker.calls == 3
    assert old.read_bytes() == old_bytes
    assert (studio.audio_dir(config, job_id) / "shot-01.wav").read_bytes() != old_bytes
    manifest = json.loads((studio.job_dir(config, job_id) / "audio.json").read_text())["shots"]
    assert manifest["shot-01"]["duration"] > manifest["shot-02"]["duration"]


def test_force_one_shot_and_retry_partial_failure(config):
    job_id = prepare_job(config, "第一句配音文本。第二句配音文本。")
    speaker = FixtureSpeaker(config)

    class FailsSecond:
        def speak(self, text, voice=None):
            if "第二句" in text:
                raise ModelError("第二镜暂时失败")
            return speaker.speak(text, voice)

    with pytest.raises(ModelError):
        studio_steps.run_step(config, job_id, "tts", speaker=FailsSecond())
    assert speaker.calls == 1
    studio_steps.run_step(config, job_id, "tts", speaker=speaker)
    assert speaker.calls == 2
    studio_steps.run_step(config, job_id, "tts", speaker=speaker, mode="force", shot_ids=["shot-02"])
    assert speaker.calls == 3


def test_corrupted_audio_is_not_reused(config):
    job_id = prepare_job(config, "这是需要生成的配音文本。")
    speaker = FixtureSpeaker(config)
    studio_steps.run_step(config, job_id, "tts", speaker=speaker)
    (studio.audio_dir(config, job_id) / "shot-01.wav").write_bytes(b"broken")
    studio_steps.run_step(config, job_id, "tts", speaker=speaker)
    assert speaker.calls == 2
    assert audio_info(studio.audio_dir(config, job_id) / "shot-01.wav")["duration"] > 0
