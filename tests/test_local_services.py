"""本机服务管理：真实子进程（一个提供 /health 的测试小服务），覆盖启停、进程组清理、崩溃、接管与权限。"""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import create_app
from settings.local_services import LocalServiceManager, _alive
from shared.config import LocalServicesConfig, load_config
from shared.storage import Store

# 测试服务：/health 返回 200；--crash 立即退出；同时拉起一个孙进程，验证停止时整个进程组都被结束。
SERVER = r"""
import http.server, os, socketserver, subprocess, sys
if "--crash" in sys.argv:
    print("boom", flush=True)
    sys.exit(3)
port = int(sys.argv[1])
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
open(sys.argv[2], "w").write(str(child.pid))
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *args):
        print("request", self.path, flush=True)
class LoopbackServer(http.server.HTTPServer):
    def server_bind(self):
        # macOS runner 的 getfqdn 可能阻塞数十秒；回环测试无需解析主机名。
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
with LoopbackServer(("127.0.0.1", port), Handler) as server:
    print("listening", port, flush=True)
    server.serve_forever()
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def services(config, tmp_path):
    script = tmp_path / "fake_service.py"
    script.write_text(SERVER, encoding="utf-8")
    port = free_port()
    config.local_services = LocalServicesConfig.model_validate(
        {
            "enabled": True,
            "services": [
                {
                    "name": "fake",
                    "label": "测试服务",
                    "role": "embedding",
                    "port": port,
                    "argv": [sys.executable, str(script), str(port), str(tmp_path / "grandchild.pid")],
                },
                {"name": "crash", "port": free_port(), "argv": [sys.executable, str(script), "--crash"]},
                {"name": "missing", "port": free_port(), "argv": ["/nonexistent/bin/server"]},
            ],
        }
    )
    client = TestClient(create_app(config))
    yield client, config, tmp_path
    client.post("/api/settings/local-services/fake/stop")


def test_start_health_logs_and_stop_kills_process_group(services):
    client, config, tmp_path = services
    listed = client.get("/api/settings/local-services").json()
    assert listed["enabled"] is True
    fake = next(item for item in listed["items"] if item["name"] == "fake")
    assert (fake["running"], fake["healthy"], fake["label"], fake["autostart"], fake["stop_on_exit"]) == (
        False,
        False,
        "测试服务",
        False,
        True,
    )

    started = client.post("/api/settings/local-services/fake/start")
    assert started.status_code == 200, started.text
    pid = started.json()["pid"]
    assert wait(lambda: client.get("/api/settings/local-services").json()["items"][0]["healthy"]), (
        client.get("/api/settings/local-services").text,
        client.get("/api/settings/local-services/fake/logs").text,
    )
    grandchild = int((tmp_path / "grandchild.pid").read_text())
    assert _alive(grandchild)
    # 重复启动是幂等的。
    assert client.post("/api/settings/local-services/fake/start").json()["pid"] == pid
    logs = client.get("/api/settings/local-services/fake/logs", params={"tail": 50}).json()["text"]
    assert "启动：" in logs and "listening" in logs

    stopped = client.post("/api/settings/local-services/fake/stop").json()
    assert stopped["running"] is False and stopped["pid"] is None
    assert wait(lambda: not _alive(pid)) and wait(lambda: not _alive(grandchild))
    assert not (config.data_dir / "locks" / "services" / "fake.json").exists()


def test_crash_and_missing_executable_are_reported(services):
    client, _config, _tmp = services
    crash = client.post("/api/settings/local-services/crash/start")
    assert (
        crash.status_code == 422 and "立即退出" in crash.json()["detail"] and "boom" in crash.json()["detail"]
    )
    missing = client.post("/api/settings/local-services/missing/start")
    assert missing.status_code == 422 and "找不到可执行文件" in missing.json()["detail"]
    assert client.post("/api/settings/local-services/nope/start").status_code == 404


def test_service_fixture_does_not_resolve_hostname(services, monkeypatch):
    """真实子进程禁用 getfqdn，防止 macOS runner 的慢解析再次阻塞监听。"""
    client, _config, tmp_path = services
    startup = tmp_path / "startup"
    startup.mkdir()
    (startup / "sitecustomize.py").write_text(
        "import socket\n"
        "def reject_lookup(*args, **kwargs):\n"
        "    raise RuntimeError('回环测试服务不应调用 getfqdn')\n"
        "socket.getfqdn = reject_lookup\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(startup))
    started = client.post("/api/settings/local-services/fake/start")
    assert started.status_code == 200, started.text
    assert started.json()["healthy"], started.text
    assert "listening" in client.get("/api/settings/local-services/fake/logs").json()["text"]


@pytest.mark.parametrize("proxy_source", ["environment", "system"])
def test_local_health_ignores_proxies(services, monkeypatch, proxy_source):
    """回环探针直连本机服务，不经环境代理或 macOS/Windows 系统代理。"""
    client, _config, _tmp = services
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    proxy = "http://127.0.0.1:9"
    if proxy_source == "environment":
        monkeypatch.setenv("HTTP_PROXY", proxy)
        monkeypatch.setenv("HTTPS_PROXY", proxy)
        monkeypatch.setenv("ALL_PROXY", proxy)
    else:
        monkeypatch.setattr("httpx._utils.getproxies", lambda: {"http": proxy, "https": proxy})
    started = client.post("/api/settings/local-services/fake/start")
    assert started.status_code == 200, started.text
    assert wait(lambda: client.get("/api/settings/local-services").json()["items"][0]["healthy"]), (
        client.get("/api/settings/local-services").text,
        client.get("/api/settings/local-services/fake/logs").text,
    )
    assert client.post("/api/settings/local-services/fake/start").json()["pid"] == started.json()["pid"]


def test_flags_persist_and_command_cannot_be_changed(services):
    client, config, _tmp = services
    patched = client.patch(
        "/api/settings/local-services/fake", json={"autostart": True, "stop_on_exit": False}
    )
    assert patched.status_code == 200 and (patched.json()["autostart"], patched.json()["stop_on_exit"]) == (
        True,
        False,
    )
    # 只允许两个开关：命令、工作目录、环境变量都不能经接口修改。
    for body in ({"argv": ["/bin/sh"]}, {"cwd": "/"}, {"env": {"X": "1"}}, {"autostart": "yes"}):
        assert client.patch("/api/settings/local-services/fake", json=body).status_code == 422
    # 开关保存在数据目录（新的应用实例同样读到），不随“恢复默认”清除。
    assert client.delete("/api/settings/business").status_code == 204
    again = TestClient(create_app(config)).get("/api/settings/local-services").json()["items"][0]
    assert (again["autostart"], again["stop_on_exit"]) == (True, False)


def test_disabled_rejects_operations(config):
    config.local_services = LocalServicesConfig.model_validate(
        {"services": [{"name": "fake", "port": free_port(), "argv": [sys.executable, "-V"]}]}
    )
    client = TestClient(create_app(config))
    assert client.get("/api/settings/local-services").json() == {"enabled": False, "items": []}
    for method, path in (
        ("post", "fake/start"),
        ("post", "fake/stop"),
        ("get", "fake/logs"),
        ("post", "stop-on-exit"),
    ):
        assert getattr(client, method)(f"/api/settings/local-services/{path}").status_code == 403
    assert client.patch("/api/settings/local-services/fake", json={"autostart": True}).status_code == 403


def test_external_service_on_port_is_not_started_or_stopped(services):
    client, config, _tmp = services
    service = config.local_services.get("fake")
    other = LocalServiceManager(Store(config.data_dir))  # 另一个进程里的管理器：模拟“不是本应用启动的”服务
    other.start(service)
    (config.data_dir / "locks" / "services" / "fake.json").unlink()  # 本应用没有它的记录
    try:
        assert wait(lambda: other.healthy(service)[0]), (other.healthy(service), other.log_tail(service))
        item = client.get("/api/settings/local-services").json()["items"][0]
        assert (item["running"], item["healthy"], item["external"]) == (False, True, True)
        assert client.post("/api/settings/local-services/fake/start").status_code == 409
    finally:
        other._children["fake"].kill()
        other._children["fake"].wait(5)


def test_restart_adopts_running_service_and_stop_on_exit(services):
    client, config, _tmp = services
    pid = client.post("/api/settings/local-services/fake/start").json()["pid"]
    # 后端重启：新的管理器没有子进程句柄，靠 pid 文件（pid + 启动时刻）接管。
    fresh = LocalServiceManager(Store(config.data_dir))
    assert fresh.status(config.local_services.get("fake"))["pid"] == pid
    # pid 文件里的启动时刻不符（pid 被复用）时不接管。
    record = config.data_dir / "locks" / "services" / "fake.json"
    saved = json.loads(record.read_text())
    record.write_text(json.dumps({**saved, "started": "Mon Jan  1 00:00:00 2001"}))
    assert (
        LocalServiceManager(Store(config.data_dir)).status(config.local_services.get("fake"))["pid"] is None
    )
    record.write_text(json.dumps(saved))
    assert client.post("/api/settings/local-services/stop-on-exit").json() == {"stopped": ["fake"]}
    assert wait(lambda: not _alive(pid))


def test_autostart_skips_failures(services):
    _client, config, _tmp = services
    manager = LocalServiceManager(Store(config.data_dir))
    for name in ("fake", "crash"):
        manager.set_flags(config.local_services.get(name), autostart=True)
    try:
        assert manager.autostart(config) == ["fake"]
    finally:
        manager.stop(config.local_services.get("fake"))


def test_config_rules_and_paths(tmp_path):
    with pytest.raises(ValueError, match="name 不能重复"):
        LocalServicesConfig.model_validate(
            {"services": [{"name": "a", "port": 1, "argv": ["x"]}, {"name": "a", "port": 2, "argv": ["x"]}]}
        )
    with pytest.raises(ValueError, match="port 不能重复"):
        LocalServicesConfig.model_validate(
            {"services": [{"name": "a", "port": 1, "argv": ["x"]}, {"name": "b", "port": 1, "argv": ["x"]}]}
        )
    with pytest.raises(ValueError):
        LocalServicesConfig.model_validate({"services": [{"name": "Bad Name", "port": 1, "argv": ["x"]}]})
    (tmp_path / "config.yaml").write_text(
        "local_services:\n  enabled: true\n  services:\n"
        "    - {name: wemm, port: 8888, cwd: models, argv: ['~/bin/llama-server', '--port', '8888']}\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path / "config.yaml")
    service = config.local_services.get("wemm")
    assert service.cwd == str((tmp_path / "models").resolve())
    assert service.argv[0] == str(Path("~/bin/llama-server").expanduser())
    # exclude=True：不进入 model_dump（任务快照、备份配置、指纹不变）。
    assert "local_services" not in config.model_dump()
