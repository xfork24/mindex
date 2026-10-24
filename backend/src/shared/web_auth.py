"""可选的 Web/API 访问令牌认证。

设置环境变量 ``MINDEX_WEB_TOKEN`` 后启用：浏览器在 ``/login`` 提交令牌，服务器设置 HttpOnly Cookie；
脚本和命令行客户端使用 ``Authorization: Bearer <令牌>``。未设置令牌时不安装任何认证，行为与此前一致。

Cookie 保存的是令牌的 HMAC 摘要，不是令牌本身。令牌比较一律使用常量时间比较。
"""

from __future__ import annotations

import hashlib
import hmac
import os
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from mindex.shared.shot_media import file_response
from mindex.shared.web_assets import frontend_root

TOKEN_ENV = "MINDEX_WEB_TOKEN"
COOKIE = "mindex_session"
# 登录页自身所需的公共静态资源（样式、脚本、文案字典），不含业务数据。
# /api/agent/attachments 由端点自行鉴权：既接受网页凭证，也接受 MINDEX_MCP_TOKEN（见 agent/api.py）。
PUBLIC_PREFIXES = ("/assets/shared/", "/api/agent/attachments")
# /mcp 自行校验 MINDEX_MCP_TOKEN（未设置令牌时不会挂载），不复用网页登录。
PUBLIC_PATHS = {"/login", "/logout", "/mcp"}


def configured_token(explicit: str | None = None) -> str:
    return (explicit if explicit is not None else os.environ.get(TOKEN_ENV, "")).strip()


def session_value(token: str) -> str:
    return hmac.new(token.encode(), b"mindex-web-session-v1", hashlib.sha256).hexdigest()


def authorized(request: Request, token: str) -> bool:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer ") and hmac.compare_digest(header[7:].strip(), token):
        return True
    # 智能助手工具的进程内回环请求：凭证是本进程启动时生成的随机值，外部无法得知。
    internal = getattr(request.app.state, "agent_internal_token", None)
    supplied = request.headers.get("x-mindex-internal", "")
    if internal and supplied and hmac.compare_digest(supplied, internal):
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), session_value(token))


def install_web_auth(app: FastAPI, token: str | None = None) -> bool:
    """令牌非空时安装认证中间件与 /login、/logout；返回是否启用。

    生效的令牌记在 ``app.state.web_auth_token``（未启用时为空串），供自行鉴权的端点复用。
    """
    secret = configured_token(token)
    app.state.web_auth_token = secret
    if not secret:
        return False

    @app.get("/login", include_in_schema=False)
    def login_page():
        return file_response(frontend_root() / "shared" / "login.html", media_type="text/html")

    @app.post("/login", include_in_schema=False)
    def login(request: Request, token: str = Form(default=""), next: str = Form(default="/")):
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        if not hmac.compare_digest(token.strip(), secret):
            return RedirectResponse(f"/login?error=1&next={quote(target)}", status_code=303)
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            COOKIE,
            session_value(secret),
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    @app.api_route("/logout", methods=["GET", "POST"], include_in_schema=False)
    def logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES) or authorized(request, secret):
            return await call_next(request)
        if path.startswith("/api/") or request.method != "GET":
            return JSONResponse(
                {"detail": "需要访问令牌：登录后使用 Cookie，或发送 Authorization: Bearer <令牌>"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return Response(status_code=303, headers={"Location": f"/login?next={quote(target)}"})

    return True
