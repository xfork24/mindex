"""分页边界、参考一致性、SQL/池/网络总期限和缓存字节预算。"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from search.eligible_set import EligibleSet
from search.lexical_query import eligible_projection, sql_bm25
from search.search import SearchEngine, lexical_search
from shared.config import Config
from shared.query_budget import QueryBudgetExceeded, deadline, http_request, scope
from shared.storage import Store
from shared.types import Filters
from test_lexical_query import QUERIES, _reference_documents, _seed
from test_scaling_correctness import pg_config as pg_config


@pytest.mark.parametrize("backend", ["sqlite", pytest.param("postgres", marks=pytest.mark.requires_postgres)])
def test_paged_relation_filters_bm25_and_lookup_match_reference(backend, tmp_path, request):
    cfg = (
        request.getfixturevalue("pg_config") if backend == "postgres" else Config(data_dir=tmp_path / "local")
    )
    with Store.open(cfg) as store:
        index_id = _seed(store)
        store.execute("INSERT INTO segment_curation(id,hidden) VALUES('seg2',1)")
        # 父镜头隐藏与 Unicode casefold 的过滤不依赖 SQL lower。
        store.execute(
            "UPDATE search_segments SET annotation_projection=? WHERE segment_id='seg6'",
            (json.dumps({"scene": ["Straße 夜景"], "emotions": [], "shot_size": ["远景"]}),),
        )
        for filters, sources in [
            (Filters(), None),
            (Filters(scene="STRASSE"), None),
            (Filters(min_duration=3, max_duration=5), {"src"}),
            (Filters(), set()),
        ]:
            expected = eligible_projection(store, index_id, "shot", filters, sources)
            expected_ids = {row["id"] for row in expected}
            with scope(store, 5):
                relation = EligibleSet.build(store, index_id, "shot", filters, sources, page_size=2)
                pages = list(relation.ids.pages())
                assert all(len(page) <= 2 for page in pages)
                assert {item for page in pages for item in page} == expected_ids
                assert len(relation) == len(expected_ids)
                assert set(relation.lookup(expected_ids)) == expected_ids
                for channel in ("analysis", "dialogue"):
                    for query in QUERIES:
                        actual = sql_bm25(store, index_id, channel, query, relation.ids, 100)
                        reference = lexical_search(
                            query, _reference_documents(store, index_id, channel, expected_ids), 100
                        )
                        assert [hit["id"] for hit in actual] == [hit["id"] for hit in reference]
                        assert [hit["score"] for hit in actual] == pytest.approx(
                            [hit["score"] for hit in reference], abs=1e-10
                        )


@pytest.mark.requires_postgres
def test_pg_snapshot_and_sql_deadline_release_connection(pg_config):
    with Store.open(pg_config) as store:
        store.execute("INSERT INTO settings(key,value) VALUES('snapshot-test','before')")
        with scope(store, 5):
            assert store.one("SELECT value FROM settings WHERE key='snapshot-test'")["value"] == "before"
            with store.backend.connect() as other:
                other.execute("UPDATE settings SET value='after' WHERE key='snapshot-test'")
            assert store.one("SELECT value FROM settings WHERE key='snapshot-test'")["value"] == "before"
        assert store.one("SELECT value FROM settings WHERE key='snapshot-test'")["value"] == "after"
        started = time.monotonic()
        with pytest.raises(QueryBudgetExceeded), scope(store, 0.15):
            store.one("SELECT pg_sleep(2)")
        assert time.monotonic() - started < 0.8
        assert store.one("SELECT 1 AS ok")["ok"] == 1


@pytest.mark.requires_postgres
def test_pool_wait_is_within_query_deadline_and_control_still_works(pg_config):
    from contextlib import ExitStack

    with Store.open(pg_config) as store, ExitStack() as stack:
        for _ in range(store.backend.pool_max):
            stack.enter_context(store.backend.connect())
        started = time.monotonic()
        with pytest.raises(QueryBudgetExceeded), deadline(0.15):
            with store.backend.connect():
                pytest.fail("查询池已耗尽，不能获得连接")
        assert time.monotonic() - started < 0.8
        with store.backend.connect(control=True) as control:
            assert control.execute("SELECT 1").fetchone()[0] == 1


def test_slow_drip_http_obeys_total_deadline_and_closes_socket():
    stopped = threading.Event()

    class Drip(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    if stopped.wait(0.02):
                        break
            except (BrokenPipeError, ConnectionResetError):
                stopped.set()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        with pytest.raises(QueryBudgetExceeded), deadline(0.15):
            http_request("GET", f"http://127.0.0.1:{server.server_port}", timeout=2, trust_env=False)
        assert time.monotonic() - started < 0.8
        assert stopped.wait(1)
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_vector_cache_enforces_bytes_count_and_disk_budget(tmp_path):
    import search.search as module

    cfg = Config(data_dir=tmp_path, search={"query_cache_bytes": 1800, "query_cache_entries": 2})
    engine = SearchEngine(cfg)
    engine._space_id = "space"
    try:
        module._QUERY_VECTORS.clear()
        for number in range(8):
            engine._cached_vector("text", str(number), lambda number=number: [float(number)] * 10)
            assert len(module._QUERY_VECTORS) <= 2
            assert sum(module._QUERY_VECTOR_SIZES.values()) <= 1800
            assert (
                sum(path.stat().st_size for path in (tmp_path / "cache/query-vectors").glob("*.json")) <= 1800
            )
        cfg.search.query_cache_bytes = 0
        engine._prune_vector_cache()
        assert not module._QUERY_VECTORS
    finally:
        engine.store.close()


@pytest.mark.requires_postgres
@pytest.mark.parametrize(
    "query",
    [
        {"text": "城市", "channels": ["analysis"]},
        {"text": "主角", "person_mode": "onscreen"},
        {"text": "主角", "person_mode": "onscreen", "target_duration": 10},
        {"text": "主角 城市", "person_mode": "onscreen", "channels": ["analysis"]},
        {"text": "主角", "person_mode": "mentioned"},
    ],
)
def test_paged_person_and_channel_search_matches_reference(pg_config, monkeypatch, query):
    from models.adapters import ModelError
    from shared.storage import dumps
    from shared.types import TextSearch

    class NoModel:
        def identity(self):
            raise ModelError("离线参考只验证 SQL 词法与人物排序")

    with Store.open(pg_config) as store:
        index_id = _seed(store)
        store.execute("INSERT INTO settings(key,value) VALUES('active',?)", (index_id,))
        entities = [
            {"id": "work", "name": "作品", "kind": "work"},
            {"id": "actor", "name": "演员", "kind": "actor"},
            {"id": "role", "name": "主角", "kind": "role", "work_ids": ["work"], "actor_ids": ["actor"]},
        ]
        for entity in entities:
            store.execute(
                "INSERT INTO person_entities(id,kind,data) VALUES(?,?,?)",
                (entity["id"], entity["kind"], dumps(entity)),
            )
        version = {
            "format": 2,
            "entities": entities,
            "evidence": [
                {
                    "id": f"e{number}",
                    "source_id": "src",
                    "shot_id": sid,
                    "actor_id": "actor",
                    "role_id": "role",
                    "start": start,
                    "end": end,
                    "status": "accepted",
                    "data": {"score": 0.9},
                }
                for number, (sid, start, end) in enumerate(
                    [("seg1", 0, 3), ("seg3", 5, 10), ("seg6", 11, 21)]
                )
            ],
            "coverage": [{"source_id": "src", "data": {"start": 0, "end": 30}}],
        }
        store.execute("INSERT INTO person_versions(id,data) VALUES('pv',?)", (dumps(version),))
        store.execute(
            "INSERT INTO search_releases(id,content_id,person_id) VALUES('release',?,'pv')", (index_id,)
        )
        store.execute("INSERT INTO settings(key,value) VALUES('active.release','release')")
    pg_config.search.id_page_size = 2
    pg_config.search.candidates = 1  # 候选页不能截断纯人物的 eligible 或改变 IoU 去重结果。
    with_engine = SearchEngine(pg_config, NoModel())
    # The query validates the same frozen metadata with both implementations; no fabricated vectors.
    request = TextSearch(granularity="shot", **query)
    try:
        monkeypatch.setattr(with_engine, "_vectors", lambda _: None)
        actual = with_engine.text(request)
        monkeypatch.setattr(with_engine, "_paged", lambda _: False)
        reference = with_engine.text(request)
        actual.pop("elapsed_ms")
        reference.pop("elapsed_ms")
        assert actual == reference
    finally:
        with_engine.store.close()


@pytest.mark.requires_postgres
def test_api_pool_timeout_returns_retryable_503_and_live_stays_up(pg_config):
    from contextlib import ExitStack

    from fastapi.testclient import TestClient

    from api import create_app

    pg_config.search.query_timeout_seconds = 0.15
    with TestClient(create_app(pg_config)) as client, ExitStack() as held:
        store = client.app.state.store
        for _ in range(store.backend.pool_max):
            held.enter_context(store.backend.connect())
        started = time.monotonic()
        response = client.post("/api/search/text", json={"text": "城市", "granularity": "shot"})
        assert response.status_code == 503, response.text
        assert response.json()["retryable"] and response.json()["code"] == "query_budget_exhausted"
        assert response.headers["retry-after"] == "1"
        assert time.monotonic() - started < 1
        assert client.get("/api/live").status_code == 200


def test_narrow_vector_scope_falls_back_to_all_id_pages_without_widening(tmp_path):
    from processing.indexing import VectorIndex

    class Collection:
        filtered = 0

        def count(self):
            return 1_000_000

        def query(self, *, query_embeddings, n_results, where, include):
            identifiers = next(
                (part["segment_id"]["$in"] for part in where.get("$and", []) if "segment_id" in part), None
            )
            if identifiers is not None:
                self.filtered += 1
                values = [(sid, {"seg6": 0.1, "seg1": 0.5}[sid]) for sid in identifiers]
            else:
                values = [(f"outside-{n}", 0.001) for n in range(n_results)]
            values.sort(key=lambda item: (item[1], item[0]))
            return {
                "ids": [[v[0] for v in values]],
                "metadatas": [[{"segment_id": v[0]} for v in values]],
                "distances": [[v[1] for v in values]],
            }

    with Store.open(Config(data_dir=tmp_path)) as store:
        index_id = _seed(store)
        with scope(store, 5):
            relation = EligibleSet.build(store, index_id, "shot", Filters(scene="夜"), {"src"})
            index = VectorIndex.__new__(VectorIndex)
            index.collection = Collection()
            hits = index.query([1, 0], "video", relation.ids, 2)
            assert [hit["id"] for hit in hits] == ["seg6", "seg1"]
            assert index.collection.filtered and relation.ids.matching(h["id"] for h in hits) == {
                "seg1",
                "seg6",
            }
