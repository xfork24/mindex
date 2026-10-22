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
        assert response.text.index(feedback) < response.text.index('/assets/' + (
            'search/app.js' if page in {'/', '/index.html'} else page.strip('/').split('.')[0] + '/' + page.strip('/').split('.')[0] + '.js'
        ))
        for asset in assets:
            resource = client.get(asset)
            assert resource.status_code == 200, asset
            assert resource.content, asset
        assert client.get("/assets/shared/../config.py").status_code == 404
