"""进程内回环客户端：工具经 ASGI 直接调用本应用的 REST 接口。

好处是严格校验、``revision``/``token`` 乐观锁、``mutation_lock``、409 冲突与 i18n 错误
全部原样生效，智能助手与页面操作走同一条路径。

- 每个进程启动时生成随机内部凭证（``app.state.agent_internal_token``），``web_auth`` 用常量时间
  比较放行；外部请求无法得知该值，伪造的头会被拒绝。
- 请求体里的 ``secret://<id>`` 引用在发送前由 ``resolve_secret`` 解引用为明文，明文不离开本模块。
- 响应规整为 ``{ok, status, data}`` 或 ``{ok: False, status, error}``；409/422 作为可恢复结果返回。
- ``upload()`` 提交 multipart：``attachment://<token>`` 引用由 ``resolve_attachment`` 按调用方归属解析为文件
  （面板会话只能用本会话附件，MCP 只能用 mcp 附件）；远程模式（``base_url``）不注入解析器，先经
  ``GET /api/agent/attachments/{token}`` 与 ``/content`` 取回 mcp 附件再上传。解析失败时返回
  ``{ok: False, status: 400, code: "attachment_invalid"}``，不向目标接口发送请求。
- 路径结构校验（``unsafe_path``）：拒绝含 ``.``/``..`` 段、``?``、``#`` 的请求路径（返回 400
``invalid_path``，
  不发送请求），防止工具参数里的 ID 经路径规整越到别的接口（例如 ``delete_job(job_id="../../admin/sources/x")
``
  变成删除素材）。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json as jsonlib
import re
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx

INTERNAL_HEADER = "x-mindex-internal"
SECRET_PREFIX = "secret://"
ATTACHMENT_PREFIX = "attachment://"

# (文件名, 路径或内容, 内容类型)
AttachmentFile = tuple[str, Path | bytes, str]
_KEEP = object()


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
            if isinstance(item, dict)
            else str(item)
            for item in detail
        )
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    return str(detail)


class SecretError(Exception):
    pass


class AttachmentRefError(Exception):
    """附件引用无法解析（不存在、过期、越权或当前环境不支持）。"""


class LoopbackError(Exception):
    """``fetch_bytes`` 收到非 2xx 响应。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def unsafe_path(path: str, *, allow_query: bool = False) -> str | None:
    """请求路径的结构校验：工具用参数拼路径（如 ``/api/studio/jobs/{job_id}``），参数里的点段会被 httpx 规整掉
        （``/api/studio/jobs/../../admin/sources/x`` → ``/api/admin/sources/x``），``?``/``#`` 会截断路径
        （``/api/studio/jobs/J?/plans/P`` → ``/api/studio/jobs/J``），
        从而越过工具本意打到别的接口（例如删除素材）
    。

        返回拒绝原因，合法时返回 None。查询参数一律经 ``params`` 传递；``allow_query`` 只给读取站内媒体地址的
        ``fetch_bytes`` 使用。
    """
    if not isinstance(path, str) or not path.startswith("/"):
        return "请求路径必须以 / 开头"
    raw = path
    if allow_query:
        raw = path.split("?", 1)[0]
    elif "?" in path:
        return "请求路径不能包含 ?（查询参数请单独传递）"
    if "#" in raw:
        return "请求路径不能包含 #"
    segments = unquote(raw).replace("\\", "/").split("/")
    if any(segment in (".", "..") for segment in segments):
        return "请求路径不能包含 . 或 .. 段（ID 参数不合法）"
    return None


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return jsonlib.dumps(value, ensure_ascii=False)
    return str(value)


def _normalize(response: httpx.Response) -> dict:
    try:
        payload = response.json() if response.content else None
    except ValueError:
        payload = response.text
    if response.is_success:
        return {"ok": True, "status": response.status_code, "data": payload}
    return {"ok": False, "status": response.status_code, "error": error_text(payload)}


class MindexClient:
    def __init__(
        self,
        app=None,
        *,
        base_url: str | None = None,
        token: str | None = None,
        resolve_secret: Callable[[str], str] | None = None,
        resolve_attachment: Callable[[str], AttachmentFile] | None = None,
        timeout: float = 120,
    ):
        self.remote = app is None
        if app is not None:
            transport = httpx.ASGITransport(app=app)
            headers = {INTERNAL_HEADER: ensure_internal_token(app)}
            self.http = httpx.AsyncClient(
                transport=transport, base_url="http://mindex.internal", headers=headers, timeout=timeout
            )
        else:
            # 独立进程（如 stdio MCP 连接远端服务）：走 HTTP + Bearer。
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            self.http = httpx.AsyncClient(
                base_url=base_url or "http://127.0.0.1:8000", headers=headers, timeout=timeout
            )
        self.studio_revisions = {}
        self.resolve_secret = resolve_secret
        self.resolve_attachment = resolve_attachment

    def with_resolvers(self, *, secret: Any = _KEEP, attachment: Any = _KEEP) -> MindexClient:
        """共享同一 HTTP 连接、换上新的解析器；未给出的解析器沿用当前值（显式传 None 表示清除）。"""
        clone = copy.copy(self)
        if secret is not _KEEP:
            clone.resolve_secret = secret
        if attachment is not _KEEP:
            clone.resolve_attachment = attachment
        return clone

    def with_secrets(self, resolve_secret: Callable[[str], str] | None) -> MindexClient:
        return self.with_resolvers(secret=resolve_secret)

    def _deref(self, value):
        if isinstance(value, str) and value.startswith(SECRET_PREFIX):
            if self.resolve_secret is None:
                raise SecretError("当前环境不能使用密钥引用")
            try:
                return self.resolve_secret(value)
            except Exception as exc:  # 引用失效等原因统一作为可恢复错误返回
                raise SecretError(str(exc)) from exc
        if isinstance(value, dict):
            return {k: self._deref(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._deref(v) for v in value]
        return value

    async def _send(self, method, path, **kwargs):
        match = re.match(r"^/api/studio/jobs/([^/?]+)", path)
        job = match[1] if match else None
        modifying = method.upper() not in {"GET", "HEAD", "OPTIONS"}
        headers = (
            {"If-Match": self.studio_revisions[job]} if job in self.studio_revisions and modifying else {}
        )
        response = await self.http.request(method, path, headers=headers, **kwargs)
        # 428 尚未执行任何修改；先获取前置条件再提交。409 始终原样返回，不覆盖新状态。
        if response.status_code == 428 and job and not headers:
            current = await self.http.get(f"/api/studio/jobs/{job}")
            revision = current.headers.get("X-Studio-Revision") if current.is_success else None
            if revision:
                self.studio_revisions[job] = revision
                for _, (_, content, _) in kwargs.get("files", []):
                    if hasattr(content, "seek"):
                        content.seek(0)
                response = await self.http.request(method, path, headers={"If-Match": revision}, **kwargs)
        revision = response.headers.get("X-Studio-Revision")
        if job and revision and response.is_success:
            self.studio_revisions[job] = revision
        return response

    async def request(self, method: str, path: str, *, json: Any = None, params: dict | None = None) -> dict:
        problem = unsafe_path(path)
        if problem:
            return {"ok": False, "status": 400, "code": "invalid_path", "error": problem}
        try:
            body = self._deref(json) if json is not None else None
        except SecretError as exc:
            return {"ok": False, "status": 400, "error": str(exc)}
        response = await self._send(
            method, path, json=body, params={k: v for k, v in (params or {}).items() if v is not None}
        )
        return _normalize(response)

    async def _attachment(self, ref: str) -> AttachmentFile:
        if not isinstance(ref, str) or not ref.startswith(ATTACHMENT_PREFIX):
            raise AttachmentRefError(f"文件字段只接受附件引用 attachment://…（收到 {str(ref)[:60]}）")
        if self.resolve_attachment is not None:
            try:
                return self.resolve_attachment(ref)
            except Exception as exc:  # 引用失效、越权等统一作为可恢复错误返回
                raise AttachmentRefError(str(exc)) from exc
        if not self.remote:
            raise AttachmentRefError("当前环境不能使用附件引用")
        # 远程模式：只能使用 mcp 附件（与进程内 MCP 的归属规则一致），先取元数据再取内容。
        tok = ref[len(ATTACHMENT_PREFIX) :]
        if not re.fullmatch(r"[0-9a-f]{32}", tok):
            raise AttachmentRefError("附件引用格式无效")
        meta = _normalize(await self.http.get(f"/api/agent/attachments/{tok}"))
        if not meta["ok"]:
            raise AttachmentRefError(meta["error"])
        data = meta["data"] or {}
        if data.get("owner") != "mcp":
            raise AttachmentRefError("附件不属于当前会话或调用方，请在当前会话中重新上传")
        try:
            content, _ctype = await self.fetch_bytes(f"/api/agent/attachments/{tok}/content")
        except LoopbackError as exc:
            raise AttachmentRefError(str(exc)) from exc
        return data.get("filename") or "file", content, data.get("content_type") or "application/octet-stream"

    async def upload(
        self,
        path: str,
        *,
        files: dict[str, str | list[str]],
        data: dict | None = None,
        method: str = "POST",
        params: dict | None = None,
    ) -> dict:
        """以 multipart 提交：``files`` 的值为附件引用或引用列表；``data`` 的值会解引用 ``secret://``，
        dict/list 编码为 JSON 字符串、布尔编码为 true/false，None 省略。

        任一引用解析失败都返回 ``{ok: False, status: 400, code: "attachment_invalid"}``，不发送目标请求。
        """
        problem = unsafe_path(path)
        if problem:
            return {"ok": False, "status": 400, "code": "invalid_path", "error": problem}
        try:
            fields = {k: _form_value(v) for k, v in (self._deref(data or {})).items() if v is not None}
        except SecretError as exc:
            return {"ok": False, "status": 400, "error": str(exc)}
        resolved: list[tuple[str, AttachmentFile]] = []
        try:
            for field, refs in files.items():
                for ref in refs if isinstance(refs, list) else [refs]:
                    resolved.append((field, await self._attachment(ref)))
        except AttachmentRefError as exc:
            return {"ok": False, "status": 400, "code": "attachment_invalid", "error": str(exc)}
        with contextlib.ExitStack() as stack:
            parts = []
            for field, (filename, content, ctype) in resolved:
                if isinstance(content, Path):
                    body = stack.enter_context(await asyncio.to_thread(content.open, "rb"))
                else:
                    body = content
                parts.append((field, (filename, body, ctype)))
            response = await self._send(
                method,
                path,
                files=parts,
                data=fields,
                params={k: v for k, v in (params or {}).items() if v is not None},
            )
        return _normalize(response)

    async def fetch_bytes(self, path: str) -> tuple[bytes, str]:
        """GET 二进制内容，返回 ``(内容, content-type)``；非 2xx 抛 ``LoopbackError``。"""
        problem = unsafe_path(path, allow_query=True)
        if problem:
            raise LoopbackError(400, problem)
        response = await self.http.get(path)
        if not response.is_success:
            try:
                payload = response.json() if response.content else None
            except ValueError:
                payload = response.text
            raise LoopbackError(response.status_code, error_text(payload) or f"HTTP {response.status_code}")
        return response.content, response.headers.get("content-type", "application/octet-stream")

    async def get(self, path: str, **params) -> dict:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, body: Any = None, **params) -> dict:
        return await self.request("POST", path, json=body if body is not None else {}, params=params)

    async def patch(self, path: str, body: Any) -> dict:
        return await self.request("PATCH", path, json=body)

    async def put(self, path: str, body: Any) -> dict:
        return await self.request("PUT", path, json=body)

    async def delete(self, path: str, **params) -> dict:
        return await self.request("DELETE", path, params=params or None)

    async def aclose(self):
        await self.http.aclose()
