"""能力入口矩阵（spec m3-post-production 需求 22）：每项能力在 REST、CLI、agent、MCP 与 Web 页面都有入口。

新增能力时在 ``MATRIX`` 加一行：REST 路由（方法 + 路径模板）、``mindex studio`` 子命令、agent 工具名、
是否经 MCP 导出、页面（HTML 文件与元素 id）。Web 与桌面 App 共用同一套页面与 REST，因此页面入口同时覆盖两端。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from typer.main import get_command

import cli
from agent.mcp_server import exported
from agent.tools import build_registry
from shared.web_assets import frontend_root

# 部分接口为兼容旧地址重复登记，生成 OpenAPI 文档时的重名告警与本测试无关。
pytestmark = pytest.mark.filterwarnings("ignore:Duplicate Operation ID")


@dataclass(frozen=True)
class Capability:
    name: str
    routes: tuple[tuple[str, str], ...]
    commands: tuple[tuple[str, ...], ...]
    tools: tuple[str, ...]
    page: tuple[tuple[str, tuple[str, ...]], ...]  # (页面或脚本文件, 必须出现的元素 id / 标记)
    mcp_hidden: tuple[str, ...] = field(default=())  # 不经 MCP 暴露的工具（需求 18.4）


MATRIX = (
    Capability(
        "studio-basic",
        routes=(
            ("GET", "/api/studio/jobs"),
            ("GET", "/api/studio/jobs/{job_id}/workspace"),
            ("GET", "/api/studio/jobs/{job_id}/plans/{plan_id}/preflight"),
            ("POST", "/api/studio/jobs/{job_id}/plans/{plan_id}/build"),
            ("GET", "/api/studio/jobs/{job_id}/builds/{build_id}/logs"),
            ("GET", "/api/studio/jobs/{job_id}/artifacts/{relative}"),
        ),
        commands=(
            ("studio", "jobs"),
            ("studio", "plans"),
            ("studio", "preflight"),
            ("studio", "build"),
            ("studio", "status"),
            ("studio", "download"),
        ),
        tools=("list_jobs", "get_workspace_summary", "get_preflight", "run_build", "get_build_status"),
        page=(
            (
                "studio/studio.html",
                (
                    'id="job-list"',
                    'id="plan-select"',
                    'id="run-all"',
                    'id="execution-log"',
                    'id="final-player"',
                ),
            ),
        ),
    ),
    Capability(
        "transitions",
        routes=(("PATCH", "/api/studio/jobs/{job_id}/plans/{plan_id}"),),
        commands=(("studio", "shot", "set"), ("studio", "shot", "show")),
        tools=("edit_shots", "get_shot_details"),
        page=(
            ("studio/studio.html", ("/assets/studio/studio-post.js",)),
            ("studio/studio-post.js", ("function renderPostSection", "dataset.postEditor")),
        ),
    ),
    Capability(
        "subtitle-style",
        routes=(("PATCH", "/api/studio/jobs/{job_id}/plans/{plan_id}"),),
        commands=(("studio", "plan", "set"), ("studio", "plan", "show"), ("studio", "shot", "set")),
        tools=("update_plan_options", "edit_shots"),
        page=(
            ("studio/studio-plan.js", ('"subtitle_style"', "ui.plan_subtitle_style")),
            ("studio/studio-post.js", ("dataset.postHighlights",)),
        ),
    ),
    Capability(
        "overlays-ass",
        routes=(("PATCH", "/api/studio/jobs/{job_id}/plans/{plan_id}"),),
        commands=(("studio", "shot", "set"),),
        tools=("edit_shots",),
        page=(("studio/studio-post.js", ("function renderOverlayEditor", "dataset.overlayEditor")),),
    ),
    Capability(
        "brands",
        routes=(
            ("GET", "/api/studio/brands"),
            ("GET", "/api/studio/brands/{brand_id}"),
            ("POST", "/api/studio/brands/{brand_id}/copy"),
            ("PUT", "/api/studio/brands/{brand_id}"),
            ("DELETE", "/api/studio/brands/{brand_id}"),
            ("POST", "/api/studio/brand-assets"),
            ("GET", "/api/studio/brand-assets/{name}"),
            ("PATCH", "/api/studio/jobs/{job_id}/plans/{plan_id}"),
        ),
        commands=(
            ("studio", "brand", "list"),
            ("studio", "brand", "show"),
            ("studio", "brand", "copy"),
            ("studio", "brand", "update"),
            ("studio", "brand", "delete"),
            ("studio", "brand", "upload"),
            ("studio", "plan", "set"),
        ),
        tools=(
            "list_brands",
            "get_brand",
            "copy_brand",
            "update_brand",
            "delete_brand",
            "upload_brand_asset",
            "update_plan_options",
        ),
        page=(
            ("studio/brands.html", ('id="brand-list"', 'id="brand-form"', "/assets/studio/brands.js")),
            ("studio/studio.html", ('href="/studio/brands"',)),
            ("studio/studio-plan.js", ("dataset.planBrand",)),
        ),
    ),
    Capability(
        "bookends-watermark",
        routes=(
            ("PUT", "/api/studio/brands/{brand_id}"),
            ("POST", "/api/studio/brand-assets"),
            ("PATCH", "/api/studio/jobs/{job_id}/plans/{plan_id}"),
        ),
        commands=(("studio", "brand", "update"), ("studio", "plan", "set")),
        tools=("update_brand", "upload_brand_asset", "update_plan_options"),
        page=(
            ("studio/brands.js", ("dataset.brandAsset", "include_bookends")),
            ("studio/studio-plan.js", ("dataset.planBookends",)),
        ),
    ),
    Capability(
        "overlays-html",
        routes=(
            ("PATCH", "/api/studio/jobs/{job_id}/plans/{plan_id}/shots/{shot_id}/overlay"),
            ("GET", "/api/studio/jobs/{job_id}/versions/{version_id}/overlay-{index}.html"),
            ("POST", "/api/studio/jobs/{job_id}/plans/{plan_id}/nodes"),
        ),
        commands=(("studio", "overlay", "show"), ("studio", "overlay", "edit"), ("studio", "shot", "set")),
        tools=("get_overlay_source", "edit_overlay_source", "edit_shots", "run_node"),
        page=(("studio/studio-post.js", ("function renderOverlayStage", "function openOverlayEditor")),),
    ),
    Capability(
        "exports",
        routes=(
            ("PATCH", "/api/studio/jobs/{job_id}/plans/{plan_id}"),
            ("GET", "/api/studio/jobs/{job_id}/plans/{plan_id}/preflight"),
            ("POST", "/api/studio/jobs/{job_id}/plans/{plan_id}/build"),
        ),
        commands=(
            ("studio", "export", "list"),
            ("studio", "export", "set"),
            ("studio", "export", "clear"),
            ("studio", "build"),
            ("studio", "download"),
        ),
        tools=("set_exports", "run_build"),
        page=(
            ("studio/studio.html", ('id="delivery-body"', "/assets/studio/studio-delivery.js")),
            ("studio/studio-delivery.js", ("DeliveryEditor", "all_canvases=true")),
            ("studio/delivery-editor.js", ('section("exports")', "delivery-export-grid")),
        ),
    ),
    Capability(
        "platforms",
        routes=(("GET", "/api/studio/platforms"),),
        commands=(("studio", "platforms"),),
        tools=("list_platforms",),
        page=(
            ("studio/delivery-templates.html", ('id="delivery-platform-management"',)),
            ("studio/delivery-templates.js", ("DeliveryPlatforms.mount",)),
            ("studio/delivery-platforms.js", ("/api/studio/platforms", "dataset.platformField")),
            ("studio/studio-delivery.js", ("DeliveryEditor",)),
            ("studio/delivery-editor.js", ("/api/studio/platforms", "pub.platform_specs")),
        ),
    ),
    Capability(
        "cover",
        routes=(
            ("GET", "/api/studio/jobs/{job_id}/plans/{plan_id}/cover-candidates"),
            ("PATCH", "/api/studio/jobs/{job_id}/plans/{plan_id}"),
        ),
        commands=(("studio", "publish", "cover"), ("studio", "publish", "set")),
        tools=("cover_candidates", "set_publish"),
        page=(("studio/studio-delivery.js", ("cover-candidates?platform=", "publish-cover")),),
    ),
    Capability(
        "publish-metadata",
        routes=(("PATCH", "/api/studio/jobs/{job_id}/plans/{plan_id}/publish-metadata/{platform}"),),
        commands=(("studio", "publish", "metadata"),),
        tools=("get_publish_metadata", "edit_publish_metadata"),
        page=(("studio/studio-delivery.js", ("/publish-metadata/",)),),
    ),
    Capability(
        "publish-package",
        routes=(
            ("POST", "/api/studio/jobs/{job_id}/plans/{plan_id}/build"),
            ("GET", "/api/studio/jobs/{job_id}/artifacts/{relative}"),
        ),
        commands=(
            ("studio", "publish", "build"),
            ("studio", "publish", "download"),
            ("studio", "publish", "show"),
        ),
        tools=("build_publish_package",),
        page=(("studio/studio-delivery.js", ("dataset.publishPlatform", "files.zip.url")),),
    ),
    Capability(
        "music-library",
        routes=(
            ("GET", "/api/studio/music-library"),
            ("POST", "/api/studio/music-library"),
            ("GET", "/api/studio/music-library/{track_id}"),
            ("PUT", "/api/studio/music-library/{track_id}"),
            ("DELETE", "/api/studio/music-library/{track_id}"),
            ("POST", "/api/studio/music-library/{track_id}/analyze"),
            ("GET", "/api/studio/music-library/{track_id}/file"),
        ),
        commands=(
            ("studio", "music", "list"),
            ("studio", "music", "show"),
            ("studio", "music", "import"),
            ("studio", "music", "update"),
            ("studio", "music", "analyze"),
            ("studio", "music", "delete"),
        ),
        tools=(
            "list_music_tracks",
            "get_music_track",
            "import_music_track",
            "update_music_track",
            "analyze_music_track",
        ),
        page=(
            (
                "admin/music.html",
                ('id="music-list"', 'id="music-import"', 'id="music-moods"', "/assets/admin/music.js"),
            ),
            ("admin/admin.html", ('href="/admin/music"',)),
        ),
    ),
    Capability(
        "music-recommend-select",
        routes=(
            ("POST", "/api/studio/music-library/recommend"),
            ("PATCH", "/api/studio/jobs/{job_id}/plans/{plan_id}"),
        ),
        commands=(("studio", "music", "recommend"), ("studio", "music", "use")),
        tools=("recommend_music", "edit_shots"),
        page=(("studio/studio-stages.js", ("dataset.musicTrack", "/api/studio/music-library/recommend")),),
    ),
    Capability(
        "beat-align",
        routes=(("PATCH", "/api/studio/jobs/{job_id}/plans/{plan_id}"),),
        commands=(("studio", "plan", "set"),),
        tools=("update_plan_options",),
        page=(("studio/studio-plan.js", ("dataset.planBeatAlign", "beat_align")),),
    ),
    Capability(
        "tool-sources",
        routes=(
            ("GET", "/api/studio/tool-sources"),
            ("GET", "/api/studio/tool-sources/{source_id}"),
            ("PUT", "/api/studio/tool-sources/{source_id}"),
            ("DELETE", "/api/studio/tool-sources/{source_id}"),
            ("POST", "/api/studio/tool-sources/{source_id}/test"),
        ),
        commands=(
            ("studio", "tools", "list"),
            ("studio", "tools", "show"),
            ("studio", "tools", "add"),
            ("studio", "tools", "test"),
            ("studio", "tools", "delete"),
        ),
        tools=("list_tool_sources", "test_tool_source", "save_tool_source", "save_stdio_tool_source"),
        page=(
            (
                "settings/settings.html",
                (
                    'id="tool-list"',
                    'id="tool-form"',
                    'id="tool-entries"',
                    'data-section="tools"',
                    "/assets/settings/tools.js",
                ),
            ),
        ),
        mcp_hidden=("save_stdio_tool_source",),
    ),
    Capability(
        "tool-shot-source",
        routes=(
            ("PATCH", "/api/studio/jobs/{job_id}/plans/{plan_id}"),
            ("POST", "/api/studio/jobs/{job_id}/plans/{plan_id}/nodes"),
        ),
        commands=(("studio", "shot", "set"),),
        tools=("edit_shots", "run_node"),
        page=(
            ("studio/studio-editor.js", ("function renderToolEditor", "/api/studio/tool-sources")),
            ("studio/studio-stages.js", ('stage === "invoke"',)),
        ),
    ),
)


@pytest.fixture(scope="module")
def app_routes(tmp_path_factory):
    from api import create_app
    from shared.config import Config

    app = create_app(Config(data_dir=tmp_path_factory.mktemp("data")))
    # 新版 FastAPI 的 include_router 以惰性包装登记，按 OpenAPI 文档取全部路由。
    return {(method.upper(), path) for path, item in app.openapi()["paths"].items() for method in item}


def command_paths():
    def walk(command, path=()):
        yield path
        for name, child in getattr(command, "commands", {}).items():
            yield from walk(child, (*path, name))

    return set(walk(get_command(cli.app)))


@pytest.mark.parametrize("capability", MATRIX, ids=[item.name for item in MATRIX])
def test_capability_has_every_entry_point(capability, app_routes):
    missing_routes = [route for route in capability.routes if route not in app_routes]
    assert not missing_routes, missing_routes
    commands = command_paths()
    assert [path for path in capability.commands if path not in commands] == []
    registry = build_registry()
    names = {spec.name for spec in registry.all()}
    assert [tool for tool in capability.tools if tool not in names] == []
    mcp = {spec.name for spec in exported(registry)}
    for tool in capability.tools:
        assert (tool in mcp) == (tool not in capability.mcp_hidden), tool
    for page, needles in capability.page:
        text = (frontend_root() / page).read_text(encoding="utf-8")
        assert [needle for needle in needles if needle not in text] == [], page
