"""服务启动后的后台预热：把首条文字检索会付出的一次性代价提前支付。

首次检索原本要做的事：对 GGUF/mmproj 与基线样本算 SHA256（数秒）、加载 jieba 词典、
读取并解析全部快照、解析 BM25 语料的分词、打开 Chroma 并把 HNSW 读进内存。
这里全部尽力而为：任何一步失败（例如嵌入服务此刻没开）只记日志，不影响服务运行，
首次真实检索时会按原路径再做一次。

发布新索引、切换活动索引、发布人物版本或维护操作改写快照后，旧缓存随签名失效，下一次查询
会重新付一遍代价。发布可能发生在别的进程（命令行、人物任务），所以后台线程轮询一个很便宜的
「服务状态签名」，变化后自动重新预热，把这笔代价从用户的查询里挪走。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from time import perf_counter

from mindex.shared.config import Config

log = logging.getLogger(__name__)

# 轮询状态签名的间隔；检测到变化后再等一个防抖间隔，让一次发布的多步写入落定后再预热。
WATCH_INTERVAL_SECONDS = 5.0
WATCH_DEBOUNCE_SECONDS = 2.0


def _step(name: str, action) -> None:
    started = perf_counter()
    try:
        action()
    except Exception as exc:  # noqa: BLE001 — 预热是优化而非功能，失败不得影响服务
        log.warning("预热跳过「%s」：%s", name, exc)
        return
    log.info("预热「%s」完成，用时 %.2f 秒", name, perf_counter() - started)


def _warm_model_files(config: Config) -> None:
    if config.embedding.backend != "llama_cpp":
        return
    from mindex.models import compatibility, llama_cpp
    from mindex.models.deployment import verified_file

    pinned = llama_cpp.manifest(config.embedding, "embedding")
    if pinned["schema"] == 2:
        # 与真实检索一致：按服务当前报告的路径定位迁移后的权重/投影，
        # 核对原清单的内容摘要，而不是重新读取清单里的历史路径。
        llama_cpp.checked_runtime(config.embedding, pinned)
        compatibility.load_baseline(config, pinned["space_contract"])
    else:
        for record in pinned.get("files", {}).values():
            verified_file(Path(record["path"]))


def warm_up(config: Config, index_id: str | None = None, *, startup: bool = True) -> None:
    """config 须是已合并设置页覆盖的生效配置。startup=False 为重新预热：跳过词典与模型文件校验。"""
    import jieba

    from mindex.persons.persons import Persons
    from mindex.processing.indexing import VectorIndex
    from mindex.search.search import lexical_search, tokens
    from mindex.shared.storage import Store

    store = Store.open(config)
    active = store.index_version(index_id) if index_id else store.active_index()
    state: dict = {}

    if startup:
        _step("分词词典", lambda: (jieba.initialize(), tokens("预热")))
        _step("嵌入模型文件校验", lambda: _warm_model_files(config))
    if active is None or active.get("status") != "ready":
        log.info("没有就绪的活动索引，跳过索引相关预热")
        return

    def snapshots():
        state["snapshots"] = store.snapshots_shared(active["id"])

    def people():
        catalogue = Persons(store)
        release = catalogue.release_for(
            active["id"], pinned=index_id is not None, release_id=active.get("search_release_id")
        )
        catalogue.published_entities(release)
        catalogue.published_evidence(release)  # 解析证据并建立区间索引，出镜检索的首次查询不再付这笔

    def corpus():
        if active["spec"].get("format", 1) < 2:
            return
        # SQL 词法路径直接读派生倒排表，不再需要进程内全量分词（plans §6.1）；
        # 派生索引缺失/版本不一致时保留参考路径的语料预热。
        from mindex.shared.lexical_store import lexical_ready

        if lexical_ready(store, active["id"]):
            log.info("派生词法索引可用，跳过 BM25 语料分词预热")
            return
        for channel in ("dialogue", "analysis"):
            documents = [
                {**s, "views": {channel: s["views"][channel]} if channel in s.get("views", {}) else {}}
                for s in state.get("snapshots", [])
            ]
            lexical_search("预热", documents, 1)

    def vectors():
        from mindex.processing.indexing import vector_client

        index = VectorIndex(store.root, active["id"],
                            client=vector_client(config, library_id=store.library_id))
        if not store.pg:
            index._representation_map()
        collection = index.collection
        if collection.count():
            vector = [1.0] + [0.0] * (config.embedding.dimensions - 1)
            collection.query(query_embeddings=[vector], n_results=1)

    if not store.pg:
        _step("索引快照", snapshots)
        _step("人物实体与证据索引", people)
        _step("BM25 语料分词", corpus)
    _step("向量索引加载", vectors)
    _step("淘汰已被取代的旧版本缓存", lambda: retire_superseded(store, active, index_id))


def retire_superseded(store, target: dict, index_id: str | None) -> dict:
    """预热完成后，淘汰不再被默认路径使用的旧索引版本缓存，避免每次发布多占一份内存。

    保留：目标索引；固定预览模式下还保留活动指针所指的索引（Studio 等仍按活动索引查询）。
    """
    from mindex.persons.persons import Persons
    from mindex.processing.indexing import retain_representation_maps

    keep_indexes = {target["id"]}
    releases = [Persons(store).release_for(
        target["id"], pinned=index_id is not None, release_id=target.get("search_release_id"))]
    if index_id:
        pointer = store.active_index()
        if pointer:
            keep_indexes.add(pointer["id"])
            releases.append(Persons(store).release_for(pointer["id"], release_id=pointer.get("search_release_id")))
    keep_people = {r["person_id"] for r in releases if r and r.get("person_id")}
    removed = {
        "快照": store.retain_snapshots(keep_indexes),
        "人物版本": Persons(store).retain_published(keep_people),
        "向量映射": retain_representation_maps(store.root, keep_indexes),
    }
    if any(removed.values()):
        log.info("已淘汰旧版本缓存：%s。Chroma 已加载的旧 HNSW 无法在进程内释放，重启服务可回收", removed)
    return removed


def serving_signature(store, index_id: str | None = None) -> tuple:
    """检索所依赖的服务状态签名，几条主键查询即可算出，变化即意味着缓存需要重建。

    包含：活动索引与活动发布指针、快照修订号（维护操作原地改写快照时更新）、目标索引的状态，
    以及该索引的发布记录数与最新记录（发布人物版本会新增记录）。固定预览（index_id）不看活动指针。
    """
    keys = ("snapshots.rev",) if index_id else ("active", "active.release", "snapshots.rev")
    settings = tuple(sorted(
        (row["key"], row["value"])
        for row in store.rows(
            f"SELECT key,value FROM settings WHERE key IN ({','.join('?' * len(keys))})", keys
        )
    ))
    target = index_id or dict(settings).get("active")
    if target is None:
        return (settings, None)
    version = store.one("SELECT status FROM index_versions WHERE id=?", (target,))
    releases = store.one(
        "SELECT count(*) AS n, COALESCE(max(rowid),0) AS latest FROM search_releases WHERE content_id=?", (target,)
    )
    return (settings, target, version["status"] if version else None, releases["n"], releases["latest"])


def watch_and_rewarm(
    config: Config,
    index_id: str | None,
    stop: threading.Event,
    *,
    interval: float = WATCH_INTERVAL_SECONDS,
    debounce: float = WATCH_DEBOUNCE_SECONDS,
    warm: Callable[..., None] | None = None,
    last: tuple | None = None,
) -> None:
    """轮询服务状态签名，变化后在本线程里重新预热，直到 stop 被设置。

    last 是已预热状态的签名；预热期间又发生的变化会在下一轮被发现并再预热一次。
    """
    from mindex.settings.runtime_settings import RuntimeConfig
    from mindex.shared.storage import Store

    store = Store.open(config)
    warm = warm or warm_up
    while not stop.wait(interval):
        try:
            current = serving_signature(store, index_id)
            if current == last:
                continue
            if stop.wait(debounce):
                return
            current = serving_signature(store, index_id)  # 防抖后取最新状态，避免预热到半截的发布
            log.info("检测到索引/人物发布状态变化，后台重新预热")
            last = current
            warm(RuntimeConfig(config, store).current(), index_id, startup=False)
        except Exception as exc:  # noqa: BLE001 — 监听线程不得因一次失败退出
            log.warning("重新预热失败，等待下一次状态变化：%s", exc)


def warm_up_in_background(
    config: Config, index_id: str | None = None, *, watch: bool = True, interval: float = WATCH_INTERVAL_SECONDS
) -> threading.Thread:
    """启动后台线程：先做一次完整预热，之后（watch=True）持续监听发布并自动重新预热。

    线程是 daemon，随进程退出；线程对象上的 stop_event 可用来停止监听（测试用）。
    """
    from mindex.settings.runtime_settings import RuntimeConfig
    from mindex.shared.storage import Store

    stop = threading.Event()

    def run():
        store = Store.open(config)
        try:
            baseline = serving_signature(store, index_id)  # 先于预热取得：预热期间的发布会触发再次预热
        except Exception as exc:  # noqa: BLE001 — 读不到基线只影响再预热判断，线程继续
            log.warning("读取服务状态失败：%s", exc)
            baseline = None
        try:
            warm_up(RuntimeConfig(config, store).current(), index_id)
        except Exception as exc:  # noqa: BLE001 — 预热是优化而非功能，失败不得影响服务
            log.warning("预热失败：%s", exc)
        if watch:
            watch_and_rewarm(config, index_id, stop, interval=interval, last=baseline)

    thread = threading.Thread(target=run, name="search-warmup", daemon=True)
    thread.stop_event = stop  # type: ignore[attr-defined]
    thread.start()
    return thread
