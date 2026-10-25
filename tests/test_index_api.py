"""内容索引版本管理：/api/indexes 列表、一致性检查、激活/回退的安全约束，以及 index 领域工具。"""

from __future__ import annotations

import asyncio
import threading

import pytest
from conftest import FixtureEmbedder
from fastapi.testclient import TestClient

from mindex.admin import indexes as indexes_api
from mindex.agent.ids import short
from mindex.agent.loopback import MindexClient
from mindex.agent.registry import ToolContext, ToolError
from mindex.agent.tools import build_registry
from mindex.agent.tools.index import resolve_index
from mindex.api import create_app
from mindex.models.adapters import ModelError
from mindex.processing.indexing import IndexBuilder
from mindex.processing.pipeline import Pipeline
from mindex.settings import settings_api
from mindex.shared.processing_lock import ProcessingLock, index_write_lock
from mindex.shared.storage import Store

pytestmark = pytest.mark.slow  # 重媒体用例：CI 快速回归跳过，每晚完整运行


class SchemaEmbedder(FixtureEmbedder):
    """schema 2 空间：激活时会比对嵌入身份并调用能力门槛。"""

    def identity(self):
        return {**super().identity(), "schema": 2}


class OtherSpaceEmbedder(SchemaEmbedder):
    def identity(self):
        return {**super().identity(), "revision": "other-revision"}


class GateFailEmbedder(SchemaEmbedder):
    def require_gate(self):
        raise ModelError("能力门槛未通过")


@pytest.fixture(autouse=True)
def quiet_warmup(monkeypatch):
    """激活后的后台预热改为记录调用，避免测试线程触达真实模型。"""
    calls = threading.Event()
    monkeypatch.setattr("mindex.search.warmup.warm_up", lambda config, startup=False: calls.set())
    return calls


def build_two(config, video_file, embedder_cls=FixtureEmbedder) -> tuple[str, str]:
    """建两个 ready 索引（模式不同）：返回（较早的, 较新且活动的）。"""
    sid = Pipeline(config).prepare(video_file)
    builder = IndexBuilder(config, embedder_cls(config))
    older = builder.build(source_id=sid, channels=["visual"], activate=True)
    newer = builder.build(source_id=sid, mode="visual", activate=True)
    assert older != newer
    return older, newer


def active_id(config) -> str:
    return Store(config.data_dir).active_index()["id"]


def corrupt(config, index_id):
    store = Store(config.data_dir)
    store.execute(
        "DELETE FROM representations WHERE rowid=(SELECT min(rowid) FROM representations WHERE index_id=?)",
        (index_id,),
    )


# ---------------------------------------------------------------- REST


def test_list_newest_first_with_active(config, video_file):
    older, newer = build_two(config, video_file)
    body = TestClient(create_app(config, FixtureEmbedder(config))).get("/api/indexes").json()
    assert [v["id"] for v in body["versions"]] == [newer, older]
    assert body["active"]["content_id"] == newer and body["active"]["release_id"]
    first = body["versions"][0]
    assert first["status"] == "ready" and first["mode"] == "visual" and first["format"] == 2
    assert first["space_model"] == config.embedding.model


def test_check_and_activate_rejects_inconsistent_index(config, video_file):
    older, newer = build_two(config, video_file)
    client = TestClient(create_app(config, FixtureEmbedder(config)))
    assert client.get(f"/api/indexes/{older}/check").json() == {"index_id": older, "ok": True, "issues": []}
    assert client.get("/api/indexes/missing/check").status_code == 404
    corrupt(config, older)
    checked = client.get(f"/api/indexes/{older}/check").json()
    assert checked["ok"] is False and any("向量与元数据不一致" in i for i in checked["issues"])
    response = client.post(f"/api/indexes/{older}/activate")
    assert response.status_code == 422
    assert response.json()["detail"]["issues"] == checked["issues"]
    assert active_id(config) == newer
    assert client.post("/api/indexes/missing/activate").status_code == 404


@pytest.mark.parametrize("embedder_cls,reason", [
    (OtherSpaceEmbedder, "当前模型空间与待发布索引不同"),
    (GateFailEmbedder, "能力门槛未通过"),
])
def test_activate_space_mismatch_or_gate_is_503(config, video_file, embedder_cls, reason):
    older, newer = build_two(config, video_file, SchemaEmbedder)
    response = TestClient(create_app(config, embedder_cls(config))).post(f"/api/indexes/{older}/activate")
    assert response.status_code == 503 and reason in response.json()["detail"]
    assert active_id(config) == newer
    # 失败路径释放了全部锁：换回一致的嵌入空间即可激活。
    ok = TestClient(create_app(config, SchemaEmbedder(config))).post(f"/api/indexes/{older}/activate")
    assert ok.status_code == 200 and ok.json()["active"] == older


def test_activate_preprocessing_mismatch_is_503(config, video_file):
    older, newer = build_two(config, video_file)
    config.video.max_frames += 1
    response = TestClient(create_app(config, FixtureEmbedder(config))).post(f"/api/indexes/{older}/activate")
    assert response.status_code == 503 and "视频预处理配置与索引不一致" in response.json()["detail"]
    assert active_id(config) == newer


def test_activate_lock_conflicts_are_409(config, video_file, monkeypatch):
    older, newer = build_two(config, video_file)
    client = TestClient(create_app(config, FixtureEmbedder(config)))
    # 删除素材时持有独占的素材库锁：激活拿不到共享处理锁。
    holder = ProcessingLock(config, source_id="any", deleting=True).acquire()
    try:
        response = client.post(f"/api/indexes/{older}/activate")
        assert response.status_code == 409 and "处理锁" in response.json()["detail"]
    finally:
        holder.release()
    assert active_id(config) == newer

    monkeypatch.setattr(indexes_api, "INDEX_LOCK_TIMEOUT_SECONDS", 0.1)
    writer = index_write_lock(config.data_dir)
    writer.acquire()
    try:
        response = client.post(f"/api/indexes/{older}/activate")
        assert response.status_code == 409 and "索引正在写入" in response.json()["detail"]
    finally:
        writer.release()
    assert active_id(config) == newer


def test_activate_rolls_back_and_search_uses_new_index(config, video_file, quiet_warmup):
    older, newer = build_two(config, video_file)
    client = TestClient(create_app(config, FixtureEmbedder(config)))
    before = client.post("/api/search/text", json={"text": "绿色", "granularity": "shot"}).json()
    assert before["index_id"] == newer
    response = client.post(f"/api/indexes/{older}/activate")
    assert response.status_code == 200
    body = response.json()
    assert body["active"] == older and body["release_id"]
    assert client.get("/api/indexes").json()["active"] == {"content_id": older, "release_id": body["release_id"]}
    assert quiet_warmup.wait(5), "激活成功后应在后台触发检索预热"
    after = client.post("/api/search/text", json={"text": "绿色", "granularity": "shot"}).json()
    assert after["index_id"] == older and after["results"]


# ---------------------------------------------------------------- 工具


def run(app, name, arguments, *, preview=False):
    spec = build_registry().get(name)

    async def go():
        client = MindexClient(app)
        try:
            ctx = ToolContext(client=client)
            return await (spec.preview if preview else spec.execute)(spec.parse(arguments), ctx)
        finally:
            await client.aclose()

    return asyncio.run(go())


@pytest.fixture
def tool_app(config, video_file, monkeypatch):
    # 预览里的空间比对走 /api/settings/embedding-status，换成测试嵌入器避免访问真实模型。
    monkeypatch.setattr(settings_api, "make_embedder", lambda current: FixtureEmbedder(current))
    older, newer = build_two(config, video_file)
    return create_app(config, FixtureEmbedder(config)), older, newer


def test_tool_risks():
    registry = build_registry()
    assert {n: registry.get(n).risk for n in ("list_index_versions", "check_index", "activate_index")} == {
        "list_index_versions": "read", "check_index": "read", "activate_index": "danger"}
    assert registry.get("activate_index").preview is not None


def test_list_and_check_tools(tool_app):
    app, older, newer = tool_app
    out = run(app, "list_index_versions", {})
    assert out.value["total"] == 2 and [v["id"] for v in out.value["versions"]] == [newer, older]
    active_line = next(line for line in out.text.splitlines() if line.startswith(short(newer)))
    assert "【活动】" in active_line
    assert "【活动】" not in next(line for line in out.text.splitlines() if line.startswith(short(older)))
    assert out.focus == {"page": "settings", "section": "releases"}
    checked = run(app, "check_index", {})
    assert checked.value["index_id"] == newer and checked.value["ok"] is True and "通过" in checked.text
    assert run(app, "check_index", {"index_id": short(older)}).value["index_id"] == older


def test_index_prefix_resolution(tool_app):
    app, older, _ = tool_app
    with pytest.raises(ToolError) as error:
        run(app, "check_index", {"index_id": older[:5]})
    assert error.value.code == "invalid_arguments"
    with pytest.raises(ToolError) as error:
        run(app, "check_index", {"index_id": "zzzzzzzz"})
    assert error.value.code == "missing_index"
    versions = [{"id": "abcdef01aa"}, {"id": "abcdef02bb"}]
    with pytest.raises(ToolError) as error:
        resolve_index(versions, "abcdef")
    assert error.value.code == "ambiguous_index" and error.value.value["candidates"] == ["abcdef01aa", "abcdef02bb"]
    assert resolve_index(versions, "ABCDEF02")["id"] == "abcdef02bb"
    assert resolve_index(versions, "abcdef01aa")["id"] == "abcdef01aa"


def test_activate_index_preview(tool_app):
    app, older, newer = tool_app
    preview = run(app, "activate_index", {"index_id": short(older)}, preview=True)
    changes = {c["label"]: c for c in preview.changes}
    assert {"内容索引", "嵌入空间", "检索组合"} <= set(changes)
    assert changes["内容索引"] == {"label": "内容索引", "before": short(newer), "after": short(older)}
    assert changes["嵌入空间"]["after"].startswith("一致")
    # 回退目标在首次构建时已有检索组合：复用它而不是新建。
    assert not changes["检索组合"]["after"].startswith("新建组合")
    assert not any("已是当前活动索引" in w for w in preview.warnings)
    same = run(app, "activate_index", {"index_id": newer}, preview=True)
    assert "已是当前活动索引" in same.warnings[0]


def test_activate_index_preview_space_mismatch_warns(tool_app, monkeypatch):
    app, older, _ = tool_app

    class Renamed(FixtureEmbedder):
        def identity(self):
            return {**super().identity(), "model": "other-model"}

    monkeypatch.setattr(settings_api, "make_embedder", lambda current: Renamed(current))
    preview = run(app, "activate_index", {"index_id": older}, preview=True)
    space = next(c for c in preview.changes if c["label"] == "嵌入空间")
    assert space["after"].startswith("不一致") and "激活会被拒绝" in preview.warnings[0]


def test_activate_index_execute_and_422(tool_app, config):
    app, older, newer = tool_app
    out = run(app, "activate_index", {"index_id": short(older)})
    assert out.value["active"] == older
    assert out.mutation == {"domain": "settings", "index_id": older}
    assert active_id(config) == older
    corrupt(config, newer)
    with pytest.raises(ToolError) as error:
        run(app, "activate_index", {"index_id": newer})
    assert error.value.code == "http_422" and error.value.value["issues"]
    assert "活动索引未改变" in str(error.value) and "向量与元数据不一致" in str(error.value)
    assert active_id(config) == older


def test_activate_index_rejects_not_ready(tool_app, config):
    app, older, _ = tool_app
    Store(config.data_dir).execute("UPDATE index_versions SET status='building' WHERE id=?", (older,))
    with pytest.raises(ToolError) as error:
        run(app, "activate_index", {"index_id": older}, preview=True)
    assert error.value.code == "index_not_ready"
