"""进程内回环客户端：工具经 ASGI 直接调用本应用的 REST 接口。

好处是严格校验、``revision``/``token`` 乐观锁、``mutation_lock``、409 冲突与 i18n 错误
全部原样生效，智能助手与页面操作走同一条路径。

- 每个进程启动时生成随机内部凭证（``app.state.agent_internal_token``），``web_auth`` 用常量时间
  比较放行；外部请求无法得知该值，伪造的头会被拒绝。
- 请求体里的 ``secret://<id>`` 引用在发送前由 ``resolve_secret`` 解引用为明文，明文不离开本模块。
- 响应规整为 ``{ok, status, data}`` 或 ``{ok: False, status, error}``；409/422 作为可恢复结果返回。
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any

import httpx

INTERNAL_HEADER = "x-mindex-internal"
SECRET_PREFIX = "secret://"


def ensure_internal_token(app) -> str:
    token = getattr(app.state, "agent_internal_token", None)
    if not token:
        token = secrets.token_urlsafe(32)
        app.state.agent_internal_token = token
    return token


def error_text(payload: Any) -> str:
    if isinstance(payload, dict):
        detail = payload.get("detail", payload.get("message", payload))
    else:
        detail = payload
    if isinstance(detail, list):
        return "；".join(
            f"{'.'.join(str(p) for p in item.get('loc', [])[1:]) or '参数'}: {item.get('msg')}"
            if isinstance(item, dict) else str(item)
            for item in detail
        )
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    return str(detail)


class SecretError(Exception):
    pass


class MindexClient:
    def __init__(self, app=None, *, base_url: str | None = None, token: str | None = None,
                 resolve_secret: Callable[[str], str] | None = None, timeout: float = 120):
        if app is not None:
            transport = httpx.ASGITransport(app=app)
            headers = {INTERNAL_HEADER: ensure_internal_token(app)}
            self.http = httpx.AsyncClient(transport=transport, base_url="http://mindex.internal",
                                          headers=headers, timeout=timeout)
        else:
            # 独立进程（如 stdio MCP 连接远端服务）：走 HTTP + Bearer。
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            self.http = httpx.AsyncClient(base_url=base_url or "http://127.0.0.1:8000", headers=headers,
                                          timeout=timeout)
        self.resolve_secret = resolve_secret

    def with_secrets(self, resolve_secret: Callable[[str], str] | None) -> MindexClient:
        clone = object.__new__(MindexClient)
        clone.http = self.http
        clone.resolve_secret = resolve_secret
        return clone

    def _deref(self, value):
        if isinstance(value, str) and value.startswith(SECRET_PREFIX):
            if self.resolve_secret is None:
                raise SecretError("当前环境不能使用密钥引用")
            try:
                return self.resolve_secret(value)
            except Exception as exc:  # noqa: BLE001 — 引用失效等原因统一作为可恢复错误返回
                raise SecretError(str(exc)) from exc
        if isinstance(value, dict):
            return {k: self._deref(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._deref(v) for v in value]
        return value

    async def request(self, method: str, path: str, *, json: Any = None, params: dict | None = None) -> dict:
        try:
            body = self._deref(json) if json is not None else None
        except SecretError as exc:
            return {"ok": False, "status": 400, "error": str(exc)}
        response = await self.http.request(
            method, path, json=body, params={k: v for k, v in (params or {}).items() if v is not None}
        )
        try:
            payload = response.json() if response.content else None
        except ValueError:
            payload = response.text
        if response.is_success:
            return {"ok": True, "status": response.status_code, "data": payload}
        return {"ok": False, "status": response.status_code, "error": error_text(payload)}

    async def get(self, path: str, **params) -> dict:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, body: Any = None, **params) -> dict:
        return await self.request("POST", path, json=body if body is not None else {}, params=params)

    async def patch(self, path: str, body: Any) -> dict:
        return await self.request("PATCH", path, json=body)

    async def put(self, path: str, body: Any) -> dict:
        return await self.request("PUT", path, json=body)

    async def delete(self, path: str) -> dict:
        return await self.request("DELETE", path)

    async def aclose(self):
        await self.http.aclose()
