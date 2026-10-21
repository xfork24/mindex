from fastapi.testclient import TestClient

from mindex.api import create_app
from mindex.models import adapters
from mindex.studio import studio


def test_create_generates_title_with_configured_generator(config, monkeypatch):
    requests = []

    def reply(method, url, **kwargs):
        requests.append((method, url, kwargs))
        return {"choices": [{"message": {"content": '{"title":"阿嬷的牵挂"}'}}]}

    monkeypatch.setattr(adapters, "request", reply)
    client = TestClient(create_app(config))
    response = client.post("/api/studio/jobs", json={"text": "阿嬷把对家人的牵挂写进一封封信。"})
    assert response.status_code == 201, response.text
    assert response.json()["title"] == "阿嬷的牵挂"
    assert studio.load_job(config, response.json()["id"])["title"] == "阿嬷的牵挂"
    method, url, kwargs = requests.pop()
    assert method == "POST"
    assert url == config.studio.generator.base_url.rstrip("/") + "/chat/completions"
    assert kwargs["json"]["model"] == config.studio.generator.model
    assert "阿嬷" in kwargs["json"]["messages"][1]["content"][0]["text"]
    assert client.post("/api/studio/jobs", json={"text": "文案", "title": "自定标题"}).json()["title"] == "自定标题"
    assert client.post("/api/studio/jobs", json={"text": "  "}).status_code == 422
    assert not requests


def test_bad_generated_title_does_not_create_task(config, monkeypatch):
    monkeypatch.setattr(adapters, "request", lambda *a, **kw: {
        "choices": [{"message": {"content": '{"title":"  "}'}}]
    })
    response = TestClient(create_app(config)).post("/api/studio/jobs", json={"text": "视频文案"})
    assert response.status_code == 503
    assert "标题" in response.json()["detail"]
    assert not studio.list_jobs(config)
