"""智能助手测试辅助：脚本化模型驱动真实应用，等待会话空闲并读取事件日志。"""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient


def wait_idle(client: TestClient, sid: str, timeout: float = 30, status=("idle",)) -> list[dict]:
    deadline = time.monotonic() + timeout
    events: list[dict] = []
    after = -1
    starts = ends = 0
    while time.monotonic() < deadline:
        # 长任务只读取新增事件，避免反复传输、解码全部历史拖慢运行中的会话。
        data = client.get(f"/api/agent/sessions/{sid}/log", params={"after": after}).json()
        events.extend(data["items"])
        if data["items"]:
            after = data["items"][-1]["seq"]
        types = [e["type"] for e in data["items"]]
        starts += types.count("turn/start")
        ends += types.count("turn/end")
        if data["status"] in status and starts and starts == ends:
            return events
        if data["status"] == "waiting" and "waiting" in status:
            return events
        time.sleep(0.02)
    raise AssertionError(f"会话 {sid} 未在 {timeout}s 内到达 {status}：{client.get(f'/api/agent/sessions/{sid}/log').json()}")


def wait_pending(client: TestClient, sid: str, kind: str, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/agent/sessions/{sid}").json()
        for item in data["pending"]:
            if item["kind"] == kind:
                return item
        time.sleep(0.02)
    raise AssertionError(f"会话 {sid} 没有出现待处理的 {kind}")


def new_session(client: TestClient) -> str:
    response = client.post("/api/agent/sessions", json={})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def send(client: TestClient, sid: str, text: str, context: dict | None = None):
    response = client.post(f"/api/agent/sessions/{sid}/messages", json={"content": text, "context": context})
    assert response.status_code == 202, response.text
    return response.json()


def results(events: list[dict]) -> list[dict]:
    return [e["data"] for e in events if e["type"] == "tool/result"]


def result_text(data: dict) -> str:
    return "".join(b["text"] for b in data["message"]["content"])


def final_text(events: list[dict]) -> str:
    messages = [e["data"]["message"] for e in events if e["type"] == "assistant/message"]
    return "".join(b.get("text", "") for b in messages[-1]["content"] if b["type"] == "text") if messages else ""


def parse_sse(raw: str) -> list[tuple[str, dict]]:
    frames = []
    for block in raw.strip().split("\n\n"):
        event, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if event:
            frames.append((event, data))
    return frames
