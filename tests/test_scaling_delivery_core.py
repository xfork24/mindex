"""目录发布、外部回执与断点上传的离线及真实共享后端验收。"""

from __future__ import annotations

import hashlib
import io
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from shared.backends.media import LocalMediaStore
from shared.directory_files import Snapshot, bind
from shared.external_calls import ReviewRequired, guard_service, scope
from shared.files import FileRevisionConflict
from shared.storage import Store
from shared.upload_sessions import UploadError, UploadSessions


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


@pytest.fixture
def store(config):
    return Store.open(config)


def test_manifest_atomic_cas_and_history(store, tmp_path):
    store.media = LocalMediaStore(tmp_path / "objects")
    first, stale = Snapshot(store, "studio/job/"), Snapshot(store, "studio/job/")
    first.put("studio/job/job.json", b"job")
    first.put("studio/job/workspace.json", b"workspace")
    assert store.one("SELECT revision FROM directory_heads") is None
    first.publish()
    previous = first.revision
    stale.put("studio/job/job.json", b"obsolete")
    with pytest.raises(FileRevisionConflict):
        stale.publish()
    assert Snapshot(store, "studio/job/").entries["studio/job/job.json"]["sha256"] == sha(b"job")
    first.put("studio/job/workspace.json", b"new workspace")
    first.publish()
    document = json.loads(
        store.one("SELECT document FROM directory_manifests WHERE revision=?", (first.revision,))["document"]
    )
    assert document["parent"] == previous
    assert store.one("SELECT count(*) AS n FROM directory_manifests")["n"] == 2


def test_manifest_transaction_rollback_and_bound_read(store, tmp_path):
    store.media = LocalMediaStore(tmp_path / "objects")
    with bind(store, "studio/job/", deferred=True) as view:
        view.put("studio/job/workspace.json", b"new")
        view.publish()
        with pytest.raises(RuntimeError), store.connect() as db:
            view.commit(db)
            raise RuntimeError("提交前崩溃")
    assert not Snapshot(store, "studio/job/").entries
    assert not store.rows("SELECT * FROM shared_files")


def test_external_receipt_replay_and_unknown_requires_review(store):
    service = SimpleNamespace(submit=lambda: "request-123")
    proxy = guard_service(service, "video")
    with scope(store, "node", task_id="task"):
        assert proxy.submit() == "request-123"
    service.submit = lambda: pytest.fail("已知请求不得重复提交")
    with scope(store, "node", task_id="task"):
        assert proxy.submit() == "request-123"
    assert store.one("SELECT request_id FROM external_calls")["request_id"] == "request-123"
    dispatched = []

    def fail():
        dispatched.append(True)
        raise TimeoutError("响应丢失")

    proxy = guard_service(SimpleNamespace(submit=fail), "video")
    with scope(store, "unknown", task_id="first"), pytest.raises(ReviewRequired):
        proxy.submit()
    with scope(store, "unknown", task_id="retry"), pytest.raises(ReviewRequired):
        proxy.submit()
    assert len(dispatched) == 1


def test_external_binary_receipt_is_immutable_and_replayed(store):
    proxy = guard_service(SimpleNamespace(generate=lambda: (b"media", {"seed": 7})), "music")
    with scope(store, "node", task_id="t"):
        assert proxy.generate() == (b"media", {"seed": 7})
    with scope(store, "node", task_id="t"):
        assert proxy.generate() == (b"media", {"seed": 7})
    receipt = store.one("SELECT receipt FROM external_calls")["receipt"]
    assert "external/" in receipt and "media" not in receipt


@pytest.mark.parametrize("backend", ["local", pytest.param("postgres", marks=pytest.mark.requires_postgres)])
@pytest.mark.parametrize("old_failed", [False, True])
def test_external_late_response_cannot_update_retry(config, request, backend, old_failed):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from shared.external_calls import Journal, resolve

    store = Store.open(request.getfixturevalue("pg_config") if backend == "postgres" else config)
    old_entered, old_release = threading.Event(), threading.Event()
    new_entered, new_release = threading.Event(), threading.Event()

    def old():
        old_entered.set()
        assert old_release.wait(10)
        if old_failed:
            raise TimeoutError("旧连接迟到的错误响应")
        return "old receipt"

    def new():
        new_entered.set()
        assert new_release.wait(10)
        return "new receipt"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(Journal(store, "node", "task").call, "video.submit", old, (), {})
        try:
            assert old_entered.wait(5)
            call_id = store.one("SELECT id FROM external_calls")["id"]
            resolve(
                store,
                call_id,
                expected_state="dispatching",
                outcome="failed",
                evidence="提供商已确认第一次失败，旧连接尚未关闭",
            )
            second = pool.submit(Journal(store, "node", "task").call, "video.submit", new, (), {})
            assert new_entered.wait(5)
            old_release.set()
            if old_failed:
                with pytest.raises(ReviewRequired):
                    first.result(5)
            else:
                assert first.result(5) == "old receipt"
            assert store.one("SELECT state FROM external_calls")["state"] == "dispatching"
        finally:
            old_release.set()
            new_release.set()
        assert second.result(5) == "new receipt"
    assert (
        Journal(store, "node", "task").call(
            "video.submit", lambda: pytest.fail("新回执应重放，不得再次提交"), (), {}
        )
        == "new receipt"
    )


def test_upload_parts_resume_ownership_digest_quota_and_expiry(config, store):
    config.uploads.part_bytes = 3
    config.uploads.pending_bytes = 8
    service = UploadSessions(store, config)
    data = b"abcdefg"
    session = service.create("clip.mp4", len(data), sha(data))
    sid, token = session["id"], session["upload_token"]
    assert not store.rows("SELECT * FROM uploaded_assets")
    with pytest.raises(UploadError):
        service.create("other", 2, sha(b"xx"))
    with pytest.raises(UploadError):
        service.get(sid, "other-user")
    with pytest.raises(UploadError):
        service.complete(sid, token)
    for number, chunk in enumerate((b"abc", b"def", b"g"), 1):
        service.part(sid, token, number, io.BytesIO(chunk), len(chunk), sha(chunk))
    service = UploadSessions(Store.open(config), config)  # 模拟换 API/重启，只有共享账本可用
    result = service.complete(sid, token)
    assert service.complete(sid, token) == result
    with store.media.open_read(result["object_key"]) as source:
        assert source.read() == data
    assert len(store.rows("SELECT * FROM uploaded_assets")) == 1
    with pytest.raises(UploadError):
        service.abort(sid, token)
    expiring = service.create("old", 1, sha(b"x"))
    service.part(expiring["id"], expiring["upload_token"], 1, io.BytesIO(b"x"), 1, sha(b"x"))
    store.execute("UPDATE upload_sessions SET expires_at=? WHERE id=?", (time.time() - 1, expiring["id"]))
    assert service.reap() == 1
    assert service.get(expiring["id"], expiring["upload_token"])["state"] == "expired"
    assert not list(store.media.iter_prefix("pending/uploads/" + expiring["id"] + "/"))


def test_upload_wrong_digest_cannot_register(config, store):
    config.uploads.part_bytes = 3
    service = UploadSessions(store, config)
    session = service.create("file", 3, sha(b"bad"))
    sid, token = session["id"], session["upload_token"]
    service.part(sid, token, 1, io.BytesIO(b"abc"), 3, sha(b"abc"))
    with pytest.raises(ValueError):
        service.complete(sid, token)
    assert not store.rows("SELECT * FROM uploaded_assets")


def test_upload_rest_content_range_and_idempotent_complete(config):
    from api import create_app

    config.uploads.part_bytes = 3
    with TestClient(create_app(config)) as client:
        made = client.post("/api/uploads", json={"filename": "x.bin", "size": 6, "sha256": sha(b"abcdef")})
        assert made.status_code == 201, made.text
        row = made.json()
        url, headers = "/api/uploads/" + row["id"], {"X-Upload-Token": row["upload_token"]}
        assert client.put(url + "/parts/1", headers=headers, content=b"abc").status_code == 200
        assert client.put(url + "/parts/2", headers=headers, content=b"def").status_code == 200
        assert client.get(url).status_code == 404
        assert client.post(url + "/complete", headers=headers).status_code == 200
        assert client.post(url + "/complete", headers=headers).status_code == 200
        assert client.get(url + "/content", headers={"Range": "bytes=2-4"}).content == b"cde"
        assert client.head(url + "/content").headers["content-length"] == "6"


# 后端 fixture 只使用 verify-scaling 创建的一次性服务。
from test_scaling_correctness import pg_config as pg_config  # noqa: E402
from test_scaling_correctness import s3_config as s3_config  # noqa: E402


@pytest.mark.requires_postgres
@pytest.mark.parametrize("outcome", ["success", "cancel", "expired"])
def test_studio_persistent_split_two_nodes_and_fencing(s3_config, tmp_path, monkeypatch, outcome):
    import threading

    from worker_runtime import Worker

    from api import create_app
    from conftest import FixtureStoryboarder
    from shared.db.queue_pg import PgTaskQueue
    from studio import studio, workspace
    from studio.workspace.handlers import split

    cfg = s3_config
    state = studio.create_job(cfg, "红色开场。蓝色结尾。", title="持久执行")
    jid = state["id"]
    w = workspace.load(cfg, jid)
    p = workspace.plan(w, w["active_plan"])
    workspace.add_script(cfg, jid, w, p, "红色开场。蓝色结尾。", "manual")
    workspace.persist(cfg, jid, w)
    entered, release = threading.Event(), threading.Event()
    requests = []

    class Model(FixtureStoryboarder):
        def split(self, *args, **kwargs):
            requests.append(True)
            entered.set()
            assert release.wait(10)
            return super().split(*args, **kwargs)

    monkeypatch.setattr(split, "make_storyboarder", lambda _: guard_service(Model(cfg), "storyboarder"))
    with TestClient(create_app(cfg)) as client:
        spec = workspace.specification(cfg, jid, w, p, "split")
        response = client.post(
            f"/api/studio/jobs/{jid}/plans/{p['id']}/nodes",
            json={
                "stage": "split",
                "revision": workspace.revision(p),
                "inputs": spec["inputs"],
                "force": True,
            },
        )
        assert response.status_code == 202, response.text
        task_id, bid = response.json()["task_id"], response.json()["id"]
    store = Store.open(cfg)
    initial = Snapshot(store, f"studio/{jid}/").revision
    worker_cfg = cfg.model_copy(deep=True, update={"data_dir": tmp_path / "other-worker"})
    worker = Worker(
        worker_cfg,
        queues=["studio"],
        once=True,
        task_ttl=3 if outcome == "expired" else 20,
        renew_interval=10,
    )
    errors = []

    def work():
        try:
            worker.run()
        except Exception as exc:  # noqa: BLE001 — 执行线程异常交回主测试线程
            errors.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    assert entered.wait(10)
    if outcome == "cancel":
        from studio.tasks import cancel

        cancel(cfg, "build", jid, bid)
    if outcome == "expired":
        time.sleep(3.3)
    release.set()
    thread.join(15)
    assert not thread.is_alive() and not errors, errors
    ledger = store.one("SELECT * FROM studio_tasks WHERE id=?", (task_id,))
    if outcome == "success":
        assert ledger["status"] == "complete", ledger
        remote = workspace.load(worker_cfg, jid)
        selected = workspace.plan(remote, p["id"])
        assert selected["board"] and selected["shots"]
        assert Snapshot(store, f"studio/{jid}/").revision != initial
    elif outcome == "cancel":
        assert ledger["status"] == "cancelled", ledger
        remote = workspace.load(worker_cfg, jid)
        assert next(b for b in remote["builds"] if b["id"] == bid)["status"] == "cancelled"
        assert not workspace.plan(remote, p["id"])["board"]
    else:
        assert ledger["status"] != "complete"
        assert Snapshot(store, f"studio/{jid}/").revision == initial
        with store.connect() as db:
            PgTaskQueue(store.library_id).reclaim_expired(db)
            db.execute("UPDATE task_queue SET run_after=clock_timestamp()")
        # 已知外部回执必须重放；重派后不再请求模型。
        Worker(worker_cfg, queues=["studio"], once=True).run()
        assert store.one("SELECT status FROM studio_tasks WHERE id=?", (task_id,))["status"] == "complete"
        assert len(requests) == 1


@pytest.mark.requires_postgres
def test_upload_and_manifest_real_s3_resume_on_another_node(s3_config, tmp_path):
    cfg = s3_config
    cfg.uploads.part_bytes = 3
    first = UploadSessions(Store.open(cfg), cfg)
    made = first.create("file.mp4", 6, sha(b"abcdef"))
    sid, token = made["id"], made["upload_token"]
    first.part(sid, token, 1, io.BytesIO(b"abc"), 3, sha(b"abc"))
    other = cfg.model_copy(deep=True, update={"data_dir": tmp_path / "second-api"})
    second = UploadSessions(Store.open(other), other)
    assert [p["number"] for p in second.get(sid, token)["parts"]] == [1]
    second.part(sid, token, 2, io.BytesIO(b"def"), 3, sha(b"def"))
    result = second.complete(sid, token)
    assert first.complete(sid, token) == result
    with first.store.media.open_read(result["object_key"]) as source:
        assert source.read() == b"abcdef"


def test_external_typed_receipt_replay_and_manual_resolution(store):
    from shared.external_calls import resolve
    from shared.types import ShotDraft

    value = ShotDraft(text="原始文案", query="风景", estimated_seconds=2)
    service = guard_service(SimpleNamespace(split=lambda: [value]), "storyboarder")
    with scope(store, "typed", task_id="t"):
        assert service.split() == [value]
    with scope(store, "typed", task_id="t"):
        assert service.split() == [value]

    def fail():
        raise TimeoutError("返回前断流")

    service = guard_service(SimpleNamespace(submit=fail), "video")
    with scope(store, "unknown", task_id="t"), pytest.raises(ReviewRequired):
        service.submit()
    unknown = store.one("SELECT * FROM external_calls WHERE state='needs_review'")
    resolve(
        store,
        unknown["id"],
        expected_state="needs_review",
        outcome="request_id",
        evidence="已在提供商控制台核对任务编号",
        request_id="known-provider-task",
    )
    with scope(store, "unknown", task_id="t"):
        assert service.submit() == "known-provider-task"


@pytest.mark.requires_postgres
def test_studio_all_manual_writes_require_revision_and_cancel_pending(s3_config):
    from api import create_app
    from studio import studio, workspace

    cfg = s3_config
    jid = studio.create_job(cfg, "测试共享工作区", title="初始标题")["id"]
    w = workspace.load(cfg, jid)
    workspace.persist(cfg, jid, w)
    with TestClient(create_app(cfg)) as client:
        url = f"/api/studio/jobs/{jid}"
        original = client.get(url)
        headers = {"If-Match": original.headers["X-Studio-Revision"]}
        assert client.patch(url + "/title", json={"title": "改名"}).status_code == 428
        changed = client.patch(url + "/title", json={"title": "改名"}, headers=headers)
        assert changed.status_code == 200, changed.text
        assert changed.headers["X-Studio-Revision"] != headers["If-Match"]
        assert client.patch(url + "/title", json={"title": "旧改名"}, headers=headers).status_code == 409
        headers = {"If-Match": changed.headers["X-Studio-Revision"]}
        assert client.post(url + "/plans", json={"name": "新方案"}, headers=headers).status_code == 201
    # 取消尚未被 worker 领取的构建，账本/队列/可见 manifest 一起进入取消态。
    from studio.tasks import cancel, publication, submit

    w = workspace.load(cfg, jid)
    w["builds"].append(
        {"id": "pending", "plan_id": w["active_plan"], "status": "running", "force": False, "nodes": []}
    )
    with publication(cfg, jid):
        workspace.persist(cfg, jid, w)
        tid = submit(cfg, jid, "build", {}, build_id="pending")
    cancel(cfg, "build", jid, "pending")
    store = Store.open(cfg)
    assert store.one("SELECT status FROM studio_tasks WHERE id=?", (tid,))["status"] == "cancelled"
    assert (
        next(b for b in workspace.load(cfg, jid)["builds"] if b["id"] == "pending")["status"] == "cancelled"
    )


def test_studio_credentials_are_references_and_restored_from_frozen_profiles(config, store):
    from settings.runtime_settings import RuntimeConfig
    from shared.task_config import freeze_config, restore_config

    store.execute(
        "INSERT INTO model_profiles(role,name,provider,config,api_key) "
        "VALUES('generator','old','custom','{}','test-old-credential')"
    )
    pid = store.one("SELECT id FROM model_profiles")["id"]
    store.execute("INSERT INTO settings(key,value) VALUES('active.profile.generator',?)", (str(pid),))
    config.studio.generator.api_key = config.studio.writer.api_key = "test-old-credential"
    source_config = freeze_config(config, store)
    assert "generator" not in json.loads(source_config)["_credential_profiles"]
    frozen = freeze_config(config, store, studio=True)
    assert "test-old-credential" not in frozen
    restored = restore_config(frozen, RuntimeConfig(config, store), store)
    assert restored.studio.generator.api_key == restored.studio.writer.api_key == "test-old-credential"
    store.execute("DELETE FROM model_profiles WHERE id=?", (pid,))
    with pytest.raises(ValueError, match="凭据"):
        restore_config(frozen, RuntimeConfig(config, store), store)
    # 不使用 Studio 的 source/index 任务不绑定无关凭据，删除它不妨碍恢复。
    restore_config(source_config, RuntimeConfig(config, store), store)


def test_upload_cli_uses_the_same_session_state(config, store, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from cli import app

    config_file = tmp_path / "config.yaml"
    config_file.write_text(f"data_dir: {config.data_dir}\n", encoding="utf-8")
    runner = CliRunner()
    made = runner.invoke(app, ["--config", str(config_file), "uploads", "create", "x.bin", "3", sha(b"abc")])
    assert made.exit_code == 0, made.output
    session = json.loads(made.stdout)
    monkeypatch.setenv("MINDEX_UPLOAD_TOKEN", session["upload_token"])
    chunk = tmp_path / "chunk"
    chunk.write_bytes(b"abc")
    sent = runner.invoke(
        app, ["--config", str(config_file), "uploads", "part", session["id"], "1", str(chunk)]
    )
    assert sent.exit_code == 0, sent.output
    completed = runner.invoke(app, ["--config", str(config_file), "uploads", "complete", session["id"]])
    assert completed.exit_code == 0, completed.output
    assert store.one("SELECT sha256 FROM uploaded_assets")["sha256"] == sha(b"abc")


@pytest.mark.requires_postgres
def test_bundle_restores_studio_history_external_receipts_and_uploads(s3_config, tmp_path):
    from maintenance.migration import export_bundle, load_bundle, verify_bundle
    from shared.config import Config
    from studio import studio, workspace

    cfg = s3_config
    store = Store.open(cfg)
    jid = studio.create_job(cfg, "用于灾备的文案", title="历史闭包")["id"]
    w = workspace.load(cfg, jid)
    p = workspace.plan(w, w["active_plan"])
    for text in ("第一个文案版本", "第二个文案版本"):
        workspace.add_script(cfg, jid, w, p, text, "manual")
        workspace.persist(cfg, jid, w)
    old_heads = store.one("SELECT count(*) AS n FROM directory_manifests")["n"]
    generator = guard_service(SimpleNamespace(generate=lambda: b"provider-result"), "image")
    with scope(store, "backup", task_id="backup-call"):
        generator.generate()
    made = UploadSessions(store, cfg).create("test.bin", 6, sha(b"abcdef"))
    UploadSessions(store, cfg).part(
        made["id"], made["upload_token"], 1, io.BytesIO(b"abcdef"), 6, sha(b"abcdef")
    )
    asset = UploadSessions(store, cfg).complete(made["id"], made["upload_token"])
    bundle = tmp_path / "bundle"
    export_bundle(cfg, bundle, with_media=True)
    local = Config(data_dir=tmp_path / "restored")
    assert load_bundle(local, bundle)["consistent"]
    assert verify_bundle(local, bundle)["consistent"]
    recovered = workspace.load(local, jid)
    assert len(recovered["versions"]) == 2
    assert all(workspace.valid_files(local, jid, item) for item in recovered["versions"].values())
    restored = Store.open(local)
    assert restored.one("SELECT count(*) AS n FROM directory_manifests")["n"] == old_heads
    with restored.media.open_read(asset["object_key"]) as content:
        assert content.read() == b"abcdef"
    with scope(restored, "backup", task_id="backup-call"):
        assert (
            generator.__class__(
                SimpleNamespace(generate=lambda: pytest.fail("恢复后不能再次调用")), "image"
            ).generate()
            == b"provider-result"
        )


@pytest.mark.requires_postgres
def test_legacy_studio_steps_are_durable_and_readable_without_cache(s3_config, tmp_path, monkeypatch):
    from worker_runtime import Worker

    from api import create_app
    from conftest import FixtureStoryboarder
    from studio import steps as studio_steps
    from studio import studio

    cfg = s3_config
    state = studio.create_job(cfg, "保持完整的旧文案。", title="旧步骤持久化")
    state["input_text"] = state.pop("outline")
    jid = state["id"]
    studio.save(studio.job_path(cfg, jid), state, config=cfg)
    monkeypatch.setattr(
        studio_steps, "make_storyboarder", lambda _: guard_service(FixtureStoryboarder(cfg), "storyboarder")
    )
    with TestClient(create_app(cfg)) as client:
        response = client.post(f"/api/studio/jobs/{jid}/steps/split", json={})
        assert response.status_code == 202, response.text
        tid = response.json()["task_id"]
    worker = cfg.model_copy(deep=True, update={"data_dir": tmp_path / "legacy-worker"})
    Worker(worker, queues=["studio"], once=True).run()
    store = Store.open(cfg)
    assert store.one("SELECT status FROM studio_tasks WHERE id=?", (tid,))["status"] == "complete"
    reader = cfg.model_copy(deep=True, update={"data_dir": tmp_path / "empty-api"})
    assert studio.load_storyboard(reader, jid).shots
    with TestClient(create_app(reader)) as client:
        detail = client.get(f"/api/studio/jobs/{jid}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["job"]["steps"]["split"]["status"] == "complete"


@pytest.mark.requires_postgres
def test_manifest_dependency_closure_ignores_obsolete_local_job(s3_config, tmp_path):
    from studio import studio, workspace
    from studio.workspace import references

    cfg = s3_config
    jid = studio.create_job(cfg, "测试依赖闭包", title="已发布的新标题")["id"]
    w = workspace.load(cfg, jid)
    p = workspace.plan(w, w["active_plan"])
    references.register(cfg, jid, p, "reference.txt", "只上传显式登记的依赖")
    workspace.persist(cfg, jid, w)
    other = cfg.model_copy(deep=True, update={"data_dir": tmp_path / "other"})
    fresh = workspace.load(other, jid)
    stale_job = studio.load_job(other, jid)
    stale_job["title"] = "节点旧缓存标题"
    studio.save(studio.job_path(other, jid), stale_job)
    workspace.persist(other, jid, fresh)
    assert studio.load_job(cfg, jid)["title"] == "已发布的新标题"
    assert references.load_texts(other, jid, workspace.plan(fresh, p["id"])) == ["只上传显式登记的依赖"]


@pytest.mark.requires_postgres
def test_unknown_studio_call_stops_and_operator_resolution_resumes_same_task(s3_config, monkeypatch):
    from worker_runtime import Worker

    from api import create_app
    from conftest import FixtureStoryboarder
    from studio import studio, workspace
    from studio.workspace.handlers import split

    cfg = s3_config
    jid = studio.create_job(cfg, "外部结果需要核对。", title="核对恢复")["id"]
    w = workspace.load(cfg, jid)
    p = workspace.plan(w, w["active_plan"])
    workspace.add_script(cfg, jid, w, p, "外部结果需要核对。", "manual")
    workspace.persist(cfg, jid, w)
    submitted = []

    class Model(FixtureStoryboarder):
        def split(self, *args, **kwargs):
            submitted.append(True)
            if len(submitted) == 1:
                raise TimeoutError("模拟提供商响应丢失")
            return super().split(*args, **kwargs)

    monkeypatch.setattr(split, "make_storyboarder", lambda _: guard_service(Model(cfg), "storyboarder"))
    with TestClient(create_app(cfg)) as client:
        root = f"/api/studio/jobs/{jid}"
        spec = workspace.specification(cfg, jid, w, p, "split")
        response = client.post(
            f"{root}/plans/{p['id']}/nodes",
            json={
                "stage": "split",
                "revision": workspace.revision(p),
                "inputs": spec["inputs"],
                "force": True,
            },
        )
        assert response.status_code == 202, response.text
        tid = response.json()["task_id"]
        Worker(cfg, queues=["studio"], once=True).run()
        assert client.get(f"{root}/tasks/{tid}").json()["status"] == "needs_review"
        assert (
            Store.open(cfg).one("SELECT status FROM task_queue WHERE ledger_id=?", (tid,))["status"]
            == "failed"
        )
        assert len(submitted) == 1
        current = client.get(root + "/workspace")
        assert current.json()["builds"][-1]["task_id"] == tid
        headers = {"If-Match": current.headers["X-Studio-Revision"]}
        assert client.post(f"{root}/tasks/{tid}/retry", headers=headers).status_code == 422
        call = client.get(root + "/external-calls").json()["items"][0]
        resolved = client.post(
            f"{root}/external-calls/{call['id']}/resolve",
            json={
                "expected_state": "needs_review",
                "outcome": "not_submitted",
                "evidence": "模拟提供商明确确认没有创建任务",
            },
        )
        assert resolved.status_code == 200, resolved.text
        retry = client.post(f"{root}/tasks/{tid}/retry", headers=headers)
        assert retry.status_code == 202, retry.text
        Worker(cfg, queues=["studio"], once=True).run()
        assert client.get(f"{root}/tasks/{tid}").json()["status"] == "complete"
        assert len(submitted) == 2
        resumed = workspace.load(cfg, jid)["builds"][-1]
        assert not resumed["nodes"][0].get("error")
        # 第三节点没有任何 Studio 文件，也能取得清单登记的产物。
        items = client.get(root + "/artifacts").json()["items"]
        version = next(item for item in items if item["path"].endswith("version.json"))
        assert client.get(root + "/artifacts/" + version["path"]).status_code == 200
        assert client.get(root + "/archive").content[:2] == b"PK"


def test_upload_staging_shortage_is_507_and_does_not_register_parts(config, tmp_path):
    from api import create_app
    from shared.backends.staging import StagingFull

    config.uploads.part_bytes = 3
    app = create_app(config)

    def full(*args, **kwargs):
        raise StagingFull("隔离测试：无可用暂存预算")

    app.state.store.media.staging = SimpleNamespace(root=tmp_path, reserve=full)
    with TestClient(app) as client:
        made = client.post("/api/uploads", json={"filename": "x", "size": 3, "sha256": sha(b"abc")}).json()
        response = client.put(
            f"/api/uploads/{made['id']}/parts/1",
            content=b"abc",
            headers={"X-Upload-Token": made["upload_token"]},
        )
        assert response.status_code == 507, response.text
        assert not app.state.store.rows("SELECT * FROM upload_parts")
        assert not app.state.store.rows("SELECT * FROM uploaded_assets")


def test_reassigned_attempt_ignores_old_node_cancel_and_honors_new_cancel(tmp_path):
    import threading

    from shared.db.fencing import Attempt, executing
    from shared.task_cancel import TaskCancelled, registry_for

    registry = registry_for(tmp_path)
    registry.request("task")  # 模拟上次进程被杀后留下的哨兵。
    assert registry.requested("task")
    attempt = Attempt(
        None, "library", SimpleNamespace(ledger_id="task"), threading.Event(), threading.Event()
    )
    with executing(attempt):
        registry.check("task")
        assert not registry.requested("task")
        registry.request("task")
        with pytest.raises(TaskCancelled):
            registry.check("task")
    attempt.close()
