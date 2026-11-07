"""后台标题与执行线程共享 job.json；不得用旧状态覆盖已完成步骤。"""

import threading
import time

from fastapi.testclient import TestClient

from api import create_app
from shared import locks
from studio import studio


def test_title_waits_for_execution_lock_before_reading_and_merging(config, monkeypatch):
    generated, read = threading.Event(), threading.Event()

    class Storyboarder:
        def title(self, text):
            generated.set()
            return "后台生成的标题"

    load_job = studio.load_job

    def observe(*args, **kwargs):
        if threading.current_thread().name.startswith("studio-title-"):
            read.set()
        return load_job(*args, **kwargs)

    monkeypatch.setattr(studio, "load_job", observe)
    with TestClient(create_app(config, storyboarder=Storyboarder())) as client:
        with locks.named(config.data_dir, "studio"):
            response = client.post("/api/studio/jobs", json={"text": "标题写回不改变步骤状态"})
            assert response.status_code == 201, response.text
            job_id = response.json()["id"]
            assert generated.wait(3)
            assert not read.wait(0.2)  # 执行持锁期间不能先读出一份将要过期的状态。
            state = load_job(config, job_id)
            state["steps"]["split"]["status"] = "complete"
            studio.save(studio.job_path(config, job_id), state, config=config)
        assert read.wait(3)
        for _ in range(100):
            state = load_job(config, job_id)
            if not state["title_pending"]:
                break
            time.sleep(0.02)
        assert state["title_pending"] is False
        assert state["title"] == "后台生成的标题"
        assert state["steps"]["split"]["status"] == "complete"
