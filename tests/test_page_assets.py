"""Page bundles remain reachable after moving resources into feature packages."""

import re

import pytest
from fastapi.testclient import TestClient

from api import create_app


@pytest.mark.parametrize(
    "page",
    [
        "/",
        "/search",
        "/search/",
        "/search.html",
        "/studio/templates",
        "/studio/templates/",
        "/studio/topics",
        "/studio/topics/",
        "/studio/brands",
        "/admin/music",
        "/admin/music/",
        *[
            url
            for name in ("admin", "persons", "studio", "settings")
            for url in (f"/{name}", f"/{name}/", f"/{name}.html")
        ],
    ],
)
def test_page_bundle_assets(config, page):
    with TestClient(create_app(config)) as client:
        response = client.get(page)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', response.text)
        assert assets
        # 版本参数由 scripts/build-assets.py 写成内容哈希（见 test_build_assets.py），这里只核对资源与顺序。
        assert all(re.search(r"\?v=[0-9a-f]{10}$", asset) for asset in assets), assets
        paths = [asset.split("?", 1)[0] for asset in assets]
        # 主页（/）是创作页；视频检索页在 /search。
        name = (
            "studio"
            if page == "/"
            else "search"
            if page.startswith("/search")
            else page.strip("/").split(".")[0].replace("/", "-")
        )
        assert "/assets/shared/feedback.js" in paths
        # 全局智能助手面板在每个页面都可唤起，且须在 i18n.js 之后加载。
        assert "/assets/shared/agent-panel.js" in paths and "/assets/shared/agent-panel.css" in paths
        # 共享请求封装须先于面板与页面脚本加载。
        chain = [
            "/assets/shared/messages.js",
            "/assets/shared/i18n.js",
            "/assets/shared/api.js",
            "/assets/shared/marked.umd.js",
            "/assets/shared/purify.min.js",
            "/assets/shared/agent-markdown.js",
            "/assets/shared/agent-panel.js",
            "/assets/shared/feedback.js",
        ]
        page_scripts = (
            ["/assets/search/app.js"]
            if name == "search"
            else [
                f"/assets/studio/studio-{part}.js"
                for part in (
                    "state",
                    "jobs",
                    "workspace",
                    "board",
                    "editor",
                    "stages",
                    "post",
                    "delivery",
                    "plan",
                    "recall",
                    "build",
                    "template-text",
                    "templates",
                    "main",
                )
            ]
            if name == "studio"
            else [
                # 文案模板管理页：创作页的子页面，与创作页共用模板显示文字。
                "/assets/studio/studio-template-text.js",
                "/assets/studio/templates.js",
            ]
            if name == "studio-templates"
            else [
                # 选题挖掘页：同为创作页子页面，推荐模板的显示文字与模板页共用。
                "/assets/studio/studio-template-text.js",
                "/assets/studio/topics.js",
            ]
            if name == "studio-topics"
            else [
                # 风格模板管理页：与创作页共用模板显示文字（内置模板名称与说明按语言显示）。
                "/assets/studio/studio-template-text.js",
                "/assets/studio/brands.js",
            ]
            if name == "studio-brands"
            else ["/assets/admin/music.js"]
            if name == "admin-music"
            else [
                # 设置页：第三方工具配置组的脚本在 settings.js 之后加载。
                "/assets/settings/settings.js",
                "/assets/settings/tools.js",
            ]
            if name == "settings"
            else [f"/assets/{name}/{name}.js"]
        )
        positions = [paths.index(item) for item in [*chain, *page_scripts]]
        assert positions == sorted(positions)
        for asset in assets:
            resource = client.get(asset)
            assert resource.status_code == 200, asset
            assert resource.content, asset
        assert client.get("/assets/shared/../config.py").status_code == 404


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/studio/music", "/admin/music"),
        ("/studio/music/?id=abc&job=j1", "/admin/music?id=abc"),
        ("/studio/tools", "/settings#tools"),
        ("/studio/tools?id=my-tool&job=j1", "/settings?tool=my-tool#tools"),
        ("/index.html", "/search"),
    ],
)
def test_moved_pages_redirect(config, old, new):
    """音乐库移到素材管理页、第三方工具移到设置页、视频检索移到 /search：旧地址重定向到新位置。"""
    with TestClient(create_app(config)) as client:
        response = client.get(old, follow_redirects=False)
        assert response.status_code == 308
        assert response.headers["location"] == new
