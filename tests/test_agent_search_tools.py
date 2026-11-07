"""检索领域工具：抽帧时间点、参数模型与 URL 生成；经进程内回环调用真实检索接口的工具测试。"""

from __future__ import annotations

import asyncio
import io
import itertools
import json
import re

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from agent.attachments import AttachmentStore
from agent.loopback import MindexClient
from agent.registry import ToolContext, ToolError
from agent.tools import build_registry
from agent.tools.search import frame_times, frame_url
from api import create_app
from conftest import FixtureEmbedder
from models.adapters import ModelError
from processing.indexing import IndexBuilder
from processing.pipeline import Pipeline
from shared.config import Config
from test_channel_recall import add_analysis, add_dialogue


def test_frame_times_even_inclusive_and_midpoint():
    assert frame_times(0, 10, 1) == [5.0]
    assert frame_times(0, 10, 3) == [0.0, 5.0, 10.0]
    assert frame_times(2, 4, 5) == [2.0, 2.5, 3.0, 3.5, 4.0]
    assert frame_times(3, 3, 4) == [3.0] * 4
    assert len(frame_times(0, 1, 99)) == 12
    times = frame_times(0.1, 0.7, 7)
    assert times[-1] == 0.7 and all(a <= b for a, b in itertools.pairwise(times))


def test_frame_url_never_exceeds_end():
    # 向下取整到毫秒：区间终点等于素材时长时不会越界（服务端对越界返回 422）。
    assert frame_url("src", 9.9996, 768) == "/api/admin/sources/src/frame?t=9.999&width=768"
    assert frame_url("src", 0, 512).endswith("t=0.000&width=512")


def test_search_specs_and_argument_rules():
    registry = build_registry()
    names = ["search_media", "search_by_media", "find_similar", "sample_frames", "get_system_status"]
    for name in names:
        spec = registry.get(name)
        assert spec.domain == "search" and spec.risk == "read" and spec.mcp
    assert registry.domain_tools()["search"][0] == "search_media"
    frames = registry.get("sample_frames")
    for bad in (
        {},
        {"source_id": "a", "segment_id": "b"},
        {"segment_id": "b", "start": 1},
        {"source_id": "a", "start": 5, "end": 2},
        {"source_id": "a", "count": 13},
    ):
        with pytest.raises(ToolError):
            frames.parse(bad)
    search = registry.get("search_media")
    with pytest.raises(ToolError):
        search.parse({"text": "  "})
    parsed = search.parse(
        {
            "text": "",
            "person_ids": ["actor-1"],
            "granularity": "scene",
            "channels": ["dialogue"],
            "filters": {"min_duration": 1},
        }
    )
    assert parsed.granularity == "scene" and parsed.channels == ["dialogue"]
    with pytest.raises(ToolError):
        registry.get("search_by_media").parse({"attachment": "/tmp/x.png"})


# ---------------------------------------------------------------------------
# 经进程内回环调用真实检索接口：参数透传、附件链路、降级提示、系统状态、定位与媒体。
# ---------------------------------------------------------------------------


class Client(TestClient):
    """测试客户端，记住数据目录以便按真实附件存储解析引用。"""

    def __init__(self, config: Config, embedder):
        super().__init__(create_app(config, embedder))
        self.data_dir = config.data_dir


class OfflineEmbedder(FixtureEmbedder):
    """嵌入服务离线：身份探测失败（与真实远端连接失败时同一异常）。"""

    def identity(self):
        raise ModelError("connection refused")


@pytest.fixture(scope="module")
def library(tmp_path_factory, video_file):
    """建一次索引：一个素材，画面 + 对白 + 分析三渠道。"""
    root = tmp_path_factory.mktemp("search-tools")
    config = Config(data_dir=root / "data")
    config.embedding.dimensions = 8
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    add_dialogue(config, sid)
    add_analysis(pipeline.store, pipeline.store.segments(sid, "moment")[0])
    IndexBuilder(config, FixtureEmbedder(config)).build(activate=True)
    with (
        Client(config, FixtureEmbedder(config)) as online,
        Client(config, OfflineEmbedder(config)) as offline,
    ):
        yield {
            "config": config,
            "source_id": sid,
            "store": pipeline.store,
            "online": online,
            "offline": offline,
        }


def multipart_fields(body: bytes) -> dict[str, str]:
    """从 multipart 请求体取出非文件字段。"""
    fields = {}
    for part in body.split(b"--")[1:]:
        head, _, value = part.partition(b"\r\n\r\n")
        match = re.search(rb'name="([^"]+)"', head)
        if match and b"filename=" not in head:
            fields[match.group(1).decode()] = value.rsplit(b"\r\n", 1)[0].decode()
    return fields


def call(client, name, arguments, *, owner="mcp", session_id=None):
    """执行工具并记录回环发出的全部请求；附件按 owner/session_id 由真实附件存储解析。"""
    spec = build_registry().get(name)
    sent: list[dict] = []

    async def record(request):
        body = await request.aread()
        entry = {"method": request.method, "path": request.url.path, "params": dict(request.url.params)}
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("application/json"):
            entry["json"] = json.loads(body)
        elif ctype.startswith("multipart/"):
            entry["form"] = multipart_fields(body)
            entry["files"] = re.findall(rb'name="([^"]+)"; filename="([^"]*)"', body)
        sent.append(entry)

    async def go():
        store = AttachmentStore(client.data_dir)
        loop = MindexClient(client.app, resolve_attachment=store.resolver(session_id=session_id, owner=owner))
        loop.http.event_hooks = {"request": [record], "response": []}
        try:
            ctx = ToolContext(client=loop, session_id=session_id, page={"page": "search"})
            return await spec.execute(spec.parse(arguments), ctx)
        finally:
            await loop.aclose()

    try:
        return asyncio.run(go()), sent
    except ToolError as exc:
        exc.sent = sent
        raise


@pytest.fixture
def online(library):
    return library["online"]


@pytest.fixture
def offline(library):
    return library["offline"]


def upload(client, data: bytes, name: str, content_type: str, session_id: str | None = None) -> str:
    form = {"session_id": session_id} if session_id else {}
    response = client.post("/api/agent/attachments", files={"file": (name, data, content_type)}, data=form)
    assert response.status_code == 201, response.text
    ref = response.json()["id"]
    assert ref.startswith("attachment://")
    return ref


def png(color: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buffer, "PNG")
    return buffer.getvalue()


def searches(sent: list[dict]) -> list[dict]:
    return [r for r in sent if r["path"].startswith("/api/search/")]


def test_search_media_passes_every_argument(online, library):
    sid = library["source_id"]
    arguments = {
        "text": "阿明",
        "granularity": "shot",
        "limit": 3,
        "source_ids": [sid],
        "channels": ["dialogue", "visual"],
        "filters": {
            "min_duration": 0.5,
            "max_duration": 9,
            "scene": "室内",
            "emotion": "平静",
            "shot_size": "近景",
        },
    }
    with pytest.raises(ToolError) as excinfo:
        # 未登记的人物在出镜模式下被服务端拒绝；请求体仍完整记录，用于核对透传。
        call(
            online,
            "search_media",
            {**arguments, "person_ids": ["ghost"], "person_mode": "onscreen", "person_relation": "any"},
        )
    (request,) = searches(excinfo.value.sent)
    assert request["path"] == "/api/search/text"
    assert request["json"] == {
        "text": "阿明",
        "granularity": "shot",
        "limit": 3,
        "channels": ["dialogue", "visual"],
        "person_ids": ["ghost"],
        "person_mode": "onscreen",
        "person_relation": "any",
        "filters": {
            "min_duration": 0.5,
            "max_duration": 9,
            "scene": "室内",
            "emotion": "平静",
            "shot_size": "近景",
            "source_ids": [sid],
        },
    }
    # 服务端的中文拒绝原因原样转述。
    assert excinfo.value.code == "http_422" and "人物" in str(excinfo.value)

    _, sent = call(
        online,
        "search_media",
        {"text": "绿色", "limit": 2, "source_ids": [sid], "filters": {"min_duration": 0.5}},
    )
    assert searches(sent)[0]["json"] == {
        "text": "绿色",
        "granularity": "moment",
        "limit": 2,
        "filters": {"min_duration": 0.5, "source_ids": [sid]},
    }
    # 缺省值不透传多余字段：人物、渠道未给出时由服务端使用默认值。
    _, sent = call(online, "search_media", {"text": "绿色"})
    assert searches(sent)[0]["json"] == {"text": "绿色", "granularity": "moment", "limit": 8}


def test_search_media_focus_media_and_text(online, library):
    out, _ = call(online, "search_media", {"text": "绿色 草地", "granularity": "shot", "limit": 3})
    assert out.focus == {"page": "search", "query": {"text": "绿色 草地", "granularity": "shot"}}
    rows = out.value["results"]
    assert rows and len(rows) <= 3 and out.meta == {"kind": "search", "count": len(rows)}
    assert all(r["kind"] == "shot" and r["source_id"] == library["source_id"] for r in rows)
    assert [m["url"] for m in out.media] == [f"/api/playlists/{r['segment_id']}" for r in rows]
    assert all(m["kind"] == "video" and m["playlist"] and m["poster"].startswith("/api/") for m in out.media)
    # 文本逐行给出排名、素材、区间与片段 ID，供模型后续 find_similar / sample_frames 使用。
    lines = out.text.splitlines()
    assert lines[0].startswith("#1 测试素材") and rows[0]["segment_id"] in lines[0]
    assert out.value["degraded"] is False and out.value["warnings"] == []
    assert "降级" not in out.text and "警告" not in out.text
    for media in out.media:  # 媒体地址都是真实可访问的本站地址
        assert online.get(media["poster"]).status_code == 200


def test_search_media_reports_degraded_and_warnings(offline):
    out, _ = call(offline, "search_media", {"text": "阿明", "channels": ["dialogue"]})
    assert out.value["degraded"] is True and out.value["results"]
    assert out.value["channels"] == ["dialogue"]
    assert out.value["warnings"] and "BM25" in out.value["warnings"][0]
    assert "本次检索已降级" in out.text
    assert (
        "警告：嵌入不可用，已降级为所选文本渠道的 BM25 检索" in out.text and "connection refused" in out.text
    )


def test_search_media_relays_embedding_unavailable(offline):
    # 只选画面渠道且嵌入离线：没有可降级的词法渠道，服务端 503 的中文原因原样转述。
    with pytest.raises(ToolError, match="没有可用词法召回渠道") as excinfo:
        call(offline, "search_media", {"text": "绿色", "channels": ["visual"]})
    assert excinfo.value.code == "http_503"
    assert excinfo.value.value["status"] == 503 and "connection refused" in excinfo.value.value["error"]


def test_search_by_media_image_attachment_chain(online, library):
    ref = upload(online, png("red"), "红色.png", "image/png")
    sid = library["source_id"]
    out, sent = call(
        online,
        "search_by_media",
        {
            "attachment": ref,
            "granularity": "shot",
            "limit": 2,
            "source_ids": [sid],
            "filters": {"min_duration": 0.2},
        },
    )
    # 先读元数据推断类型，再以 multipart 提交：file 字段是附件内容，filters 是 JSON 字符串。
    assert sent[0]["method"] == "GET" and sent[0]["path"] == f"/api/agent/attachments/{ref[13:]}"
    (request,) = searches(sent)
    assert request["path"] == "/api/search/image"
    assert request["files"] == [(b"file", "红色.png".encode())]
    assert request["form"]["granularity"] == "shot" and request["form"]["limit"] == "2"
    assert json.loads(request["form"]["filters"]) == {"min_duration": 0.2, "source_ids": [sid]}
    assert out.value["kind"] == "image" and out.focus == {"page": "search"}
    rows = out.value["results"]
    assert rows and all(r["kind"] == "shot" for r in rows)
    # 红色查询图命中红色开场镜头（FixtureEmbedder 按颜色给向量）。
    assert rows[0]["start"] == 0
    assert [m["url"] for m in out.media] == [f"/api/playlists/{r['segment_id']}" for r in rows]


def test_search_by_media_explicit_kind_and_empty_filters(online, video_file):
    # 内容类型无法推断时可显式指定 kind；显式指定时不再读取元数据。
    ref = upload(online, video_file.read_bytes(), "clip.bin", "application/octet-stream")
    out, sent = call(online, "search_by_media", {"attachment": ref, "kind": "video", "limit": 1})
    (request,) = sent
    assert request["path"] == "/api/search/video" and request["form"]["filters"] == "{}"
    assert out.value["kind"] == "video" and len(out.value["results"]) == 1
    with pytest.raises(ToolError, match="kind=image 或 video") as excinfo:
        call(online, "search_by_media", {"attachment": ref})
    assert excinfo.value.code == "invalid_arguments" and not searches(excinfo.value.sent)


def test_search_by_media_invalid_attachment_sends_no_search(online):
    missing = "attachment://" + "0" * 32
    for arguments in ({"attachment": missing}, {"attachment": missing, "kind": "image"}):
        with pytest.raises(ToolError) as excinfo:
            call(online, "search_by_media", arguments)
        assert excinfo.value.code == "attachment_invalid" and not searches(excinfo.value.sent)
    # 归属不符：会话附件不能被 MCP 调用方使用（反之亦然），且不发送检索请求。
    sid = online.post("/api/agent/sessions", json={}).json()["id"]
    session_ref = upload(online, png("blue"), "蓝.png", "image/png", session_id=sid)
    with pytest.raises(ToolError, match="不属于当前会话") as excinfo:
        call(online, "search_by_media", {"attachment": session_ref})
    assert excinfo.value.code == "attachment_invalid" and not searches(excinfo.value.sent)
    out, _ = call(online, "search_by_media", {"attachment": session_ref}, owner="session", session_id=sid)
    assert out.value["kind"] == "image" and out.value["results"]
    other = online.post("/api/agent/sessions", json={}).json()["id"]
    with pytest.raises(ToolError) as excinfo:
        call(online, "search_by_media", {"attachment": session_ref}, owner="session", session_id=other)
    assert excinfo.value.code == "attachment_invalid" and not searches(excinfo.value.sent)


def test_find_similar_passthrough_offline_and_errors(offline, library):
    store, sid = library["store"], library["source_id"]
    shots = [s for s in store.snapshots(store.active_index()["id"]) if s["kind"] == "shot"]
    seed = shots[0]
    # 嵌入服务离线时仍可用：只读库内向量（Req 7.4）。
    out, sent = call(
        offline,
        "find_similar",
        {
            "segment_id": seed["id"],
            "granularity": "shot",
            "limit": 2,
            "source_ids": [sid],
            "filters": {"min_duration": 0.1},
        },
    )
    assert sent[0]["json"] == {
        "segment_id": seed["id"],
        "limit": 2,
        "granularity": "shot",
        "filters": {"min_duration": 0.1, "source_ids": [sid]},
    }
    assert out.value["seed_segment_id"] == seed["id"] and out.focus == {"page": "search"}
    rows = out.value["results"]
    assert rows and all(r["segment_id"] != seed["id"] and r["kind"] == "shot" for r in rows)
    assert out.text.startswith(f"与片段 {seed['id']} 画面相似的片段：")
    assert [m["url"] for m in out.media] == [f"/api/playlists/{r['segment_id']}" for r in rows]
    _, sent = call(offline, "find_similar", {"segment_id": seed["id"]})
    assert sent[0]["json"] == {"segment_id": seed["id"], "limit": 8}
    with pytest.raises(ToolError, match="使用当前检索结果里的 segment_id") as excinfo:
        call(offline, "find_similar", {"segment_id": "missing-segment"})
    assert excinfo.value.code == "http_404"


def test_system_status_reports_index_coverage_and_embedding(online, library, monkeypatch):
    out, sent = call(online, "get_system_status", {})
    # 不带 summary：summary 模式不计算渠道覆盖。
    assert [(r["path"], r["params"]) for r in sent] == [("/api/status", {})]
    active = library["store"].active_index()
    index = out.value["active_index"]
    assert index["id"] == active["id"] and index["status"] == "ready"
    assert out.value["source_count"] == 1 and out.value["segment_counts"]["shot"] >= 2
    coverage = out.value["channel_coverage"]
    assert set(coverage) == {"visual", "dialogue", "analysis"}
    assert coverage["visual"]["indexed"] == coverage["visual"]["total"] > 0
    assert out.focus == {"page": "search"} and "embedding" not in out.value
    assert f"活动索引：{active['id']}" in out.text and "渠道覆盖：画面" in out.text

    monkeypatch.setattr("settings.api.make_embedder", FixtureEmbedder)
    out, sent = call(online, "get_system_status", {"include_embedding": True})
    assert sent[-1]["path"] == "/api/settings/embedding-status"
    assert out.value["embedding"]["matches_index"] is True
    assert "嵌入身份：与活动索引一致，检索可用" in out.text

    # 嵌入服务未就绪：如实报告不可用及原因，并提示画面检索可能降级。
    monkeypatch.setattr("settings.api.make_embedder", OfflineEmbedder)
    out, _ = call(online, "get_system_status", {"include_embedding": True})
    assert out.value["embedding"]["matches_index"] is False
    assert "connection refused" in out.value["embedding"]["error"]
    assert "嵌入身份：与活动索引不一致或不可用（connection refused）" in out.text and "降级" in out.text


def test_system_status_without_active_index(config):
    with Client(config, FixtureEmbedder(config)) as client:
        out, _ = call(client, "get_system_status", {"include_embedding": True})
    assert out.value["active_index"] is None and out.value["source_count"] == 0
    assert all(c["total"] == 0 for c in out.value["channel_coverage"].values())
    assert "活动索引：无（尚未发布）" in out.text and "没有活动索引，无需匹配" in out.text


def test_sample_frames_segment_media_and_focus(online, library):
    store = library["store"]
    segment = next(s for s in store.snapshots(store.active_index()["id"]) if s["kind"] == "shot")
    out, sent = call(online, "sample_frames", {"segment_id": segment["id"], "count": 3, "width": 320})
    assert sent[0]["path"] == f"/api/admin/segments/{segment['id']}"
    assert out.focus == {"page": "admin", "source_id": library["source_id"]}
    assert [m["url"] for m in out.media] == out.value["images"] and len(out.media) == 3
    assert all(m["kind"] == "image" and not m["collapsed"] for m in out.media)
    for url in out.value["images"]:
        response = online.get(url)
        assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
