"""本地分镜服务协议及逐帧流式边界，不依赖应用环境安装 TensorFlow。"""

import threading

import numpy as np
import pytest
from fastapi.testclient import TestClient
from filelock import FileLock

from mindex import adapters
from mindex.adapters import HTTPShotDetector
from mindex.media import command, frame_timestamps
from mindex.model_server import TransNetEngine, create_model_app


@pytest.mark.parametrize("frames", [1, 49, 50, 51, 75, 99, 100, 101, 150])
def test_streaming_detect_preserves_every_frame(tmp_path, frames):
    video = tmp_path / "frames.mp4"
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=s=48x28:r=25",
            "-frames:v",
            str(frames),
            "-c:v",
            "libx264",
            str(video),
        ]
    )

    class FrameModel:
        def predict_raw(self, frames):
            assert frames.shape == (1, 100, 27, 48, 3)
            output = np.zeros((1, 100, 1), dtype=np.float32)
            return output, output

    engine = TransNetEngine.__new__(TransNetEngine)
    engine.model, engine.lock = FrameModel(), threading.Lock()
    result = engine.detect(video, 0.5)
    assert result == {"model": "TransNetV2", "frame_count": frames, "cut_frames": []}
    assert len(frame_timestamps(video)) == frames


def test_local_cpu_service_auth_upload_and_client(config, video_file, monkeypatch, tmp_path):
    gpu_lock = tmp_path / "gpu.lock"
    monkeypatch.setenv("MINDEX_GPU_LOCK", str(gpu_lock))
    monkeypatch.setenv("MINDEX_MODEL_API_KEY", "fixture-key")
    monkeypatch.setenv("MINDEX_TRANSNET_DEVICE", "cpu")
    config.detector.backend = "transnet"
    config.detector.base_url = "http://127.0.0.1:18203"

    class Engine:
        def __init__(self):
            self.metadata = {"model": "TransNetV2", "device": "cpu"}

        def detect(self, path, threshold):
            assert threshold == 0.5 and path.stat().st_size
            return {"model": "TransNetV2", "frame_count": len(frame_timestamps(path)), "cut_frames": [25, 50]}

    # CPU 服务不抢占其他模型的 GPU 锁。
    with (
        FileLock(str(gpu_lock), timeout=0),
        TestClient(create_model_app(config, "detector", Engine)) as client,
    ):
        assert client.get("/health").status_code == 401
        headers = {"Authorization": "Bearer fixture-key"}
        assert client.get("/health", headers=headers).json() == {
            "ready": True,
            "role": "detector",
            "model": "TransNetV2",
            "device": "cpu",
        }
        assert (
            client.post("/detect/video", headers=headers, files={"file": ("empty.mp4", b"")}).status_code
            == 422
        )
        assert (
            client.post(
                "/detect/video",
                headers=headers,
                data={"threshold": 1},
                files={"file": ("video.mp4", video_file.read_bytes())},
            ).status_code
            == 422
        )

        def request(method, url, **kwargs):
            kwargs.pop("timeout", None)
            response = client.request(method, url.removeprefix(config.detector.base_url), **kwargs)
            response.raise_for_status()
            return response.json()

        monkeypatch.setattr(adapters, "request", request)
        assert HTTPShotDetector(config).detect(video_file) == [(0, 1), (1, 2), (2, 10)]
        config.source_upload_max_bytes = 1
        assert (
            client.post("/detect/video", headers=headers, files={"file": ("big.mp4", b"xx")}).status_code
            == 413
        )
