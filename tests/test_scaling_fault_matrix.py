"""统一单机故障矩阵（plans §3）：两 API、两 worker、单网关真实进程。

覆盖第六批剩余验收：网关重启重放与 SSE 事件身份、附件/MCP 断流与认证、
worker kill -9、SIGSTOP 超租约、旧进程恢复、取消竞争、发布边界崩溃，以及
双 API 真实 PG/S3 目录修订 CAS。仍不代表双机、容量或真实模型验收。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import yaml
from fastapi import FastAPI, UploadFile

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_s3, pytest.mark.requires_chroma_server]

# 演练用短租约：SIGSTOP/kill 后约 8-12 秒被回收，不必等完整 120 秒 TTL。
DRILL_TTL = 8
DRILL_RENEW = 2


def free_port():
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def wait_until(predicate, timeout, interval=0.1, message="条件未在期限内满足"):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"{message}（{timeout}s）")
        time.sleep(interval)


class SseReader:
    """后台读取 SSE 流，记录带 id 的事件；断线自动结束并保留原因。"""

    def __init__(self, url, headers):
        self.url, self.headers = url, headers
        self.events: list[dict] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._response = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        try:
            with httpx.Client(timeout=httpx.Timeout(10, read=None), trust_env=False) as client:
                with client.stream("GET", self.url, headers=self.headers) as response:
                    self._response = response
                    response.raise_for_status()
                    event = {}
                    for line in response.iter_lines():
                        if self._stop.is_set():
                            break
                        if line.startswith("id: "):
                            event["id"] = int(line[4:])
                        elif line.startswith("data: "):
                            event["data"] = json.loads(line[6:])
                            if "id" in event:  # ready/ping 心跳没有事件身份，不参与断言
                                self.events.append(event)
                            event = {}
        except Exception as exc:  # noqa: BLE001 — 意外断线记录原因供测试断言
            if not self._stop.is_set():  # stop() 主动关流产生的中断不算错误
                self.errors.append(f"{type(exc).__name__}: {exc}")

    def stop(self, timeout=5):
        self._stop.set()
        if self._response is not None:
            self._response.close()  # 关闭流让阻塞中的读取线程立即结束
        if self._thread is not None:
            self._thread.join(timeout)
        return self.errors

    def seen(self, marker):
        return [
            event
            for event in self.events
            if (event.get("data") or {}).get("data", {}).get("marker") == marker
        ]


class DrillEmbedder:
    """进程内工程嵌入服务：确定性归一化向量，space 身份按同一 Config 计算从而一致。

    只服务发布演练的真实 build/activate；不代表任何模型质量结论。
    """

    def __init__(self):

        from shared.config import Config, fingerprint

        self.config = Config(embedding={"base_url": "http://127.0.0.1:1", "dimensions": 8})
        self.identity = {
            "model": self.config.embedding.model,
            "revision": "fault-matrix-drill",
            "quantization": self.config.embedding.quantization,
            "dimensions": 8,
            "normalized": True,
            "fps": self.config.video.fps,
            "max_frames": self.config.video.max_frames,
            "max_pixels": self.config.video.max_pixels,
            "implementation": "fault-matrix-drill",
        }
        self.space_id = fingerprint(self.identity)
        app = FastAPI()
        app.get("/health")(lambda: {"ready": True, "space": self.identity, "engineering_only": True})

        def vector(seed: bytes) -> list[float]:
            digest = hashlib.sha256(seed).digest()
            values = [1.0 + (digest[index % len(digest)] / 255.0) for index in range(8)]
            norm = sum(value * value for value in values) ** 0.5
            return [value / norm for value in values]

        @app.post("/embed/text")
        def text(body: dict):
            return {"space_id": self.space_id, "vectors": [vector(value.encode()) for value in body["texts"]]}

        @app.post("/embed/video")
        async def video(file: UploadFile):
            return {"space_id": self.space_id, "vectors": [vector(await file.read())]}

        @app.post("/embed/image")
        async def image(file: UploadFile):
            return {"space_id": self.space_id, "vectors": [vector(await file.read())]}

        import uvicorn

        self.port = free_port()
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True, name="drill-embedder")
        self.url = f"http://127.0.0.1:{self.port}"

    def start(self):
        self._thread.start()
        with httpx.Client(timeout=5, trust_env=False) as client:
            wait_until(
                lambda: client.get(self.url + "/health").status_code == 200, 30, 0.2, "工程嵌入服务未就绪"
            )
        return self

    def stop(self):
        self._server.should_exit = True
        self._thread.join(timeout=10)

    def vector_of(self, seed: bytes) -> list[float]:
        digest = hashlib.sha256(seed).digest()
        values = [1.0 + (digest[index % len(digest)] / 255.0) for index in range(8)]
        norm = sum(value * value for value in values) ** 0.5
        return [value / norm for value in values]


class FaultCluster:
    """两 API + 单网关 + 两个 worker 目录；只清理本轮 bucket、schema 与进程。"""

    def __init__(self, root: Path, video: Path):
        import boto3
        import psycopg

        from shared.db.migrate_pg import migrate_pg

        self.root, self.video, self.logs = root, video, []
        self.processes = []
        self.dsn = os.environ["MINDEX_TEST_POSTGRES_DSN"]
        self.endpoint = os.environ["MINDEX_TEST_S3_ENDPOINT"]
        self.chroma = os.environ["MINDEX_TEST_CHROMA_SERVER"]
        self.access = os.environ.get("MINDEX_TEST_S3_KEY", "mindex")
        self.secret = os.environ.get("MINDEX_TEST_S3_SECRET_KEY", "mindex-test")
        self.web_token = uuid4().hex
        self.mcp_token = uuid4().hex
        self.s3 = boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=self.access,
            aws_secret_access_key=self.secret,
            region_name="us-east-1",
        )
        self.bucket = f"mindex-fault-{uuid4().hex[:12]}"
        self.s3.create_bucket(Bucket=self.bucket)
        with psycopg.connect(self.dsn, autocommit=True) as db:
            db.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        with psycopg.connect(self.dsn) as db:
            migrate_pg(db)
        self.env = {
            key: value for key, value in os.environ.items() if not key.startswith(("MINDEX_", "MINIO_"))
        }
        self.env.update(
            MINDEX_DATABASE_DSN=self.dsn,
            MINDEX_S3_ACCESS_KEY=self.access,
            MINDEX_S3_SECRET_KEY=self.secret,
            MINDEX_WEB_TOKEN=self.web_token,
            MINDEX_MCP_TOKEN=self.mcp_token,
            OMP_NUM_THREADS="1",
            OPENBLAS_NUM_THREADS="1",
        )
        self.embedder = DrillEmbedder().start()
        self.gateway_port = free_port()
        self.configs: list[Path] = []
        for index in range(5):
            node = root / f"node-{index}"
            config = root / f"node-{index}.yaml"
            config.write_text(
                yaml.safe_dump(
                    {
                        "data_dir": str(node),
                        "runtime_overrides": False,
                        "gpu": {"cpu_queues": ["source", "index"]},
                        "agent": {
                            "gateway_url": f"http://127.0.0.1:{self.gateway_port}",
                            "mcp": {"http_enabled": True},
                        },
                        "server": {"form": "cluster", "role": "agent-gateway" if index == 4 else "api"},
                        "database": {"backend": "postgres"},
                        "queue": {"backend": "postgres"},
                        "locks": {"backend": "postgres"},
                        "vector": {"backend": "chroma_server", "chroma_server": {"url": self.chroma}},
                        "storage": {
                            "backend": "s3",
                            "s3": {
                                "endpoint_url": self.endpoint,
                                "bucket": self.bucket,
                                "prefix": "library/",
                                "min_free_gb": 0,
                                "staging_quota_gb": 1,
                            },
                        },
                        "topics": {"scheduler": {"enabled": False}},
                        "local_services": {"enabled": False},
                        "embedding": {"base_url": self.embedder.url, "dimensions": 8},
                        # 后台标题生成指向本机拒绝端口：立即失败保留占位标题，不触达局域网。
                        "studio": {"generator": {"base_url": "http://127.0.0.1:9"}},
                    }
                ),
                encoding="utf-8",
            )
            self.configs.append(config)
        self.urls: list[str] = []
        self.gateway_process = None

    # ------------------------------------------------------------ 进程与 HTTP
    def launch(self, config, role, *args):
        log = (self.root / f"{config.stem}-{role}-{uuid4().hex[:6]}.log").open("w+")
        self.logs.append(log)
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import mindex; from cli import app; app()",
                "--config",
                str(config),
                role,
                *args,
            ],
            env=self.env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self.processes.append(process)
        return process, log

    def launch_worker(self, index, queues="source", once=False):
        args = ["--queues", queues, "--task-ttl", str(DRILL_TTL), "--renew-interval", str(DRILL_RENEW)]
        if once:
            args.append("--once")
        return self.launch(self.configs[index], "worker", *args)

    def client(self, index=0, token=None):
        return httpx.Client(
            base_url=self.urls[index],
            timeout=30,
            trust_env=False,
            headers={"Authorization": f"Bearer {token or self.web_token}"},
        )

    def wait_ready(self, process, url, timeout=45):
        with httpx.Client(
            timeout=2, trust_env=False, headers={"Authorization": f"Bearer {self.web_token}"}
        ) as probe:
            wait_until(lambda: _alive(probe, url), timeout, 0.2, f"{url} 未就绪")
        assert process.poll() is None, "服务进程提前退出"

    def start_apis(self, stack: ExitStack):
        for index in range(2):
            port = free_port()
            process, _ = self.launch(self.configs[index], "serve", "--port", str(port))
            stack.callback(stop, process)
            self.urls.append(f"http://127.0.0.1:{port}")
            self.wait_ready(process, self.urls[-1])
        self.restart_gateway(stack)

    def kill_gateway(self):
        self.gateway_process.send_signal(signal.SIGKILL)
        self.gateway_process.wait(timeout=10)

    def restart_gateway(self, stack: ExitStack):
        process, _ = self.launch(self.configs[4], "serve", "--port", str(self.gateway_port))
        self.gateway_process = process
        stack.callback(stop, process)
        self.wait_ready(process, f"http://127.0.0.1:{self.gateway_port}")

    # ------------------------------------------------------------ 业务动作
    def publish_change(self, index, marker, kind="mutation"):
        with self.client(index) as client:
            response = client.post("/api/agent/changes", json={"kind": kind, "data": {"marker": marker}})
            assert response.status_code == 201, response.text
            return response.json()["outbox_id"]

    def upload_source(self, index, name):
        payload = self.video.read_bytes() + uuid4().hex.encode()
        with self.client(index) as client:
            uploaded = client.post("/api/admin/sources/upload", files={"file": (name, payload, "video/mp4")})
            assert uploaded.status_code == 201, uploaded.text
            return uploaded.json()["source_id"]

    def enqueue_shots(self, index, source_id):
        with self.client(index) as client:
            task = client.post(f"/api/admin/sources/{source_id}/shots", json={"publish": False})
            assert task.status_code == 202, task.text
            return task.json()["id"]

    def queue_row(self, ledger_table, task_id):
        """按账本引用查队列行（API 返回的是账本 UUID，队列自身是 bigint 自增 id）。"""
        import psycopg

        with psycopg.connect(self.dsn) as db:
            return db.execute(
                "SELECT status,owner,attempt_id,attempts,fencing_token,cancel_requested"
                " FROM task_queue WHERE ledger_table=%s AND ledger_id=%s",
                (ledger_table, task_id),
            ).fetchone()

    def ledger_row(self, task_id):
        import psycopg

        with psycopg.connect(self.dsn) as db:
            return db.execute("SELECT status,error FROM source_tasks WHERE id=%s", (task_id,)).fetchone()

    def source_shots(self, source_id):
        import psycopg

        with psycopg.connect(self.dsn) as db:
            return db.execute(
                "SELECT s.id, f.sha256 FROM segments s JOIN shared_files f ON f.path=s.path"
                " WHERE s.source_id=%s AND s.kind='shot' ORDER BY s.start",
                (source_id,),
            ).fetchall()

    def index_task_status(self, task_id):
        import psycopg

        with psycopg.connect(self.dsn) as db:
            row = db.execute("SELECT status FROM index_tasks WHERE id=%s", (task_id,)).fetchone()
        return row[0] if row else None

    def source_task_status(self, task_id):
        import psycopg

        with psycopg.connect(self.dsn) as db:
            row = db.execute("SELECT status FROM source_tasks WHERE id=%s", (task_id,)).fetchone()
        return row[0] if row else None

    def active_index(self):
        import psycopg

        with psycopg.connect(self.dsn) as db:
            row = db.execute("SELECT value FROM settings WHERE key='active'").fetchone()
        return row[0] if row else None

    def dump_logs(self):

        evidence = Path(os.environ.get("MINDEX_TEST_EVIDENCE_DIR") or "")
        for log in self.logs:
            if not log.closed:
                log.flush()
                log.seek(0)
                if evidence.is_dir():
                    shutil.copyfileobj(log, (evidence / f"fault-{Path(log.name).name}").open("w"))
                log.seek(0)
                print(log.read()[-4000:])


def _marker_of(item):
    data = item["data"]
    if isinstance(data, str):  # SQLite 行内仍是 JSON 文本
        data = json.loads(data)
    return data.get("marker")


def _alive(client, url):
    try:
        return client.get(url + "/api/live").status_code == 200
    except httpx.TransportError:
        return False


@pytest.fixture(scope="module")
def cluster(tmp_path_factory, video_file):
    if not all(
        os.environ.get(name)
        for name in ("MINDEX_TEST_POSTGRES_DSN", "MINDEX_TEST_S3_ENDPOINT", "MINDEX_TEST_CHROMA_SERVER")
    ):
        pytest.skip("需要隔离的 PostgreSQL、MinIO 和 Chroma 服务")
    cluster = FaultCluster(tmp_path_factory.mktemp("fault-matrix"), video_file)
    try:
        with ExitStack() as stack:
            cluster.stack = stack  # 测试内重启的网关也登记到模块级清理
            cluster.start_apis(stack)
            yield cluster
    except BaseException:
        cluster.dump_logs()
        raise
    finally:
        for process in reversed(cluster.processes):
            stop(process)
        cluster.dump_logs()
        cluster.embedder.stop()
        for page in cluster.s3.get_paginator("list_objects_v2").paginate(Bucket=cluster.bucket):
            if page.get("Contents"):
                cluster.s3.delete_objects(
                    Bucket=cluster.bucket,
                    Delete={"Objects": [{"Key": item["Key"]} for item in page["Contents"]]},
                )
        cluster.s3.delete_bucket(Bucket=cluster.bucket)


# ================================================================ 网关故障
def test_gateway_sse_events_last_event_id_and_auth(cluster):
    """SSE 经业务 API 转发到网关：事件带稳定 id，Last-Event-ID 续传不重放；令牌双向校验。"""
    headers = {"Authorization": f"Bearer {cluster.web_token}"}
    reader = SseReader(cluster.urls[0] + "/api/agent/changes?follow=true", headers).start()
    try:
        cluster.publish_change(0, "sse-first")
        cluster.publish_change(1, "sse-second")
        wait_until(
            lambda: len(reader.seen("sse-first")) + len(reader.seen("sse-second")) >= 2,
            30,
            0.2,
            "网关未在期限内转入两条改动",
        )
        ids = [event["id"] for event in reader.events]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)
        # 未带令牌的请求被业务 API 与网关同时拒绝；错误令牌同样拒绝。
        with httpx.Client(base_url=cluster.urls[0], timeout=10, trust_env=False) as anonymous:
            assert anonymous.get("/api/agent/sessions").status_code == 401
        with httpx.Client(
            base_url=f"http://127.0.0.1:{cluster.gateway_port}", timeout=10, trust_env=False
        ) as direct:
            assert direct.get("/api/agent/sessions").status_code == 401
            assert (
                direct.get("/api/agent/sessions", headers={"Authorization": "Bearer wrong"}).status_code
                == 401
            )
            assert direct.get("/api/agent/sessions", headers=headers).status_code == 200
    finally:
        assert not reader.stop()
    # 断线后按最后事件 id 续传：只收到此后的事件，不回放历史。
    last = reader.events[-1]["id"]
    cluster.publish_change(0, "sse-after-reconnect")

    def replay_markers():
        with httpx.Client(
            base_url=cluster.urls[1],
            timeout=10,
            trust_env=False,
            headers={**headers, "Last-Event-ID": str(last), "Accept": "text/event-stream"},
        ) as client:
            replay = client.get("/api/agent/changes?follow=false")
            assert replay.status_code == 200, replay.text
            return [
                json.loads(line[6:]).get("data", {}).get("marker")
                for line in replay.text.splitlines()
                if line.startswith("data: ") and '"marker"' in line
            ]

    # 网关桥按固定周期把 outbox 转入本地事件流：等待送达后断言，只收到此事件、不回放历史。
    markers = wait_until(lambda: replay_markers() or None, 30, 0.5, "续传未见新事件")
    assert markers == ["sse-after-reconnect"], markers


def test_gateway_restart_replays_outbox_without_loss_or_duplicate(cluster):
    """网关在事件落盘前后与确认前后被 SIGKILL：重启按持久回执重放，可见事件身份不变。"""
    headers = {"Authorization": f"Bearer {cluster.web_token}"}
    reader = SseReader(cluster.urls[0] + "/api/agent/changes?follow=true", headers).start()
    try:
        cluster.publish_change(0, "crash-before")
        wait_until(lambda: bool(reader.seen("crash-before")), 30, 0.2, "网关未转入首条改动")
        identity_before = reader.seen("crash-before")[0]["id"]
        # 崩溃可能发生在本地落盘后、PG 确认前：回执保证重放不重复。
        cluster.kill_gateway()
        # 网关停机：助手明确不可用（503），业务 API 存活；期间改动仍写入共享 outbox。
        with cluster.client() as client:
            status = client.get("/api/agent/status")
            assert status.status_code == 503 and "网关" in status.json()["detail"]
            assert client.get("/api/live").status_code == 200
        cluster.publish_change(1, "during-1")
        cluster.publish_change(0, "during-2")
    finally:
        reader.stop()
    # 重启的网关登记到模块级清理：后续附件/MCP 用例继续使用它。
    cluster.restart_gateway(cluster.stack)
    with cluster.client() as client:
        wait_until(
            lambda: client.get("/api/agent/status").status_code == 200, 30, 0.2, "网关重启后助手未恢复"
        )
        log = client.get("/api/agent/changes/log?after=0").json()["items"]
        seqs = [item["seq"] for item in log]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        by_marker = {_marker_of(item): item["seq"] for item in log}
        assert by_marker["crash-before"] == identity_before, "重启后已见事件的可见身份变化"
    # 客户端按崩溃前的最后事件 id 续传：无论网关桥何时补齐停机期间的事件，都不丢不重。
    resumed = SseReader(
        cluster.urls[0] + f"/api/agent/changes?after={identity_before}&follow=true", headers
    ).start()
    try:
        wait_until(
            lambda: len(resumed.seen("during-1")) + len(resumed.seen("during-2")) >= 2,
            30,
            0.2,
            "重启后未重放停机期间的改动",
        )
        cluster.publish_change(1, "after-restart")
        wait_until(lambda: bool(resumed.seen("after-restart")), 30, 0.2, "重启后新改动未送达")
        markers = [event["data"]["data"]["marker"] for event in resumed.events]
        assert markers.count("during-1") == 1 and markers.count("during-2") == 1
        assert markers.count("after-restart") == 1 and "crash-before" not in markers
        ids = [event["id"] for event in resumed.events]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)
    finally:
        assert not resumed.stop()
    import psycopg

    with psycopg.connect(cluster.dsn) as db:
        pending = db.execute("SELECT count(*) FROM agent_outbox WHERE consumed_at IS NULL").fetchone()[0]
    assert pending == 0, "重放后仍存在未确认的 outbox 条目"


def test_attachment_interrupt_and_mcp_stream_break_through_gateway(cluster):
    """附件与 MCP 流经网关：中途断开后资源可复用、内容一致，MCP 断流后可立即重建。"""
    payload = os.urandom(512 * 1024)
    mcp_headers = {
        "Authorization": f"Bearer {cluster.mcp_token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    with cluster.client(1, token=cluster.mcp_token) as client:
        uploaded = client.post(
            "/api/agent/attachments", files={"file": ("fault.bin", payload, "application/octet-stream")}
        )
        assert uploaded.status_code == 201, uploaded.text
        meta = uploaded.json()
        assert meta["sha256"] == hashlib.sha256(payload).hexdigest(), "转发上传的内容已损坏"
        token = meta["id"].removeprefix("attachment://")  # 端点路径取裸 token
        assert (
            hashlib.sha256(client.get(f"/api/agent/attachments/{token}/content").content).hexdigest()
            == hashlib.sha256(payload).hexdigest()
        )
    # 下载读到首个分块即断开：网关与附件随后仍可完整读取并删除。
    with httpx.Client(
        base_url=cluster.urls[0],
        timeout=10,
        trust_env=False,
        headers={"Authorization": f"Bearer {cluster.mcp_token}"},
    ) as client:
        with client.stream("GET", f"/api/agent/attachments/{token}/content") as stream:
            for _ in stream.iter_bytes():
                break  # 故意只读第一块，随后关闭连接
    with cluster.client(token=cluster.mcp_token) as client:
        body = client.get(f"/api/agent/attachments/{token}/content").content
        assert hashlib.sha256(body).hexdigest() == hashlib.sha256(payload).hexdigest()
        assert client.delete(f"/api/agent/attachments/{token}").status_code == 204
    # MCP：初始化与工具列举经网关正常；通知流断开后立刻可用；未带令牌拒绝。
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "fault-matrix", "version": "0"},
        },
    }
    with cluster.client(0) as client:
        response = client.post("/mcp", json=initialize, headers=mcp_headers)
        assert response.status_code == 200, response.text
        listing = client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=mcp_headers
        )
        assert listing.status_code == 200
        tools = next(json.loads(line[5:]) for line in listing.text.splitlines() if line.startswith("data:"))
        assert tools["result"]["tools"]
        assert client.post("/mcp", json=initialize).status_code == 401
    stream_headers = {**mcp_headers, "Accept": "text/event-stream"}
    with httpx.Client(
        base_url=cluster.urls[1], timeout=httpx.Timeout(10, read=None), trust_env=False
    ) as client:
        opened = client.send(client.build_request("GET", "/mcp", headers=stream_headers), stream=True)
        if opened.status_code == 200:
            opened.close()  # 通知流立即断开
    with cluster.client(1) as client:
        assert client.post("/mcp", json={**initialize, "id": 3}, headers=mcp_headers).status_code == 200


# ================================================================ worker 故障
def test_worker_sigkill_reclaims_and_restarts_consistently(cluster):
    """执行中 kill -9：租约到期回收重派，账本/执行凭证/产物摘要一致。"""
    source_id = cluster.upload_source(0, "fault-kill.mp4")
    task_id = cluster.enqueue_shots(0, source_id)
    first, _ = cluster.launch_worker(2)
    try:
        wait_until(
            lambda: (row := cluster.queue_row("source_tasks", task_id)) and row[0] == "running",
            30,
            0.05,
            "任务未被领取",
        )
        victim_owner = cluster.queue_row("source_tasks", task_id)[1]
        first.send_signal(signal.SIGKILL)
        first.wait(timeout=10)
        second, _ = cluster.launch_worker(3)
        # 重复领取互斥：同一时刻只有一个 owner；回收后由另一 worker 完成。
        wait_until(
            lambda: cluster.queue_row("source_tasks", task_id)[0] == "succeeded",
            DRILL_TTL + 120,
            0.2,
            "任务未在崩溃后完成",
        )
        row = cluster.queue_row("source_tasks", task_id)
        assert row[3] == 2 and row[4] == 2, row  # attempts 与 fencing_token 均推进
        assert row[1] != victim_owner and row[2], row
        assert cluster.ledger_row(task_id)[0] == "complete"
        shots = cluster.source_shots(source_id)
        assert shots, "崩溃重派后未产出镜头"
        with cluster.client(1) as client:  # 另一 API 读取产物并核对摘要
            for shot_id, digest in shots:
                clip = client.get(f"/api/admin/segments/{shot_id}/media")
                assert clip.status_code == 200, clip.text[:400]
                assert hashlib.sha256(clip.content).hexdigest() == digest
        stop(second)
    finally:
        stop(first)


def test_worker_sigstop_past_ttl_old_process_cannot_commit(cluster):
    """SIGSTOP 超租约：任务被回收重派完成；旧进程恢复后不得再提交或改变产物。"""
    source_id = cluster.upload_source(1, "fault-stop.mp4")
    task_id = cluster.enqueue_shots(1, source_id)
    stopped, _ = cluster.launch_worker(2)
    try:
        wait_until(
            lambda: (row := cluster.queue_row("source_tasks", task_id)) and row[0] == "running",
            30,
            0.05,
            "任务未被领取",
        )
        stopped_owner = cluster.queue_row("source_tasks", task_id)[1]
        stopped.send_signal(signal.SIGSTOP)
        takeover, _ = cluster.launch_worker(3)
        wait_until(
            lambda: cluster.queue_row("source_tasks", task_id)[0] == "succeeded",
            DRILL_TTL + 300,
            0.2,
            f"任务未被回收重派完成；现场 {cluster.queue_row('source_tasks', task_id)}，"
            f"账本 {cluster.ledger_row(task_id)}",
        )
        row = cluster.queue_row("source_tasks", task_id)
        assert row[3] == 2 and row[1] != stopped_owner, row
        shots = cluster.source_shots(source_id)
        assert shots
        # 旧进程恢复：其租约已失效，提交被 fencing 拒绝，账本与产物不回退、不重复。
        stopped.send_signal(signal.SIGCONT)
        time.sleep(6)
        assert cluster.queue_row("source_tasks", task_id)[0] == "succeeded"
        assert cluster.ledger_row(task_id)[0] == "complete"
        assert cluster.source_shots(source_id) == shots, "旧进程恢复后改变了产物"
        stop(takeover)
    finally:
        stopped.send_signal(signal.SIGCONT)
        stop(stopped)


def test_cancel_race_during_running_marks_cancelled_everywhere(cluster):
    """执行中取消竞争：队列与账本一致转为 cancelled，worker 不崩溃。"""
    source_id = cluster.upload_source(0, "fault-cancel.mp4")
    task_id = cluster.enqueue_shots(0, source_id)
    worker, _ = cluster.launch_worker(2)
    try:
        wait_until(
            lambda: (row := cluster.queue_row("source_tasks", task_id)) and row[0] == "running",
            30,
            0.05,
            "任务未被领取",
        )
        with cluster.client(1) as client:
            cancelled = client.post(f"/api/admin/source-tasks/{task_id}/cancel")
            assert cancelled.status_code == 200, cancelled.text
        wait_until(
            lambda: cluster.queue_row("source_tasks", task_id)[0] == "cancelled", 90, 0.2, "取消未传播到终态"
        )
        assert cluster.ledger_row(task_id)[0] == "cancelled"
        assert worker.poll() is None, "取消导致 worker 进程退出"
    finally:
        stop(worker)


def test_publish_boundary_crash_keeps_pointer_atomic(cluster):
    """真实构建后在发布事务中途崩溃：活动索引指针要么未发布要么完整，重派后一致激活。"""
    source_id = cluster.upload_source(0, "fault-publish.mp4")
    with cluster.client(0) as client:
        shots = client.post(f"/api/admin/sources/{source_id}/shots", json={"publish": False})
        assert shots.status_code == 202, shots.text
        shots_task = shots.json()["id"]
        build = client.post(
            "/api/index-tasks", json={"action": "build", "source_id": source_id, "channels": ["visual"]}
        )
        assert build.status_code == 202, build.text
        build_id = build.json()["id"]
    worker, _ = cluster.launch_worker(2, queues="source,index")
    try:
        wait_until(
            lambda: cluster.source_task_status(shots_task) == "complete", 180, 0.3, "前置镜头任务未完成"
        )
        wait_until(lambda: cluster.index_task_status(build_id) == "complete", 240, 0.3, "真实索引构建未完成")
        with cluster.client(0) as client:
            built = client.get(f"/api/index-tasks/{build_id}").json()
            index_id = built["result"]["index_id"]
            activate = client.post("/api/index-tasks", json={"action": "activate", "index_id": index_id})
            assert activate.status_code == 202, activate.text
            activate_id = activate.json()["id"]
        wait_until(lambda: cluster.index_task_status(activate_id) == "running", 30, 0.02, "激活任务未被领取")
        # 冻结在发布路径内，再 kill -9：崩溃点可能位于指针提交前或提交后、终态事务前。
        worker.send_signal(signal.SIGSTOP)
        time.sleep(0.5)
        worker.send_signal(signal.SIGKILL)
        worker.wait(timeout=10)
        assert cluster.active_index() in (None, index_id), "崩溃留下半发布状态"
        retry, _ = cluster.launch_worker(3, queues="source,index")
        wait_until(
            lambda: cluster.index_task_status(activate_id) == "complete",
            DRILL_TTL + 90,
            0.2,
            "激活任务未在崩溃后完成",
        )
        assert cluster.active_index() == index_id
        with cluster.client(1) as client:
            versions = client.get("/api/indexes").json()
            assert any(v["id"] == index_id and v["status"] == "ready" for v in versions["versions"])
        stop(retry)
    finally:
        stop(worker)


# ================================================================ 双 API CAS
def test_dual_api_real_directory_revision_cas(cluster):
    """双 API 共享真实 PG/S3：并发同修订保存恰有一个成功；缺/旧修订分别 428/409。"""
    with cluster.client(0) as client:
        created = client.post("/api/studio/jobs", json={"text": "故障矩阵双 API 修订竞争。" * 4})
        assert created.status_code == 201, created.text
        job_id = created.json()["id"]

        def settled_workspace():
            response = client.get(f"/api/studio/jobs/{job_id}/workspace")
            assert response.status_code == 200, response.text
            body = response.json()
            if body.get("job", {}).get("title_pending"):
                return None
            body["__directory_revision__"] = response.headers.get("x-studio-revision")
            return body

        # 后台标题生成会发布新目录修订：等它收敛后再取修订并发起竞争。
        settled = wait_until(settled_workspace, 30, 0.3, "后台标题生成未收敛")
        plan_id = settled["active_plan"]
        plan_revision = settled["revision"]  # 活动方案的输入修订
        # If-Match 必须是目录修订（X-Studio-Revision），与工作区内容指纹是两个值。
        revision = settled["__directory_revision__"]
        assert revision and plan_revision
    patch = {"revision": plan_revision, "name": "并发修订"}
    # 缺 If-Match：428 前置条件明确。
    with cluster.client(1) as client:
        missing = client.patch(f"/api/studio/jobs/{job_id}/plans/{plan_id}", json=patch)
        assert missing.status_code == 428, missing.text
    # 同一修订经两个 API 并发提交：恰有一个 200，另一个 409（真实 PG/S3 CAS）。
    outcomes: list[int] = []
    barrier = threading.Barrier(2)

    def save(index):
        with cluster.client(index) as client:
            barrier.wait(timeout=5)
            response = client.patch(
                f"/api/studio/jobs/{job_id}/plans/{plan_id}", json=patch, headers={"If-Match": revision}
            )
            outcomes.append(response.status_code)

    threads = [threading.Thread(target=save, args=(index,)) for index in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(outcomes) == [200, 409], outcomes
    # 冲突方刷新修订后可以保存；新修订立即对两个 API 可见。
    with cluster.client(1) as client:
        refreshed = client.get(f"/api/studio/jobs/{job_id}/workspace")
        latest = refreshed.headers.get("x-studio-revision")
        assert latest and latest != revision
        retry = client.patch(
            f"/api/studio/jobs/{job_id}/plans/{plan_id}",
            json={"revision": refreshed.json()["revision"], "name": "冲突后重试"},
            headers={"If-Match": latest},
        )
        assert retry.status_code == 200, retry.text
    with cluster.client(0) as client:
        final = client.get(f"/api/studio/jobs/{job_id}/workspace").json()
        assert final["plans"][plan_id]["name"] in {"并发修订", "冲突后重试"}
