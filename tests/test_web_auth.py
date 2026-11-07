"""可选访问令牌：未设置时行为不变；设置后页面跳转登录，API 返回 401，Cookie 与 Bearer 均可通过。"""

import pytest
from fastapi.testclient import TestClient

from api import create_app

TOKEN = "unit-test-token"


@pytest.fixture
def open_client(config, monkeypatch):
    monkeypatch.delenv("MINDEX_WEB_TOKEN", raising=False)
    return TestClient(create_app(config), follow_redirects=False)


@pytest.fixture
def client(config, monkeypatch):
    monkeypatch.delenv("MINDEX_WEB_TOKEN", raising=False)
    return TestClient(create_app(config, web_token=TOKEN), follow_redirects=False)


def test_without_token_nothing_changes(open_client):
    assert open_client.get("/").status_code == 200
    assert open_client.get("/api/status").status_code == 200
    assert open_client.get("/login").status_code == 404


def test_environment_variable_enables_auth(config, monkeypatch):
    monkeypatch.setenv("MINDEX_WEB_TOKEN", TOKEN)
    assert TestClient(create_app(config), follow_redirects=False).get("/api/status").status_code == 401


def test_unauthenticated_requests_are_rejected(client):
    api = client.get("/api/status")
    assert api.status_code == 401 and api.headers["www-authenticate"] == "Bearer"
    assert client.post("/api/search/text", json={"text": "红色"}).status_code == 401
    assert client.get("/docs").status_code == 303
    page = client.get("/admin?tab=1")
    assert page.status_code == 303 and page.headers["location"] == "/login?next=/admin%3Ftab%3D1"


def test_login_page_and_shared_assets_are_public(client):
    assert client.get("/login").status_code == 200
    for path in ("/assets/shared/login.js", "/assets/shared/i18n.js", "/assets/shared/messages.js"):
        assert client.get(path).status_code == 200
    # 业务页面脚本不是公共资源。
    assert client.get("/assets/admin/admin.js").status_code == 303


def test_bearer_token(client):
    assert client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_cookie_login_and_logout(client):
    bad = client.post("/login", data={"token": "wrong", "next": "/admin"})
    assert bad.status_code == 303 and bad.headers["location"].startswith("/login?error=1")
    assert "mindex_session" not in bad.headers.get("set-cookie", "")
    ok = client.post("/login", data={"token": TOKEN, "next": "/admin"})
    assert ok.status_code == 303 and ok.headers["location"] == "/admin"
    cookie = ok.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and TOKEN not in cookie
    assert client.get("/api/status").status_code == 200
    assert client.get("/admin").status_code == 200
    assert client.get("/logout").status_code == 405
    assert client.get("/api/status").status_code == 200
    out = client.post("/logout")
    assert out.status_code == 303 and out.headers["location"] == "/login"
    client.cookies.clear()
    assert client.get("/api/status").status_code == 401


def test_login_redirect_cannot_leave_the_site(client):
    for target in (
        "//evil.example",
        "https://evil.example",
        "/\\evil.example",
        "/\\/evil.example",
        "\\\\evil.example",
        "/admin\nLocation: https://evil.example",
        "/\tevil",
        "javascript:alert(1)",
        "evil.example",
        "",
    ):
        response = client.post("/login", data={"token": TOKEN, "next": target})
        assert response.headers["location"] == "/"


def test_login_keeps_site_relative_next(client):
    response = client.post("/login", data={"token": TOKEN, "next": "/studio?job=a%2Fb#x"})
    assert response.headers["location"] == "/studio?job=a%2Fb#x"


def _serve(monkeypatch, tmp_path, args, token=None):
    from typer.testing import CliRunner

    from cli import app

    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: calls.append((host, port)))
    monkeypatch.setattr("search.warmup.warm_up_in_background", lambda *_: None)
    if token is None:
        monkeypatch.delenv("MINDEX_WEB_TOKEN", raising=False)
    else:
        monkeypatch.setenv("MINDEX_WEB_TOKEN", token)
    path = tmp_path / "config.yaml"
    path.write_text(f"data_dir: {tmp_path / 'data'}\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["-c", str(path), "serve", *args])
    return result, calls


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::"])
def test_serve_refuses_public_host_without_token(monkeypatch, tmp_path, host):
    result, calls = _serve(monkeypatch, tmp_path, ["--host", host])
    assert result.exit_code == 1 and calls == []
    assert "MINDEX_WEB_TOKEN" in result.output and "--allow-insecure" in result.output


@pytest.mark.parametrize(
    ("args", "token"),
    [
        (["--host", "0.0.0.0", "--allow-insecure"], None),
        (["--host", "0.0.0.0"], TOKEN),
        (["--host", "127.0.0.2"], None),
        (["--host", "localhost"], None),
        (["--host", "::1"], None),
        ([], None),
    ],
)
def test_serve_allows_loopback_token_or_explicit_insecure(monkeypatch, tmp_path, args, token):
    result, calls = _serve(monkeypatch, tmp_path, args, token)
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    if "--allow-insecure" in args:
        assert "警告" in result.output
