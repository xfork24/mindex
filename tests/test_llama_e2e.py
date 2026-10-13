from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from mindex import adapters, e2e
from mindex.cli import app
from mindex.config import fingerprint
from mindex.llama_cpp import PREFIX, SUFFIX, LlamaEmbedder, LlamaTranscriber, inspect_service
from mindex.media import command, content_hash, cut, probe
from mindex.pipeline import Pipeline
from mindex.search import lexical_search


@pytest.fixture
def llama(config, monkeypatch, tmp_path):
    config.embedding.backend = "llama_cpp"
    props = {
        "build_info": "b-test",
        "model_path": "wemm.gguf",
        "chat_template": "test-template",
        "modalities": {"vision": True},
    }
    seen = []
    reply = {"data": [{"index": 0, "embedding": [2.0] + [0.0] * 7}]}

    def wire(method, url, **kwargs):
        if url.endswith("/health"):
            return {"status": "ok"}
        if url.endswith("/props"):
            return props
        if url.endswith("/v1/models"):
            return {"data": [{"id": "wemm", "meta": {"n_embd": 8}}]}
        seen.append(kwargs["json"])
        return reply

    monkeypatch.setattr(adapters, "request", wire)
    observed = inspect_service(config.embedding)
    path = tmp_path / "deployment.json"
    pinned = {
        "schema": 1,
        "role": "embedding",
        "model": config.embedding.model,
        "weights_sha256": "a" * 64,
        "mmproj_sha256": "b" * 64,
        "quantization": "Q8_0",
        "pooling": "last",
        "video_fps": 4.0,
        **observed,
    }
    path.write_text(json.dumps(pinned))
    config.embedding.deployment_manifest = str(path)
    return config, props, seen, reply, pinned


def test_native_embedding_template_media_and_normalization(llama, video_file, tmp_path):
    config, _props, seen, _reply, _pinned = llama
    client = adapters.make_embedder(config)
    assert isinstance(client, LlamaEmbedder)
    assert client.text(["中文找视频"])[0] == [1.0] + [0.0] * 7
    assert seen[-1]["input"][0]["content"] == [{"type": "text", "text": PREFIX + "中文找视频" + SUFFIX}]
    short = tmp_path / "short.mp4"
    cut(video_file, 0, 0.625, short)
    client.video(short)
    content = seen[-1]["input"][0]["content"]
    assert content[0]["text"] == PREFIX and content[-1]["text"] == SUFFIX
    assert content[1]["type"] == "input_video"
    uploaded = tmp_path / "upload.mp4"
    uploaded.write_bytes(base64.b64decode(content[1]["input_video"]["data"]))
    info = probe(uploaded)
    assert not info["audio"] and info["width"] * info["height"] <= config.video.max_pixels
    assert info["fps"] <= config.video.fps
    frames = int(
        command(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-count_frames",
                "-show_entries",
                "stream=nb_read_frames",
                "-of",
                "csv=p=0",
                str(uploaded),
            ]
        )
    )
    assert 1 <= frames <= config.video.max_frames
    with pytest.raises(adapters.ModelError, match="六片段"):
        client.require_gate()
    space = client.identity()
    e2e.save(
        config.data_dir / "capabilities" / (fingerprint(space) + ".json"), {"passed": True, "space": space}
    )
    client.require_gate()


def test_native_image_uses_vision_content_and_preserves_text_video_space(llama, tmp_path):
    import io

    from PIL import Image

    config, _props, seen, _reply, _pinned = llama
    client = adapters.make_embedder(config)
    previous_space = LlamaEmbedder(config).identity()
    path = tmp_path / "reference.webp"
    Image.new("RGB", (1600, 900), "blue").save(path)
    assert client.image(path) == [1.0] + [0.0] * 7
    assert client.identity() == previous_space
    content = seen[-1]["input"][0]["content"]
    assert content[0] == {"type": "text", "text": PREFIX}
    assert content[-1] == {"type": "text", "text": SUFFIX}
    assert content[1]["type"] == "image_url"
    uri = content[1]["image_url"]["url"]
    assert uri.startswith("data:image/png;base64,")
    with Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))) as image:
        assert image.width * image.height <= config.video.max_pixels
        assert image.mode == "RGB"


def test_native_identity_drift_invalid_values_and_missing_vision(llama, monkeypatch):
    config, props, _seen, reply, pinned = llama
    client = LlamaEmbedder(config)
    client.identity()
    original = adapters.request

    def drift(method, url, **kwargs):
        value = original(method, url, **kwargs)
        if url.endswith("/v1/embeddings"):
            props["build_info"] = "changed-during-inference"
        return value

    monkeypatch.setattr(adapters, "request", drift)
    with pytest.raises(adapters.ModelError, match="运行身份"):
        client.text(["中文"])
    monkeypatch.setattr(adapters, "request", original)
    props["build_info"] = "b-test"
    for vector in ([0] * 8, [float("nan")] * 8, [1] * 7):
        reply["data"][0]["embedding"] = vector
        with pytest.raises(adapters.ModelError):
            client.text(["中文"])
    props["modalities"]["vision"] = False
    observed = inspect_service(config.embedding)
    pinned.update(observed)
    Path(config.embedding.deployment_manifest).write_text(json.dumps(pinned))
    with pytest.raises(adapters.ModelError, match="视觉投影"):
        LlamaEmbedder(config).identity()


def test_native_asr_keeps_raw_response_and_requires_alignment(llama, tmp_path, monkeypatch):
    config, _props, seen, reply, pinned = llama
    config.asr.backend = "llama_cpp"
    pinned.update(role="asr", model=config.asr.model)
    path = tmp_path / "asr.json"
    path.write_text(json.dumps(pinned))
    config.asr.deployment_manifest = str(path)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"wav-test")
    reply.clear()
    reply.update(choices=[{"message": {"content": "language Chinese<asr_text>你好"}}])
    client = LlamaTranscriber(config)
    result = client.raw(audio)
    assert result["text"] == "你好" and result["language"] == "Chinese"
    assert result["raw_response"] == reply
    assert base64.b64decode(seen[-1]["messages"][0]["content"][0]["input_audio"]["data"]) == b"wav-test"
    with pytest.raises(adapters.ModelError, match="伪造"):
        client.transcribe(audio)
    reply["choices"][0]["message"]["content"] = "不是 ASR 协议"
    with pytest.raises(adapters.ModelError, match="时间戳"):
        client.raw(audio)


def test_experiment_freeze_gate_failure_and_resume(config, video_file, tmp_path, monkeypatch):
    video_file = tmp_path / "moving-test-only.mp4"
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
            "testsrc2=s=160x120:r=10:d=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video_file),
        ]
    )
    config.video.window_seconds = 2
    config.video.stride_seconds = 1
    pipeline = Pipeline(config)
    source = pipeline.prepare(video_file)
    directory = tmp_path / "experiment"
    e2e.initialize(config, source, directory)
    draft = directory / "queries.draft.jsonl"
    with pytest.raises(ValueError, match="人工确认"):
        e2e.freeze(directory, draft, "reviewer")
    assert len(list((directory / "video-queries").glob("*.mp4"))) == 12

    class Constant:
        def identity(self):
            return {"model": "test-only", "dimensions": 8}

        def text(self, texts, role="query"):
            return [[1.0] + [0.0] * 7 for _ in texts]

        def video(self, path):
            return [1.0] + [0.0] * 7

    monkeypatch.setattr(e2e, "make_embedder", lambda _: Constant())
    with pytest.raises(adapters.ModelError, match="门槛未通过"):
        e2e.run_phase(config, directory, "smoke")
    state = json.loads((directory / "run.json").read_text())
    assert state["phases"]["smoke"]["status"] == "failed"
    gate = config.data_dir / "capabilities" / (fingerprint(Constant().identity()) + ".json")
    assert not json.loads(gate.read_text())["passed"]

    class Diverse(Constant):
        def vector(self, value):
            rng = np.random.default_rng(int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big"))
            vector = rng.normal(size=8)
            return (vector / np.linalg.norm(vector)).tolist()

        def text(self, texts, role="query"):
            return [self.vector(t) for t in texts]

        def video(self, path):
            return self.vector(str(path))

    monkeypatch.setattr(e2e, "make_embedder", lambda _: Diverse())
    assert e2e.run_phase(config, directory, "smoke")["status"] == "complete"
    entries = [json.loads(v) for v in draft.read_text().splitlines()]
    entries = [entries[0]]
    entries[0].update(
        text="红色画面", relevant=[{"source_id": source, "start": 0, "end": 1}], label_status="confirmed"
    )
    draft.write_text(json.dumps(entries[0], ensure_ascii=False) + "\n")
    frozen = Path(e2e.freeze(directory, draft, "tester"))
    assert json.loads(frozen.with_suffix(".manifest.json").read_text())["sha256"] == content_hash(frozen)
    with pytest.raises(ValueError, match="已存在"):
        e2e.freeze(directory, draft, "tester")
    pipeline.store.execute("INSERT INTO source_curation(id,display_name) VALUES(?,?)", (source, "changed"))
    with pytest.raises(ValueError, match="整理状态"):
        e2e.run_phase(config, directory, "smoke")


def test_pending_alignment_blocks_annotation_and_lexical_ablation(config, video_file):
    pipeline = Pipeline(config)
    source = pipeline.prepare(video_file)
    pipeline.store.put_json(
        "transcripts", "source_id", source, config.asr_key, {"status": "pending_alignment", "utterances": []}
    )
    with pytest.raises(ValueError, match="时间对齐"):
        pipeline._dialogue(source, 0, 1)
    snapshots = [{"id": "x", "views": {"description": "室内", "dialogue": "香蕉", "style": "近景"}}]
    assert lexical_search("香蕉", snapshots, 10)
    assert not lexical_search("香蕉", snapshots, 10, exclude=["dialogue"])


def test_probe_cli_reports_failure_without_traceback(tmp_path, monkeypatch):
    def failure(*args, **kwargs):
        raise adapters.ModelError("offline")

    monkeypatch.setattr(adapters, "request", failure)
    output = tmp_path / "probe.json"
    result = CliRunner().invoke(app, ["e2e", "probe", "--output", str(output)])
    assert result.exit_code == 1
    assert json.loads(output.read_text())["ready"] is False
    assert "offline" in result.output and "Traceback" not in result.output


def test_alignment_worker_isolated_from_package_types_module():
    import sys

    import mindex.align_worker

    help_text = command([sys.executable, "-I", mindex.align_worker.__file__, "--help"]).decode()
    assert "--cache-dir" in help_text


def test_loading_full_default_yaml_preserves_existing_layout(tmp_path):
    import yaml

    from mindex.config import Config, load_config

    minimal = tmp_path / "minimal.yaml"
    full = tmp_path / "full.yaml"
    minimal.write_text("data_dir: ./data\n")
    full.write_text(yaml.safe_dump(Config().model_dump(mode="json")))
    assert load_config(minimal).layout_key == load_config(full).layout_key


def test_native_asr_without_audio_requires_no_running_service(config, video_file):
    config.asr.backend = "llama_cpp"
    pipeline = Pipeline(config)
    source = pipeline.prepare(video_file)
    pipeline.transcribe(source)
    assert (
        pipeline.store.json_value("transcripts", "source_id", source, config.asr_key)["status"] == "no_audio"
    )


def test_zero_duration_alignment_tokens_preserve_text_and_observed_bounds():
    from mindex.align_worker import coalesce_alignment

    rows = [
        {"start": 0.72, "end": 0.72, "text": "先"},
        {"start": 0.72, "end": 1.1, "text": "生"},
        {"start": 1.1, "end": 1.1, "text": "好"},
    ]
    assert coalesce_alignment(rows) == [{"start": 0.72, "end": 1.1, "text": "先生好"}]
    with pytest.raises(ValueError, match="全部对齐项"):
        coalesce_alignment(rows[:1])
