"""显式后端选择及缺少加速依赖时的错误，无需 TensorFlow/ONNX 重依赖。"""

from __future__ import annotations

import importlib.metadata
import sys
from types import SimpleNamespace

import pytest

from mindex.models.model_server import configure_transnet_device
from mindex.persons.person_server import execution_providers


def tensorflow_stub(*, cuda=False, gpu=True):
    visible = ["GPU:0"] if gpu else []
    growth = []

    def hide(devices, kind):
        assert kind == "GPU"
        visible[:] = devices

    return SimpleNamespace(
        config=SimpleNamespace(
            set_visible_devices=hide,
            get_visible_devices=lambda kind: visible,
            experimental=SimpleNamespace(set_memory_growth=lambda device, enabled: growth.append(device)),
        ),
        test=SimpleNamespace(is_built_with_cuda=lambda: cuda),
        visible=visible, growth=growth,
    )


@pytest.mark.parametrize("backend", ["metal", "cuda", "cpu", "auto"])
def test_transnet_selects_device_before_loading_model(monkeypatch, backend):
    tf = tensorflow_stub(cuda=backend == "cuda")
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "test-only")
    configure_transnet_device(tf, backend)
    assert bool(tf.visible) == (backend != "cpu")
    assert bool(tf.growth) == (backend in {"cuda", "auto"})


@pytest.mark.parametrize("backend", ["metal", "cuda"])
def test_transnet_explicit_gpu_does_not_fall_back_to_cpu(monkeypatch, backend):
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "test-only")
    with pytest.raises(RuntimeError, match="未发现可见 GPU"):
        configure_transnet_device(tensorflow_stub(cuda=True, gpu=False), backend)


def test_transnet_missing_acceleration_dependencies(monkeypatch):
    with pytest.raises(RuntimeError, match="支持 CUDA"):
        configure_transnet_device(tensorflow_stub(), "cuda")

    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    with pytest.raises(RuntimeError, match="安装 tensorflow-metal"):
        configure_transnet_device(tensorflow_stub(), "metal")
    with pytest.raises(ValueError, match="MINDEX_TRANSNET_DEVICE"):
        configure_transnet_device(tensorflow_stub(), "unknown")


@pytest.mark.parametrize("backend,provider", [
    ("cuda", "CUDAExecutionProvider"), ("cpu", "CPUExecutionProvider"),
])
def test_person_selects_available_provider(monkeypatch, backend, provider):
    monkeypatch.setenv("MINDEX_PERSON_BACKEND", backend)
    monkeypatch.setenv("MINDEX_PERSON_PROVIDERS", "LegacyProvider")
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(get_available_providers=lambda: [provider]))
    assert execution_providers() == ([provider] if backend == "cpu" else [provider, "CPUExecutionProvider"])


def test_person_missing_provider_does_not_fall_back_to_cpu(monkeypatch):
    monkeypatch.setenv("MINDEX_PERSON_BACKEND", "cuda")
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        get_available_providers=lambda: ["CPUExecutionProvider"],
    ))
    with pytest.raises(RuntimeError, match="所选后端 cuda 不可用"):
        execution_providers()


def test_person_metal_reports_pinned_sdk_limitation_even_if_coreml_is_available(monkeypatch):
    monkeypatch.setenv("MINDEX_PERSON_BACKEND", "metal")
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        get_available_providers=lambda: ["CoreMLExecutionProvider", "CPUExecutionProvider"],
    ))
    with pytest.raises(RuntimeError, match="PersonAnalysis 仅支持 CPU/CUDA"):
        execution_providers()


def test_person_default_legacy_providers_and_invalid_backend(monkeypatch):
    monkeypatch.delenv("MINDEX_PERSON_BACKEND", raising=False)
    monkeypatch.delenv("MINDEX_PERSON_PROVIDERS", raising=False)
    assert execution_providers() == ["CPUExecutionProvider"]
    monkeypatch.setenv("MINDEX_PERSON_PROVIDERS", "CustomProvider, CPUExecutionProvider")
    assert execution_providers() == ["CustomProvider", "CPUExecutionProvider"]
    monkeypatch.setenv("MINDEX_PERSON_BACKEND", "invalid")
    with pytest.raises(ValueError, match="MINDEX_PERSON_BACKEND"):
        execution_providers()
