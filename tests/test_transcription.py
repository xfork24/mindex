from __future__ import annotations

import pytest

from models.adapters import ModelError
from processing.pipeline import Pipeline
from shared.media import command
from shared.types import Utterance


def test_transcript_chunk_resume_and_source_time_mapping(config, tmp_path):
    video = tmp_path / "audio-video.mp4"
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
            "color=c=red:s=160x120:r=2:d=31.2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=31.2",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ]
    )

    class InterruptingTranscriber:
        def __init__(self):
            self.calls = 0

        def transcribe(self, path):
            self.calls += 1
            if self.calls == 2:
                raise ModelError("ASR interrupted")
            return [Utterance(start=0.1, end=0.4, text="你"), Utterance(start=0.4, end=0.8, text="好")]

    transcriber = InterruptingTranscriber()
    pipeline = Pipeline(config, transcriber=transcriber)
    source = pipeline.prepare(video)
    with pytest.raises(ModelError, match="ASR interrupted"):
        pipeline.transcribe()
    assert len(list((config.data_dir / "transcripts").rglob("*.json"))) == 1
    assert pipeline.store.json_value("transcripts", "source_id", source, config.asr_key) is None
    pipeline.transcribe()
    assert transcriber.calls == 3  # First completed chunk was not called again.
    result = pipeline.store.json_value("transcripts", "source_id", source, config.asr_key)
    assert result["status"] == "complete"
    assert result["utterances"][-1]["start"] == pytest.approx(30.4)
    assert result["utterances"][-1]["end"] == pytest.approx(30.8)
    dialogue = pipeline._dialogue(source, 30, 31)
    assert "".join(item["text"] for item in dialogue) == "你好"
    assert dialogue[0]["start"] == pytest.approx(0.1)
    pipeline.transcribe()
    assert transcriber.calls == 3
    assert pipeline.store.rows("SELECT status FROM runs WHERE stage='transcribe'")[0]["status"] == "failed"


def test_disabled_transcription_is_explicit(config, video_file):
    config.asr.enabled = False
    pipeline = Pipeline(config)
    source = pipeline.prepare(video_file)
    pipeline.transcribe()
    result = pipeline.store.json_value("transcripts", "source_id", source, config.asr_key)
    assert result == {"utterances": [], "status": "disabled"}
