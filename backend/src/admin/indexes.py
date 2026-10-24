"""内容索引版本管理：列表、一致性检查、激活/回退；安全约束与 `mindex indexes activate` 一致。"""

from __future__ import annotations

import json
import logging
import threading

from fastapi import APIRouter, HTTPException
from filelock import Timeout

from mindex.admin.curation import EntityNotFound
from mindex.models.adapters import ModelError, MultimodalEmbedder
from mindex.shared.processing_lock import ProcessingLock, index_write_lock

log = logging.getLogger(__name__)

INDEX_LOCK_TIMEOUT_SECONDS = 10


def _version_summary(row: dict) -> dict:
    spec = row["spec"] if isinstance(row["spec"], dict) else json.loads(row["spec"])
    return {
        "id": row["id"],
        "status": row["status"],
        "mode": spec.get("mode"),
        "format": spec.get("format", 1),
        "created_at": row["created_at"],
        "space_model": (spec.get("space") or {}).get("model"),
    }


def _active(store) -> dict | None:
    active = store.active_index()
    return {"content_id": active["id"], "release_id": active.get("search_release_id")} if active else None


def _require_version(store, index_id: str) -> dict:
    if not store.one("SELECT id FROM index_versions WHERE id=?", (index_id,)):
        raise EntityNotFound("找不到索引版本")
    return store.index_version(index_id)


def _warm_in_background(config) -> None:
    def run():
        from mindex.search.warmup import warm_up

        try:
            warm_up(config, startup=False)
        except Exception as exc:  # noqa: BLE001 — 预热只是优化，失败不影响激活结果
            log.warning("激活索引后的检索预热失败：%s", exc)

    threading.Thread(target=run, name="index-activate-warmup", daemon=True).start()


def indexes_router(runtime, store, embedder: MultimodalEmbedder | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/indexes", tags=["索引版本"])

    @router.get("")
    def list_versions():
        rows = store.rows("SELECT * FROM index_versions ORDER BY created_at DESC, rowid DESC")
        return {"active": _active(store), "versions": [_version_summary(row) for row in rows]}

    @router.get("/{index_id}/check")
    def check(index_id: str):
        # 同步端点由 FastAPI 放进线程池执行：检查要读取向量库与媒体引用，耗时较长。
        from mindex.processing.indexing import check_index

        _require_version(store, index_id)
        issues = check_index(store, index_id)
        return {"index_id": index_id, "ok": not issues, "issues": issues}

    @router.post("/{index_id}/activate")
    def activate(index_id: str):
        from mindex.processing.indexing import check_index

        config = runtime.current()
        _require_version(store, index_id)
        processing = ProcessingLock(config)
        try:
            processing.acquire()
        except Timeout as exc:
            raise HTTPException(409, "处理锁被占用，请稍后再激活索引") from exc
        try:
            writer = index_write_lock(store.root)
            try:
                writer.acquire(timeout=INDEX_LOCK_TIMEOUT_SECONDS)
            except Timeout as exc:
                raise HTTPException(409, "索引正在写入，请稍后再激活索引") from exc
            try:
                issues = check_index(store, index_id)
                if issues:
                    raise HTTPException(422, {"message": "索引检查失败", "issues": issues})
                spec = store.index_version(index_id)["spec"]
                if spec["space"].get("schema") == 2:
                    from mindex.models.adapters import make_embedder

                    client = embedder or make_embedder(config)
                    if client.identity() != spec["space"]:
                        raise ModelError("当前模型空间与待发布索引不同")
                    if hasattr(client, "require_gate"):
                        client.require_gate()
                if config.video.model_dump() != spec.get("preprocessing"):
                    raise ModelError("视频预处理配置与索引不一致")
                store.activate(index_id)
            finally:
                writer.release()
        finally:
            processing.release()
        _warm_in_background(config)
        active = _active(store) or {}
        return {"active": active.get("content_id"), "release_id": active.get("release_id")}

    return router
