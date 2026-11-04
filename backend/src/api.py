from __future__ import annotations

import json
import os
import time
from functools import cache
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
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
from mindex.shared.backends import open_context
from mindex.shared.config import Config, load_config
from mindex.shared.shot_media import file_response as FileResponse
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
    topic_llm=None,
    scheduler: bool = False,
) -> FastAPI:
    """``topic_llm`` 供测试注入选题挖掘的脚本化模型；``scheduler`` 为真时随应用启停每日选题调度器
    （``mindex serve`` 按 ``topics.scheduler.enabled`` 传入，测试与内嵌应用缺省关闭）。"""
    config = config or load_config()
    # 运行后端（db/media/locks/vectors/queue）经 BackendContext 构造：阶段 0 恒为
    # local 形态，store 即 metadata.sqlite3 的 Store；集群形态（阶段 1 起）由此切换。
    backends = open_context(config)
    store = backends.store
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
            from mindex.shared.analysis_translations import display_metadata

            data = display_metadata(add_message_metadata(json.loads(payload)), store.root)
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
    from mindex.admin.groups import groups_router
    app.include_router(groups_router(store))
    app.include_router(admin_router(store, runtime))
    app.include_router(settings_router(runtime, store))
    app.include_router(studio_router(
        runtime, store, embedder=embedder, storyboarder=storyboarder, speaker=speaker, writer=writer,
        video_generator=video_generator, animator=animator, animation_capture=animation_capture,
        image_generator=image_generator, music_generator=music_generator,
    ))
    from mindex.topics.api import topics_router

    topics = topics_router(runtime, store, embedder, topic_llm=topic_llm, scheduler=scheduler)
    app.include_router(topics)
    app.state.topic_scheduler = None
    if scheduler:
        from mindex.topics.scheduler import install as install_topic_scheduler

        install_topic_scheduler(app, store, topics.topic_runs, config)

    from mindex.agent.api import agent_router, build_runtime

    # 智能助手：同一工具注册表经回环调用上面的 REST 路由；agent_llm 供测试注入脚本化模型。
    # cluster 形态的业务 API 不建独立会话运行时：/api/agent 固定由单实例
    # agent-gateway 提供（plans §9；网关故障时助手明确不可用，不假数据降级）。
    if config.server.role == "api" and backends.form == "cluster":
        from fastapi import APIRouter as _APIRouter
        from fastapi.responses import JSONResponse as _JSON

        def _agent_redirect(request: Request):
            gateway = config.agent.gateway_url or "(未配置 agent.gateway_url)"
            return _JSON(status_code=503, content={
                "detail": f"智能助手由单实例 agent-gateway 提供：{gateway}",
                "gateway_url": config.agent.gateway_url})

        stub = _APIRouter(prefix="/api/agent")
        stub.add_api_route("/{path:path}", _agent_redirect,
                           methods=["GET", "POST", "PUT", "DELETE"], include_in_schema=False)
        app.include_router(stub)
    else:
        app.state.agent_runtime = build_runtime(app, runtime, store, agent_llm=agent_llm)
        app.include_router(agent_router(app.state.agent_runtime, runtime, injected=agent_llm is not None))
    app.state.runtime_config, app.state.store, app.state.backends = runtime, store, backends
    # /mcp（streamable-http）与独立 `mindex mcp` 一样都依赖本进程的会话运行时：
    # cluster 业务 API 上同样只由 agent-gateway 提供（plans §9）。
    if config.agent.mcp.http_enabled and not (config.server.role == "api" and backends.form == "cluster"):
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

    # 主页（/）是创作页；视频检索移到 /search，旧的 /index.html 由 moved 重定向过去。
    register_page("search", "index.html", ("/search", "/search/", "/search.html"))
    for group in ("admin", "persons", "settings"):
        register_page(group, f"{group}.html", (f"/{group}", f"/{group}/", f"/{group}.html"))
    # 创作页同时挂在 / 与 /studio：前者是主页，后者是站内固定链接。
    register_page("studio", "studio.html", ("/", "/studio", "/studio/", "/studio.html"))
    # 文案模板管理是创作页的子页面（与创作任务并列的分区标签），资源仍在 /assets/studio/ 下。
    register_page("studio", "templates.html", ("/studio/templates", "/studio/templates/"))
    # 选题挖掘同为创作页子页面（标签位于创作任务与文案模板之间）。
    register_page("studio", "topics.html", ("/studio/topics", "/studio/topics/"))
    register_page("studio", "delivery-templates.html", ("/studio/delivery-templates", "/studio/delivery-templates/"))
    register_page("studio", "brands.html", ("/studio/brands", "/studio/brands/"))
    # 音乐库是素材管理页的子页面（与素材并列的分区标签）；第三方工具是设置页的一个配置组。
    register_page("admin", "groups.html", ("/admin/groups", "/admin/groups/"))
    register_page("admin", "music.html", ("/admin/music", "/admin/music/"))

    def moved(target, rename=None):
        """旧地址（创作页的音乐库、第三方工具标签）重定向到新位置，兼容书签与旧链接。

        保留查询参数（按 rename 改名），丢弃只用于返回创作任务的 job 参数。
        """
        def redirect(request: Request):
            params = {(rename or {}).get(key, key): value for key, value in request.query_params.items() if key != "job"}
            path, _, fragment = target.partition("#")
            url = path + (f"?{urlencode(params)}" if params else "") + (f"#{fragment}" if fragment else "")
            return RedirectResponse(url, status_code=308)
        return redirect

    for url in ("/studio/music", "/studio/music/"):
        app.add_api_route(url, moved("/admin/music"), methods=["GET"], include_in_schema=False)
    for url in ("/studio/tools", "/studio/tools/"):
        app.add_api_route(url, moved("/settings#tools", {"id": "tool"}), methods=["GET"], include_in_schema=False)
    # 视频检索页改址到 /search：旧的 /index.html 重定向过去，保留查询参数。
    app.add_api_route("/index.html", moved("/search"), methods=["GET"], include_in_schema=False)
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
        payload = {"version": app_version(), "pid": os.getpid(), "data_dir": str(config.data_dir),
                   "busy": bool(tasks), "tasks": tasks}
        backends = getattr(app.state, "backends", None)
        if backends is not None:  # 部署形态概览（plans §12 监控；doctor 提供完整诊断）
            payload["backends"] = {"form": backends.form, "database": backends.db.backend.backend,
                                   "vectors": backends.vectors.backend,
                                   "storage": backends.media.backend,
                                   "locks": backends.locks.backend,
                                   "queue": backends.queue.backend}
        return payload

    # 设置 MINDEX_WEB_TOKEN（或传入 web_token）后才启用访问令牌；未设置时行为不变。
    install_web_auth(app, web_token)

    return app
