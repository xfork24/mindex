from __future__ import annotations

import json
import os
import time
from functools import cache

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from mindex.admin.admin import admin_router
from mindex.admin.curation import EntityNotFound
from mindex.models.adapters import (
    Animator,
    ModelError,
    MultimodalEmbedder,
    Speaker,
    Storyboarder,
    VideoGenerator,
    Writer,
)
from mindex.persons import profiling
from mindex.search.api import search_router
from mindex.settings.runtime_settings import RuntimeConfig
from mindex.settings.settings_api import settings_router
from mindex.shared.config import Config, load_config
from mindex.shared.shot_media import file_response as FileResponse
from mindex.shared.storage import Store
from mindex.shared.web_assets import frontend_root
from mindex.shared.web_auth import install_web_auth
from mindex.studio.studio_api import studio_router


@cache
def app_version() -> str:
    """已安装包的版本号（与 pyproject 一致；桌面外壳据此确认连到的是同版本后端）。"""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("mindex")
    except PackageNotFoundError:  # pragma: no cover - 只在未安装的源码树中出现
        return "0+unknown"


def create_app(
    config: Config | None = None,
    embedder: MultimodalEmbedder | None = None,
    *,
    index_id: str | None = None,
    storyboarder: Storyboarder | None = None,
    speaker: Speaker | None = None,
    writer: Writer | None = None,
    video_generator: VideoGenerator | None = None,
    animator: Animator | None = None,
    animation_capture=None,
    image_generator=None,
    music_generator=None,
    web_token: str | None = None,
    agent_llm=None,
) -> FastAPI:
    config = config or load_config()
    store = Store(config.data_dir)
    runtime = RuntimeConfig(config, store)  # 设置页保存后经覆盖层热生效，无需重启
    app = FastAPI(title="Mindex 视频素材检索", version="0.1.0")
    from mindex.agent.loopback import ensure_internal_token

    ensure_internal_token(app)  # 智能助手回环请求的进程内凭证，须在认证中间件生效前就绪

    @app.middleware("http")
    async def localized_messages(request, call_next):
        # Additive metadata on JSON API responses; HTTP status and diagnostics stay intact.
        response = await call_next(request)
        if not request.url.path.startswith("/api/") or "application/json" not in response.headers.get("content-type", ""):
            return response
        from mindex.shared.i18n import add_message_metadata

        rewrite_started = time.perf_counter()
        payload = b"".join([chunk async for chunk in response.body_iterator])
        try:
            data = add_message_metadata(json.loads(payload))
        except (ValueError, UnicodeDecodeError):
            from starlette.responses import Response
            return Response(payload, status_code=response.status_code, headers=dict(response.headers))
        headers = {key: value for key, value in response.headers.items() if key != "content-length"}
        result = JSONResponse(data, status_code=response.status_code, headers=headers, background=response.background)
        if profiling.enabled() and request.url.path.startswith("/api/persons/"):
            # 仅在人物库性能分析开关打开时追加：中间件整包解析并重写 JSON 的耗时与体积。
            timing = f'middleware;dur={(time.perf_counter() - rewrite_started) * 1000:.2f};desc="bytes={len(payload)}"'
            existing = result.headers.get("server-timing")
            result.headers["Server-Timing"] = f"{existing}, {timing}" if existing else timing
        return result

    if index_id and store.index_version(index_id)["status"] != "ready":
        raise ValueError("固定预览只允许就绪索引")
    from mindex.admin.dialogue_tasks import dialogue_router
    from mindex.admin.indexes import indexes_router
    from mindex.admin.source_tasks import source_tasks_router
    from mindex.persons.person_api import person_router

    app.include_router(person_router(runtime, store))
    app.include_router(dialogue_router(runtime, store, embedder))
    app.include_router(source_tasks_router(runtime, store, embedder))
    app.include_router(indexes_router(runtime, store, embedder))
    app.include_router(admin_router(store, runtime))
    app.include_router(settings_router(runtime, store))
    app.include_router(studio_router(
        runtime, store, embedder=embedder, storyboarder=storyboarder, speaker=speaker, writer=writer,
        video_generator=video_generator, animator=animator, animation_capture=animation_capture,
        image_generator=image_generator, music_generator=music_generator,
    ))

    from mindex.agent.api import agent_router, build_runtime

    # 智能助手：同一工具注册表经回环调用上面的 REST 路由；agent_llm 供测试注入脚本化模型。
    app.state.agent_runtime = build_runtime(app, runtime, store, agent_llm=agent_llm)
    app.state.runtime_config, app.state.store = runtime, store
    app.include_router(agent_router(app.state.agent_runtime, runtime, injected=agent_llm is not None))
    if config.agent.mcp.http_enabled:
        try:
            from mindex.agent.mcp_server import install_http
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确指引
            raise RuntimeError("开启 agent.mcp.http_enabled 需要安装 MCP 依赖：uv sync --extra mcp") from exc
        install_http(app, runtime, store)

    @app.exception_handler(EntityNotFound)
    async def entity_not_found(_, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    assets_root = frontend_root()

    def register_page(group, filename, urls):
        def page():
            return FileResponse(assets_root / group / filename, media_type="text/html")
        for url in urls:
            app.add_api_route(url, page, methods=["GET"], include_in_schema=False)

    register_page("search", "index.html", ("/", "/index.html"))
    for group in ("admin", "persons", "studio", "settings"):
        register_page(group, f"{group}.html", (f"/{group}", f"/{group}/", f"/{group}.html"))
    # 文案模板管理是创作页的子页面（与创作任务并列的分区标签），资源仍在 /assets/studio/ 下。
    register_page("studio", "templates.html", ("/studio/templates", "/studio/templates/"))
    for group in ("search", "admin", "persons", "studio", "settings", "shared"):
        app.mount(f"/assets/{group}", StaticFiles(directory=assets_root / group), name=f"assets-{group}")

    @app.exception_handler(ModelError)
    async def model_error(_, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def value_error(_, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    app.include_router(search_router(config, runtime, store, embedder, index_id=index_id))

    @app.get("/api/health")
    def health():
        """轻量就绪检查（桌面外壳轮询用）：不读素材表、不探测模型服务；随 web_auth 一起受令牌保护。"""
        from mindex.shared.background_tasks import busy_categories
        from mindex.studio.executor import busy_executors

        tasks = (["studio"] if busy_executors(config.data_dir) else []) + busy_categories(config.data_dir)
        return {"version": app_version(), "pid": os.getpid(), "data_dir": str(config.data_dir),
                "busy": bool(tasks), "tasks": tasks}

    # 设置 MINDEX_WEB_TOKEN（或传入 web_token）后才启用访问令牌；未设置时行为不变。
    install_web_auth(app, web_token)

    return app
