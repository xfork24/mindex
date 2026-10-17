"""Real FFmpeg + person HTTP service + persisted jobs + retrieval/workspace integration.

Only model inference is deterministic; this does not measure recognition quality.
"""

import io
import socket
import threading
import time
from typing import ClassVar

import numpy as np
import uvicorn
from conftest import FixtureEmbedder
from fastapi.testclient import TestClient
from PIL import Image

from mindex.api import create_app
from mindex.indexing import IndexBuilder
from mindex.media import command
from mindex.person_server import create_person_app
from mindex.pipeline import Pipeline


class ColorPersonEngine:
    space: ClassVar[dict] = {"model": "cheetah_l", "implementation": "test-only-color"}

    def image(self, path):
        with Image.open(path) as image:
            color = np.asarray(image.convert("RGB")).mean(axis=(0, 1))
            width, height = image.size
        feature = np.eye(3)[int(np.argmax(color))].tolist()
        return {
            "space": self.space,
            "observations": [
                {
                    "body_bbox": [0, 0, width, height],
                    "face_bbox": [0, 0, width, height],
                    "det_score": 0.99,
                    "face_quality": 0.95,
                    "face_feature": feature,
                    "body_feature": feature,
                }
            ],
        }


def test_subtitle_person_task_publish_strict_mentions_and_studio_media(
    config, video_file, tmp_path, monkeypatch
):
    # A real embedded subtitle in the green shot mentions an actor seen only in red.
    subtitles = tmp_path / "dialogue.srt"
    subtitles.write_text("1\n00:00:02,100 --> 00:00:02,900\n验收演员甲，明天再见。\n", encoding="utf-8")
    source_file = tmp_path / "subtitled.mp4"
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(video_file),
            "-i",
            str(subtitles),
            "-map",
            "0:v",
            "-map",
            "1:s",
            "-c:v",
            "copy",
            "-c:s",
            "mov_text",
            "-metadata:s:s:0",
            "language=zho",
            str(source_file),
        ]
    )
    config.studio.recall.granularity = "shot"
    monkeypatch.setattr(
        "mindex.pipeline.make_analyzer",
        lambda *_: (_ for _ in ()).throw(AssertionError("LLM must stay offline")),
    )
    monkeypatch.setattr(
        "mindex.dialogue.make_transcriber",
        lambda *_: (_ for _ in ()).throw(AssertionError("Subtitles must bypass ASR")),
    )
    monkeypatch.setenv("MINDEX_GPU_LOCK", str(tmp_path / "fixture-gpu.lock"))
    monkeypatch.delenv("MINDEX_MODEL_API_KEY", raising=False)
    # Bind before launching so the port cannot be taken between selection and startup.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        config.person.base_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(
            uvicorn.Config(create_person_app(config, ColorPersonEngine), log_level="error")
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                assert thread.is_alive() and time.monotonic() < deadline
                time.sleep(0.01)
            pipeline = Pipeline(config)
            source = pipeline.prepare(source_file)
            embedder = FixtureEmbedder(config)
            IndexBuilder(config, embedder).build(activate=True, kinds=("shot",))
            with TestClient(create_app(config, embedder)) as client:
                dialogue = client.post(f"/api/admin/sources/{source}/dialogue", json={"publish": True})
                assert dialogue.status_code == 202

                def complete(path):
                    deadline = time.monotonic() + 20
                    while True:
                        response = client.get(path)
                        assert response.status_code == 200, response.text
                        task = response.json()
                        assert task["status"] not in {"failed", "interrupted"}, task
                        if task["status"] == "complete":
                            return task
                        assert time.monotonic() < deadline, task
                        time.sleep(0.02)

                task = complete(f"/api/admin/dialogue-tasks/{dialogue.json()['id']}")
                assert task["result"]["sources"][0]["source"] == "subtitle"
                actor_response = client.post(
                    "/api/persons/entities", json={"kind": "actor", "name": "验收演员甲"}
                )
                assert actor_response.status_code == 201
                actor = actor_response.json()["id"]
                buffer = io.BytesIO()
                Image.new("RGB", (100, 100), "red").save(buffer, format="PNG")
                reference = client.post(
                    "/api/persons/references",
                    data={"actor_id": actor},
                    files={"file": ("red.png", buffer.getvalue())},
                )
                assert reference.status_code == 201, reference.text
                started = client.post(f"/api/persons/sources/{source}/process", json={"publish": True})
                assert started.status_code == 202, started.text
                task = complete(f"/api/persons/tasks/{started.json()['id']}")
                assert task["result"]["accepted"] > 0 and task["result"]["candidates"] > 0
                strict = client.post(
                    "/api/search/text", json={"text": "验收演员甲", "granularity": "shot"}
                ).json()
                mentioned = client.post(
                    "/api/search/text",
                    json={"text": "验收演员甲", "person_mode": "mentioned", "granularity": "shot"},
                ).json()
                assert strict["results"] and all(hit["end"] <= 1 for hit in strict["results"])
                assert mentioned["results"] and all(hit["start"] >= 2 for hit in mentioned["results"])
                assert strict["results"][0]["person_evidence"]
                assert client.get(strict["results"][0]["person_evidence"][0]["image_url"]).status_code == 200
                shot = strict["results"][0]["id"]
                details = client.get(f"/api/admin/segments/{shot}/details").json()
                assert details["person"]["status"] == "processed"
                assert details["asr"]["status"] == "not_processed"
                assert details["llm"]["status"] == "not_processed"
                assert details["subtitles"]["tracks"]
                # Model service calls stop here; a published name query stays available.
                calls = embedder.calls
                assert client.post(
                    "/api/search/text", json={"text": "验收演员甲", "granularity": "shot"}
                ).json()["results"]
                assert embedder.calls == calls
                job = client.post("/api/studio/jobs", json={"text": "红色开场。"}).json()["id"]
                workspace = client.get(f"/api/studio/jobs/{job}/workspace").json()
                plan = workspace["active_plan"]
                upload = client.post(
                    f"/api/studio/jobs/{job}/plans/{plan}/query-media",
                    data={"kind": "image"},
                    files={"file": ("red.png", buffer.getvalue())},
                )
                assert upload.status_code == 201, upload.text
                assert client.get(upload.json()["preview_url"]).status_code == 200
                revision = workspace["revision"]
                edited = client.patch(
                    f"/api/studio/jobs/{job}/plans/{plan}",
                    json={
                        "revision": revision,
                        "shots": [
                            {
                                "id": "a",
                                "text": "红色开场。",
                                "query": "红色",
                                "estimated_seconds": 1,
                                "recall": {
                                    "mode": "image",
                                    "media_id": upload.json()["media_id"],
                                    "media_kind": "image",
                                    "filters": {"source_id": source},
                                },
                            }
                        ],
                    },
                )
                assert edited.status_code == 200, edited.text
                workspace = client.get(f"/api/studio/jobs/{job}/workspace").json()
                assert workspace["revision"] != revision
                inputs = {"content": workspace["plans"][plan]["shots"]["a"]["content"]}
                built = client.post(
                    f"/api/studio/jobs/{job}/plans/{plan}/nodes",
                    json={
                        "revision": workspace["revision"],
                        "stage": "recall",
                        "shot_id": "a",
                        "inputs": inputs,
                    },
                )
                assert built.status_code == 202, built.text
                deadline = time.monotonic() + 10
                while True:
                    workspace = client.get(f"/api/studio/jobs/{job}/workspace").json()
                    if workspace["builds"][-1]["status"] != "running":
                        break
                    assert time.monotonic() < deadline
                    time.sleep(0.02)
                assert workspace["builds"][-1]["status"] == "complete", workspace["builds"][-1]
                recalled = workspace["versions"][workspace["plans"][plan]["shots"]["a"]["recall"]]["data"]
                assert recalled["mode"] == "image" and recalled["results"][0]["start"] == 0
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
