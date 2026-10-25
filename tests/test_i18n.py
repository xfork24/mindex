"""UI dictionary, additive API metadata and preserved content contracts."""
import json
import re
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from mindex.api import create_app
from mindex.shared.i18n import add_message_metadata, message_metadata

STATIC = Path(__file__).resolve().parents[1] / "frontend/src"


def test_dictionary_and_bindings():
    catalog = json.loads((STATIC / "shared/messages.json").read_text())
    assert json.loads((STATIC / "shared/messages.js").read_text().split("window.MindexMessages = ", 1)[1].rstrip(";\n")) == catalog
    # messages.js 必须由 scripts/build-messages.py 从 messages.json 生成，逐字节一致。
    script = Path(__file__).resolve().parents[1] / "scripts/build-messages.py"
    checked = subprocess.run([sys.executable, str(script), "--check"], check=False, capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    for key, item in catalog.items():
        assert set(item) == {"zh", "en"}, key
        assert item["zh"] and item["en"], key
        assert not re.search(r"[一-鿿]", item["en"]), key
        assert sorted(re.findall(r"\{\w+\}", item["zh"])) == sorted(re.findall(r"\{\w+\}", item["en"])), key
    for path in STATIC.glob("*/*.js"):
        if path.name == "messages.js":
            continue
        for key in re.findall(r'\bui\.[a-f0-9]{12}\b', path.read_text()):
            assert key in catalog, (path.name, key)
    for name in ("index", "admin", "persons", "settings", "studio"):
        group = "search" if name == "index" else name
        html = (STATIC / group / f"{name}.html").read_text()
        assert html.count('data-language="zh"') == html.count('data-language="en"') == 1
        page_script = {"index": "app", "studio": "studio-state"}.get(name, name)  # 创作页首个分片脚本
        assert html.index('/assets/shared/i18n.js') < html.index(f'/assets/{group}/{page_script}.js')
        for key in re.findall(r'\bui\.[a-f0-9]{12}\b', html):
            assert key in catalog
    # 文案模板管理页（创作页的子页面）：语言切换与 HTML 中引用的全部文案键都要存在。
    html = (STATIC / "studio" / "templates.html").read_text()
    assert html.count('data-language="zh"') == html.count('data-language="en"') == 1
    assert html.index('/assets/shared/i18n.js') < html.index('/assets/studio/templates.js')
    for key in re.findall(r'(?:data-i18n-[a-z-]+="|&quot;)(ui\.[\w.]+)', html):
        assert key in catalog, key
    for key in re.findall(r'"(ui\.templates?_\w+|ui\.studio_tab\w*)"', (STATIC / "studio" / "templates.js").read_text()):
        assert key in catalog, key


def test_system_metadata_preserves_content_and_diagnostics():
    payload = {"name": "中文素材", "annotation": {"description": "开始执行"},
               "input_text": "原始创作文案", "message": "调用模型服务，第 2 次尝试"}
    translated = add_message_metadata(payload)
    assert all(translated[k] == v for k, v in payload.items())
    assert translated["message_params"] == {"p0": "2"}
    assert add_message_metadata(translated) == translated
    diagnostic = message_metadata("upstream diagnostic with an unknown code")
    assert diagnostic == {"message_key": "system.diagnostic", "message_params": {"diagnostic": "upstream diagnostic with an unknown code"}}
    assert message_metadata("参考照片需要恰好一张清晰人脸，且属于画面主体")["message_key"].startswith("ui.")


def test_cached_message_metadata_is_not_mutated_by_callers():
    from mindex.shared.i18n import _message_metadata

    _message_metadata.cache_clear()
    original = message_metadata("调用模型服务，第 2 次尝试")
    changed = message_metadata("调用模型服务，第 2 次尝试")
    changed["message_params"]["p0"] = "changed"
    changed["message_key"] = "changed"
    assert message_metadata("调用模型服务，第 2 次尝试") == original
    assert _message_metadata.cache_info().misses == 1
    assert _message_metadata.cache_info().hits == 2


def test_error_and_asset_contract(config):
    client = TestClient(create_app(config))
    response = client.get('/api/admin/sources/missing')
    assert response.status_code == 404
    assert response.json()["detail"]
    assert response.json()["message_key"].startswith("ui.")
    assert "message_params" in response.json()
    assert client.get('/assets/shared/i18n.js').status_code == client.get('/assets/shared/messages.js').status_code == 200


def test_historical_log_messages_and_content_boundaries():
    catalog = json.loads((STATIC / "shared/messages.json").read_text())
    for message in ("开始执行", "开始拆分镜", "拆分镜：完成", "执行结束：全部完成"):
        metadata = message_metadata(message)
        entry = catalog[metadata["message_key"]]
        rendered = entry["en"]
        for key, value in metadata["message_params"].items():
            rendered = rendered.replace("{" + key + "}", str(value))
        assert not re.search(r"[\u4e00-\u9fff]", rendered), rendered
    content = {"annotation": {"message": "对白原文", "error": "台词原文"}, "notes": "素材备注"}
    assert add_message_metadata(content) == content
    progress = add_message_metadata({"stage": "embedding", "completed": 1, "total": 3})
    assert progress["message_key"] == "ui.c56c156ecf6c"
    assert add_message_metadata(progress) == progress
