"""第三方工具（spec m3-post-production 需求 18、19）：工具源登记与密钥只写、白名单与上传授权、HTTP 适配器的
提交 / 轮询 / 下载、超时、超限与主机拒绝、MCP 适配器（stdio 与 streamable-http）、tool 分镜来源的完整构建
（视频与图片产物）、上传授权阻塞与产物篡改校验。"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_studio_ai_media import media, run, shot  # noqa: F401 — media 是夹具
from test_studio_sources import frame_count, setup
from tool_stub_server import StubServer, make_png_bytes

from mindex.api import create_app
from mindex.shared.storage import METADATA_STEPS, Store
from mindex.studio import studio
from mindex.studio import workspace as ws
from mindex.studio.tools import adapter, artifacts, registry

SERVER = Path(__file__).with_name("mcp_tool_server.py")


def http_source(url, **extra):
    tools = [{"name": name, "output": output, **({"allow_upload": True} if name == "echo" else {}),
              **({"timeout": 5} if name == "slow" else {}), **({"max_mb": 1} if name == "big" else {})}
             for name, output in (("make_video", "video"), ("make_image", "image"), ("slow", "video"),
                                  ("fail", "video"), ("big", "image"), ("elsewhere", "image"), ("echo", "image"),
                                  ("redirect", "image"))]
    return {"name": "测试工具", "protocol": "http",
            "http": {"base_url": url, "headers": {"Authorization": "secret:token"}, "poll_interval": 0.05,
                     "tools_path": "/tools"},
            "tools": tools, "secrets": {"token": "Bearer s3cret"}, **extra}


@pytest.fixture
def stub(tmp_path):
    with StubServer(tmp_path) as server:
        yield server


@pytest.fixture
def client(media):  # noqa: F811
    with TestClient(create_app(media)) as client:
        yield media, client


def test_register_mask_secrets_and_validate(client, stub):
    config, client = client
    saved = client.put("/api/studio/tool-sources/stub", json=http_source(stub.url))
    assert saved.status_code == 200, saved.text
    item = saved.json()
    assert item["secrets"] == [{"name": "token", "set": True}] and item["missing_secrets"] == []
    assert "s3cret" not in saved.text and "s3cret" not in client.get("/api/studio/tool-sources").text
    assert item["connection"]["headers"]["Authorization"] == "secret:token"
    # 替换时未给出的密钥保留；显式 null 删除。
    again = client.put("/api/studio/tool-sources/stub", json={**http_source(stub.url), "secrets": None}).json()
    assert again["secrets"] == [{"name": "token", "set": True}]
    cleared = client.put("/api/studio/tool-sources/stub", json={**http_source(stub.url), "secrets": {"token": None}})
    assert cleared.json()["missing_secrets"] == ["token"]
    client.put("/api/studio/tool-sources/stub", json=http_source(stub.url))
    bad = [{**http_source(stub.url), "protocol": "mcp-stdio"},
           {**http_source(stub.url), "tools": []},
           {**http_source(stub.url), "artifact_hosts": ["https://x.com/path"]},
           {"name": "x", "protocol": "mcp-stdio", "stdio": {"command": "python"}, "tools": [{"name": "a"}]},
           {**http_source(stub.url), "tools": [{"name": "a"}, {"name": "a"}]}]
    for body in bad:
        assert client.put("/api/studio/tool-sources/other", json=body).status_code == 422, body
    assert client.put("/api/studio/tool-sources/Bad_Id", json=http_source(stub.url)).status_code == 422
    tested = client.post("/api/studio/tool-sources/stub/test").json()
    assert tested["ok"] and {"make_video", "make_image"} <= {tool["name"] for tool in tested["tools"]}
    assert "slow" in tested["missing"]
    assert stub.requests == []  # 测试连接只列工具，不提交任务
    # 备份导出（不含密钥）清空 secret 列。
    from mindex.maintenance import backup_db

    assert "tool_sources" in backup_db.TABLES and "secret" in backup_db.SECRET_FIELDS
    with Store(config.data_dir).connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(METADATA_STEPS)


def test_http_adapter_submit_poll_download_and_limits(client, stub, tmp_path):
    config, client = client
    client.put("/api/studio/tool-sources/stub", json=http_source(stub.url))
    record = registry.get_source(Store(config.data_dir), "stub")
    tool = adapter(record)
    found, task = tool.call("make_video", {"prompt": "x"}, [], timeout=10)
    assert task["polls"] >= 2 and task["status"] == "succeeded"
    assert stub.requests[-1]["headers"]["Authorization"] == "Bearer s3cret"
    assert stub.requests[-1]["body"] == {"tool": "make_video", "arguments": {"prompt": "x"}, "uploads": []}
    path = artifacts.download(found[0], tmp_path / "a.bin", hosts=registry.allowed_hosts(record),
                              max_bytes=10 * 1024 * 1024, timeout=10)
    target, info = artifacts.normalize(path, tmp_path, "video", 40_000_000)
    assert target.name == "artifact.mp4" and info["duration"] == pytest.approx(2.0, abs=0.1)
    with pytest.raises(studio.StudioError, match="超时"):
        tool.call("slow", {}, [], timeout=0.5)
    with pytest.raises(studio.StudioError, match="boom"):
        tool.call("fail", {}, [], timeout=5)
    big, _ = tool.call("big", {}, [], timeout=5)
    with pytest.raises(studio.StudioError, match="大小上限"):
        artifacts.download(big[0], tmp_path / "b.bin", hosts=registry.allowed_hosts(record), max_bytes=1024 * 1024,
                           timeout=5)
    for name in ("elsewhere", "redirect"):
        other, _ = tool.call(name, {}, [], timeout=5)
        with pytest.raises(studio.StudioError, match="白名单"):
            artifacts.download(other[0], tmp_path / "c.bin", hosts=registry.allowed_hosts(record),
                               max_bytes=1024 * 1024, timeout=5)
    assert not list(tmp_path.glob("*.partial"))
    garbage = tmp_path / "garbage.bin"
    garbage.write_bytes(b"not a video")
    with pytest.raises(studio.StudioError, match="不是可解码的视频"):
        artifacts.normalize(garbage, tmp_path, "video", 40_000_000)
    with pytest.raises(studio.StudioError, match="不是可用的图片"):
        artifacts.normalize(garbage, tmp_path, "image", 40_000_000)


def test_tool_shot_sources_build_video_and_image(client, stub):
    config, client = client
    client.put("/api/studio/tool-sources/stub", json=http_source(stub.url))
    shots = [shot("a", "红色开场。", source={"kind": "tool", "tool_id": "stub", "tool": "make_video",
                                         "arguments": {"prompt": "{{text}}"}}),
             shot("b", "蓝色结尾。", source={"kind": "tool", "tool_id": "stub", "tool": "make_image", "output": "image"})]
    job_id, w, p = setup(config, shots)
    assert ws.stages.plan_shot_stages(w, p, "a") == ("tts", "invoke", "select", "render")
    assert ws.stages.plan_shot_stages(w, p, "b") == ("tts", "invoke", "render")
    w, p = run(config, job_id, w, p)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    invoked = ws.version(w, p["shots"]["a"]["invoke"])
    assert invoked["data"]["output"] == "video" and invoked["data"]["origin"].startswith("127.0.0.1")
    assert stub.requests[0]["body"]["arguments"] == {"prompt": "红色开场。"}
    request = ws.file_path(config, job_id, invoked, "request").read_text()
    assert "s3cret" not in request
    selected = ws.version(w, p["shots"]["a"]["select"])
    assert selected["data"]["event"]["video"]["ref"] == {"version": invoked["id"], "file": "artifact"}
    image = ws.version(w, p["shots"]["b"]["invoke"])
    assert image["files"]["artifact"]["path"].endswith("artifact.png")
    final = ws.version(w, p["final"])
    frames, _, _ = frame_count(ws.file_path(config, job_id, final, "mp4"))
    assert frames == final["data"]["total_frames"]
    # 第二次构建全部复用，不再调用工具。
    calls = len(stub.requests)
    w, p = run(config, job_id, w, p)
    assert len(stub.requests) == calls
    # 改动工具配置中决定产物的部分（产物类型以外的连接设置）让 invoke 过期；只改超时不影响。
    body = http_source(stub.url)
    body["tools"][0]["timeout"] = 30
    client.put("/api/studio/tool-sources/stub", json=body)
    checked = ws.preflight(config, job_id, w, p)
    assert next(n for n in checked["nodes"] if n["key"] == "invoke:a")["action"] == "reuse"
    body["http"]["submit_path"] = "/v2/tools/{tool}/jobs"
    client.put("/api/studio/tool-sources/stub", json=body)
    checked = ws.preflight(config, job_id, w, p)
    assert next(n for n in checked["nodes"] if n["key"] == "invoke:a")["action"] != "reuse"
    # 被分镜使用的工具源拒绝删除。
    assert client.delete("/api/studio/tool-sources/stub").status_code == 409


def test_preflight_blocks_and_tamper_check(client, stub, tmp_path):
    config, client = client
    client.put("/api/studio/tool-sources/stub", json=http_source(stub.url))
    from test_studio_sources import store_input

    job_id, w, p = setup(config, [shot("a", "开场。")])
    picture = tmp_path / "ref.png"
    picture.write_bytes(make_png_bytes((10, 200, 10)))
    image_id = store_input(config, job_id, picture)
    cases = [({"tool_id": "missing", "tool": "make_video"}, "找不到第三方工具源"),
             ({"tool_id": "stub", "tool": "nope"}, "白名单"),
             ({"tool_id": "stub", "tool": "make_image"}, "产物是图片"),
             ({"tool_id": "stub", "tool": "make_image", "output": "image",
               "uploads": [{"kind": "image", "media_id": image_id}]}, "未允许携带素材上传")]
    for extra, reason in cases:
        ws.set_board(config, job_id, w, p, [shot("a", "开场。", source={"kind": "tool", **extra})])
        node = next(n for n in ws.preflight(config, job_id, w, p)["nodes"] if n["key"] == "invoke:a")
        assert node["action"] == "blocked" and reason in node["reason"], (extra, node)
    # 允许上传的工具携带素材调用：请求体带上 base64 素材。
    ws.set_board(config, job_id, w, p, [shot("a", "开场。", source={
        "kind": "tool", "tool_id": "stub", "tool": "echo", "output": "image",
        "uploads": [{"kind": "image", "media_id": image_id}]})])
    ws.persist(config, job_id, w)
    w, p = run(config, job_id, w, p)
    assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
    upload = stub.requests[-1]["body"]["uploads"][0]
    assert upload["sha256"] == image_id and upload["base64"]
    # 篡改产物文件：下游读取时复验摘要并失败。
    made = ws.version(w, p["shots"]["a"]["invoke"])
    path = ws.file_path(config, job_id, made, "artifact")
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(studio.StudioError):
        ws.file_path(config, job_id, made, "artifact")


def test_tool_source_patch_validation(client):
    config, client = client
    job_id, _, p = setup(config, [shot("a", "开场。")])
    revision = client.get(f"/api/studio/jobs/{job_id}/workspace").json()["revision"]
    body = {"revision": revision, "shots": [{"id": "a", "text": "开场。", "query": "q", "estimated_seconds": 1,
                                                   "source": {"kind": "tool", "tool_id": "stub", "tool": "t",
                                                              "uploads": [{"kind": "image", "media_id": "a" * 64}]}}]}
    response = client.patch(f"/api/studio/jobs/{job_id}/plans/{p['id']}", json=body)
    assert response.status_code == 422 and "上传素材" in response.text
    with pytest.raises(ValueError, match="8000"):
        shot("a", "x", source={"kind": "tool", "tool_id": "s", "tool": "t", "arguments": {"k": "x" * 9000}})


# ---------------------------------------------------------------- MCP


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stdio_record(**extra):
    return {"id": "local", "name": "本机", "protocol": "mcp-stdio",
            "connection": {"command": sys.executable, "args": [str(SERVER)], "env": {}, "cwd": None},
            "tools": [{"name": "make_image", "output": "image", "allow_upload": False, "timeout": 60, "max_mb": 10},
                      {"name": "make_video", "output": "video", "allow_upload": False, "timeout": 60, "max_mb": 10},
                      {"name": "count_uploads", "output": "image", "allow_upload": True, "timeout": 60, "max_mb": 10},
                      {"name": "broken", "output": "image", "allow_upload": False, "timeout": 60, "max_mb": 10}],
            "artifact_hosts": [], "secrets": {}, **extra}


def test_mcp_stdio_adapter(tmp_path):
    pytest.importorskip("mcp")
    tool = adapter(stdio_record())
    assert {"make_image", "make_video", "count_uploads"} <= {item["name"] for item in tool.list_tools()}
    found, _ = tool.call("make_image", {"color": "green"}, [], timeout=60)
    assert found[0].data[:4] == b"\x89PNG" and found[0].mime == "image/png"
    found, _ = tool.call("make_video", {"seconds": 1}, [], timeout=60)
    path = artifacts.download(found[0], tmp_path / "v.bin", hosts=set(), max_bytes=10 * 1024 * 1024, timeout=10)
    _, info = artifacts.normalize(path, tmp_path, "video", 40_000_000)
    assert info["duration"] == pytest.approx(1.0, abs=0.1)
    found, record = tool.call("count_uploads", {}, [{"name": "a.png", "mime": "image/png", "sha256": "0", "base64": ""}],
                              timeout=60)
    assert record["text"] == "uploads=1" and found[0].data
    with pytest.raises(studio.StudioError, match="tool exploded"):
        tool.call("broken", {}, [], timeout=60)
    with pytest.raises(studio.StudioError, match="MCP"):
        adapter(stdio_record(connection={"command": "/nonexistent/server", "args": [], "env": {}, "cwd": None})
                ).list_tools(timeout=10)


def test_mcp_streamable_http_adapter_and_build(client):
    pytest.importorskip("mcp")
    config, client = client
    port = free_port()
    process = subprocess.Popen([sys.executable, str(SERVER), "http", str(port)], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        saved = client.put("/api/studio/tool-sources/remote", json={
            "name": "远端 MCP", "protocol": "mcp-http", "mcp_http": {"url": f"http://127.0.0.1:{port}/mcp"},
            "tools": [{"name": "make_image", "output": "image"}]})
        assert saved.status_code == 200, saved.text
        tested = client.post("/api/studio/tool-sources/remote/test").json()
        assert tested["ok"] and tested["missing"] == [], tested
        job_id, w, p = setup(config, [shot("a", "开场。", source={"kind": "tool", "tool_id": "remote",
                                                                 "tool": "make_image", "output": "image",
                                                                 "arguments": {"color": "yellow"}})])
        w, p = run(config, job_id, w, p)
        assert w["builds"][-1]["status"] == "complete", w["builds"][-1]
        assert ws.version(w, p["shots"]["a"]["invoke"])["data"]["origin"] == "inline"
    finally:
        process.terminate()
        process.wait(10)
