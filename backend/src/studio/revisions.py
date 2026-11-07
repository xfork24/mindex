"""共享 Studio 人工修改协议：目录修订前置条件与单次请求原子发布。"""

from __future__ import annotations

import re

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from shared.directory_files import Snapshot, bind
from shared.files import FileRevisionConflict

JOB = re.compile(r"^/api/studio/jobs/([^/]+)(/.*)?$")
# 这些 POST 使用自己的输入版本/预检 token，或不修改工作区。
EXECUTION = re.compile(
    r"/(build|nodes|execute-column|regenerate-column|run|run/[^/]+|steps/[^/]+|cancel|preflight|query-suggestion|lyrics|resolve)$"
)


def install(app, config, store):
    if config.queue.backend != "postgres" or config.storage.backend != "s3":
        return

    @app.middleware("http")
    async def revisions(request, call_next):
        match = JOB.match(request.url.path)
        if match is None:
            return await call_next(request)
        job_id, suffix = match.groups()
        prefix = f"studio/{job_id}/"
        modifying = request.method in {"POST", "PUT", "PATCH", "DELETE"} and not EXECUTION.search(
            suffix or ""
        )
        if modifying:
            expected = request.headers.get("if-match", "").strip().strip('"')
            if not expected:
                return JSONResponse(
                    {"detail": "共享工作区修改需要 If-Match 修订，请先读取任务"}, status_code=428
                )
            with bind(store, prefix, deferred=True) as view:
                if view.revision != expected:
                    return JSONResponse({"detail": "工作区已被其他操作修改，请刷新后重试"}, status_code=409)
                request.state.directory_revision_checked = True
                response = await call_next(request)
                if response.status_code < 400:
                    view.deferred = False
                    try:
                        await run_in_threadpool(view.publish)
                    except FileRevisionConflict as exc:
                        return JSONResponse({"detail": str(exc)}, status_code=409)
        else:
            with bind(store, prefix):
                response = await call_next(request)
        latest = await run_in_threadpool(Snapshot, store, prefix)
        if latest.revision:
            response.headers["X-Studio-Revision"] = latest.revision
        return response
