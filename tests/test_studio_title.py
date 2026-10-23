import time

from fastapi.testclient import TestClient

from mindex.api import create_app
from mindex.models import adapters
from mindex.studio import studio


def _await_title(config, job_id, timeout=5.0):
    """标题在后台线程补全，轮询到 title_pending 落位为止。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = studio.load_job(config, job_id)
        if not state.get("title_pending"):
            return state
        time.sleep(0.02)
    raise AssertionError("标题在超时内仍未生成完成")


def test_create_generates_title_with_configured_generator(config, monkeypatch):
    requests = []

    def reply(method, url, **kwargs):
        requests.append((method, url, kwargs))
        return {"choices": [{"message": {"content": '{"title":"阿嬷的牵挂"}'}}]}

    monkeypatch.setattr(adapters, "request", reply)
    client = TestClient(create_app(config))
    response = client.post("/api/studio/jobs", json={"text": "阿嬷把对家人的牵挂写进一封封信。"})
    assert response.status_code == 201, response.text
    # 创建立即返回占位标题并标记待生成，真实标题由后台线程补全。
    assert response.json()["title_pending"] is True
    job_id = response.json()["id"]
    settled = _await_title(config, job_id)
    assert settled["title"] == "阿嬷的牵挂"
    # 标题基于视频大纲生成；此时任务里没有文案。
    assert settled["outline"] == "阿嬷把对家人的牵挂写进一封封信。" and "input_text" not in settled
    # 后台线程确实调用了配置的生成服务。
    _method, url, kwargs = next(r for r in requests if r[0] == "POST")
    assert url == config.studio.generator.base_url.rstrip("/") + "/chat/completions"
    assert kwargs["json"]["model"] == config.studio.generator.model
    assert "阿嬷" in kwargs["json"]["messages"][1]["content"][0]["text"]
    # 显式指定标题时不进入待生成态，也不触发生成请求。
    requests.clear()
    explicit = client.post("/api/studio/jobs", json={"text": "文案", "title": "自定标题"}).json()
    assert explicit["title"] == "自定标题"
    assert explicit["title_pending"] is False
    assert client.post("/api/studio/jobs", json={"text": "  "}).status_code == 422
    assert not requests


def test_bad_generated_title_keeps_placeholder(config, monkeypatch):
    # 生成服务返回空标题时，创建不再失败：占位标题继续可用，任务照常建立。
    monkeypatch.setattr(adapters, "request", lambda *a, **kw: {
        "choices": [{"message": {"content": '{"title":"  "}'}}]
    })
    response = TestClient(create_app(config)).post("/api/studio/jobs", json={"text": "视频文案"})
    assert response.status_code == 201, response.text
    job_id = response.json()["id"]
    assert response.json()["title"] == "视频文案"
    settled = _await_title(config, job_id)
    assert settled["title"] == "视频文案"  # 坏标题被丢弃，保留占位
    assert studio.list_jobs(config)
