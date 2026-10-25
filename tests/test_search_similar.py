import json

import pytest
from conftest import FixtureEmbedder
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mindex.admin.curation import CurationPatch, EntityNotFound, Library
from mindex.api import create_app
from mindex.models.adapters import ModelError
from mindex.processing.indexing import IndexBuilder, VectorIndex
from mindex.processing.pipeline import Pipeline
from mindex.search.search import SearchEngine, SimilarUnavailable
from mindex.shared.config import Config
from mindex.shared.media import command, cut
from mindex.shared.types import Filters, TextSearch

pytestmark = pytest.mark.slow  # 重媒体用例：CI 快速回归跳过，每晚完整运行


class NoEmbedder:
    """找相似只读库内向量：任何嵌入调用都算违规。"""

    def __getattr__(self, name):
        raise AssertionError(f"similar must not call embedder.{name}")


def test_similar_smoke_excludes_seed_and_never_embeds(config, video_file):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    IndexBuilder(config, FixtureEmbedder(config)).build(source_id=sid, channels=["visual"], activate=True)
    engine = SearchEngine(config, NoEmbedder())
    shots = [s for s in pipeline.store.snapshots(pipeline.store.active_index()["id"]) if s["kind"] == "shot"]
    seed = shots[0]
    result = engine.similar(seed["id"])
    assert result["seed_segment_id"] == seed["id"]
    assert result["results"], "fixture has several non-overlapping shots"
    for hit in result["results"]:
        assert hit["id"] != seed["id"] and hit["kind"] == "shot"
        assert not (hit["source_id"] == seed["source_id"]
                    and min(hit["end"], seed["end"]) - max(hit["start"], seed["start"]) > 0)
        assert {m["route"] for m in hit["matches"]} == {"video"}
    with pytest.raises(EntityNotFound):
        engine.similar("missing-segment")


def test_similar_endpoint_returns_seed_and_maps_missing_to_404(config, video_file):
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    IndexBuilder(config, FixtureEmbedder(config)).build(source_id=sid, channels=["visual"], activate=True)
    seed = next(s for s in pipeline.store.snapshots(pipeline.store.active_index()["id"]) if s["kind"] == "shot")
    client = TestClient(create_app(config, FixtureEmbedder(config)))
    response = client.post("/api/search/similar", json={"segment_id": seed["id"], "limit": 5})
    assert response.status_code == 200
    body = response.json()
    assert body["seed_segment_id"] == seed["id"]
    assert all(hit["id"] != seed["id"] for hit in body["results"])
    assert client.post("/api/search/similar", json={"segment_id": "missing-segment"}).status_code == 404


def _color_video(path, colors):
    """拼接若干纯色段的测试视频；颜色切换处会切出不同镜头。"""
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for color, seconds in colors:
        args += ["-f", "lavfi", "-i", f"color=c={color}:s=160x120:r=25:d={seconds}"]
    inputs = "".join(f"[{i}:v]" for i in range(len(colors)))
    args += ["-filter_complex", f"{inputs}concat=n={len(colors)}:v=1:a=0[v]", "-map", "[v]",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    command(args)
    return path


@pytest.fixture(scope="module")
def similar_library(tmp_path_factory, video_file):
    """建一次索引供性质测试反复使用：两个素材，各含多个镜头与时刻（时刻之间有重叠）。"""
    root = tmp_path_factory.mktemp("similar")
    config = Config(data_dir=root / "data")
    config.embedding.dimensions = 8
    pipeline = Pipeline(config)
    pipeline.prepare(video_file)
    pipeline.prepare(_color_video(root / "第二素材.mp4", [("blue", 2), ("red", 2), ("green", 3)]))
    IndexBuilder(config, FixtureEmbedder(config)).build(channels=["visual"], activate=True)
    store = pipeline.store
    snapshots = store.snapshots(store.active_index()["id"])
    assert {s["kind"] for s in snapshots} >= {"shot", "moment"}
    assert len({s["source_id"] for s in snapshots}) == 2
    return {
        "config": config,
        "store": store,
        "snapshots": sorted(snapshots, key=lambda s: s["id"]),
        "sources": sorted({s["source_id"] for s in snapshots}),
        "hidden": {},  # 当前写入库中的隐藏标记，按需增量修改
    }


def _apply_hidden(library_state, wanted: dict) -> None:
    """把隐藏标记同步为 wanted（未列出的视为不隐藏），只改有变化的实体。"""
    library = Library(library_state["store"])
    current = library_state["hidden"]
    for (kind, identifier) in set(current) | set(wanted):
        value = wanted.get((kind, identifier), False)
        if current.get((kind, identifier), False) != value:
            library.patch(kind, identifier, CurationPatch(hidden=value))
            current[(kind, identifier)] = value


@st.composite
def similar_cases(draw, snapshots, sources):
    seed = draw(st.sampled_from(snapshots))
    granularity = draw(st.sampled_from([None, "shot", "moment"]))
    min_duration = draw(st.sampled_from([0, 0.5, 1, 2, 3]))
    max_duration = draw(st.sampled_from([None, 1, 2, 3, 4, 8]).filter(lambda v: v is None or v >= min_duration))
    source_ids = draw(st.sets(st.sampled_from(sources)).map(sorted))
    hidden = {}
    for source in sources:
        if draw(st.integers(0, 3)) == 0:  # 素材隐藏会清空大半候选，降低其概率
            hidden[("source", source)] = True
    for segment in draw(st.sets(st.sampled_from(snapshots).map(lambda s: s["id"]), max_size=4)):
        hidden[("segment", segment)] = True
    limit = draw(st.integers(1, 20))
    filters = Filters(min_duration=min_duration, max_duration=max_duration, source_ids=source_ids)
    return seed, granularity, filters, hidden, limit


def test_similar_exclusion_property(similar_library):
    # Feature: agent-capability-expansion, Property 8: 相似排除
    """**Validates: Requirements 7.2, 7.3**"""
    snapshots = similar_library["snapshots"]
    by_id = {s["id"]: s for s in snapshots}
    engine = SearchEngine(similar_library["config"], NoEmbedder())

    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(similar_cases(snapshots, similar_library["sources"]))
    def check(case):
        seed, granularity, filters, hidden, limit = case
        _apply_hidden(similar_library, hidden)
        result = engine.similar(seed["id"], granularity=granularity, limit=limit, filters=filters)
        assert result["seed_segment_id"] == seed["id"]
        assert len(result["results"]) <= limit
        wanted_kind = granularity or seed["kind"]

        def is_hidden(segment):
            return any(hidden.get(key, False) for key in [
                ("source", segment["source_id"]),
                ("segment", segment["id"]),
                ("segment", segment.get("parent_id")),
            ])

        for hit in result["results"]:
            stored = by_id[hit["id"]]
            assert hit["id"] != seed["id"]
            assert not (stored["source_id"] == seed["source_id"]
                        and min(stored["end"], seed["end"]) - max(stored["start"], seed["start"]) > 0)
            assert hit["kind"] == stored["kind"] == wanted_kind
            assert not is_hidden(stored) and not hit["effective_hidden"]
            duration = stored["end"] - stored["start"]
            assert duration >= filters.min_duration
            assert filters.max_duration is None or duration <= filters.max_duration
            assert not filters.source_ids or stored["source_id"] in filters.source_ids

    try:
        check()
    finally:
        _apply_hidden(similar_library, {})


# --- 集成测试：多窗口融合、隐藏过滤、离线可用、错误映射与既有检索回归 ---


class OfflineEmbedder:
    """查询嵌入服务离线：任何嵌入调用都抛 ModelError（与真实适配器断连时一致）。"""

    def identity(self):
        raise ModelError("嵌入服务离线")

    def text(self, texts, role="document"):
        raise ModelError("嵌入服务离线")

    def video(self, path):
        raise ModelError("嵌入服务离线")

    def image(self, path):
        raise ModelError("嵌入服务离线")


def _visual_index(config, video_file):
    """用测试素材（红 0-1s、蓝 1-2s、绿 2-10s）建一个只含画面渠道的活动索引。"""
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    IndexBuilder(config, FixtureEmbedder(config)).build(source_id=sid, channels=["visual"], activate=True)
    store = pipeline.store
    active = store.active_index()
    shots = sorted(
        (s for s in store.snapshots(active["id"]) if s["kind"] == "shot"), key=lambda s: s["start"]
    )
    assert len(shots) == 3
    return store, active, shots


def _video_representations(store, index_id, segment_id):
    return [
        r["id"]
        for r in store.rows(
            "SELECT id FROM representations WHERE index_id=? AND segment_id=? AND view='video' ORDER BY id",
            (index_id, segment_id),
        )
    ]


def _axis(position):
    vector = [0.0] * 8
    vector[position] = 1.0
    return vector


def test_similar_fuses_every_seed_window_with_equal_weights(config, video_file):
    store, active, (red, blue, green) = _visual_index(config, video_file)
    assert green["end"] - green["start"] > config.video.short_shot_seconds
    ids = _video_representations(store, active["id"], green["id"])
    windows = len(ids)
    assert windows >= 3, "8 秒长镜头按 4 秒窗口、3 秒步长应切成至少 3 个窗口"
    # 让种子各窗口指向不同画面：第一个窗口像红色，其余像蓝色（FixtureEmbedder：红→0，蓝→1）。
    vectors = VectorIndex(store.root, active["id"])
    for index, identifier in enumerate(ids):
        vectors.upsert(identifier, _axis(0 if index == 0 else 1), green, "video", "")
    result = SearchEngine(config, NoEmbedder()).similar(green["id"])
    hits = {hit["id"]: hit for hit in result["results"]}
    assert set(hits) == {red["id"], blue["id"]}
    k = config.search.rrf_k
    # 红色：第一窗口第 1 名、其余窗口第 2 名；蓝色相反。每个窗口权重 1/窗口数。
    expected = {
        red["id"]: (1 / (k + 1) + (windows - 1) / (k + 2)) / windows,
        blue["id"]: (1 / (k + 2) + (windows - 1) / (k + 1)) / windows,
    }
    for identifier, score in expected.items():
        assert hits[identifier]["score"] == pytest.approx(score)
        assert len(hits[identifier]["matches"]) == windows
        assert {m["route"] for m in hits[identifier]["matches"]} == {"video"}
    assert [hit["id"] for hit in result["results"]] == [blue["id"], red["id"]]
    assert result["score_type"] == "weighted_rrf" and result["index_id"] == active["id"]


def test_similar_drops_hidden_segment_and_source(similar_library):
    snapshots = similar_library["snapshots"]
    engine = SearchEngine(similar_library["config"], NoEmbedder())
    # 测试素材的红色镜头（0-1s）为种子；第二素材的红色镜头（2-4s）画面完全相同，应排第一。
    seed = next(s for s in snapshots if s["kind"] == "shot" and s["start"] == 0 and s["end"] < 1.5)
    twin = next(
        s for s in snapshots
        if s["kind"] == "shot" and s["source_id"] != seed["source_id"] and abs(s["start"] - 2) < 0.2
    )
    try:
        assert engine.similar(seed["id"])["results"][0]["id"] == twin["id"]
        _apply_hidden(similar_library, {("segment", twin["id"]): True})
        hits = engine.similar(seed["id"])["results"]
        assert hits and twin["id"] not in {hit["id"] for hit in hits}
        _apply_hidden(similar_library, {("source", twin["source_id"]): True})
        hits = engine.similar(seed["id"])["results"]
        assert all(hit["source_id"] != twin["source_id"] for hit in hits)
    finally:
        _apply_hidden(similar_library, {})


def test_similar_works_while_embedding_service_is_offline(config, video_file):
    import io

    from PIL import Image

    _, active, (red, _, _) = _visual_index(config, video_file)
    client = TestClient(create_app(config, OfflineEmbedder()))
    response = client.post("/api/search/similar", json={"segment_id": red["id"]})
    assert response.status_code == 200
    body = response.json()
    assert body["index_id"] == active["id"] and body["results"]
    # 对照：需要查询嵌入的以图搜视频此时确实不可用。
    raw = io.BytesIO()
    Image.new("RGB", (160, 120), "blue").save(raw, format="PNG")
    assert client.post("/api/search/image", files={"file": ("q.png", raw.getvalue())}).status_code != 200


def test_similar_seed_without_visual_view_returns_422(config, video_file):
    store, active, (red, _, _) = _visual_index(config, video_file)
    assert active["spec"].get("format", 1) >= 2
    row = store.one(
        "SELECT snapshot FROM index_members WHERE index_id=? AND segment_id=?", (active["id"], red["id"])
    )
    snapshot = json.loads(row["snapshot"])
    assert "video" in snapshot["indexed_views"]
    # 模拟只有对白渠道的种子：快照里没有画面表示。
    snapshot["indexed_views"] = ["dialogue"]
    store.execute(
        "UPDATE index_members SET snapshot=? WHERE index_id=? AND segment_id=?",
        (json.dumps(snapshot, ensure_ascii=False), active["id"], red["id"]),
    )
    client = TestClient(create_app(config, NoEmbedder()))
    response = client.post("/api/search/similar", json={"segment_id": red["id"]})
    assert response.status_code == 422
    assert "没有画面表示" in response.json()["detail"]


def test_similar_missing_seed_vectors_returns_409(config, video_file):
    store, active, (red, _, _) = _visual_index(config, video_file)
    VectorIndex(store.root, active["id"]).delete(_video_representations(store, active["id"], red["id"]))
    client = TestClient(create_app(config, NoEmbedder()))
    response = client.post("/api/search/similar", json={"segment_id": red["id"]})
    assert response.status_code == 409
    assert "向量缺失" in response.json()["detail"]
    with pytest.raises(SimilarUnavailable):
        SearchEngine(config, NoEmbedder()).similar(red["id"])


def test_text_image_video_search_unchanged_after_context_split(config, video_file, tmp_path):
    from PIL import Image

    _, active, _ = _visual_index(config, video_file)
    engine = SearchEngine(config, FixtureEmbedder(config))

    def check(result):
        assert result["index_id"] == active["id"]
        assert result["score_type"] == "weighted_rrf"
        assert result["results"] and result["eligible_segments"] > 0
        assert result["results"][0]["start"] == pytest.approx(1, abs=0.1)  # 蓝色画面位于 1-2s
        assert {m["route"] for hit in result["results"] for m in hit["matches"]} == {"video"}
        for hit in result["results"]:
            assert {"id", "score", "matches", "media_url", "thumbnail_url"} <= set(hit)

    text = engine.text(TextSearch(text="蓝色", channels=["visual"]))
    check(text)
    assert text["channels"] == ["visual"] and text["degraded"] is False
    picture = tmp_path / "blue.png"
    Image.new("RGB", (160, 120), "blue").save(picture)
    check(engine.image(picture))
    clip = tmp_path / "blue.mp4"
    cut(video_file, 1.1, 1.9, clip)
    check(engine.video(clip))
