"""仅使用明确的测试文件与向量验证身份、门槛和漂移拒绝，不代表模型质量。"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from mindex import adapters, compatibility, deployment, e2e
from mindex.analyzers import PinnedQwenAnalyzer
from mindex.api import create_app
from mindex.cli import app
from mindex.config import fingerprint, load_config
from mindex.indexing import IndexBuilder
from mindex.llama_cpp import LlamaEmbedder, LlamaTranscriber, checked_runtime, inspect_service, manifest
from mindex.runtime_settings import RuntimeConfig, activate_profile, apply_runtime_overrides, save_profile
from mindex.search import SearchEngine
from mindex.storage import Store
from mindex.types import TextSearch


@pytest.fixture
def native(config, tmp_path, monkeypatch):
    config.embedding.backend = "llama_cpp"
    config.embedding.quantization = "Q8_0"
    weights, mmproj = tmp_path / "weights.gguf", tmp_path / "mmproj.gguf"
    weights.write_bytes(b"TEST-ONLY-weights-a")
    mmproj.write_bytes(b"TEST-ONLY-projection-a")
    state = {
        "props": {
            "build_info": "build-test",
            "model_path": str(weights),
            "chat_template": "test",
            "modalities": {"vision": True, "video": True, "audio": True},
            "total_slots": 1,
            "default_generation_settings": {"n_ctx": 32768},
        },
        "models": {
            "data": [
                {
                    "id": "live-model",
                    "meta": {
                        "vocab_type": 2,
                        "n_vocab": 100,
                        "n_ctx_train": 32768,
                        "n_embd": 8,
                        "n_params": 1000,
                        "size": 2000,
                        "ftype": "Q8_0",
                        "n_ctx": 32768,
                    },
                }
            ]
        },
        "posts": [],
        "weights": weights,
        "mmproj": mmproj,
        "config": config,
        "rotation": False,
    }

    def wire(method, url, **kwargs):
        if url.endswith("/health"):
            return {"status": "ok"}
        if url.endswith("/props"):
            return state["props"]
        if url.endswith("/v1/models"):
            return state["models"]
        body = kwargs["json"]
        state["posts"].append(body)
        if url.endswith("/v1/embeddings"):
            digest = hashlib.sha256(json.dumps(body["input"], sort_keys=True).encode()).digest()
            rng = np.random.default_rng(int.from_bytes(digest[:8], "big"))
            vector = rng.normal(size=8)
            if state["rotation"]:
                vector = -vector
            if "on_inference" in state:
                state["on_inference"]()
            return {"data": [{"index": 0, "embedding": state.get("vector", vector.tolist())}]}
        return {"choices": [{"message": {"content": state.get("chat", "language Chinese<asr_text>你好")}}]}

    monkeypatch.setattr(adapters, "request", wire)
    path = tmp_path / "v2.json"
    config.embedding.deployment_manifest = str(path)

    def pin(role="embedding", *, remote=False, launch=None):
        service = getattr(config, role)
        report = inspect_service(service)
        kwargs = (
            {
                "weights_sha256": hashlib.sha256(weights.read_bytes()).hexdigest(),
                "mmproj_sha256": hashlib.sha256(mmproj.read_bytes()).hexdigest(),
            }
            if remote
            else {
                "weights_file": weights,
                "mmproj_file": mmproj,
                "server_cwd": tmp_path,
            }
        )
        value = deployment.create_manifest(config, role, report, "Q8_0", **kwargs, launch_parameters=launch)
        target = path if role == "embedding" else tmp_path / (role + ".json")
        e2e.save(target, value)
        service.deployment_manifest = str(target)
        return value

    state["pin"] = pin
    state["pinned"] = pin()
    return state


def establish(native):
    """构造显式测试基线；不执行真实 smoke 或冒充真实视频。"""
    config = native["config"]
    client = LlamaEmbedder(config)
    space = client.identity()
    samples = []
    vectors = np.eye(8)[:6].tolist()
    for number in range(6):
        path = config.data_dir / f"test-only-{number}.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"TEST-ONLY-video-{number}".encode())
        samples.append(
            {
                "id": str(number),
                "path": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "text": f"测试文本{number}",
            }
        )
    report = {
        "checks": dict.fromkeys(compatibility.CHECKS, True),
        "space": space,
        "space_id": fingerprint(space),
        "text_vectors": vectors,
        "video_vectors": vectors,
    }
    gate = compatibility.complete_validation(
        config, space, client._runtime, samples, report, vectors, vectors, ["a" * 64] * 6, None
    )
    compatibility.save_validation(config, space, client._runtime["deployment_id"], gate)
    client.require_gate()
    return client, gate


def test_content_identity_tracks_actual_weights_and_projection(native):
    original = native["pinned"]["file_identity"]
    native["weights"].write_bytes(b"TEST-ONLY-weights-b")
    changed_weights = native["pin"]()["file_identity"]
    native["mmproj"].write_bytes(b"TEST-ONLY-projection-b")
    changed_projection = native["pin"]()["file_identity"]
    assert len({fingerprint(v) for v in (original, changed_weights, changed_projection)}) == 3
    assert native["models"]["data"][0]["meta"]["ftype"] == "Q8_0"


def test_space_excludes_paths_aliases_addresses_build_and_ctx(native, tmp_path):
    config = native["config"]
    space = LlamaEmbedder(config).identity()
    moved = tmp_path / "renamed.gguf"
    moved.write_bytes(native["weights"].read_bytes())
    native["weights"] = moved  # pin 闭包使用原路径；另行核对新的本地路径。
    native["props"]["model_path"] = str(moved)
    native["models"]["data"][0]["id"] = "new-alias"
    native["props"]["build_info"] = "new-build"
    native["props"]["default_generation_settings"]["n_ctx"] = 16384
    config.embedding.base_url = "http://new-host:9999"
    config.embedding.model = "operator-label-renamed"
    report = inspect_service(config.embedding)
    pinned = deployment.create_manifest(
        config,
        "embedding",
        report,
        "Q8_0",
        weights_file=moved,
        mmproj_file=native["mmproj"],
        server_cwd=tmp_path,
    )
    e2e.save(Path(config.embedding.deployment_manifest), pinned)
    assert LlamaEmbedder(config).identity() == space
    assert pinned["deployment_id"] != native["pinned"]["deployment_id"]
    assert all(k not in space for k in ("base_url", "model", "runtime_id", "adapter_sha256", "model_path"))


def test_explicit_semantic_versions_change_space(native, monkeypatch):
    config = native["config"]
    content = native["pinned"]["file_identity"]
    original = deployment.space_contract(config, content)
    monkeypatch.setattr(deployment, "PREFIX", "new-template")
    assert deployment.space_contract(config, content) != original
    monkeypatch.setattr(deployment, "PREFIX", original["template"]["prefix"])
    config.embedding.numerical_implementation = "llama-cpp-wemm-2"
    assert deployment.space_contract(config, content) != original
    with pytest.raises(adapters.ModelError, match="空间契约"):
        LlamaEmbedder(config).identity()


@pytest.mark.parametrize("field", deployment.METADATA_FIELDS)
@pytest.mark.parametrize("bad", [None, [], True])
def test_metadata_missing_or_invalid_is_rejected(native, field, bad):
    native["models"]["data"][0]["meta"][field] = bad
    with pytest.raises(adapters.ModelError, match="元数据"):
        inspect_service(native["config"].embedding)
    assert native["posts"] == []


@pytest.mark.parametrize(
    "change,message",
    [
        ("vision", "视觉投影"),
        ("video", "支持视频"),
        ("dimension", "维度"),
        ("quantization", "量化"),
    ],
)
def test_role_structure_rejected_before_pin(native, change, message):
    if change in {"vision", "video"}:
        native["props"]["modalities"][change] = False
    else:
        native["models"]["data"][0]["meta"]["n_embd" if change == "dimension" else "ftype"] = (
            7 if change == "dimension" else "Q4_0"
        )
    with pytest.raises(adapters.ModelError, match=message):
        native["pin"]()


def test_full_reports_preserve_legacy_runtime_hash_and_read_only_probe(native):
    report = inspect_service(native["config"].embedding)
    assert report["runtime_id"] == fingerprint(report["observable"])
    assert report["models"] == native["models"]
    assert report["metadata"]["n_embd"] == 8
    checked = checked_runtime(native["config"].embedding, native["pinned"])
    assert set(report) <= set(checked)
    assert native["posts"] == []


def test_schema1_original_manifest_and_runtime_checks(native):
    config = native["config"]
    report = inspect_service(config.embedding)
    pinned = {
        "schema": 1,
        "role": "embedding",
        "model": config.embedding.model,
        "weights_sha256": "a" * 64,
        "mmproj_sha256": "b" * 64,
        "quantization": "Q8_0",
        "pooling": "last",
        "video_fps": 4.0,
        **report,
    }
    e2e.save(Path(config.embedding.deployment_manifest), pinned)
    assert manifest(config.embedding, "embedding")["schema"] == 1
    assert checked_runtime(config.embedding, pinned)["model_id"] == "live-model"
    native["props"]["model_path"] = "other.gguf"
    with pytest.raises(adapters.ModelError, match="运行身份"):
        checked_runtime(config.embedding, pinned)


def test_unvalidated_inference_is_blocked_but_smoke_context_can_infer(native):
    client = LlamaEmbedder(native["config"])
    with pytest.raises(adapters.ModelError, match="兼容性门槛"):
        client.text(["测试"])
    assert not native["posts"]
    with client.validating():
        assert len(client.text(["测试"])[0]) == 8
    assert native["posts"][-1]["model"] == "live-model"
    with pytest.raises(adapters.ModelError, match="兼容性门槛"):
        client.text(["测试"])


@pytest.mark.parametrize("vector", [[0.0] * 8, [float("nan")] * 8, [float("inf")] * 8, [1.0] * 7])
def test_invalid_native_vectors_rejected(native, vector):
    native["vector"] = vector
    with LlamaEmbedder(native["config"]).validating() as client, pytest.raises(adapters.ModelError):
        client.text(["测试"])


@pytest.mark.parametrize("field", ["build", "alias", "ctx", "file_content", "file_stat", "manifest"])
def test_drift_during_request_rejects_result(native, field):
    def drift():
        if field == "build":
            native["props"]["build_info"] = "changed"
        elif field == "alias":
            native["models"]["data"][0]["id"] = "changed"
        elif field == "ctx":
            native["props"]["default_generation_settings"]["n_ctx"] = 16384
        elif field == "file_content":
            native["mmproj"].write_bytes(b"changed")
        elif field == "file_stat":
            native["mmproj"].touch()
        else:
            p = Path(native["config"].embedding.deployment_manifest)
            value = json.loads(p.read_text())
            value["trust"]["attestation"] = "changed"
            e2e.save(p, value)

    native["on_inference"] = drift
    with LlamaEmbedder(native["config"]).validating() as client, pytest.raises(adapters.ModelError):
        client.text(["测试"])


def test_file_hash_cache_rechecks_on_change(native, monkeypatch):
    deployment._file_hash.cache_clear()
    count = 0
    original = Path.open

    def counted(path, *args, **kwargs):
        nonlocal count
        if path in {native["weights"], native["mmproj"]}:
            count += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted)
    client = LlamaEmbedder(native["config"])
    client.identity()
    client.identity()
    assert count == 2
    native["weights"].touch()
    client.identity()
    assert count == 3
    native["weights"].write_bytes(b"changed")
    with pytest.raises(adapters.ModelError, match="SHA256"):
        client.identity()


def test_cross_deployment_requires_separate_validation_and_threshold(native):
    config = native["config"]
    client, old_gate = establish(native)
    native["props"]["build_info"] = "new-build"
    native["pin"]()
    assert LlamaEmbedder(config).identity() == client.identity()
    with pytest.raises(adapters.ModelError, match="兼容性门槛"):
        client.require_gate()
    baseline = compatibility.load_baseline(config, client.identity())
    samples = baseline["samples"]
    good = compatibility.complete_validation(
        config,
        client.identity(),
        client._runtime,
        samples,
        old_gate,
        baseline["raw_text_vectors"],
        baseline["raw_video_vectors"],
        [s["prepared_sha256"] for s in samples],
        baseline,
    )
    compatibility.save_validation(config, client.identity(), client._runtime["deployment_id"], good)
    client.require_gate()
    bad_vectors = copy.deepcopy(baseline["raw_video_vectors"])
    bad_vectors[-1] = [-v for v in bad_vectors[-1]]
    failed = compatibility.complete_validation(
        config,
        client.identity(),
        client._runtime,
        samples,
        old_gate,
        baseline["raw_text_vectors"],
        bad_vectors,
        [s["prepared_sha256"] for s in samples],
        baseline,
    )
    assert failed["passed"] is False
    assert failed["compatibility"]["cosine"]["video"][-1] == -1
    compatibility.save_validation(config, client.identity(), client._runtime["deployment_id"], failed)
    with pytest.raises(adapters.ModelError, match="兼容性门槛"):
        client.require_gate()
    assert compatibility.load_baseline(config, client.identity()) == baseline


@pytest.mark.parametrize(
    "broken",
    [
        "baseline_missing",
        "baseline_corrupt",
        "marker_missing",
        "sample_changed",
        "validation_corrupt",
        "validation_vectors",
        "threshold",
    ],
)
def test_corrupt_or_missing_baseline_and_validation_fail_closed(native, broken):
    client, gate = establish(native)
    directory = compatibility.location(native["config"], client.identity())
    path = compatibility.validation_path(
        native["config"], client.identity(), client._runtime["deployment_id"]
    )
    if broken == "baseline_missing":
        (directory / "baseline.json").unlink()
    elif broken == "baseline_corrupt":
        (directory / "baseline.json").write_text("{}")
    elif broken == "marker_missing":
        (directory / "baseline.sha256").unlink()
    elif broken == "sample_changed":
        (directory / "samples/01.mp4").write_bytes(b"changed")
    elif broken == "validation_corrupt":
        path.write_text("not-json")
    elif broken == "validation_vectors":
        gate["raw_video_vectors"][0] = [float("nan")] * 8
        e2e.save(path, gate)
    else:
        gate["compatibility"]["threshold"] = 0.9
        e2e.save(path, gate)
    with pytest.raises(adapters.ModelError, match="兼容性门槛"):
        client.require_gate()
    if broken.startswith("baseline") or broken == "marker_missing":
        with pytest.raises(adapters.ModelError):
            compatibility.load_baseline(native["config"], client.identity(), allow_missing=True)


def test_local_model_path_resolved_against_server_cwd(native):
    native["props"]["model_path"] = native["weights"].name
    native["pin"]()
    checked_runtime(native["config"].embedding, native["pin"]())
    native["props"]["model_path"] = "wrong-file.gguf"
    with pytest.raises(adapters.ModelError, match="model_path"):
        native["pin"]()


def test_launch_parameter_changes_require_new_deployment_gate(native):
    client, _ = establish(native)
    sid = fingerprint(client.identity())
    native["pin"](launch={"cache_type_k": "q8_0"})
    assert fingerprint(client.identity()) == sid
    with pytest.raises(adapters.ModelError, match="兼容性门槛"):
        client.require_gate()


def test_remote_trust_is_explicit_and_does_not_read_local_files(native):
    pinned = native["pin"](remote=True)
    assert pinned["trust"]["source"] == "operator-attested-sha256"
    native["weights"].unlink()
    native["mmproj"].unlink()
    assert checked_runtime(native["config"].embedding, pinned)["file_states"] == {}


def test_asr_and_analyzer_schema2_use_current_service_id(native, tmp_path):
    config = native["config"]
    config.asr.backend = config.analyzer.backend = "llama_cpp"
    native["models"]["data"][0]["id"] = "current-asr"
    native["pin"]("asr")
    audio = tmp_path / "test.wav"
    audio.write_bytes(b"TEST-ONLY-AUDIO")
    assert LlamaTranscriber(config).raw(audio)["text"] == "你好"
    assert native["posts"][-1]["model"] == "current-asr"
    native["models"]["data"][0]["id"] = "current-analyzer"
    native["pin"]("analyzer")
    native["chat"] = '{"answer": "测试"}'
    assert PinnedQwenAnalyzer(config)._chat([], "测试") == {"answer": "测试"}
    assert native["posts"][-1]["model"] == "current-analyzer"


def test_pin_cli_local_remote_and_mutual_exclusion(native, tmp_path):
    config = native["config"]
    yaml_path = tmp_path / "fixed.yaml"
    import yaml

    yaml_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    runner = CliRunner()
    base = ["--config", str(yaml_path), "--no-runtime-overrides", "e2e", "pin", "--quantization", "Q8_0"]
    for remote in (False, True):
        output = tmp_path / ("remote.json" if remote else "local.json")
        args = (
            ["--weights-sha256", "a" * 64, "--mmproj-sha256", "b" * 64]
            if remote
            else [
                "--weights-file",
                str(native["weights"]),
                "--mmproj-file",
                str(native["mmproj"]),
                "--server-cwd",
                str(tmp_path),
            ]
        )
        result = runner.invoke(app, [*base, "--output", str(output), *args])
        assert result.exit_code == 0, result.output
        assert json.loads(output.read_text())["schema"] == 2
        repeated = runner.invoke(app, [*base, "--output", str(output), *args])
        assert repeated.exit_code != 0
    output = tmp_path / "mixed.json"
    result = runner.invoke(
        app,
        [
            *base,
            "--output",
            str(output),
            "--weights-file",
            str(native["weights"]),
            "--weights-sha256",
            "a" * 64,
        ],
    )
    assert result.exit_code != 0 and "互斥" in result.output and not output.exists()


def test_fixed_yaml_persists_through_cli_and_runtime_web(config, tmp_path, monkeypatch):
    import yaml

    store = Store(config.data_dir)
    profile = save_profile(
        store,
        role="embedding",
        provider="custom",
        name="override-test",
        config={"backend": "mindex", "model": "dynamic-model", "base_url": "http://dynamic"},
    )
    activate_profile(store, profile["id"])
    path = tmp_path / "fixed.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    observed = []
    monkeypatch.setattr(
        "mindex.indexing.IndexBuilder.build",
        lambda self, **kwargs: observed.append(self.config.embedding.model) or "test-index",
    )
    monkeypatch.setattr("mindex.api.status", lambda cfg, **kwargs: {"model": cfg.embedding.model})
    monkeypatch.setattr(
        "uvicorn.run",
        lambda web, **kwargs: observed.append(TestClient(web).get("/api/status").json()["model"]),
    )
    runner = CliRunner()
    for command in (["index"], ["serve"]):
        result = runner.invoke(app, ["--config", str(path), "--no-runtime-overrides", *command])
        assert result.exit_code == 0, result.output
    assert observed == [config.embedding.model] * 2
    fixed = load_config(path)
    fixed.runtime_overrides = False
    runtime = RuntimeConfig(fixed, store)
    assert runtime.current() is fixed and apply_runtime_overrides(fixed) is fixed
    web = TestClient(create_app(fixed))
    assert web.get("/api/status").json()["model"] == config.embedding.model
    config.runtime_overrides = True
    assert RuntimeConfig(config, store).current().embedding.model == "dynamic-model"


def test_index_and_search_require_gate_even_without_inference(native, monkeypatch):
    config = native["config"]
    space = LlamaEmbedder(config).identity()
    monkeypatch.setattr("mindex.pipeline.Pipeline.visual_snapshots", lambda *_: [{"id": "test"}])
    with pytest.raises(adapters.ModelError, match="兼容性门槛"):
        IndexBuilder(config).build(mode="visual")
    store = Store(config.data_dir)
    store.execute(
        "INSERT INTO index_versions(id,spec,status) VALUES(?,?,'ready')",
        ("test-index", json.dumps({"space": space, "preprocessing": config.video.model_dump()})),
    )
    with pytest.raises(adapters.ModelError, match="兼容性门槛"):
        SearchEngine(config, index_id="test-index").text(TextSearch(text="测试"))
    assert native["posts"] == []


def test_real_media_smoke_reuses_fixed_baseline_and_rejects_changed_deployment(native, tmp_path, monkeypatch):
    from mindex.media import command
    from mindex.pipeline import Pipeline

    config = native["config"]
    # 真实 FFmpeg 切片，模型响应仍为明确的测试向量。
    video = tmp_path / "TEST-ONLY-moving.mp4"
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
            str(video),
        ]
    )
    config.video.window_seconds = 2
    config.video.stride_seconds = 1
    pipeline = Pipeline(config)
    source = pipeline.prepare(video)
    directory = tmp_path / "experiment"
    e2e.initialize(config, source, directory)
    first = e2e.smoke(config, directory)
    assert first["passed"]
    sid = first["space_id"]
    baseline_path = compatibility.location(config, first["space"]) / "baseline.json"
    original_bytes = baseline_path.read_bytes()
    assert len({s["sha256"] for s in json.loads(original_bytes)["samples"]}) == 6
    LlamaEmbedder(config).require_gate()
    native["props"]["build_info"] = "second-build"
    native["pin"]()
    with pytest.raises(adapters.ModelError, match="兼容性门槛"):
        LlamaEmbedder(config).require_gate()
    second = e2e.smoke(config, directory)
    assert second["passed"] and second["space_id"] == sid
    assert second["deployment_id"] != first["deployment_id"]
    assert baseline_path.read_bytes() == original_bytes
    # 结构依旧通过，但数值反向：应保存失败对照并拒绝复用空间。
    native["rotation"] = True
    with pytest.raises(adapters.ModelError, match="兼容性门槛未通过"):
        e2e.smoke(config, directory)
    failed = json.loads((directory / "smoke.json").read_text())
    assert all(failed["checks"].values())
    assert max(failed["compatibility"]["cosine"]["video"]) < -0.999
    assert baseline_path.read_bytes() == original_bytes
    with pytest.raises(adapters.ModelError, match="兼容性门槛"):
        LlamaEmbedder(config).require_gate()

    def interrupt(*args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(e2e, "smoke", interrupt)
    with pytest.raises(KeyboardInterrupt):
        e2e.run_phase(config, directory, "smoke")
    state = json.loads((directory / "run.json").read_text())
    assert state["phases"]["smoke"]["status"] == "failed"
    assert state["phases"]["smoke"]["error_type"] == "KeyboardInterrupt"


def test_cli_probe_uses_fixed_yaml_with_active_override(native, tmp_path):
    import yaml

    config = native["config"]
    store = Store(config.data_dir)
    profile = save_profile(
        store,
        role="embedding",
        provider="custom",
        name="dynamic",
        config={"model": "dynamic", "base_url": "http://dynamic", "backend": "mindex"},
    )
    activate_profile(store, profile["id"])
    path = tmp_path / "fixed.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    output = tmp_path / "probe.json"
    result = CliRunner().invoke(
        app, ["--config", str(path), "--no-runtime-overrides", "e2e", "probe", "--output", str(output)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(output.read_text())["base_url"] == config.embedding.base_url


def test_restart_marker_is_volatile_but_request_restart_is_rejected(native):
    native["props"]["media_marker"] = "random-instance-one"
    pinned = native["pin"]()
    native["props"]["media_marker"] = "random-instance-two"
    assert checked_runtime(native["config"].embedding, pinned)["deployment_id"] == pinned["deployment_id"]

    def restart():
        native["props"]["media_marker"] = "random-instance-three"

    native["on_inference"] = restart
    with (
        LlamaEmbedder(native["config"]).validating() as client,
        pytest.raises(adapters.ModelError, match="推理期间"),
    ):
        client.text(["测试"])


def test_keyboard_interrupt_records_failed_pipeline_run(config):
    store = Store(config.data_dir)
    with pytest.raises(KeyboardInterrupt), store.run("index"):
        raise KeyboardInterrupt()
    row = store.one("SELECT status,message,finished_at FROM runs ORDER BY id DESC LIMIT 1")
    assert row["status"] == "failed" and row["message"] == "KeyboardInterrupt"
    assert row["finished_at"] is not None
