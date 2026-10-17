import pytest

from mindex.adapters import ModelError
from mindex.dialogue import (
    DialogueProcessor,
    choose_subtitle,
    coalesce_words,
    current_dialogue,
    extract_subtitles,
    parse_minimax_words,
    relative_dialogue,
    subtitle_tracks,
)
from mindex.media import command
from mindex.pipeline import Pipeline


def test_minimax_word_timestamps_silence_invalid_and_speaker():
    words = parse_minimax_words(
        {
            "text": "你好！",
            "segments": [
                {"text": "你", "start": 0.1, "end": 0.3, "speaker": 2},
                {"text": "好！", "start": 0.3, "end": 0.8, "speaker": 2},
            ],
        },
        1,
    )
    assert coalesce_words(words)[0]["text"] == "你好！"
    assert words[0]["speaker_id"] == 2
    assert parse_minimax_words({"text": "", "segments": []}) == []
    for segment in (
        [],
        [{"text": "话"}],
        [{"text": "话", "start": 1, "end": 0}],
        [{"text": "话", "start": 0, "end": float("nan")}],
        [{"text": "话", "start": 0, "end": 20}],
    ):
        with pytest.raises(ModelError):
            parse_minimax_words({"text": "话", "segments": segment}, 1)


def test_real_ffmpeg_subtitle_priority_styles_and_offsets(config, video_file, tmp_path):
    zh = tmp_path / "zh.srt"
    en = tmp_path / "en.srt"
    zh.write_text("1\n00:00:02,500 --> 00:00:03,500\n<i>阿明</i>，你好！\n\n", encoding="utf-8")
    en.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n", encoding="utf-8")
    video = tmp_path / "subtitles.mkv"
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(video_file),
            "-i",
            str(en),
            "-i",
            str(zh),
            "-i",
            str(zh),
            "-map",
            "0:v",
            "-map",
            "1:0",
            "-map",
            "2:0",
            "-map",
            "3:0",
            "-c",
            "copy",
            "-metadata:s:s:0",
            "language=eng",
            "-metadata:s:s:1",
            "language=zho",
            "-metadata:s:s:2",
            "language=zho",
            "-disposition:s:0",
            "default",
            "-disposition:s:1",
            "0",
            "-disposition:s:2",
            "forced",
            str(video),
        ]
    )
    tracks = subtitle_tracks(video)
    selected = choose_subtitle(tracks)
    assert selected["index"] == 2
    assert choose_subtitle(tracks, 1)["language"] == "eng"
    assert choose_subtitle([t for t in tracks if t["forced"]]) is None
    values = extract_subtitles(video, selected, 10)
    assert values[0]["text"] == "阿明，你好！"
    assert values[0]["start"] == 2.5
    assert values[0]["end"] == 3.5
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video)

    class NeverASR:
        def raw(self, *args, **kwargs):
            pytest.fail("Subtitles must not call ASR")

    processor = DialogueProcessor(config, NeverASR())
    result = processor.process(sid)["sources"][0]
    assert result["source"] == "subtitle" and result["status"] == "complete"
    config.asr.model = "changed-model"
    assert processor.process(sid)["sources"][0]["reused"]
    assert processor.process(sid, subtitle_stream=1)["sources"][0]["utterances"][0]["text"] == "Hello"
    data = current_dialogue(pipeline.store, sid)
    assert relative_dialogue(data, 0, 0.5) == []
    assert relative_dialogue(data, 1.5, 1.8)[0]["evidence_start"] == 1.5
    with pytest.raises(ValueError, match="不存在"):
        processor.process(sid, subtitle_stream=99)


def test_minimax_overlap_checkpoints_recovery(config, tmp_path):
    video = tmp_path / "audio.mp4"
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=s=32x32:r=1:d=61",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=61",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(video),
        ]
    )
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video)
    config.asr.backend = "minimax"

    class ASR:
        calls = 0
        fail = True

        def raw(self, path, *, timestamps):
            assert timestamps
            self.calls += 1
            if self.calls == 2 and self.fail:
                raise ModelError("interrupted")
            # middle chunk begins at original 29s; first word belongs to previous chunk.
            words = [
                {"text": "前", "start": 0.1, "end": 0.4, "speaker": 0},
                {"text": "后。", "start": 1.2, "end": 1.6, "speaker": 0},
            ]
            if self.calls == 3:
                words.append({"text": "边界", "start": 0.8, "end": 1.1, "speaker": 0})
                words.sort(key=lambda w: w["start"])
            return {"raw_response": {"text": "".join(w["text"] for w in words), "segments": words}}

    asr = ASR()
    processor = DialogueProcessor(config, asr)
    with pytest.raises(ModelError):
        processor.process(sid)
    assert pipeline.store.one("SELECT status FROM channel_data")["status"] == "failed"
    assert len(list((config.data_dir / "dialogue").rglob("0.json"))) == 1
    asr.fail = False
    result = processor.process(sid)["sources"][0]
    assert asr.calls == 4  # completed first chunk reused
    assert result["completed_chunks"] == 3
    assert all(30 <= (w["start"] + w["end"]) / 2 < 60 for w in result["words"] if w["chunk"] == 1)
    assert not any(w["text"] == "前" for w in result["words"] if w["chunk"] == 1)
    assert result["words"][-1]["start"] == pytest.approx(60.2)
    processor.process(sid)
    assert asr.calls == 4
    assert len(list((config.data_dir / "dialogue").rglob("raw-*.json"))) == 3


def test_no_audio_does_not_initialize_models(config, video_file, monkeypatch):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    monkeypatch.setattr("mindex.dialogue.make_transcriber", lambda *_: pytest.fail("no audio"))
    result = pipeline.enrich_dialogue(sid)["sources"][0]
    assert result["status"] == "no_audio" and result["words"] == []


def test_minimax_rate_limit_rewinds_upload_and_requests_words(config, tmp_path, monkeypatch):
    import httpx

    from mindex import adapters

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF-test-audio")
    attempts = []

    def response(method, url, **options):
        handle = options["files"]["file"][1]
        attempts.append(handle.tell())
        assert handle.read() == b"RIFF-test-audio"
        assert options["data"]["response_format"] == "verbose_json"
        assert options["data"]["timestamp_level"] == "word"
        request = httpx.Request(method, url)
        if len(attempts) == 1:
            return httpx.Response(429, json={"error": "rate limit"}, request=request)
        return httpx.Response(
            200,
            json={"text": "你", "segments": [{"text": "你", "start": 0.1, "end": 0.2, "speaker": "S1"}]},
            request=request,
        )

    monkeypatch.setattr(httpx, "request", response)
    monkeypatch.setattr(adapters.time, "sleep", lambda _: None)
    result = adapters.MiniMaxTranscriber(config).transcribe(audio)
    assert attempts == [0, 0]
    assert result[0].start == 0.1


@pytest.mark.parametrize("fallback", ["empty_subtitle", "extraction_failure", "bitmap"])
def test_subtitle_fallback_and_silent_audio_are_explicit(config, tmp_path, monkeypatch, fallback):
    import json

    from mindex.media import MediaError

    video = tmp_path / "silent.mp4"
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=s=32x32:r=2:d=2",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(video),
        ]
    )
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video)
    config.asr.backend = "minimax"
    track = {
        "index": 2,
        "codec": "subrip",
        "text": fallback != "bitmap",
        "language": "zho",
        "default": True,
        "forced": False,
    }
    monkeypatch.setattr("mindex.dialogue.subtitle_tracks", lambda _: [track])

    def extract(*args):
        if fallback == "extraction_failure":
            raise MediaError("TEST extraction failed")
        return []

    monkeypatch.setattr("mindex.dialogue.extract_subtitles", extract)

    class Silence:
        calls = 0

        def raw(self, *args, **kwargs):
            self.calls += 1
            return {"raw_response": {"text": "", "segments": []}}

    asr = Silence()
    result = DialogueProcessor(config, asr).process(sid, subtitle_stream=2)["sources"][0]
    assert result["status"] == "empty" and result["words"] == [] and result["utterances"] == []
    assert result["fallback_reason"] and asr.calls == 1
    assert len(list((config.data_dir / "dialogue").rglob("raw-*.json"))) == 1
    # Invalid timestamp responses remain available for diagnosis and are never committed as successful chunks.
    channel = pipeline.store.one("SELECT profile FROM channel_data WHERE source_id=?", (sid,))
    pipeline.store.execute("DELETE FROM channel_data WHERE source_id=?", (sid,))
    (config.data_dir / "dialogue" / sid / channel["profile"] / "0.json").unlink()
    asr.raw = lambda *a, **k: {"raw_response": {"text": "invalid", "segments": []}}
    with pytest.raises(ModelError, match="时间戳"):
        DialogueProcessor(config, asr).process(sid, subtitle_stream=2)
    assert pipeline.store.one("SELECT status FROM channel_data")["status"] == "failed"
    raw = next((config.data_dir / "dialogue").rglob("raw-0.json"))
    assert json.loads(raw.read_text())["raw_response"]["text"] == "invalid"
