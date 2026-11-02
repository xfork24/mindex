"""Managed official SDK subprocess; all Mindex capabilities go through the shared tool channel."""

from __future__ import annotations

import asyncio
import base64
import importlib.metadata
import os
import sys
import threading

from mindex.agent.dsh_bridge import bridge
from mindex.agent.prompts import system_prompt
from mindex.agent.runtime import RuntimeSupport, page_domain

DSH_VERSION = "0.2.1rc2"
DISABLED = (
    "llm-deepseek",
    "tool-fs",
    "tool-working-directory",
    "persistent-bash",
    "persistent-pwsh",
    "terminal-bash",
    "terminal-pwsh",
    "session-title",
    "mcp-resources",
)


def readiness(settings):
    errors = []
    frozen = bool(getattr(sys, "frozen", False))
    install = "请更新或重新安装 Mindex 桌面版；如仍异常，请反馈安装包问题" if frozen else "uv sync --extra dsh"
    repair = install if frozen else f"运行 {install}"
    for package in ("deepseek-harness-sdk", "deepseek-harness-runtime-bin", "mcp"):
        try:
            version = importlib.metadata.version(package)
            if package != "mcp" and version != DSH_VERSION:
                errors.append(f"{package} 需要 {DSH_VERSION}（当前 {version}）；{repair}")
        except importlib.metadata.PackageNotFoundError:
            errors.append(f"缺少 {package}；{repair}")
    if not errors:
        try:
            from deepseek_harness_runtime import resolve_bundled_launch_args

            resolve_bundled_launch_args("exe" if frozen else None)
        except (ImportError, OSError, ValueError):
            errors.append(f"dsh 运行时文件不完整；{repair}")
    if not settings.llm.model.strip() or not settings.llm.base_url.startswith(("http://", "https://")):
        errors.append("请先配置智能助手模型和服务地址")
    return {"ready": not errors, "errors": errors, "install": install, "version": DSH_VERSION}


def patch(settings, url):
    llm = settings.llm
    base_url = llm.base_url.rstrip("/")
    # The Anthropic SDK appends /v1; Mindex accepts both a root and a /v1 base.
    if llm.backend == "anthropic" and base_url.endswith("/v1"):
        base_url = base_url[:-3]
    return [{"id": item, "disabled": True} for item in DISABLED] + [
        {
            "insert": [
                {
                    "id": "mindex-llm",
                    "name": "@deepseek-ai/dsh-llm-pi-ai",
                    "config": {
                        "providers": {
                            "mindex": {
                                "apiKeyEnv": "MINDEX_DSH_MODEL_KEY",
                                "baseURL": base_url,
                                "api": "anthropic-messages"
                                if llm.backend == "anthropic"
                                else "openai-completions",
                                "models": [
                                    {
                                        "id": llm.model,
                                        "input": ["text", "image"] if llm.vision else ["text"],
                                        # 总容量与单次输出预算分开；兼容未配置容量的旧档案。
                                        "contextWindow": llm.context_window or settings.max_context_tokens,
                                        "maxTokens": llm.max_tokens,
                                    }
                                ],
                                "retryPolicy": {"mode": "normal", "maxRetries": 0},
                            }
                        }
                    },
                },
                {
                    "id": "mindex-tools",
                    "name": "@deepseek-ai/dsh-mcp-client",
                    "config": {
                        "serverName": "mindex",
                        "transport": "streamable-http",
                        "url": url,
                        "headers": {"Authorization": "__TOKEN__"},
                        "toolCallTimeoutMs": 86400000,
                    },
                },
            ]
        }
    ]


def safe_event(value, secrets=()):
    """Never persist model-bound image bytes in Mindex's journal."""
    if isinstance(value, dict):
        if value.get("type") == "image":
            return {k: v for k, v in value.items() if k not in ("data", "source", "base64")}
        return {k: safe_event(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_event(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
    return value


class DshRuntime(RuntimeSupport):
    @staticmethod
    def _bundled_executable():
        if getattr(sys, "frozen", False):
            from deepseek_harness_runtime import resolve_bundled_launch_args

            return resolve_bundled_launch_args("exe")[0]
        return None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.clients = {}
        self.bridge_results = {}
        self.used_calls = set()

    def _result(self, sid, turn, step, call_id, text, *, error=None, meta=None, images=None):
        from mindex.agent.llm.types import tool_message

        self.bridge_results[(sid, call_id)] = {
            "turn": turn,
            "step": step,
            "message": tool_message(call_id, text, is_error=error is not None, images=images),
            **({"error": error} if error else {}),
            **({"meta": meta} if meta else {}),
        }

    def _ensure_turn(self, sid, previous, message):
        if len(self.store.events(sid, types=("turn/start",))) == previous:
            self._append(sid, "turn/start", {"turn": self.store.last_turn(sid) + 1})
            self._append(sid, "user/message", message)

    def _finish_interrupted_tools(self, sid):
        from mindex.agent.llm.types import tool_message

        turn = self.store.last_turn(sid)
        events = [
            e for e in self.store.events(sid) if isinstance(e["data"], dict) and e["data"].get("turn") == turn
        ]
        completed = {e["data"]["message"]["toolCallId"] for e in events if e["type"] == "tool/result"}
        for event in events:
            data = event["data"]
            if event["type"] == "tool/call" and data["callId"] not in completed:
                self._append(
                    sid,
                    "tool/result",
                    {
                        "turn": turn,
                        "step": data.get("step", 0),
                        "message": tool_message(
                            data["callId"], "Error: dsh 轮次已中断；不会自动重试操作", is_error=True
                        ),
                        "error": {"code": "interrupted", "name": "Interrupted"},
                    },
                )
                completed.add(data["callId"])

    async def _native_request(self, sid, client, request):
        if request.method == "approval/request":
            from uuid import uuid4

            rid = f"approval-{uuid4().hex}"
            data = {
                "requestId": rid,
                "callId": request.payload.get("callId", ""),
                "name": request.payload.get("toolName", "dsh"),
                "risk": "danger",
                "arguments": request.payload.get("arguments", "{}"),
                "allowGrant": False,
                "preview": {"summary": request.payload.get("reason", "dsh 请求确认")},
            }
            self._append(sid, "plugin:mindex/approval-asked", data)
            answer = await self._wait(sid, rid, {"kind": "approval", **data})
            outcome = "allowed-once" if answer["outcome"] == "allowed-once" else "rejected"
            client.respond(request.id, {"outcome": outcome})
            self._append(sid, "plugin:mindex/approval-decided", {"requestId": rid, **answer})
        elif request.method == "user-question/ask":
            answer = await self.ask_question(
                sid, request.payload.get("callId", ""), request.payload.get("questions", [])
            )
            client.respond(request.id, answer)
        else:
            client.respond_error(request.id, code=-32601, message="Unsupported client request")

    async def _run_turn(self, sid, message, context):
        settings = self.settings()
        check = readiness(settings)
        if not check["ready"]:
            self._append(sid, "turn/start", {"turn": self.store.last_turn(sid) + 1})
            self._append(sid, "user/message", message)
            self._append(
                sid,
                "turn/end",
                {
                    "turn": self.store.last_turn(sid),
                    "reason": {
                        "kind": "error",
                        "error": {"code": "dsh_unavailable", "message": "；".join(check["errors"])},
                    },
                },
            )
            return
        self._set_status(sid, "running")
        page = context or self.store.get(sid)["context"] or {}
        if page_domain(page):
            self._touch_domains(sid, [page_domain(page)])
        context_message = self._context_message(sid, context)
        home = self.store.store.path.parent / "dsh" / sid
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        resumed = (home / "initialized").exists()
        before = len(self.store.events(sid, types=("turn/start",)))
        client = None
        worker = None
        requests_worker = None
        handlers = set()
        try:
            async with bridge(self, sid) as (url, token):
                from deepseek_harness import HarnessClient, HarnessConfig

                content = patch(settings, url)
                # Secrets live only in the child environment; YAML carries an expression.
                import yaml

                text = yaml.safe_dump(content, allow_unicode=True).replace(
                    "__TOKEN__", "!!js process.env.MINDEX_DSH_BRIDGE_AUTH"
                )
                config = home / "managed.patch.yml"
                config.write_text(text)
                config.chmod(0o600)
                env = {
                    "MINDEX_DSH_BRIDGE_AUTH": "Bearer " + token,
                    "MINDEX_DSH_MODEL_KEY": settings.llm.api_key
                    or os.environ.get(settings.llm.api_key_env, ""),
                    "DSH_SYSTEM_PROMPT": system_prompt(
                        page, self.loaded_domains(sid), self.registry.domain_tools()
                    ),
                    "DSH_TELEMETRY_DISABLED": "1",
                }
                client = HarnessClient(
                    HarnessConfig(
                        dsh_bin=self._bundled_executable(),
                        dsh_home=str(home),
                        cwd=str(home),
                        profile="sdk-minimal",
                        patches=(str(config),),
                        env=env,
                        request_timeout_seconds=30,
                    )
                )
                self.clients[sid] = client
                queue = asyncio.Queue()
                loop = asyncio.get_running_loop()
                blocks = [
                    {"type": "text", "text": b["text"]} for b in message["content"] if b["type"] == "text"
                ]
                if context_message:
                    blocks.insert(0, {"type": "text", "text": context_message["content"][0]["text"]})
                if settings.llm.vision:
                    for block in (
                        [b for b in message["content"] if b["type"] == "image"][-settings.max_images :]
                        if settings.max_images
                        else []
                    ):
                        from mindex.agent.vision import encode_jpeg

                        raw = encode_jpeg(await self._image_loader(sid)(block["ref"]))
                        blocks.append(
                            {
                                "type": "image",
                                "data": base64.b64encode(raw).decode(),
                                "mimeType": "image/jpeg",
                            }
                        )

                ready = threading.Event()
                transport_ready = False

                def run():
                    nonlocal transport_ready
                    try:
                        client.start()
                        transport_ready = True
                        ready.set()
                        from deepseek_harness.models import InitializeResponse

                        client.request(
                            "initialize",
                            {
                                "cwd": str(home),
                                "provider": "mindex",
                                "model": settings.llm.model,
                                "maxTokens": settings.llm.max_tokens,
                                "approvalRequests": True,
                                "capabilities": {"userQuestions": True},
                            },
                            response_model=InitializeResponse,
                            timeout_seconds=30,
                        )
                        if resumed:
                            client.resume_session(sid)
                        with client.subscribe_session_notifications(sid) as subscription:
                            client.session_prompt(sid, blocks, notification_subscription=subscription)
                            (home / "initialized").touch()
                            active = False
                            while True:
                                note = subscription.next()
                                loop.call_soon_threadsafe(queue.put_nowait, note)
                                if note.method == "session.event":
                                    active = True
                                if (
                                    active
                                    and note.method == "session.status"
                                    and note.payload.get("status") == "idle"
                                ):
                                    break
                    except Exception as exc:  # noqa: BLE001 — propagate SDK failures to the event loop
                        loop.call_soon_threadsafe(queue.put_nowait, exc)
                    finally:
                        ready.set()
                        loop.call_soon_threadsafe(queue.put_nowait, None)

                def requests():
                    ready.wait()
                    if not transport_ready:
                        return
                    try:
                        while True:
                            request = client.next_request()
                            loop.call_soon_threadsafe(queue.put_nowait, request)
                    except Exception:  # noqa: BLE001 — close releases the SDK request reader
                        pass

                requests_worker = asyncio.create_task(asyncio.to_thread(requests))
                worker = asyncio.create_task(asyncio.to_thread(run))
                while True:
                    note = await queue.get()
                    if note is None:
                        break
                    if isinstance(note, Exception):
                        raise note
                    from deepseek_harness.models import IncomingRequest

                    if isinstance(note, IncomingRequest):
                        handler = asyncio.create_task(self._native_request(sid, client, note))
                        handlers.add(handler)
                        continue
                    if note.method == "session.event":
                        event = note.payload["event"]
                        upstream = event["seq"]
                        data = safe_event(event["data"], (env["MINDEX_DSH_MODEL_KEY"], token))
                        # dsh 的工作目录快照也是 user/message，必须保留 source，避免重复显示用户原话。
                        if event["type"] == "user/message" and data.get("source", {}).get("kind") == "user":
                            data = message  # references and attachment metadata stay in Mindex's format
                        if event["type"] == "tool/result":
                            details = self.bridge_results.pop((sid, data["message"]["toolCallId"]), None)
                            if details:
                                data = {
                                    **data,
                                    **{k: v for k, v in details.items() if k in ("meta", "error")},
                                }
                                if details["message"].get("images"):
                                    data["message"]["images"] = details["message"]["images"]
                        stored = self.store.append(
                            sid,
                            event["type"],
                            data,
                            ignorable=event.get("ignorable", False),
                            upstream_seq=upstream,
                        )
                        if stored:
                            self._publish(sid, {"event": "session.event", "data": stored})
                    elif note.method == "assistant.stream":
                        self._publish(
                            sid,
                            {
                                "event": note.method,
                                "data": safe_event(note.payload, (env["MINDEX_DSH_MODEL_KEY"], token)),
                            },
                        )
                await worker
        except asyncio.CancelledError:
            self._ensure_turn(sid, before, message)
            self._close_pending(sid, "cancelled")
            self._finish_interrupted_tools(sid)
            self._append(sid, "turn/end", {"turn": self.store.last_turn(sid), "reason": {"kind": "aborted"}})
            raise
        except Exception:  # noqa: BLE001 — transport errors must terminate the turn
            self._ensure_turn(sid, before, message)
            # SDK diagnostics may include credentials/transport headers. Never expose them.
            self._close_pending(sid, "cancelled")
            self._finish_interrupted_tools(sid)
            self._append(
                sid,
                "turn/end",
                {
                    "turn": self.store.last_turn(sid),
                    "reason": {
                        "kind": "error",
                        "error": {
                            "code": "dsh_failed",
                            "message": "dsh 进程或连接异常，当前轮次已结束；操作不会自动重试。请检查依赖和模型配置。",
                        },
                    },
                },
            )
        finally:
            if client:
                await asyncio.to_thread(client.close)
            if worker:
                await asyncio.gather(worker, return_exceptions=True)
            if requests_worker:
                await asyncio.gather(requests_worker, return_exceptions=True)
            for handler in handlers:
                handler.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
            for key in list(self.bridge_results):
                if key[0] == sid:
                    self.bridge_results.pop(key)
            self.used_calls = {key for key in self.used_calls if key[0] != sid}
            self.clients.pop(sid, None)
