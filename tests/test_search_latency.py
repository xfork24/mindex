"""文字检索降延迟的四项优化：行为必须与优化前一致，缓存必须在数据变化时失效。"""

import json
import math
from collections import Counter

import numpy as np
import pytest

from conftest import FixtureEmbedder
from models.adapters import ModelError
from persons.persons import Persons
from processing.indexing import IndexBuilder, VectorIndex
from processing.pipeline import Pipeline
from search import search as search_module
from search.search import SearchEngine, document_terms, lexical_search, tokens
from shared.storage import Store, bump_snapshot_revision
from shared.types import TextSearch

# ---------- 1. VectorIndex.query：无过滤多取 + 内存后过滤 ----------


def build_index(root, segments=40, windows=2, seed=7):
    index = VectorIndex(root, "ix-postfilter", create=True)
    rng = np.random.default_rng(seed)
    for number in range(segments):
        segment = {"id": f"seg-{number}", "source_id": "src", "kind": "moment"}
        for view in ("video", "dialogue"):
            if view == "dialogue" and number % 3 == 0:
                continue  # 部分片段没有对白向量
            for window in range(windows):
                vector = rng.normal(size=6)
                index.upsert(
                    f"{view}-{number}-{window}",
                    (vector / np.linalg.norm(vector)).tolist(),
                    segment,
                    view,
                    "doc",
                )
    return index


def reference(index, vector, view, eligible, count):
    """优化前的实现：按块 where 过滤。"""
    total = index.collection.count()
    return index._best_per_segment(index._query_filtered(vector, view, eligible, count, total), count)


@pytest.mark.parametrize("view", ["video", "dialogue"])
@pytest.mark.parametrize("count", [3, 10, 100])
def test_postfiltered_query_matches_filtered_query(tmp_path, view, count):
    index = build_index(tmp_path)
    eligible = [f"seg-{n}" for n in range(0, 40, 2)]
    vector = np.random.default_rng(1).normal(size=6).tolist()
    assert index.query(vector, view, eligible, count) == reference(index, vector, view, eligible, count)


def test_postfilter_grows_candidates_until_enough(tmp_path, monkeypatch):
    from processing import indexing

    index = build_index(tmp_path)
    monkeypatch.setattr(indexing, "POSTFILTER_MIN_CANDIDATES", 2)
    monkeypatch.setattr(indexing, "POSTFILTER_OVERFETCH", 1)
    sizes = []
    original = type(index.collection).query

    def spy(self, *args, **kwargs):
        sizes.append((kwargs["n_results"], "where" in kwargs))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(index.collection), "query", spy)
    eligible = ["seg-1", "seg-4", "seg-7"]
    vector = np.random.default_rng(2).normal(size=6).tolist()
    result = index.query(vector, "video", eligible, 3)
    monkeypatch.undo()
    assert [hit["id"] for hit in result] and len({hit["id"] for hit in result}) == 3
    assert len(sizes) > 1 and sizes == sorted(sizes) and not any(where for _, where in sizes)
    assert result == reference(index, vector, "video", eligible, 3)


def test_postfilter_falls_back_to_where_filter_beyond_cap(tmp_path, monkeypatch):
    from processing import indexing

    index = build_index(tmp_path)
    monkeypatch.setattr(indexing, "POSTFILTER_MIN_CANDIDATES", 5)
    monkeypatch.setattr(indexing, "POSTFILTER_OVERFETCH", 1)
    monkeypatch.setattr(indexing, "POSTFILTER_MAX_CANDIDATES", 4)  # 一次都放不下，必须回退
    calls = []
    original = type(index.collection).query

    def spy(self, *args, **kwargs):
        calls.append("where" in kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(index.collection), "query", spy)
    eligible = ["seg-2", "seg-5"]
    vector = np.random.default_rng(3).normal(size=6).tolist()
    result = index.query(vector, "video", eligible, 5)
    monkeypatch.undo()
    assert calls and all(calls)  # 全部走 where 过滤
    assert result == reference(index, vector, "video", eligible, 5)


def test_postfilter_ignores_other_views_and_ineligible_segments(tmp_path):
    index = build_index(tmp_path)
    result = index.query(np.ones(6).tolist(), "dialogue", ["seg-1", "seg-3"], 10)
    assert {hit["id"] for hit in result} <= {"seg-1", "seg-3"}
    assert index.query(np.ones(6).tolist(), "video", [], 10) == []


# ---------- 2. lexical_search：文档分词缓存 ----------


def old_lexical_search(text, snapshots, count, exclude=()):
    terms = Counter(tokens(text))
    documents = [
        Counter(tokens("\n".join(v for k, v in s["views"].items() if k not in exclude))) for s in snapshots
    ]
    lengths = [sum(doc.values()) for doc in documents]
    mean_length = sum(lengths) / max(1, len(lengths)) or 1
    frequency = Counter(term for document in documents for term in document)
    results = []
    for segment, document, length in zip(snapshots, documents, lengths):
        score = 0.0
        for term in terms:
            tf = document[term]
            if not tf:
                continue
            idf = math.log(1 + (len(documents) - frequency[term] + 0.5) / (frequency[term] + 0.5))
            score += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * length / mean_length))
        if score > 0:
            results.append({"id": segment["id"], "score": score})
    return sorted(results, key=lambda v: (-v["score"], v["id"]))[:count]


def corpus():
    lines = [
        "雨夜里一个女性角色在街道上奔跑",
        "男人在办公室里打电话",
        "女性角色回头微笑，女性角色流泪",
        "",
        "两个人在雨中争吵，雨越下越大",
        "空镜头：城市夜景",
    ]
    return [
        {"id": f"s{i}", "views": {"description": t, "dialogue": t[::-1], "style": ""}}
        for i, t in enumerate(lines)
    ]


@pytest.mark.parametrize("query", ["女性角色", "雨夜 街道", "不存在的词汇xyz", "雨", ""])
@pytest.mark.parametrize("exclude", [(), ("dialogue",), ("description", "style", "dialogue")])
def test_lexical_search_matches_uncached_reference(query, exclude):
    snapshots = corpus()
    assert lexical_search(query, snapshots, 10, exclude) == old_lexical_search(query, snapshots, 10, exclude)


def test_document_terms_are_cached_by_text_content():
    document_terms.cache_clear()
    snapshots = corpus()
    lexical_search("女性角色", snapshots, 10)
    first = document_terms.cache_info()
    lexical_search("雨夜", snapshots, 10)  # 同一语料，不同查询：不再重新分词
    second = document_terms.cache_info()
    assert second.misses == first.misses and second.hits > first.hits
    snapshots[1]["views"]["description"] = "改过的文本"  # 文本变了就是新键，不会读到旧词频
    assert lexical_search("改过的文本", snapshots, 10)[0]["id"] == "s1"


# ---------- 3. 快照与人物发布缓存及其失效 ----------


def put_member(store, index_id, number, **extra):
    snapshot = {"id": f"seg-{number}", "source_id": "src", "start": 0, "end": 1, **extra}
    store.execute(
        "INSERT INTO index_members(index_id,segment_id,snapshot,complete) VALUES(?,?,?,1)",
        (index_id, snapshot["id"], json.dumps(snapshot)),
    )


def test_snapshots_shared_caches_and_invalidates(tmp_path):
    store = Store(tmp_path)
    for number in range(3):
        put_member(store, "ix", number)
    first = store.snapshots_shared("ix")
    assert store.snapshots_shared("ix") is first and first == store.snapshots("ix")

    put_member(store, "ix", 3)  # 新增行
    second = store.snapshots_shared("ix")
    assert second is not first and len(second) == 4

    store.execute("UPDATE index_members SET complete=0 WHERE segment_id='seg-0' AND index_id='ix'")
    assert len(store.snapshots_shared("ix")) == 3  # 完成状态变化

    store.execute("DELETE FROM index_members WHERE segment_id='seg-1' AND index_id='ix'")
    assert {s["id"] for s in store.snapshots_shared("ix")} == {"seg-2", "seg-3"}


def test_snapshots_shared_sees_same_length_rewrite_after_revision_bump(tmp_path):
    store = Store(tmp_path)
    put_member(store, "ix", 0, note="aaaa")
    assert store.snapshots_shared("ix")[0]["note"] == "aaaa"
    with store.connect() as db:  # 与 shot_repair 相同的原地改写，且长度不变
        db.execute("UPDATE index_members SET snapshot=replace(snapshot,'aaaa','bbbb')")
        bump_snapshot_revision(db)
    assert store.snapshots_shared("ix")[0]["note"] == "bbbb"


def put_person_release(store, content_id="ix", person_id="pv1", release_id="rel1"):
    value = {
        "format": 2,
        "entities": [
            {"id": "a1", "kind": "actor", "name": "演员甲", "aliases": []},
            {
                "id": "r1",
                "kind": "role",
                "name": "角色乙",
                "aliases": [],
                "work_id": "w1",
                "actor_ids": ["a1"],
            },
        ],
        "evidence": [
            {
                "id": "e1",
                "actor_id": "a1",
                "stage_id": "r1",
                "role_id": "old",
                "source_id": "src",
                "shot_id": "seg-0",
                "start": 0,
                "end": 1,
                "status": "accepted",
                "data": {"score": 0.9, "body_feature": [0.1] * 8, "space": {"model": "m", "n": 1}},
            },
            {
                "id": "e2",
                "actor_id": "a1",
                "role_id": "r1",
                "source_id": "src",
                "shot_id": "seg-1",
                "start": 1,
                "end": 2,
                "status": "accepted",
                "data": {"score": 0.8, "body_feature": [0.2] * 8, "space": {"model": "m", "n": 1}},
            },
        ],
        "coverage": [{"source_id": "src", "data": {"start": 0, "end": 2}}],
    }
    store.execute(
        "INSERT OR REPLACE INTO person_versions(id,data) VALUES(?,?)", (person_id, json.dumps(value))
    )
    store.execute(
        "INSERT OR REPLACE INTO search_releases(id,content_id,person_id) VALUES(?,?,?)",
        (release_id, content_id, person_id),
    )
    return value


def test_published_entities_and_evidence_equal_published(tmp_path):
    store = Store(tmp_path)
    put_person_release(store)
    catalogue = Persons(store)
    release, full = catalogue.published("ix", pinned=True)
    assert catalogue.release_for("ix", pinned=True) == release

    shared = catalogue.published_entities(release)
    assert shared["entities"] == full["entities"] and shared["format"] == 2

    evidence = catalogue.published_evidence(release)
    assert evidence["coverage"] == full["coverage"]
    for light, original in zip(evidence["evidence"], full["evidence"]):
        assert "body_feature" not in light["data"]
        assert {k: v for k, v in original["data"].items() if k != "body_feature"} == light["data"]
        assert {k: v for k, v in original.items() if k != "data"} == {
            k: v for k, v in light.items() if k != "data"
        }
    assert evidence["evidence"][0]["data"]["space"] is evidence["evidence"][1]["data"]["space"]  # 去重共享
    assert catalogue.published_entities(release) is shared  # 命中缓存
    assert catalogue.published_evidence(release) is evidence


def test_published_cache_is_keyed_by_version_content(tmp_path):
    store = Store(tmp_path)
    put_person_release(store)
    catalogue = Persons(store)
    release = catalogue.release_for("ix", pinned=True)
    before = catalogue.published_entities(release)
    # 同 id 的版本被离线迁移改写（长度变化）后，不能读到旧缓存。
    changed = put_person_release(store)
    changed["entities"][0]["name"] = "改名后的演员甲"
    store.execute("UPDATE person_versions SET data=? WHERE id='pv1'", (json.dumps(changed),))
    assert catalogue.published_entities(release)["entities"][0]["name"] == "改名后的演员甲"
    assert before["entities"][0]["name"] == "演员甲"


def test_unpublished_release_yields_empty_data(tmp_path):
    catalogue = Persons(Store(tmp_path))
    assert catalogue.published_entities(None) == {"entities": []}
    assert catalogue.published_evidence(None) == {"evidence": [], "coverage": []}


# ---------- 4. 查询向量缓存 ----------


class CountingEmbedder(FixtureEmbedder):
    def __init__(self, config, revision="fixture-revision"):
        super().__init__(config)
        self.revision = revision

    def identity(self):
        return {**super().identity(), "revision": self.revision}


def test_query_vector_cache_hits_and_is_scoped_to_embedding_space(config, video_file):
    pipeline = Pipeline(config)
    pipeline.prepare(video_file)
    embedder = CountingEmbedder(config)
    IndexBuilder(embedder.config, embedder).build(activate=True, mode="visual")
    search_module._QUERY_VECTORS.clear()

    baseline = embedder.calls
    first = SearchEngine(config, embedder).text(TextSearch(text="红色的房间", channels=["visual"]))
    assert embedder.calls == baseline + 1
    second = SearchEngine(config, embedder).text(TextSearch(text="红色的房间", channels=["visual"]))
    assert embedder.calls == baseline + 1  # 同一空间同一文字：不再推理
    assert [r["id"] for r in first["results"]] == [r["id"] for r in second["results"]]
    assert [r["score"] for r in first["results"]] == [r["score"] for r in second["results"]]

    SearchEngine(config, embedder).text(TextSearch(text="蓝色的房间", channels=["visual"]))
    assert embedder.calls == baseline + 2  # 不同文字重新推理

    # 换了部署（空间标识变化）：即便文字相同也不能复用向量。索引空间校验会拒绝，但缓存键必须不同。
    other = CountingEmbedder(config, revision="another-revision")
    engine = SearchEngine(config, other)
    with pytest.raises(ModelError):
        engine.text(TextSearch(text="红色的房间", channels=["visual"]))
    assert other.calls == 0


# ---------- 5. 人物出镜过滤：区间索引 / 覆盖区间 / 向量映射 ----------


def random_evidence(rng, count=300, sources=3, people=6):
    items = []
    for number in range(count):
        start = round(float(rng.uniform(0, 100)), 1)
        # 含零长度与端点恰好相接的区间，覆盖边界条件
        end = round(start + float(rng.choice([0, 0.5, 2, 5, 20])), 1)
        items.append(
            {
                "id": f"e{number}",
                "source_id": f"src{int(rng.integers(sources))}",
                "actor_id": f"a{int(rng.integers(people))}",
                "role_id": f"r{int(rng.integers(people))}" if rng.random() < 0.8 else None,
                "start": start,
                "end": end,
            }
        )
    return items


def random_segments(rng, count=80, sources=3):
    segments = []
    for number in range(count):
        start = round(float(rng.uniform(0, 100)), 1)
        segments.append(
            {
                "id": f"s{number}",
                "source_id": f"src{int(rng.integers(sources))}",
                "start": start,
                "end": round(start + float(rng.choice([1, 3, 6])), 1),
            }
        )
    return segments


def test_evidence_index_matches_linear_scan():
    from persons.person_names import EvidenceIndex, overlaps_for

    rng = np.random.default_rng(11)
    evidence = random_evidence(rng)
    index = EvidenceIndex(evidence)
    groups_options = [
        ["a0"],
        ["r1"],
        ["a0", "a1"],
        [["a0", "r2"], ["a3"]],
        [["a1", "a2", "r0"], ["r3", "a4"]],
        [["a5"], ["a0"], ["r1"]],
    ]
    checked = hits = 0
    for segment in random_segments(rng):
        for groups in groups_options:
            for relation in ("all", "any"):
                expected = overlaps_for(segment, evidence, groups, relation)
                assert overlaps_for(segment, index, groups, relation) == expected
                checked += 1
                hits += bool(expected)
    assert checked > 900 and hits > 50  # 样本确实有命中，不是空比较


def test_evidence_index_boundaries_are_half_open():
    from persons.person_names import EvidenceIndex

    evidence = [{"id": "e", "source_id": "s", "actor_id": "a", "role_id": None, "start": 1.0, "end": 2.0}]
    index = EvidenceIndex(evidence)
    assert index.overlapping("s", "a", 2.0, 3.0) == []  # 证据 end == 片段 start：不重叠
    assert index.overlapping("s", "a", 0.0, 1.0) == []  # 证据 start == 片段 end：不重叠
    assert index.overlapping("s", "a", 1.9, 2.5) == [0]
    assert index.overlapping("s", "other", 0, 9) == [] and index.overlapping("x", "a", 0, 9) == []


def old_coverage_missing(snapshots, coverage):
    by_source = {}
    for c in coverage:
        by_source.setdefault(c["source_id"], []).append((c["data"]["start"], c["data"]["end"]))
    missing = set()
    for s in snapshots:
        end = s["start"]
        for a, b in sorted(by_source.get(s["source_id"], [])):
            if a > end + 1e-6:
                break
            end = max(end, b)
            if end >= s["end"] - 1e-6:
                break
        if end < s["end"] - 1e-6:
            missing.add(s["source_id"])
    return sorted(missing)


def test_coverage_missing_matches_reference_implementation():
    from persons.person_names import coverage_missing

    rng = np.random.default_rng(5)
    outcomes = set()
    for trial in range(400):
        dense = trial % 2 == 0  # 稀疏与致密覆盖交替，两个分支（有缺失 / 完整覆盖）都要比对到
        lengths = [30, 60, 120] if dense else [0, 1, 5]
        coverage = [
            {
                "source_id": f"src{int(rng.integers(3))}",
                "data": {
                    "start": (a := round(float(rng.uniform(0, 100 if not dense else 20)), 1)),
                    "end": round(a + float(rng.choice(lengths)), 1),
                },
            }
            for _ in range(int(rng.integers(0, 14)))
        ]
        segments = random_segments(rng, count=int(rng.integers(1, 6)))
        expected = old_coverage_missing(segments, coverage)
        assert coverage_missing(segments, coverage) == expected
        outcomes.add(tuple(expected))
    assert () in outcomes and any(outcomes - {()})  # 既出现完整覆盖，也出现缺失
    assert len(outcomes) >= 5


def build_mapped_index(root, segments=40):
    """带 representations 表的真实布局：向量 id → (segment_id, view) 走映射而不是 chroma 元数据。"""
    store = Store(root)
    index = VectorIndex(root, "ix-mapped", create=True)
    rng = np.random.default_rng(3)
    for number in range(segments):
        segment = {"id": f"seg-{number}", "source_id": "src", "kind": "moment"}
        for view in ("video", "dialogue"):
            if view == "dialogue" and number % 3 == 0:
                continue
            identifier = f"{view}-{number}"
            vector = rng.normal(size=6)
            index.upsert(identifier, (vector / np.linalg.norm(vector)).tolist(), segment, view, "doc")
            store.execute(
                "INSERT INTO representations(id,index_id,segment_id,view,document) VALUES(?,?,?,?,?)",
                (identifier, "ix-mapped", segment["id"], view, "doc"),
            )
    return store, index


@pytest.mark.parametrize("view", ["video", "dialogue"])
def test_mapped_postfilter_matches_filtered_query_and_skips_chroma_metadata(tmp_path, view, monkeypatch):
    from processing import indexing

    indexing._REPRESENTATION_MAPS.clear()
    _, index = build_mapped_index(tmp_path)
    assert len(index._representation_map()) > 40
    includes = []
    original = type(index.collection).query

    def spy(self, *args, **kwargs):
        includes.append(tuple(kwargs.get("include", ())))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(index.collection), "query", spy)
    eligible = [f"seg-{n}" for n in range(1, 40, 2)]
    vector = np.random.default_rng(9).normal(size=6).tolist()
    result = index.query(vector, view, eligible, 8)
    monkeypatch.undo()
    assert includes and all(item == ("distances",) for item in includes)
    assert result == reference(index, vector, view, eligible, 8)


def test_mapped_postfilter_fetches_metadata_for_ids_missing_from_map(tmp_path):
    from processing import indexing

    indexing._REPRESENTATION_MAPS.clear()
    store, index = build_mapped_index(tmp_path)
    store.execute("DELETE FROM representations WHERE id IN ('video-1','video-3','dialogue-5')")
    indexing._REPRESENTATION_MAPS.clear()
    eligible = [f"seg-{n}" for n in range(40)]
    vector = np.random.default_rng(10).normal(size=6).tolist()
    for view in ("video", "dialogue"):
        assert index.query(vector, view, eligible, 50) == reference(index, vector, view, eligible, 50)


def test_candidates_are_shared_between_channels_for_the_same_vector(tmp_path, monkeypatch):
    from processing import indexing

    indexing._REPRESENTATION_MAPS.clear()
    _, index = build_mapped_index(tmp_path)
    calls = []
    original = type(index.collection).query

    def spy(self, *args, **kwargs):
        calls.append(kwargs["n_results"])
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(index.collection), "query", spy)
    vector = np.random.default_rng(12).normal(size=6).tolist()
    eligible = [f"seg-{n}" for n in range(40)]
    for view in ("video", "dialogue", "video"):
        index.query(vector, view, eligible, 5)
    monkeypatch.undo()
    assert len(calls) == 1  # 三次查询共用一次无过滤候选
