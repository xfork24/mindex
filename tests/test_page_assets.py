"""Page bundles remain reachable after moving resources into feature packages."""
import re

import pytest
from fastapi.testclient import TestClient

from mindex.api import create_app


@pytest.mark.parametrize("page", ["/", "/index.html", *[
    url for name in ("admin", "persons", "studio", "settings")
    for url in (f"/{name}", f"/{name}/", f"/{name}.html")
]])
def test_page_bundle_assets(config, page):
    with TestClient(create_app(config)) as client:
        response = client.get(page)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', response.text)
        assert assets
        feedback = '/assets/shared/feedback.js?v=20261022-local-feedback'
        assert feedback in assets
        # 全局智能助手面板在每个页面都可唤起，且须在 i18n.js 之后加载。
        panel = '/assets/shared/agent-panel.js?v=20261101-attach'
        assert panel in assets and '/assets/shared/agent-panel.css?v=20261101-attach' in assets
        # 面板依赖的文案与 i18n 与面板同步换版本，避免浏览器缓存旧文案（附件按钮等新键缺失）。
        # 文案每次新增键（如创作页“取消执行”“删除方案”、设置页视觉开关）都要换 messages.js 版本。
        assert '/assets/shared/messages.js?v=20261103-vision' in assets
        if page.strip('/').split('.')[0] == 'settings':
            assert '/assets/settings/settings.js?v=20261103-vision' in assets
        assert '/assets/shared/i18n.js?v=20261101-attach' in assets
        if page.strip('/').split('.')[0] == 'studio':
            assert '/assets/studio/studio.js?v=20261102-cancel' in assets
        # Markdown 依赖须在面板之前加载：i18n < marked < DOMPurify < agent-markdown < agent-panel。
        chain = ['/assets/shared/i18n.js', '/assets/shared/marked.umd.js?v=18.1.1',
                 '/assets/shared/purify.min.js?v=3.4.17', '/assets/shared/agent-markdown.js?v=20261025-media', panel]
        positions = [response.text.index(item) for item in chain]
        assert positions == sorted(positions)
        assert response.text.index(feedback) < response.text.index('/assets/' + (
            'search/app.js' if page in {'/', '/index.html'} else page.strip('/').split('.')[0] + '/' + page.strip('/').split('.')[0] + '.js'
        ))
        for asset in assets:
            resource = client.get(asset)
            assert resource.status_code == 200, asset
            assert resource.content, asset
        assert client.get("/assets/shared/../config.py").status_code == 404
