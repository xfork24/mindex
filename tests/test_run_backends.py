"""执行所有启动脚本，用替身服务核对参数和环境；不加载模型或要求本机 GPU。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted(path.name for path in (ROOT / "scripts").glob("run-*.sh"))
BACKEND_ENV = {
    "run-wemm.sh": "MINDEX_WEMM_BACKEND",
    "run-qwen3-asr.sh": "MINDEX_ASR_BACKEND",
    "run-tts-voiceclone.sh": "MINDEX_TTS_BACKEND",
    "run-tts-voicedesign.sh": "MINDEX_TTS_BACKEND",
    "run-music-gen.sh": "MINDEX_MUSIC3_BACKEND",
    "run-person-service.sh": "MINDEX_PERSON_BACKEND",
    "run-transnet.sh": "MINDEX_TRANSNET_BACKEND",
}


@pytest.fixture
def launch(tmp_path):
    project = tmp_path / "project with spaces"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    shutil.copytree(ROOT / "scripts" / "lib", scripts / "lib")
    for name in SCRIPTS:
        shutil.copy(ROOT / "scripts" / name, scripts / name)
    (project / "models" / "WeMM-Embedding").mkdir(parents=True)
    for executable in ("bin/llama-server", "bin/tts-server", "bin/audiocpp_server",
                       ".venv-person/bin/python", ".venv-detector/bin/python"):
        target = project / executable
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            '#!/usr/bin/env python3\nimport json, os, sys\n'
            'print(json.dumps({"args": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd()}))\n'
        )
        target.chmod(0o755)

    def run(name, *args, env=None):
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("MINDEX_")}
        return subprocess.run(
            ["bash", str(scripts / name), *args], cwd=tmp_path,
            env={**clean_env, **(env or {})}, text=True, capture_output=True, timeout=10, check=False,
        )

    return run


def assert_backend(name, result, backend):
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    args = data["args"]
    assert data["cwd"].endswith("project with spaces")
    if name in {"run-wemm.sh", "run-qwen3-asr.sh"}:
        assert args[args.index("--device") + 1] == {"metal": "MTL0", "cuda": "CUDA0", "cpu": "none"}[backend]
        assert args[args.index("-ngl") + 1] == ("0" if backend == "cpu" else "99")
        if backend == "cpu":
            assert {"--no-mmproj-offload", "--no-kv-offload", "--no-op-offload"} <= set(args)
    elif name.startswith("run-tts-"):
        assert args[args.index("--device") + 1] == {"metal": "MTL0", "cuda": "CUDA0", "cpu": "CPU"}[backend]
    elif name == "run-music-gen.sh":
        assert args[args.index("--backend") + 1] == backend
    elif name == "run-person-service.sh":
        assert data["env"]["MINDEX_PERSON_BACKEND"] == backend
    else:
        assert data["env"]["MINDEX_TRANSNET_DEVICE"] == backend


@pytest.mark.parametrize("name", SCRIPTS)
@pytest.mark.parametrize("backend", ["metal", "cuda", "cpu"])
@pytest.mark.parametrize("equals", [False, True])
def test_all_scripts_select_requested_backend(launch, name, backend, equals):
    args = [f"--backend={backend}"] if equals else ["--backend", backend]
    assert_backend(name, launch(name, *args), backend)


@pytest.mark.parametrize("name", SCRIPTS)
def test_backend_environment_and_cli_precedence(launch, name):
    env = {BACKEND_ENV[name]: "cuda"}
    assert_backend(name, launch(name, env=env), "cuda")
    assert_backend(name, launch(name, "--backend", "cpu", env=env), "cpu")


@pytest.mark.parametrize("name", SCRIPTS)
def test_default_launch_keeps_previous_device_selection(launch, name):
    result = launch(name)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    args = data["args"]
    if name in {"run-wemm.sh", "run-qwen3-asr.sh"}:
        assert args[args.index("-ngl") + 1] == "99" and "--device" not in args
    elif name.startswith("run-tts-"):
        assert "--device" not in args
    elif name == "run-music-gen.sh":
        assert args[args.index("--backend") + 1] == "metal"
    elif name == "run-person-service.sh":
        assert "MINDEX_PERSON_BACKEND" not in data["env"]
    else:
        assert data["env"]["MINDEX_TRANSNET_DEVICE"] == "cpu"


@pytest.mark.parametrize("name", SCRIPTS)
@pytest.mark.parametrize("args", [("--backend",), ("--backend=",), ("--backend", "vulkan"), ("--unknown",)])
def test_invalid_arguments_fail_before_launch(launch, name, args):
    result = launch(name, *args)
    assert result.returncode == 2 and result.stderr
    assert not result.stdout


@pytest.mark.parametrize("name", SCRIPTS)
def test_help_without_models_or_installed_services(tmp_path, name):
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    shutil.copytree(ROOT / "scripts" / "lib", scripts / "lib")
    shutil.copy(ROOT / "scripts" / name, scripts / name)
    result = subprocess.run(
        ["bash", str(scripts / name), "--help"], text=True, capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--backend metal|cuda|cpu" in result.stdout


def test_legacy_python_device_overrides_remain_supported(launch):
    result = launch("run-transnet.sh", env={"MINDEX_TRANSNET_DEVICE": "auto"})
    assert json.loads(result.stdout)["env"]["MINDEX_TRANSNET_DEVICE"] == "auto"
    result = launch("run-person-service.sh", env={"MINDEX_PERSON_PROVIDERS": "CustomProvider"})
    assert json.loads(result.stdout)["env"]["MINDEX_PERSON_PROVIDERS"] == "CustomProvider"
