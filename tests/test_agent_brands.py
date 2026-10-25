"""智能助手 / MCP 的品牌模板入口（spec m3-post-production 需求 7、22）：studio_post 领域的品牌工具与
update_plan_options 的 brand_id / brand_overrides。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_agent_studio_recall import attachment, run
from test_studio_brands import png
from test_studio_sources import setup, shot, small  # noqa: F401 — small 是夹具

from mindex.agent.mcp_server import exported
from mindex.agent.registry import ToolError
from mindex.agent.tools import build_registry
from mindex.api import create_app


@pytest.fixture
def agent(small):  # noqa: F811
    job_id, _, _ = setup(small, [shot("a", "红色开场。")])
    with TestClient(create_app(small)) as client:
        yield small, client, job_id


def test_brand_tools_are_in_studio_post_and_exported():
    registry = build_registry()
    names = ("list_brands", "get_brand", "copy_brand", "update_brand", "delete_brand", "upload_brand_asset")
    assert {registry.get(name).domain for name in names} == {"studio_post"}
    assert registry.get("delete_brand").risk == "danger"
    assert set(names) <= {spec.name for spec in exported(registry)}
    assert {spec.name for spec in exported(registry, ["studio"])}.isdisjoint(names)


def test_copy_upload_update_select_and_delete(agent):
    config, client, job_id = agent
    copied = run(client, config, "copy_brand", {"brand_id": "news", "name": "频道"})[1].value
    ref = attachment(client, png(), "logo.png", "image/png")
    uploaded = run(client, config, "upload_brand_asset", {"attachment": ref, "purpose": "watermark"})[1].value
    assert uploaded["asset"]["kind"] == "image"
    preview, output, _ = run(client, config, "update_brand", {
        "brand_id": copied["id"], "watermark": {"asset": uploaded["asset"], "position": "bottom-left"}})
    assert "左下" in preview.changes[0]["after"] and "重新合成成片" in preview.warnings[0]
    assert output.value["watermark"]["position"] == "bottom-left"
    with pytest.raises(ToolError, match="只读"):
        run(client, config, "update_brand", {"brand_id": "news", "name": "改"})
    preview, output, _ = run(client, config, "update_plan_options", {"job_id": job_id, "changes": {
        "brand_id": copied["id"], "brand_overrides": {"disable": ["watermark"]}}})
    assert any("频道" in str(change["after"]) for change in preview.changes)
    assert output.value["options"]["brand_id"] == copied["id"]
    assert output.value["options"]["brand_overrides"]["disable"] == ["watermark"]
    with pytest.raises(ToolError, match="409|引用"):
        run(client, config, "delete_brand", {"brand_id": copied["id"]})
    _, output, _ = run(client, config, "update_plan_options", {"job_id": job_id, "changes": {
        "brand_id": "none", "brand_overrides": {}}})
    assert output.value["options"].get("brand_id") is None and output.value["options"].get("brand_overrides") is None
    run(client, config, "delete_brand", {"brand_id": copied["id"]})
