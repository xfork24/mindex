"""智能助手构建工具：真实后台构建、构建跟踪与自动总结、重启后续跟踪、409 冲突、高风险操作不可授权。"""

from __future__ import annotations

import time

import pytest
from agent_helpers import new_session, result_text, results, send, wait_idle, wait_pending
from conftest import FixtureEmbedder, FixtureSpeaker, FixtureStoryboarder, legacy_job
from fastapi.testclient import TestClient
from filelock import FileLock

from mindex.agent.llm.scripted import ScriptedLlm, call, say
from mindex.api import create_app
from mindex.processing.indexing import IndexBuilder
from mindex.processing.pipeline import Pipeline
from mindex.studio import studio
from mindex.studio import studio_workspace as ws

SHOTS = ["shot-a1daad59aa", "shot-925281d5bb", "shot-77f0c3e1cc"]


@pytest.fixture
def prepared(config, video_file):
    config.studio.render.width = 160
    config.studio.render.height = 120
    config.studio.render.preset = "ultrafast"
    Pipeline(config).prepare(video_file)
    embedder = FixtureEmbedder(config)
    IndexBuilder(config, embedder).build(mode="visual", kinds=("shot",), activate=True)
    job_id = legacy_job(config, "红色开场。蓝色中段。绿色结尾。", options={"burn": False})["id"]
    w = ws.load(config, job_id)
    p = ws.plan(w, w["active_plan"])
    ws.set_board(config, job_id, w, p, [
        studio.Shot(id=sid, text=text, query=text[:2], estimated_seconds=1.0)
        for sid, text in zip(SHOTS, ["红色开场。", "蓝色中段。", "绿色结尾。"])
    ])
    p["board_script"] = p["script"]
    ws.persist(config, job_id, w)
    return config, embedder, job_id


def make_app(config, embedder, llm):
    app = create_app(config, embedder, storyboarder=FixtureStoryboarder(config), speaker=FixtureSpeaker(config),
                     agent_llm=llm)
    app.state.agent_runtime.watcher.interval = 0.05
    return app


def approve(client, sid):
    pending = wait_pending(client, sid, "approval")
    client.post(f"/api/agent/sessions/{sid}/approvals/{pending['requestId']}", json={"outcome": "allowed-once"})
    return pending


def wait_type(client, sid, kind, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items = client.get(f"/api/agent/sessions/{sid}/log").json()["items"]
        if any(e["type"] == kind for e in items):
            return items
        time.sleep(0.05)
    raise AssertionError(f"没有出现 {kind}")


def test_merge_then_build_with_followup_summary(prepared):
    config, embedder, job = prepared
    llm = ScriptedLlm([
        [call("edit_shots", job_id=job, operations=[{"op": "merge", "shots": ["a1daad59", "925281d5"]}])],
        [call("run_build", job_id=job)],
        [say("已提交构建，完成后我会总结。")],
        lambda request: [say("构建完成：" + request.messages[-1]["content"][0]["text"].splitlines()[0])],
    ])
    with TestClient(make_app(config, embedder, llm)) as client:
        sid = new_session(client)
        send(client, sid, "合并 a1daad59 和 925281d5 两个分镜，并重新生成该分镜的后续流程")
        approve(client, sid)
        pending = approve(client, sid)
        assert pending["name"] == "run_build" and pending["risk"] == "danger" and not pending["allowGrant"]
        assert pending["preview"]["changes"][0]["label"] == "重建"
        events = wait_type(client, sid, "plugin:mindex/build-finished", timeout=120)
        events = wait_idle(client, sid, timeout=60)
    finished = next(e for e in events if e["type"] == "plugin:mindex/build-finished")["data"]
    assert finished["status"] == "complete", finished
    assert finished["counts"].get("complete", 0) >= 1 and finished["final"]
    followup = [e for e in events if e["type"] == "user/message" and e["data"]["source"]["kind"] == "followup"]
    assert len(followup) == 1 and "已结束，状态：complete" in followup[0]["data"]["content"][0]["text"]
    final = [e for e in events if e["type"] == "assistant/message"][-1]["data"]["message"]["content"][0]["text"]
    assert final.startswith("构建完成：[系统通知]")
    watch = next(e for e in events if e["type"] == "plugin:mindex/build-watch")
    assert watch["data"]["build_id"] == finished["build_id"]
    assert sum(1 for e in events if e["type"] == "turn/start") == 2


def test_restart_resumes_watch_and_conflict(prepared):
    config, embedder, job = prepared
    app = make_app(config, embedder, ScriptedLlm())
    with TestClient(app) as client:
        data = client.get(f"/api/studio/jobs/{job}/workspace").json()
        pid = data["active_plan"]
        token = client.get(f"/api/studio/jobs/{job}/plans/{pid}/preflight").json()["token"]
        build = client.post(f"/api/studio/jobs/{job}/plans/{pid}/build", json={"token": token}).json()
        deadline = time.monotonic() + 120
        while client.get(f"/api/studio/jobs/{job}/builds/{build['id']}/logs").json()["status"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.1)
        sid = new_session(client)
    store = app.state.agent_runtime.store
    store.append(sid, "plugin:mindex/build-watch", {"job_id": job, "build_id": build["id"], "plan_id": pid})

    llm = ScriptedLlm([[say("重启后补发的总结")]])
    with TestClient(make_app(config, embedder, llm)) as client:
        client.get("/api/agent/sessions")  # 首次访问触发恢复
        events = wait_type(client, sid, "plugin:mindex/build-finished")
        events = wait_idle(client, sid)
        assert any(e["type"] == "user/message" and e["data"]["source"]["kind"] == "followup" for e in events)

        # 全局构建锁被占用：409 作为可恢复结果交给模型。
        llm.extend([call("run_build", job_id=job)], [say("有其他构建在执行，稍后再试。")])
        lock = FileLock(str(studio.lock_path(config)), timeout=0)
        lock.acquire()
        try:
            send(client, sid, "构建")
            approve(client, sid)
            events = wait_idle(client, sid)
        finally:
            lock.release()
    failed = results(events)[-1]
    assert failed["message"]["isError"] and "HTTP 409" in result_text(failed) and "稍后重试" in result_text(failed)


def test_danger_cannot_be_granted_and_delete(prepared):
    config, embedder, job = prepared
    llm = ScriptedLlm([[call("delete_job", job_id=job)], [say("已删除")]])
    with TestClient(make_app(config, embedder, llm)) as client:
        sid = new_session(client)
        send(client, sid, "删除这个任务")
        pending = wait_pending(client, sid, "approval")
        assert "无法恢复" in pending["preview"]["warnings"][0]
        client.post(f"/api/agent/sessions/{sid}/approvals/{pending['requestId']}",
                    json={"outcome": "allowed-once", "grant": True})
        events = wait_idle(client, sid)
        assert client.get(f"/api/agent/sessions/{sid}").json()["grants"] == []
        assert client.get(f"/api/studio/jobs/{job}").status_code == 404
    mutation = next(e for e in events if e["type"] == "plugin:mindex/mutation")["data"]
    assert mutation["deleted"] is True
