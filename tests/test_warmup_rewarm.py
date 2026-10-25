"""发布索引/人物版本后的后台自动重新预热：签名要灵敏（该变化的变化）、也要迟钝（无关写入不触发）。"""

import json
import threading
import time

import pytest

from mindex.processing.indexing import VectorIndex
from mindex.search import warmup
from mindex.shared import storage
from mindex.shared.config import Config
from mindex.shared.storage import Store, bump_snapshot_revision

SPEC = json.dumps({"format": 2, "mode": "auto", "space": {}, "preprocessing": {}})


def add_index(store, index_id, members=2, status="ready"):
    store.execute("INSERT INTO index_versions(id,spec,status) VALUES(?,?,?)", (index_id, SPEC, status))
    for number in range(members):
        snapshot = {"id": f"{index_id}-seg{number}", "source_id": "src", "kind": "moment", "start": 0, "end": 1,
                    "views": {"dialogue": f"对白{number}", "analysis": f"分析{number}"},
                    "indexed_views": ["video", "dialogue"]}
        store.execute("INSERT INTO index_members VALUES(?,?,?,1)",
                      (index_id, snapshot["id"], json.dumps(snapshot, ensure_ascii=False)))
    VectorIndex(store.root, index_id, create=True)


def wait_for(condition, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "data")
    add_index(store, "A")
    add_index(store, "B")
    store.activate("A")
    return store


# ---------- 签名 ----------


def test_signature_changes_on_activation_release_revision_and_status(store):
    base = warmup.serving_signature(store)

    store.activate("B")
    after_activation = warmup.serving_signature(store)
    assert after_activation != base

    store.execute("INSERT INTO person_versions(id,data) VALUES('pv','{}')")
    store.execute("INSERT INTO search_releases(id,content_id,person_id) VALUES('rel-new','B','pv')")
    after_release = warmup.serving_signature(store)  # 发布人物版本新增一条发布记录
    assert after_release != after_activation

    with store.connect() as db:
        bump_snapshot_revision(db)  # 维护操作原地改写快照
    after_revision = warmup.serving_signature(store)
    assert after_revision != after_release

    store.execute("UPDATE index_versions SET status='building' WHERE id='B'")
    assert warmup.serving_signature(store) != after_revision


def test_signature_ignores_unrelated_writes(store):
    base = warmup.serving_signature(store)
    store.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('theme','dark')")
    store.execute("INSERT INTO runs(stage,source_id,status) VALUES('prepare',NULL,'running')")
    add_index(store, "C", status="building")  # 正在构建的别的版本
    store.execute("INSERT INTO person_versions(id,data) VALUES('pv2','{}')")
    store.execute("INSERT INTO search_releases(id,content_id,person_id) VALUES('rel-other','C','pv2')")
    assert warmup.serving_signature(store) == base


def test_pinned_preview_ignores_active_pointer_but_sees_its_own_releases(store):
    add_index(store, "C")
    pinned = warmup.serving_signature(store, "B")
    store.activate("C")  # 活动指针移到别的索引，与固定到 B 的预览无关
    assert warmup.serving_signature(store, "B") == pinned
    store.execute("INSERT INTO person_versions(id,data) VALUES('pv','{}')")
    store.execute("INSERT INTO search_releases(id,content_id,person_id) VALUES('rel-b','B','pv')")
    assert warmup.serving_signature(store, "B") != pinned  # B 自己的新发布记录要感知
    after_release = warmup.serving_signature(store, "B")
    with store.connect() as db:
        bump_snapshot_revision(db)
    assert warmup.serving_signature(store, "B") != after_release


def test_signature_without_active_index_is_stable(tmp_path):
    store = Store(tmp_path / "empty")
    assert warmup.serving_signature(store) == warmup.serving_signature(store)


# ---------- 监听线程 ----------


def run_watcher(config, index_id, calls, last, interval=0.03, debounce=0.01):
    stop = threading.Event()

    def fake_warm(effective, requested_index, *, startup):
        calls.append((requested_index, startup))

    thread = threading.Thread(
        target=warmup.watch_and_rewarm, args=(config, index_id, stop),
        kwargs={"interval": interval, "debounce": debounce, "warm": fake_warm, "last": last}, daemon=True)
    thread.start()
    return stop, thread


def test_watcher_rewarms_once_per_change_and_not_when_idle(store):
    config = Config(data_dir=store.root)
    calls = []
    stop, thread = run_watcher(config, None, calls, warmup.serving_signature(store))
    try:
        time.sleep(0.4)
        assert calls == []  # 空闲不重复预热

        store.activate("B")
        assert wait_for(lambda: len(calls) == 1)
        time.sleep(0.3)
        assert calls == [(None, False)]  # 一次发布只预热一次，且跳过词典/模型文件校验

        store.execute("INSERT INTO person_versions(id,data) VALUES('pv','{}')")
        store.execute("INSERT INTO search_releases(id,content_id,person_id) VALUES('rel','B','pv')")
        assert wait_for(lambda: len(calls) == 2)
    finally:
        stop.set()
        thread.join(2)
    assert not thread.is_alive()


def test_watcher_survives_warm_failures_and_retries_on_next_change(store):
    config = Config(data_dir=store.root)
    calls = []
    stop = threading.Event()

    def flaky(effective, requested_index, *, startup):
        calls.append(startup)
        if len(calls) == 1:
            raise RuntimeError("嵌入服务暂时不可用")

    thread = threading.Thread(
        target=warmup.watch_and_rewarm, args=(config, None, stop),
        kwargs={"interval": 0.03, "debounce": 0.01, "warm": flaky, "last": warmup.serving_signature(store)},
        daemon=True)
    thread.start()
    try:
        store.activate("B")
        assert wait_for(lambda: len(calls) == 1)
        time.sleep(0.3)
        assert len(calls) == 1 and thread.is_alive()  # 失败不死循环重试，线程仍存活
        store.activate("A")
        assert wait_for(lambda: len(calls) == 2)  # 下一次变化照常预热
    finally:
        stop.set()
        thread.join(2)


def test_change_during_initial_warmup_triggers_another_warmup(store, monkeypatch):
    """预热期间发生的发布：基线签名先于预热取得，所以监听线程会补一次。"""
    config = Config(data_dir=store.root)
    calls = []

    def fake_warm(effective, requested_index=None, *, startup=True):
        calls.append(startup)
        if startup:
            store.activate("B")  # 首轮预热期间发生了发布

    monkeypatch.setattr(warmup, "warm_up", fake_warm)
    thread = warmup.warm_up_in_background(config, None, interval=0.03)
    try:
        assert wait_for(lambda: calls == [True, False])
    finally:
        thread.stop_event.set()
        thread.join(2)


def test_no_watch_runs_a_single_warmup(store, monkeypatch):
    calls = []
    monkeypatch.setattr(warmup, "warm_up", lambda *a, **k: calls.append(k.get("startup", True)))
    thread = warmup.warm_up_in_background(Config(data_dir=store.root), None, watch=False)
    thread.join(3)
    assert calls == [True] and not thread.is_alive()


# ---------- 真实预热：切换活动索引后新索引的快照已进缓存 ----------


def test_rewarm_after_activation_loads_new_index_into_caches(store):
    config = Config(data_dir=store.root)
    storage._SNAPSHOT_CACHE.clear()
    thread = warmup.warm_up_in_background(config, None, interval=0.05)
    path = str(store.path)
    try:
        assert wait_for(lambda: (path, "A") in storage._SNAPSHOT_CACHE)
        assert (path, "B") not in storage._SNAPSHOT_CACHE

        store.activate("B")
        assert wait_for(lambda: (path, "B") in storage._SNAPSHOT_CACHE, timeout=10)
        assert [s["id"] for s in storage._SNAPSHOT_CACHE[(path, "B")][1]] == ["B-seg0", "B-seg1"]
        # 切换后旧版本不再被默认路径使用，预热完成时已被淘汰
        assert wait_for(lambda: (path, "A") not in storage._SNAPSHOT_CACHE, timeout=10)
    finally:
        thread.stop_event.set()
        thread.join(5)


# ---------- 淘汰已被取代的旧版本 ----------


def test_retain_snapshots_only_touches_this_store_and_non_kept_versions(store, tmp_path):
    other = Store(tmp_path / "other")
    add_index(other, "A")
    storage._SNAPSHOT_CACHE.clear()
    for target in (store, other):
        target.snapshots_shared("A")
        target.snapshots_shared("B") if target is store else None
    assert {k[1] for k in storage._SNAPSHOT_CACHE if k[0] == str(store.path)} == {"A", "B"}

    assert store.retain_snapshots({"B"}) == 1
    assert {k[1] for k in storage._SNAPSHOT_CACHE if k[0] == str(store.path)} == {"B"}
    assert (str(other.path), "A") in storage._SNAPSHOT_CACHE  # 别的数据目录不受影响
    assert store.retain_snapshots({"B"}) == 0


def test_retain_representation_maps(tmp_path):
    from mindex.processing import indexing

    store = Store(tmp_path / "data")
    for index_id in ("old", "new"):
        add_index(store, index_id)
        store.execute("INSERT INTO representations(id,index_id,segment_id,view,document) VALUES(?,?,?,?,?)",
                      (f"rep-{index_id}", index_id, f"{index_id}-seg0", "video", "doc"))
        VectorIndex(store.root, index_id)._representation_map()
    resolved = str(store.root.resolve())
    assert {(resolved, "old"), (resolved, "new")} <= set(indexing._REPRESENTATION_MAPS)
    assert indexing.retain_representation_maps(store.root, {"new"}) == 1
    assert (resolved, "old") not in indexing._REPRESENTATION_MAPS and (resolved, "new") in indexing._REPRESENTATION_MAPS


def test_retain_published_person_versions(tmp_path):
    from test_search_latency import put_person_release

    from mindex.persons.persons import _ENTITY_CACHE, _EVIDENCE_CACHE, Persons

    store = Store(tmp_path / "data")
    catalogue = Persons(store)
    for number in (1, 2):
        put_person_release(store, content_id=f"ix{number}", person_id=f"pv{number}", release_id=f"rel{number}")
        release = catalogue.release_for(f"ix{number}", pinned=True)
        catalogue.published_entities(release)
        catalogue.published_evidence(release)
    path = str(store.path)
    names = lambda cache: {k[1] for k in cache if k[0] == path}
    assert names(_ENTITY_CACHE) == names(_EVIDENCE_CACHE) == {"pv1", "pv2"}
    assert catalogue.retain_published({"pv2"}) == 2
    assert names(_ENTITY_CACHE) == names(_EVIDENCE_CACHE) == {"pv2"}


def test_retire_superseded_keeps_only_active_in_normal_mode(store):
    storage._SNAPSHOT_CACHE.clear()
    path = str(store.path)
    store.snapshots_shared("A")
    store.snapshots_shared("B")
    assert {k[1] for k in storage._SNAPSHOT_CACHE if k[0] == path} == {"A", "B"}
    store.activate("B")  # 活动指针移到 B 后，目标是 B：A 不再被默认路径使用
    warmup.retire_superseded(store, store.active_index(), None)
    assert {k[1] for k in storage._SNAPSHOT_CACHE if k[0] == path} == {"B"}


def test_retire_superseded_keeps_pinned_target_and_active_pointer(store):
    storage._SNAPSHOT_CACHE.clear()
    path = str(store.path)
    add_index(store, "C")
    # 缓存容量按数据目录算 2 个，所以分两步放入，覆盖「固定 C，活动指针在 A」的情形
    store.snapshots_shared("A")
    store.snapshots_shared("C")
    warmup.retire_superseded(store, store.index_version("C"), "C")
    assert {k[1] for k in storage._SNAPSHOT_CACHE if k[0] == path} == {"A", "C"}  # 固定的 C 与活动指针所指的 A 都保留

    store.snapshots_shared("B")  # 第三个版本进来时按容量挤掉最久未用的一个
    assert len({k for k in storage._SNAPSHOT_CACHE if k[0] == path}) == 2
    warmup.retire_superseded(store, store.index_version("C"), "C")
    assert {k[1] for k in storage._SNAPSHOT_CACHE if k[0] == path} <= {"A", "C"}  # 无关的 B 被淘汰
