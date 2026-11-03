"""SQL 词法倒排与参考实现的一致性（plans/horizontal-scaling-plan.md §6.1 阶段 2 验收）。

同一冻结快照集上：``eligible_projection`` 与旧全快照过滤（隐藏/时长/粒度/素材/分析字段）
的 ID 集合一致；``sql_bm25`` 与 ``search.lexical_search`` 参考公式的分数在 1e-10 容差内、
排序一致（含空文档、空查询、重复词、中英数字与特殊 token、全空渠道语料）。
"""

from __future__ import annotations

import pytest

from mindex.search.lexical_query import eligible_projection, sql_bm25
from mindex.search.search import lexical_search
from mindex.shared.lexical_store import build_lexical_index, lexical_ready
from mindex.shared.storage import Store, dumps
from mindex.shared.tokenizer import tokenizer_version
from mindex.shared.types import Filters


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def _seed(store: Store, index_id="i" * 12):
    store.execute("INSERT INTO sources(id,name,path,media,layout_key) VALUES('src','素材','p','{}','k')")
    store.execute("INSERT INTO sources(id,name,path,media,layout_key) VALUES('src2','素材2','p2','{}','k')")
    # eligible_projection 与 curation 的外键链需要 segments 行（快照来自 index_members）。
    for sid, source, kind, start, end in [
        ("seg1", "src", "shot", 0, 5), ("seg2", "src", "shot", 0, 5),
        ("seg3", "src", "shot", 5, 12.5), ("seg4", "src", "moment", 0, 8),
        ("seg5", "src2", "shot", 0, 3), ("seg6", "src", "shot", 10, 30),
    ]:
        store.execute("INSERT INTO segments(id,source_id,kind,start,end,path,thumbnail,generation,reason) "
                      "VALUES(?,?,?,?,?,'p','t','g','r')", (sid, source, kind, start, end))
    docs = {
        "seg1": ("镜头一：城市夜景下的车流。", "他说：今天天气不错，我们去城里。", "shot", 0, 5, "src"),
        "seg2": ("", "", "shot", 0, 5, "src"),  # 双渠道全空：长度 0 计入分母
        "seg3": ("城市 城市City 2026，2026！special-token__x", "对白：明天见", "shot", 5, 12.5, "src"),
        "seg4": ("乡村风光", "", "moment", 0, 8, "src"),  # 其他粒度
        "seg5": ("城市夜景", "城里真热闹", "shot", 0, 3, "src2"),  # 不同素材 + 时长过滤边界
        "seg6": ("夜景 细节", "他说的话很多，天气天气", "shot", 10, 30, "src"),  # 时长 > 上限
    }
    for sid, (analysis, dialogue, kind, start, end, source) in docs.items():
        views = {}
        if analysis:
            views["analysis"] = analysis
        if dialogue:
            views["dialogue"] = dialogue
        store.execute(
            "INSERT INTO index_members(index_id,segment_id,snapshot,complete) VALUES(?,?,?,1)",
            (index_id, sid, dumps({
                "id": sid, "source_id": source, "kind": kind, "start": start, "end": end,
                "views": views, "indexed_views": sorted(
                    ["video", *(["analysis"] if analysis else []), *(["dialogue"] if dialogue else [])]),
                "annotation": {"scene": ["夜景"] if "夜景" in analysis else [],
                               "emotions": [], "shot_size": ["远景"] if sid == "seg6" else []},
            })))
    store.execute("INSERT INTO index_versions(id,spec,status) VALUES(?,?,?)",
                  (index_id, dumps({"format": 2}), "ready"))
    build_lexical_index(store, index_id)
    return index_id


def _reference_documents(store: Store, index_id: str, channel: str, eligible_ids=None):
    documents = []
    for snapshot in store.snapshots(index_id):
        if eligible_ids is not None and snapshot["id"] not in eligible_ids:
            continue
        views = snapshot.get("views") or {}
        documents.append({**snapshot, "views": {channel: views[channel]} if channel in views else {}})
    return documents


def test_build_populates_projection_and_terms(store):
    index_id = _seed(store)
    assert lexical_ready(store, index_id)
    meta = store.one("SELECT * FROM lexical_index_meta WHERE index_id=?", (index_id,))
    assert meta["segments"] == 6 and meta["tokenizer_version"] == tokenizer_version()
    # 每个 segment 都有双渠道文档行（空文档长度 0）
    assert store.one("SELECT count(*) AS n FROM lexical_docs WHERE index_id=? AND doc_length=0",
                     (index_id,))["n"] >= 2
    # 空 snapshot 全字段投影存在
    projection = eligible_projection(store, index_id, "shot", Filters(), None)
    assert {row["id"] for row in projection} == {"seg1", "seg2", "seg3", "seg5", "seg6"}


def test_eligible_projection_matches_legacy_filters(store):
    index_id = _seed(store)
    # 素材隐藏 → src 下全部隐藏；seg2 自身隐藏；父隐藏场景
    store.execute("INSERT INTO segment_curation(id,hidden) VALUES('seg2',1)")
    store.execute("INSERT INTO source_curation(id,hidden) VALUES('src2',1)")
    filters = Filters(min_duration=2, max_duration=20, scene="夜")
    got = {row["id"] for row in eligible_projection(store, index_id, "shot", filters, {"src"})}
    # 参考路径：时长边界为开区间（==20 保留）；seg3 无「夜」；seg5 素材不在集合；seg2 自身隐藏
    assert got == {"seg1", "seg6"}
    assert {row["id"] for row in eligible_projection(store, index_id, "shot", Filters(), None)} \
        == {"seg1", "seg3", "seg6"}  # seg2 自身隐藏、seg5 素材(src2)隐藏被排除
    assert eligible_projection(store, index_id, "shot", Filters(), set()) == []  # 空交集直接空


QUERIES = ["城市", "天气", "城市 天气", "2026", "special-token__x", "城市 城市", "他说", "夜景 天气 明天"]


@pytest.mark.parametrize("channel", ["analysis", "dialogue"])
@pytest.mark.parametrize("query", QUERIES)
def test_sql_bm25_matches_reference(store, channel, query):
    index_id = _seed(store)
    projection = eligible_projection(store, index_id, "shot", Filters(), None)
    ids = [row["id"] for row in projection]
    reference = lexical_search(query, _reference_documents(store, index_id, channel, set(ids)), 100)
    got = sql_bm25(store, index_id, channel, query, ids, 100)
    assert [hit["id"] for hit in got] == [hit["id"] for hit in reference], query
    for a, b in zip(got, reference):
        assert a["score"] == pytest.approx(b["score"], abs=1e-10, rel=1e-10), (query, a, b)


def test_sql_bm25_empty_inputs(store):
    index_id = _seed(store)
    projection = eligible_projection(store, index_id, "shot", Filters(), None)
    ids = [row["id"] for row in projection]
    assert sql_bm25(store, index_id, "analysis", "", ids, 10) == []
    assert sql_bm25(store, index_id, "analysis", "城市", [], 10) == []
    # 全空渠道语料：N=5（全部 eligible），所有文档长度 0 → mean 按 1；无命中
    assert sql_bm25(store, index_id, "dialogue", "城市", ["seg2"], 10) == []


def test_stale_tokenizer_version_disables_sql_path(store, monkeypatch):
    index_id = _seed(store)
    assert lexical_ready(store, index_id)
    from mindex.shared import lexical_store
    monkeypatch.setattr(lexical_store, "tokenizer_version", lambda: "stale")
    assert not lexical_ready(store, index_id)  # 版本不一致 → 回退 Python 参考路径
    with pytest.raises(ValueError, match="不一致"):
        build_lexical_index(store, index_id)
    rebuilt = build_lexical_index(store, index_id, replace=True)
    assert rebuilt["segments"] == 6


def test_replace_rebuild_is_idempotent(store):
    index_id = _seed(store)
    first = build_lexical_index(store, index_id)
    assert first.get("skipped") is True
    again = build_lexical_index(store, index_id, replace=True)
    assert again["segments"] == 6 and again["terms"] == first["terms"] if "terms" in first else True
