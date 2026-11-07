"""真实 PG/MinIO：浏览器 endpoint、原生 multipart 与完成边界恢复。"""

from __future__ import annotations

import hashlib
import io
import time

import httpx
import pytest

from shared.backends import open_context
from shared.upload_sessions import UploadError, UploadSessions
from test_scaling_correctness import pg_config as pg_config
from test_scaling_correctness import s3_config as s3_config

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_s3]


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def browser_endpoint(s3_config):
    import http.client
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit

    upstream = urlsplit(s3_config.storage.s3.endpoint_url)
    requests = []
    drop_response = threading.Event()

    class Proxy(BaseHTTPRequestHandler):
        def forward(self):
            requests.append((self.command, self.headers.get("Origin"), self.headers.get("Host")))
            content = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            connection = http.client.HTTPConnection(upstream.hostname, upstream.port, timeout=20)
            try:
                connection.request(
                    self.command,
                    self.path,
                    body=content or None,
                    headers={k: v for k, v in self.headers.items() if k.lower() != "connection"},
                )
                response = connection.getresponse()
                if self.command == "PUT" and drop_response.is_set():
                    drop_response.clear()
                    response.read()
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in {"connection", "transfer-encoding", "server", "date"}:
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(response.read())
            finally:
                connection.close()

        do_PUT = do_GET = do_HEAD = do_OPTIONS = forward

        def log_message(self, *_):
            pass

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    s3_config.storage.s3.browser_endpoint_url = f"http://127.0.0.1:{proxy.server_port}"
    try:
        yield s3_config.storage.s3.browser_endpoint_url, requests, drop_response
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(5)


def send(service, session, number, data):
    signed = service.sign_part(session["id"], session["upload_token"], number)
    response = httpx.put(signed["url"], content=data, timeout=20)
    assert response.status_code == 200, response.text
    return response.headers["etag"]


def test_native_resume_recovers_unregistered_parts_and_idempotent_complete(
    s3_config, browser_endpoint, tmp_path
):
    endpoint, requests, _ = browser_endpoint
    payload = b"a" * (5 * 1024**2) + b"last"
    s3_config.uploads.part_bytes = 5 * 1024**2
    with open_context(s3_config) as first:
        service = UploadSessions(first.store, s3_config)
        session = service.create("video.mp4", len(payload), sha(payload), "s3")
        sid, token = session["id"], session["upload_token"]
        signed = service.sign_part(sid, token, 1)
        assert signed["url"].startswith(endpoint)
        # 签名包含 Content-Length，拒绝超出本会话分片预算的上传。
        assert "content-length" in signed["url"].lower()
        with pytest.raises(UploadError):
            service.sign_part(sid, "wrong-owner", 1)
        small = service.sign_part(sid, token, 2)
        bad_signature = small["url"][:-1] + ("a" if small["url"][-1] != "a" else "b")
        assert httpx.put(bad_signature, content=b"last", timeout=20).status_code == 403
        assert httpx.put(small["url"], content=b"wrong-size", timeout=20).status_code == 403
        etag = send(service, session, 1, payload[: session["part_size"]])
        with pytest.raises(UploadError):
            service.register_part(sid, token, 1, '"wrong-etag"')
        assert not first.store.rows("SELECT * FROM uploaded_assets")
    other = s3_config.model_copy(deep=True, update={"data_dir": tmp_path / "other-node"})
    with open_context(other) as second:
        service = UploadSessions(second.store, other)
        recovered = service.get(sid, token)
        assert recovered["parts"][0]["etag"] == etag
        send(service, session, 2, payload[session["part_size"] :])
        asset = service.complete(sid, token)
        assert service.complete(sid, token) == asset
        with second.media.open_read(asset["object_key"]) as source:
            assert source.read() == payload
        second.store.execute("UPDATE upload_sessions SET expires_at=0 WHERE id=?", (sid,))
        service.reap()
        assert not list(second.media.iter_prefix(f"pending/uploads/{sid}/"))
        assert second.store.one("SELECT count(*) AS n FROM uploaded_assets")["n"] == 1
    assert any(method == "PUT" for method, _, _ in requests)


@pytest.mark.parametrize("boundary", ["native_complete", "asset_write"])
def test_complete_recovers_external_success_before_persistent_publish(
    s3_config, browser_endpoint, monkeypatch, boundary
):
    with open_context(s3_config) as context:
        service = UploadSessions(context.store, s3_config)
        session = service.create("test.bin", 3, sha(b"abc"), "s3")
        send(service, session, 1, b"abc")
        if boundary == "native_complete":
            original = context.media.client.complete_multipart_upload

            def interrupted(**kwargs):
                original(**kwargs)
                raise ConnectionError("模拟 S3 完成后进程中断")

            monkeypatch.setattr(context.media.client, "complete_multipart_upload", interrupted)
        else:
            original = context.media.put_immutable

            def interrupted(*args, **kwargs):
                original(*args, **kwargs)
                raise ConnectionError("模拟资产对象落盘后进程中断")

            monkeypatch.setattr(context.media, "put_immutable", interrupted)
        with pytest.raises(ConnectionError):
            service.complete(session["id"], session["upload_token"])
        assert not context.store.rows("SELECT * FROM uploaded_assets")
        assert service.get(session["id"], session["upload_token"])["state"] == "completing"
        if boundary == "native_complete":
            monkeypatch.setattr(context.media.client, "complete_multipart_upload", original)
        else:
            monkeypatch.setattr(context.media, "put_immutable", original)
        # 新上下文/新客户端仅依赖持久状态恢复，无需客户端重新上传。
        with open_context(s3_config) as resumed:
            result = UploadSessions(resumed.store, s3_config).complete(session["id"], session["upload_token"])
            assert result["sha256"] == sha(b"abc")


def test_native_digest_failure_abort_and_unlogged_upload_recovery(s3_config, browser_endpoint):
    from shared.s3_multipart import S3Multipart

    with open_context(s3_config) as context:
        service = UploadSessions(context.store, s3_config)
        session = service.create("test.bin", 3, sha(b"xxx"), "s3")
        send(service, session, 1, b"abc")
        with pytest.raises(UploadError, match="摘要"):
            service.complete(session["id"], session["upload_token"])
        assert not context.store.rows("SELECT * FROM uploaded_assets")
        service.abort(session["id"], session["upload_token"])
        assert not list(context.media.iter_prefix(f"pending/uploads/{session['id']}/"))
        session = service.create("new.bin", 3, sha(b"abc"), "s3")
        service.sign_part(session["id"], session["upload_token"], 1)
        original = context.store.one("SELECT multipart_id FROM upload_sessions WHERE id=?", (session["id"],))[
            "multipart_id"
        ]
        context.store.execute("UPDATE upload_sessions SET multipart_id=NULL WHERE id=?", (session["id"],))
        service.sign_part(session["id"], session["upload_token"], 1)
        row = context.store.one("SELECT * FROM upload_sessions WHERE id=?", (session["id"],))
        assert row["multipart_id"] == original
        assert len(S3Multipart(context.store).uploads(row)) == 1
        service.abort(session["id"], session["upload_token"])
        assert not S3Multipart(context.store).uploads(row)


def test_signed_url_expires_and_media_conditions(s3_config, browser_endpoint):
    from fastapi.testclient import TestClient

    from api import create_app

    with TestClient(create_app(s3_config)) as client:
        store = client.app.state.store
        service = UploadSessions(store, s3_config)
        session = service.create("clip.mp4", 3, sha(b"abc"))
        service.part(session["id"], session["upload_token"], 1, io.BytesIO(b"abc"), 3, sha(b"abc"))
        asset = service.complete(session["id"], session["upload_token"])
        # 可用性断言用宽限期签名（满载时签名到请求可能超过 1 秒）；1 秒签名只用于过期断言。
        assert httpx.get(store.media.presign_get(asset["object_key"], expires=30)).status_code == 200
        url = store.media.presign_get(asset["object_key"], expires=1)
        time.sleep(2.1)
        assert httpx.get(url).status_code == 403
        head = client.head(asset["url"])
        assert head.status_code == 200 and head.headers["content-type"] == "video/mp4"
        etag = head.headers["etag"]
        assert client.get(asset["url"], headers={"If-None-Match": etag}).status_code == 304
        assert client.get(asset["url"], headers={"If-Match": '"wrong"'}).status_code == 412
        assert client.get(asset["url"], headers={"Range": "bytes=1-2", "If-Range": etag}).content == b"bc"
        assert (
            client.get(asset["url"], headers={"Range": "bytes=1-2", "If-Range": '"wrong"'}).content == b"abc"
        )


def test_real_browser_uploads_through_independent_endpoint_and_cors(s3_config, browser_endpoint, tmp_path):
    import json
    import os
    import subprocess
    from pathlib import Path

    from api import create_app
    from shared.media import command
    from shared.s3_multipart import probe_browser
    from test_agent_panel_ui import ROOT, serve

    video_file = tmp_path / "long-seek.mp4"
    command(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "75",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "28",
            "-c:a",
            "aac",
            str(video_file),
        ]
    )
    app = create_app(s3_config)
    server, thread, url = serve(app)
    evidence = Path(os.environ.get("MINDEX_TEST_EVIDENCE_DIR", str(tmp_path)))
    screenshot = evidence / "s3-browser-upload.png"
    try:
        assert "通过" in probe_browser(app.state.backends.media, [url])
        browser_endpoint[2].set()  # PUT 已被 MinIO 接收后断开响应，真实浏览器必须续传。
        result = subprocess.run(
            ["node", str(ROOT / "tests/s3_multipart_browser.cjs"), url, str(video_file), str(screenshot)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert (
            report["cors_upload"]
            and report["source_import"]
            and report["page_errors"] == []
            and report["seek_seconds"] > 50
            and report["resumed"]
        )
        assert screenshot.is_file()
        methods = [method for method, origin, _ in browser_endpoint[1] if origin == url]
        assert "OPTIONS" in methods and methods.count("PUT") == 2  # 探针一次、上传一次；续传不重传。
        assert app.state.store.one("SELECT count(*) AS n FROM uploaded_assets")["n"] == 1
    finally:
        server.should_exit = True
        thread.join(15)
        assert not thread.is_alive()
