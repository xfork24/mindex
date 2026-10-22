from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from mindex.admin.admin import admin_router
from mindex.admin.curation import EntityNotFound
from mindex.models.adapters import ModelError, MultimodalEmbedder, Speaker, Storyboarder
from mindex.search.api import search_router
from mindex.settings.runtime_settings import RuntimeConfig
from mindex.settings.settings_api import settings_router
from mindex.shared.config import Config, load_config
from mindex.shared.shot_media import file_response as FileResponse
from mindex.shared.storage import Store
from mindex.shared.web_assets import frontend_root
from mindex.shared.web_auth import install_web_auth
from mindex.studio.studio_api import studio_router


def create_app(
    config: Config | None = None,
    embedder: MultimodalEmbedder | None = None,
    *,
    index_id: str | None = None,
    storyboarder: Storyboarder | None = None,
    speaker: Speaker | None = None,
    web_token: str | None = None,
) -> FastAPI:
    config = config or load_config()
    store = Store(config.data_dir)
    runtime = RuntimeConfig(config, store)  # 设置页保存后经覆盖层热生效，无需重启
    app = FastAPI(title="Mindex 视频素材检索", version="0.1.0")

    @app.middleware("http")
    async def localized_messages(request, call_next):
        # Additive metadata on JSON API responses; HTTP status and diagnostics stay intact.
        response = await call_next(request)
        if not request.url.path.startswith("/api/") or "application/json" not in response.headers.get("content-type", ""):
            return response
        from mindex.shared.i18n import add_message_metadata

        payload = b"".join([chunk async for chunk in response.body_iterator])
        try:
            data = add_message_metadata(json.loads(payload))
        except (ValueError, UnicodeDecodeError):
            from starlette.responses import Response
            return Response(payload, status_code=response.status_code, headers=dict(response.headers))
        headers = {key: value for key, value in response.headers.items() if key != "content-length"}
        return JSONResponse(data, status_code=response.status_code, headers=headers, background=response.background)

    if index_id and store.index_version(index_id)["status"] != "ready":
        raise ValueError("固定预览只允许就绪索引")
    from mindex.admin.dialogue_tasks import dialogue_router
    from mindex.admin.source_tasks import source_tasks_router
    from mindex.persons.person_api import person_router

    app.include_router(person_router(runtime, store))
    app.include_router(dialogue_router(runtime, store, embedder))
    app.include_router(source_tasks_router(runtime, store, embedder))
    app.include_router(admin_router(store, runtime))
    app.include_router(settings_router(runtime, store))
    app.include_router(studio_router(runtime, store, embedder=embedder, storyboarder=storyboarder, speaker=speaker))

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
    for group in ("search", "admin", "persons", "studio", "settings", "shared"):
        app.mount(f"/assets/{group}", StaticFiles(directory=assets_root / group), name=f"assets-{group}")

    @app.exception_handler(ModelError)
    async def model_error(_, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def value_error(_, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    app.include_router(search_router(config, runtime, store, embedder, index_id=index_id))

    # 设置 MINDEX_WEB_TOKEN（或传入 web_token）后才启用访问令牌；未设置时行为不变。
    install_web_auth(app, web_token)

    return app
