"""第三方工具测试替身：进程内 HTTP manifest 服务（提交 / 轮询 / 下载），按工具名切换行为。

- make_video：异步任务，第一次轮询 running，之后 succeeded，产物 ``/files/clip.mp4``（2 秒 160×120）；
- make_image：同步完成，产物 ``/files/still.png``；
- slow：永远 running（超时）；fail：failed + error；big：产物超过大小上限；elsewhere：产物在白名单外的主机；
- echo：返回收到的上传素材数（产物仍为图片）。
记录每次提交的请求头与请求体（``server.requests``）。
"""

from __future__ import annotations

import io
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def make_clip_bytes(directory: Path, seconds: float = 2.0) -> bytes:
    target = directory / "stub-clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=25:duration={seconds}",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(target)], check=True)
    return target.read_bytes()


def make_png_bytes(color=(200, 40, 40)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (160, 120), color).save(buffer, "PNG")
    return buffer.getvalue()


class StubServer:
    def __init__(self, directory: Path):
        self.requests: list[dict] = []
        self.polls: dict[str, int] = {}
        self.files = {"/files/clip.mp4": (make_clip_bytes(directory), "video/mp4"),
                      "/files/still.png": (make_png_bytes(), "image/png"),
                      "/files/big.bin": (b"x" * (2 * 1024 * 1024), "application/octet-stream")}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, payload, kind="application/json"):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/tools":
                    return self.reply(200, {"tools": [{"name": name, "description": f"stub {name}"}
                                                      for name in ("make_video", "make_image", "echo")]})
                if self.path in stub.files:
                    content, kind = stub.files[self.path]
                    return self.reply(200, content, kind)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "http://not-allowed.invalid/clip.mp4")
                    self.end_headers()
                    return None
                if self.path.startswith("/jobs/"):
                    job = self.path.rsplit("/", 1)[1]
                    count = stub.polls[job] = stub.polls.get(job, 0) + 1
                    tool = job.split("-", 1)[0]
                    if tool == "slow":
                        return self.reply(200, {"id": job, "status": "running"})
                    if tool == "fail":
                        return self.reply(200, {"id": job, "status": "failed", "error": "boom"})
                    if count < 2:
                        return self.reply(200, {"id": job, "status": "running"})
                    return self.reply(200, {"id": job, "status": "succeeded", "output": {"url": "/files/clip.mp4"}})
                return self.reply(200, {"ok": True}) if self.path == "/" else self.reply(404, {"error": "missing"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                stub.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                tool = body.get("tool")
                if tool in ("make_video", "slow", "fail"):
                    return self.reply(200, {"id": f"{tool}-{len(stub.requests)}", "status": "queued"})
                url = {"big": "/files/big.bin", "elsewhere": "http://not-allowed.invalid/x.png",
                       "redirect": "/redirect"}.get(tool, "/files/still.png")
                return self.reply(200, {"id": "sync", "status": "succeeded", "output": {"url": url},
                                        "uploads": len(body.get("uploads") or [])})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
