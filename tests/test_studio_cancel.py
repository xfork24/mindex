from __future__ import annotations

import copy
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from api import create_app
from conftest import FixtureSpeaker, FixtureStoryboarder
from studio import executor, studio
from studio import workspace as ws
from studio.logs import read_events
from studio.workspace import builder, signatures
from test_studio_api import create_legacy_job
from test_studio_columns import request_body
from test_studio_workspace import setup_workspace, wait_build

KEYS = [("tts", "a"), ("tts", "b"), ("select", "a"), ("select", "b")]


def add_build(config, job_id, w, p, bid="b1", status="running"):
    nodes = [
        {"key": f"{stage}:{sid}", "stage": stage, "shot_id": sid, "action": "rebuild", "status": "pending"}
        for stage, sid in KEYS
    ]
    w["builds"].append(
        {"id": bid, "plan_id": p["id"], "status": status, "force": False, "auto_split": False, "nodes": nodes}
    )
    ws.persist(config, job_id, w)
    return bid


def fake_execution(monkeypatch, on_execute):
    """节点执行替换为确定性桩：只记录执行顺序，由 on_execute 在执行中途触发取消。"""
    calls = []

    def execute(config, job_id, w, p, stage, sid, **_):
        calls.append(f"{stage}:{sid}")
        on_execute(len(calls))
        return f"v-{stage}-{sid}"

    monkeypatch.setattr(signatures, "match", lambda *a, **k: None)
    monkeypatch.setattr(builder, "execute", execute)
    return calls


def test_sentinel_cancel_lets_running_node_finish_and_blocks_rest(config, monkeypatch):
    jid, w, p = setup_workspace(config)
    bid = add_build(config, jid, w, p)

    def on_execute(n):
        if n == 2:  # 第二个节点执行期间请求取消（模拟另一进程写哨兵）
            executor.request_cancel(config, "build", jid, bid)

    calls = fake_execution(monkeypatch, on_execute)
    ws.run_build(config, jid, bid)

    assert calls == ["tts:a", "tts:b"]
    build = next(b for b in ws.load(config, jid)["builds"] if b["id"] == bid)
    assert build["status"] == "cancelled" and build["finished_at"] and "board_result" in build
    nodes = {n["key"]: n for n in build["nodes"]}
    assert nodes["tts:a"]["status"] == nodes["tts:b"]["status"] == "complete"
    assert nodes["tts:b"]["version"] == "v-tts-b"
    for key in ("select:a", "select:b"):
        assert nodes[key]["status"] == "blocked" and nodes[key]["error"] == "已取消"
        assert "started_at" not in nodes[key]
    assert not executor.cancel_sentinel(config, "build", jid, bid).exists()
    messages = [e["message"] for e in read_events(config, jid, bid)["items"]]
    assert any("执行结束：已取消" in m for m in messages)


def test_event_cancel_before_first_node_runs_nothing(config, monkeypatch):
    jid, w, p = setup_workspace(config)
    bid = add_build(config, jid, w, p)
    event = threading.Event()
    event.set()
    calls = fake_execution(monkeypatch, lambda n: None)
    assert executor.cancel_requested(config, "build", jid, bid, event)
    assert not executor.cancel_requested(config, "build", jid, bid)

    ws.run_build(config, jid, bid, cancel_event=event)

    assert calls == []
    build = next(b for b in ws.load(config, jid)["builds"] if b["id"] == bid)
    assert build["status"] == "cancelled"
    assert all(n["status"] == "blocked" and n["error"] == "已取消" for n in build["nodes"])


def test_uncancelled_build_unaffected(config, monkeypatch):
    jid, w, p = setup_workspace(config)
    bid = add_build(config, jid, w, p)
    calls = fake_execution(monkeypatch, lambda n: None)
    ws.run_build(config, jid, bid)
    assert len(calls) == len(KEYS)
    build = next(b for b in ws.load(config, jid)["builds"] if b["id"] == bid)
    assert build["status"] == "complete"


def test_recover_leaves_cancelled_build_and_clears_stale_sentinel(config):
    jid, w, p = setup_workspace(config)
    done = add_build(config, jid, w, p, bid="done", status="cancelled")
    w = ws.load(config, jid)
    cancelled = next(b for b in w["builds"] if b["id"] == done)
    cancelled_before = dict(cancelled, nodes=[dict(n) for n in cancelled["nodes"]])
    live = add_build(config, jid, w, p, bid="live")
    executor.request_cancel(config, "build", jid, live)
    w = ws.load(config, jid)

    ws.recover(config, jid, w)

    fresh = {b["id"]: b for b in ws.load(config, jid)["builds"]}
    assert fresh[done] == cancelled_before
    assert fresh[live]["status"] == "partial"
    assert not executor.cancel_sentinel(config, "build", jid, live).exists()
    executor.clear_cancel(config, "build", jid, live)  # 幂等


# ---------------------------------------------------------------- API：构建取消、旧执行器取消、删除方案


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (value := predicate()) is not None:
            return value
        time.sleep(0.02)
    raise AssertionError("等待超时")


def test_cancel_build_endpoint_finishes_running_node_and_blocks_rest(config):
    entered, release = threading.Event(), threading.Event()

    class Blocking(FixtureSpeaker):
        def speak(self, text, voice=None):
            entered.set()
            assert release.wait(10)
            return super().speak(text, voice)

    jid, w, p = setup_workspace(config)
    client = TestClient(create_app(config, speaker=Blocking(config)))
    base = f"/api/studio/jobs/{jid}"
    response = client.post(
        f"{base}/plans/{p['id']}/regenerate-column", json=request_body(config, jid, w, p, "tts")
    )
    assert response.status_code == 202, response.text
    bid = response.json()["id"]
    try:
        assert entered.wait(5)
        cancel = client.post(f"{base}/builds/{bid}/cancel")
        assert cancel.status_code == 200, cancel.text
        assert cancel.json() == {"build_id": bid, "status": "cancelling"}
        assert executor.cancel_sentinel(config, "build", jid, bid).exists()
    finally:
        release.set()
    done = wait_build(client, jid)["builds"][-1]
    assert done["status"] == "cancelled"
    nodes = {n["key"]: n for n in done["nodes"]}
    assert nodes["tts:a"]["status"] == "complete"  # 已开始的节点照常完成
    assert nodes["tts:b"]["status"] == "blocked" and nodes["tts:b"]["error"] == "已取消"
    assert not executor.cancel_sentinel(config, "build", jid, bid).exists()
    messages = [e["message"] for e in client.get(f"{base}/builds/{bid}/logs").json()["items"]]
    assert any("已请求取消" in m for m in messages) and messages[-1] == "执行结束：已取消"

    again = client.post(f"{base}/builds/{bid}/cancel")
    assert again.status_code == 409 and "不在执行中" in again.json()["detail"]
    assert not executor.cancel_sentinel(config, "build", jid, bid).exists()
    assert client.post(f"{base}/builds/missing/cancel").status_code == 404
    assert client.post("/api/studio/jobs/missing/builds/x/cancel").status_code == 404


def test_cancel_legacy_steps_between_steps(config):
    entered, release = threading.Event(), threading.Event()

    class Slow(FixtureStoryboarder):
        def split(self, text, vocabulary=None):
            entered.set()
            assert release.wait(10)
            return super().split(text, vocabulary)

    client = TestClient(create_app(config, storyboarder=Slow(config)))
    client.config = config
    jid = create_legacy_job(client, "红色首先出现。随后蓝色的天空调转。")
    base = f"/api/studio/jobs/{jid}"
    idle = client.post(f"{base}/cancel")
    assert idle.status_code == 409 and "没有正在执行" in idle.json()["detail"]
    assert client.post(f"{base}/run").status_code == 202
    try:
        assert entered.wait(5)
        cancel = client.post(f"{base}/cancel")
        assert cancel.status_code == 200, cancel.text
        assert cancel.json() == {"job_id": jid, "status": "cancelling"}
    finally:
        release.set()

    def finished():
        job = client.get(base).json()["job"]
        return job if job["execution"]["status"] != "running" else None

    job = wait_until(finished)
    assert job["execution"] == {"status": "cancelled", "current_step": None, "remaining_steps": []}
    assert job["steps"]["split"]["status"] == "complete"  # 已开始的步骤照常完成
    assert job["steps"]["recall"]["status"] == "pending"  # 后续步骤未开始
    assert not (studio.job_dir(config, jid) / "steps.cancel").exists()
    assert client.post(f"{base}/cancel").status_code == 409
    # 取消后可以继续执行剩余步骤；残留哨兵不会误伤下次执行。
    (studio.job_dir(config, jid) / "steps.cancel").touch()
    assert client.post(f"{base}/run").status_code == 202
    assert not (studio.job_dir(config, jid) / "steps.cancel").exists()
    job = wait_until(finished)
    assert job["steps"]["recall"]["status"] in {"complete", "failed"}
    assert job["execution"]["status"] != "cancelled"


def test_delete_plan_keeps_versions_and_switches_active(config):
    jid, _w, p = setup_workspace(config)
    client = TestClient(create_app(config, storyboarder=FixtureStoryboarder(config)))
    base = f"/api/studio/jobs/{jid}"
    only = client.delete(f"{base}/plans/{p['id']}")
    assert only.status_code == 422 and only.json()["detail"] == "至少保留一个方案"

    second = client.post(f"{base}/plans", json={"name": "副本", "copy_from": p["id"]}).json()["active_plan"]
    third = client.post(f"{base}/plans", json={"name": "三号", "copy_from": p["id"]}).json()["active_plan"]
    before = ws.load(config, jid)
    assert client.delete(f"{base}/plans/missing").status_code == 422

    # 删除非当前方案：当前方案不变。
    data = client.delete(f"{base}/plans/{second}")
    assert data.status_code == 200, data.text
    assert data.json()["active_plan"] == third and set(data.json()["plans"]) == {p["id"], third}
    # 删除当前方案：切到其余方案中最新创建的一个。
    data = client.delete(f"{base}/plans/{third}").json()
    assert data["active_plan"] == p["id"] and set(data["plans"]) == {p["id"]}
    after = ws.load(config, jid)
    assert after["versions"].keys() == before["versions"].keys()
    assert after["builds"] == before["builds"]
    for vid, item in before["versions"].items():
        assert ws.valid_files(config, jid, item) == ws.valid_files(config, jid, after["versions"][vid])


def test_delete_plan_rejected_while_build_running(config):
    jid, _w, p = setup_workspace(config)
    client = TestClient(create_app(config, storyboarder=FixtureStoryboarder(config)))
    base = f"/api/studio/jobs/{jid}"
    other = client.post(f"{base}/plans", json={"name": "副本", "copy_from": p["id"]}).json()["active_plan"]
    w = ws.load(config, jid)
    add_build(config, jid, w, p, bid="live")
    before = ws.path(config, jid).read_bytes()
    response = client.delete(f"{base}/plans/{other}")
    assert response.status_code == 409 and "构建正在执行" in response.json()["detail"]
    assert ws.path(config, jid).read_bytes() == before


# ---------------------------------------------------------------- 集成：重启恢复、进程外取消、终态拒绝、
# 删除边界


def start_blocked_build(config):
    """提交一次配音整列重做，配音桩卡在第一个分镜上，直到调用方放行。"""
    entered, release = threading.Event(), threading.Event()

    class Blocking(FixtureSpeaker):
        def speak(self, text, voice=None):
            entered.set()
            assert release.wait(10)
            return super().speak(text, voice)

    jid, w, p = setup_workspace(config)
    client = TestClient(create_app(config, speaker=Blocking(config)))
    base = f"/api/studio/jobs/{jid}"
    response = client.post(
        f"{base}/plans/{p['id']}/regenerate-column", json=request_body(config, jid, w, p, "tts")
    )
    assert response.status_code == 202, response.text
    assert entered.wait(5)
    return client, jid, base, response.json()["id"], release


def test_cancelled_build_survives_restart_recovery(config):
    client, jid, base, bid, release = start_blocked_build(config)
    try:
        assert client.post(f"{base}/builds/{bid}/cancel").status_code == 200
    finally:
        release.set()
    cancelled = wait_build(client, jid)["builds"][-1]
    assert cancelled["status"] == "cancelled"
    snapshot = copy.deepcopy(next(b for b in ws.load(config, jid)["builds"] if b["id"] == bid))
    logs = client.get(f"{base}/builds/{bid}/logs").json()["items"]

    # 模拟重启前进程被杀：另有一个构建停在 running，且残留了取消请求。
    w = ws.load(config, jid)
    crashed = add_build(config, jid, w, ws.plan(w, w["active_plan"]), bid="crashed")
    executor.request_cancel(config, "build", jid, crashed)

    restarted = TestClient(create_app(config, speaker=FixtureSpeaker(config)))  # 新进程：读取即触发恢复
    data = restarted.get(f"{base}/workspace")
    assert data.status_code == 200, data.text
    builds = {b["id"]: b for b in ws.load(config, jid)["builds"]}
    assert builds[bid] == snapshot  # 已取消的构建与节点保持原样，不被改成 partial
    nodes = {n["key"]: n["status"] for n in builds[bid]["nodes"]}
    assert nodes == {"tts:a": "complete", "tts:b": "blocked"}
    assert builds[crashed]["status"] == "partial"
    assert all(n["status"] == "blocked" and n["error"] != ws.CANCELLED for n in builds[crashed]["nodes"])
    assert not executor.cancel_sentinel(config, "build", jid, crashed).exists()
    assert restarted.get(f"{base}/builds/{bid}/logs").json()["items"] == logs
    assert restarted.post(f"{base}/builds/{bid}/cancel").status_code == 409
    assert restarted.post(f"{base}/builds/{crashed}/cancel").status_code == 409


def test_sentinel_from_another_process_cancels_api_build(config):
    client, jid, base, bid, release = start_blocked_build(config)
    try:
        executor.request_cancel(config, "build", jid, bid)  # 只写哨兵：模拟 CLI / 另一个服务进程发起取消
    finally:
        release.set()
    done = wait_build(client, jid)["builds"][-1]
    assert done["status"] == "cancelled"
    nodes = {n["key"]: n for n in done["nodes"]}
    assert nodes["tts:a"]["status"] == "complete"
    assert nodes["tts:b"]["status"] == "blocked" and nodes["tts:b"]["error"] == ws.CANCELLED
    assert not executor.cancel_sentinel(config, "build", jid, bid).exists()
    messages = [e["message"] for e in client.get(f"{base}/builds/{bid}/logs").json()["items"]]
    assert not any("已请求取消" in m for m in messages)  # 未经本进程接口
    assert any("已取消执行" in m for m in messages) and messages[-1] == "执行结束：已取消"


@pytest.mark.parametrize("status", ["complete", "partial", "failed", "cancelled"])
def test_cancel_finished_build_rejected_without_side_effects(config, status):
    jid, w, p = setup_workspace(config)
    bid = add_build(config, jid, w, p, bid=f"done-{status}", status=status)
    client = TestClient(create_app(config))
    before = ws.path(config, jid).read_bytes()
    response = client.post(f"/api/studio/jobs/{jid}/builds/{bid}/cancel")
    assert response.status_code == 409 and response.json()["detail"] == "构建不在执行中，无法取消"
    assert not executor.cancel_sentinel(config, "build", jid, bid).exists()
    assert ws.path(config, jid).read_bytes() == before
    assert not any("已请求取消" in e["message"] for e in read_events(config, jid, bid)["items"])


def test_delete_plan_boundaries(config):
    from filelock import FileLock

    from studio.logs import ExecutionLog

    jid, _w, p = setup_workspace(config)
    client = TestClient(create_app(config, storyboarder=FixtureStoryboarder(config)))
    base = f"/api/studio/jobs/{jid}"
    other = client.post(f"{base}/plans", json={"name": "副本", "copy_from": p["id"]}).json()["active_plan"]
    w = ws.load(config, jid)
    bid = add_build(config, jid, w, ws.plan(w, other), bid="old", status="complete")
    ExecutionLog(config, jid, bid).emit("执行结束：全部完成")

    # 创作锁被占用（另一进程在执行或修改）：409，工作台不变。
    lock = FileLock(str(studio.lock_path(config)), timeout=0, thread_local=False)
    lock.acquire()
    try:
        before = ws.path(config, jid).read_bytes()
        busy = client.delete(f"{base}/plans/{other}")
        assert busy.status_code == 409 and "已有创作步骤在执行" in busy.json()["detail"]
        assert ws.path(config, jid).read_bytes() == before
    finally:
        lock.release()

    deleted = client.delete(f"{base}/plans/{other}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["active_plan"] == p["id"]
    again = client.delete(f"{base}/plans/{other}")
    assert again.status_code == 422 and again.json()["detail"] == "找不到构建方案"
    # 已删方案的构建记录与日志仍可读取。
    logs = client.get(f"{base}/builds/{bid}/logs")
    assert logs.status_code == 200 and logs.json()["status"] == "complete"
    assert logs.json()["items"][-1]["message"] == "执行结束：全部完成"
    assert any(b["id"] == bid and b["plan_id"] == other for b in ws.load(config, jid)["builds"])
    # 剩余的当前方案照常可读、可预检、可新建。
    workspace = client.get(f"{base}/workspace")
    assert workspace.status_code == 200 and workspace.json()["active_plan"] == p["id"]
    preflight = client.get(f"{base}/plans/{p['id']}/preflight")
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["token"]
    gone = client.get(f"{base}/plans/{other}/preflight")
    assert gone.status_code == 422 and gone.json()["detail"] == "找不到构建方案"
    created = client.post(f"{base}/plans", json={"name": "再建", "copy_from": p["id"]})
    assert created.status_code in (200, 201), created.text


def test_cancel_legacy_during_last_step_finishes_normally(config):
    entered, release = threading.Event(), threading.Event()

    class Slow(FixtureStoryboarder):
        def split(self, text, vocabulary=None):
            entered.set()
            assert release.wait(10)
            return super().split(text, vocabulary)

    client = TestClient(create_app(config, storyboarder=Slow(config)))
    client.config = config
    jid = create_legacy_job(client, "红色首先出现。随后蓝色的天空调转。")
    base = f"/api/studio/jobs/{jid}"
    assert client.post(f"{base}/steps/split").status_code == 202  # 单步入口：只有一个步骤
    try:
        assert entered.wait(5)
        assert client.post(f"{base}/cancel").status_code == 200
        assert (studio.job_dir(config, jid) / "steps.cancel").exists()
    finally:
        release.set()

    def finished():
        job = client.get(base).json()["job"]
        return job if job["execution"]["status"] != "running" else None

    job = wait_until(finished)
    # 取消只在步骤之间生效：唯一的步骤已开始，照常完成，没有剩余步骤可取消。
    assert job["execution"]["status"] == "complete"
    assert job["steps"]["split"]["status"] == "complete"
    assert job["steps"]["recall"]["status"] == "pending"
    assert not (studio.job_dir(config, jid) / "steps.cancel").exists()
    assert client.post(f"{base}/cancel").status_code == 409


def test_remove_plan_picks_latest_created(config):
    w = {
        "active_plan": "a",
        "plans": {
            "a": {"id": "a", "created_at": "2"},
            "b": {"id": "b", "created_at": "3"},
            "c": {"id": "c", "created_at": "1"},
        },
        "versions": {"v": {}},
    }
    assert ws.remove_plan(w, "a") == "b" and set(w["plans"]) == {"b", "c"}
    assert ws.remove_plan(w, "c") == "b"
    with pytest.raises(studio.StudioError, match="至少保留一个方案"):
        ws.remove_plan(w, "b")
    assert set(w["plans"]) == {"b"} and w["versions"] == {"v": {}}


# ---------------------------------------------------------------- 性质测试

ALL_KEYS = [(stage, sid) for stage in ws.STAGES for sid in ("a", "b")]
node_specs = st.lists(
    st.tuples(st.sampled_from(ALL_KEYS), st.sampled_from(["rebuild", "skip"]), st.booleans()),
    min_size=1,
    max_size=len(ALL_KEYS),
    unique_by=lambda spec: spec[0],
).map(lambda specs: sorted(specs, key=lambda s: (ws.STAGES.index(s[0][0]), s[0][1])))


def expected_outcome(w, p, specs, cancel_at):
    """按 _run_build 的语义推演：cancel_at=0 表示开始前已取消，m≥1 表示第 m 次执行节点期间请求取消。

    返回（首个未开始节点的位置 k，各节点期望状态）。取消只在节点之间检查，因此请求取消时
    正在执行的节点照常完成，k 为其后一位；请求从未发出或发生在最后一个节点期间时 k = n。
    """
    failed, statuses, calls = set(), [], 0
    k = 0 if cancel_at == 0 else len(specs)
    for i, ((stage, sid), action, fails) in enumerate(specs):
        if i >= k:
            statuses.append("cancelled")
            continue
        key = f"{stage}:{sid}"
        deps = ws.node_dependencies(w, p, stage, sid)
        if action == "skip":
            statuses.append("skipped")
        elif any(dep in failed for dep in deps):
            statuses.append("blocked")
            failed.add(key)
        else:
            calls += 1
            statuses.append("failed" if fails else "complete")
            if fails:
                failed.add(key)
            if calls == cancel_at:
                k = i + 1
    return k, statuses


def test_property_cancel_state(config, monkeypatch):
    # Feature: agent-capability-expansion, Property 10: 取消后状态
    # **Validates: Requirements 13.2**
    jid, _w, _p = setup_workspace(config)  # 工作台只建一次；每个样例使用独立的构建 ID 与取消事件

    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(specs=node_specs, cancel_at=st.integers(0, len(ALL_KEYS) + 1), via_sentinel=st.booleans())
    def check(specs, cancel_at, via_sentinel):
        w = ws.load(config, jid)
        p = ws.plan(w, w["active_plan"])
        bid = uuid.uuid4().hex
        event = threading.Event()
        fails = {f"{stage}:{sid}" for (stage, sid), _action, bad in specs if bad}
        calls = []

        def request():
            if via_sentinel:
                executor.request_cancel(config, "build", jid, bid)  # 模拟另一进程写哨兵
            else:
                event.set()

        def execute(config_, job_id, w_, p_, stage, sid, **_):
            calls.append(f"{stage}:{sid}")
            if len(calls) == cancel_at:
                request()  # 节点执行期间请求取消：该节点仍须照常完成
            if f"{stage}:{sid}" in fails:
                raise RuntimeError("桩失败")
            return f"v-{stage}-{sid}"

        monkeypatch.setattr(signatures, "match", lambda *a, **k: None)
        monkeypatch.setattr(builder, "execute", execute)
        w["builds"].append(
            {
                "id": bid,
                "plan_id": p["id"],
                "status": "running",
                "force": False,
                "auto_split": False,
                "nodes": [
                    {
                        "key": f"{stage}:{sid}",
                        "stage": stage,
                        "shot_id": sid,
                        "action": action,
                        "status": "pending",
                    }
                    for (stage, sid), action, _bad in specs
                ],
            }
        )
        ws.persist(config, jid, w)
        if cancel_at == 0:
            request()

        ws.run_build(config, jid, bid, cancel_event=event)

        k, statuses = expected_outcome(w, p, specs, cancel_at)
        build = next(b for b in ws.load(config, jid)["builds"] if b["id"] == bid)
        nodes = build["nodes"]
        assert [n["key"] for n in nodes] == [f"{stage}:{sid}" for (stage, sid), _a, _b in specs]
        executed = [n["key"] for n, s in zip(nodes, statuses) if s in ("complete", "failed")]
        assert calls == executed  # 取消时点之后没有任何节点被执行
        for node, status in zip(nodes[:k], statuses[:k]):
            assert node["status"] == status and node.get("error") != ws.CANCELLED
            assert node["finished_at"]
            if status == "complete":
                assert node["version"] == f"v-{node['stage']}-{node['shot_id']}"
        for node in nodes[k:]:
            assert node["status"] == "blocked" and node["error"] == ws.CANCELLED
            assert "started_at" not in node and "version" not in node
        if k < len(nodes):
            assert build["status"] == "cancelled"
        else:
            # 取消请求发生在最后一个节点期间（或从未发出）：没有剩余节点可取消，按正常结果收尾。
            done = [s for s in statuses if s == "complete"]
            bad = [s for s in statuses if s in ("failed", "blocked")]
            assert build["status"] == (("partial" if done else "failed") if bad else "complete")
        assert build["finished_at"] and "board_result" in build
        assert not executor.cancel_sentinel(config, "build", jid, bid).exists()

    check()


plan_sets = st.lists(st.integers(0, 5), min_size=2, max_size=6).flatmap(
    lambda stamps: st.tuples(
        st.just({f"p{i}": {"id": f"p{i}", "created_at": f"2024-01-0{s + 1}"} for i, s in enumerate(stamps)}),
        st.integers(0, len(stamps) - 1),
    )
)


# Feature: agent-capability-expansion, Property 11: 删除方案不丢版本
# **Validates: Requirements 13.5**
@settings(max_examples=100, deadline=None)
@given(
    setup=plan_sets,
    versions=st.dictionaries(
        st.text("0123456789abcdef", min_size=4, max_size=8),
        st.fixed_dictionaries({"stage": st.sampled_from(ws.STAGES), "status": st.just("ok")}),
        max_size=5,
    ),
    builds=st.lists(
        st.fixed_dictionaries(
            {"id": st.uuids().map(str), "status": st.sampled_from(["complete", "cancelled"])}
        ),
        max_size=3,
    ),
    data=st.data(),
)
def test_property_remove_plan_keeps_versions(setup, versions, builds, data):
    plans, active_index = setup
    w = {"active_plan": f"p{active_index}", "plans": plans, "versions": versions, "builds": builds}
    versions_before, builds_before = copy.deepcopy(versions), copy.deepcopy(builds)
    for _ in range(data.draw(st.integers(1, len(plans) + 1))):
        target = data.draw(st.sampled_from([*sorted(w["plans"]), "missing"]))
        before = copy.deepcopy(w["plans"])
        was_active = w["active_plan"]
        if target == "missing" or len(w["plans"]) == 1:
            message = "找不到构建方案" if target == "missing" else "至少保留一个方案"
            with pytest.raises(studio.StudioError, match=message):
                ws.remove_plan(w, target)
            assert w["plans"] == before and w["active_plan"] == was_active  # 拒绝时不做任何修改
        else:
            active = ws.remove_plan(w, target)
            assert target not in w["plans"] and set(w["plans"]) == set(before) - {target}
            assert active == w["active_plan"]
            if target == was_active:
                latest = max(p["created_at"] for p in w["plans"].values())
                assert w["plans"][active]["created_at"] == latest
            else:
                assert active == was_active
        assert w["active_plan"] in w["plans"]
        assert w["versions"] == versions_before and w["builds"] == builds_before
