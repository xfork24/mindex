"""调用第三方工具阶段（逐镜，tool 来源的画面阶段）：按工具源登记调用 HTTP 或 MCP 工具，下载并校验产物。

- 签名：工具源 ID、工具配置摘要（``registry.tool_hash``：协议、连接、产物类型与上传许可，不含密钥值）、
工具名、
  参数（模板替换后）、上传素材摘要与产物类型；
- 预检阻塞：工具源未登记、工具不在白名单、产物类型不符、携带素材但未允许上传、缺密钥、缺 mcp 组件、
上传素材缺失；
- 产物：``versions/<vid>/artifact.mp4``（视频，之后在产物内选窗对齐）或 ``artifact.png``（图片，
直接按运动参数渲染），
  以及 ``request.json``（参数、任务记录与产物来源主机，无密钥）。产物经 ffprobe
或图片解码校验后登记为不可变版本文件。
"""

from __future__ import annotations

import base64
import mimetypes
from urllib.parse import urlsplit

from studio import studio
from studio.artifacts import sanitized
from studio.logs import active_log
from studio.workspace import sources
from studio.workspace.stages import StageHandler, register, shot_content
from studio.workspace.versions import file_info

INVOKE_VERSION = 1
PLACEHOLDERS = ("text", "visual", "query")


def tool_source(w, p, sid) -> dict:
    source = sources.plan_source(w, p, sid) or {}
    if source.get("kind") != "tool":
        raise studio.StudioError("该分镜的画面来源不是第三方工具")
    return source


def arguments(w, p, sid) -> dict:
    """分镜工具参数：字符串值中的 ``{{text}}``/``{{visual}}``/``{{query}}`` 替换为本镜内容。"""
    _, content = shot_content(w, p, sid)
    values = {key: str(content.get(key) or "") for key in PLACEHOLDERS}

    def fill(value):
        if isinstance(value, str):
            for key, text in values.items():
                value = value.replace("{{" + key + "}}", text)
            return value
        if isinstance(value, list):
            return [fill(item) for item in value]
        if isinstance(value, dict):
            return {key: fill(item) for key, item in value.items()}
        return value

    return fill(tool_source(w, p, sid).get("arguments") or {})


def upload_payload(config, job_id, source) -> list[dict]:
    """随调用上传的素材（复验摘要）：``{name, mime, sha256, base64}``。"""
    found = []
    for item in source.get("uploads") or []:
        path = sources.reference_file(config, job_id, item["kind"], item["media_id"])
        found.append(
            {
                "name": path.name,
                "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "sha256": item["media_id"],
                "base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        )
    return found


class InvokeHandler(StageHandler):
    name, label, scope = "invoke", "调用第三方工具", "shot"

    def settings(self, config, p, *, store=None, display=False):
        return {}

    def specification(self, config, job_id, w, p, sid, params, resources):
        from admin.curation import EntityNotFound
        from studio.tools import registry

        selected, _ = shot_content(w, p, sid)
        source = tool_source(w, p, sid)
        try:
            record = registry.get_source(
                resources.database() if resources else _store(config), source["tool_id"]
            )
            tool_hash = registry.tool_hash(record, source["tool"])
        except (EntityNotFound, studio.StudioError):
            tool_hash = None  # 未登记或不在白名单：预检阻塞，签名照常可算
        payload = {
            "tool_id": source["tool_id"],
            "tool": source["tool"],
            "tool_hash": tool_hash,
            "arguments": arguments(w, p, sid),
            "uploads": [[item["kind"], item["media_id"]] for item in source.get("uploads") or []],
            "output": source.get("output", "video"),
            "invoke_version": INVOKE_VERSION,
        }
        return payload, {"content": selected["content"]}, params

    def dependencies(self, w, p, sid, *, resplit=False):
        return []

    def preflight(self, config, job_id, w, p, sid, spec, found, node):
        if found:
            return
        from studio.tools import registry

        source = tool_source(w, p, sid)
        reason = registry.blocker(_store(config), source)
        if reason is None:
            reason = sources.missing_reference(config, job_id, {"references": source.get("uploads") or []})
        if reason:
            node.update(action="blocked", reason=reason)

    def run(self, ctx):
        from studio.tools import adapter, artifacts, registry

        store = _store(ctx.config)
        source = tool_source(ctx.w, ctx.p, ctx.sid)
        if reason := registry.blocker(store, source):
            raise studio.StudioError(reason)
        record = registry.get_source(store, source["tool_id"])
        tool = registry.tool_entry(record, source["tool"])
        values = arguments(ctx.w, ctx.p, ctx.sid)
        uploads = upload_payload(ctx.config, ctx.job_id, source)
        log = active_log.get()
        record_path = ctx.directory / "request.json"
        request = {
            "tool_id": record["id"],
            "tool": tool["name"],
            "protocol": record["protocol"],
            "arguments": values,
            "uploads": [{key: item[key] for key in ("name", "mime", "sha256")} for item in uploads],
            "submitted_at": studio.now(),
            "finished_at": None,
        }

        def save(**update):
            request.update(update)
            studio.save(record_path, sanitized(request))

        save()
        if log:
            log.emit(f"正在调用第三方工具 {record['name']} / {tool['name']}")
        cancelled = ctx.services.get("cancelled") or (lambda: False)

        def notify(status):
            if log:
                log.emit(f"第三方工具任务状态：{status}")

        try:
            from shared.external_calls import guard_service

            client = adapter(record)
            if record["protocol"] != "http":
                client = guard_service(client, "tool")
            found, task = client.call(
                tool["name"], values, uploads, timeout=tool["timeout"], cancelled=cancelled, notify=notify
            )
            wanted = next((item for item in found if _matches(item, tool["output"])), found[0])
            raw = artifacts.download(
                wanted,
                ctx.directory / "download.bin",
                hosts=registry.allowed_hosts(record),
                max_bytes=tool["max_mb"] * 1024 * 1024,
                timeout=min(tool["timeout"], 600),
            )
            target, info = artifacts.normalize(
                raw, ctx.directory, tool["output"], ctx.config.video.max_pixels
            )
            raw.unlink(missing_ok=True)
        except Exception as exc:
            save(finished_at=studio.now(), error=str(exc)[:2000])
            raise
        origin = urlsplit(wanted.url).netloc if wanted.url else "inline"
        save(finished_at=studio.now(), task=task, artifact={"origin": origin, "mime": wanted.mime})
        data = {
            "tool_id": record["id"],
            "tool": tool["name"],
            "output": tool["output"],
            "origin": origin,
            **info,
        }
        name = "artifact"
        return data, {
            name: file_info(ctx.config, ctx.job_id, target),
            "request": file_info(ctx.config, ctx.job_id, record_path),
        }


def _matches(item, output: str) -> bool:
    if item.mime:
        return item.mime.startswith(output + "/")
    guessed = mimetypes.guess_type(urlsplit(item.url or "").path)[0] or ""
    return guessed.startswith(output + "/") or not guessed


def _store(config):
    from shared.storage import Store

    return Store.open(config)


register(InvokeHandler())
