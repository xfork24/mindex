"""索引版本保留与删除：活动、building 与最近 N 个非活动 ready 一律保留。

删除一个版本 = chroma 集合 + representations/index_members/index_versions 行 +
index_report 设置键 + 关联检索组合（search_releases，其人物快照无其他引用则连带删除）。
删除后该版本不再可用于 indexes activate 回退，也失去增量发布的向量复用来源。
"""

from __future__ import annotations

import logging

from .clean import CleanAction, CleanItem, Scan

log = logging.getLogger(__name__)


def retained_versions(store, keep_recent: int) -> dict:
    """返回 {"active", "keep", "deletable"}：keep = 活动 ∪ 全部非 ready ∪ 最近 N 个
    其余 ready；deletable = 其余 ready 版本（按 created_at 新到旧）。"""
    active = store.one("SELECT value FROM settings WHERE key='active'")
    active_id = active["value"] if active else None
    versions = store.rows(
        "SELECT id, status, created_at FROM index_versions ORDER BY created_at DESC, id"
    )
    inactive_ready = [v for v in versions if v["status"] == "ready" and v["id"] != active_id]
    keep = {v["id"] for v in versions if v["status"] != "ready"}
    keep |= {v["id"] for v in inactive_ready[: max(keep_recent, 0)]}
    if active_id:
        keep.add(active_id)
    deletable = [v for v in inactive_ready if v["id"] not in keep]
    return {"active": active_id, "keep": sorted(keep), "deletable": deletable}


def delete_index_version(store, index_id: str) -> dict:
    """删除单个非活动索引版本；集合缺失时容错跳过（与 purge 同先例）。"""
    import chromadb
    from chromadb.config import Settings

    active = store.one("SELECT value FROM settings WHERE key='active'")
    if active and active["value"] == index_id:
        raise ValueError("活动索引版本不可删除")
    client = chromadb.PersistentClient(
        path=str(store.root / "chroma"), settings=Settings(anonymized_telemetry=False)
    )
    try:
        client.delete_collection("idx-" + index_id)
    except Exception as exc:  # noqa: BLE001 — 旧集合可能已被清理
        log.warning("索引 %s 的向量集合删除跳过：%s", index_id, exc)
    active_release = store.one("SELECT value FROM settings WHERE key='active.release'")
    with store.connect() as db:
        db.execute("DELETE FROM representations WHERE index_id=?", (index_id,))
        db.execute("DELETE FROM index_members WHERE index_id=?", (index_id,))
        db.execute("DELETE FROM settings WHERE key=?", ("index_report." + index_id,))
        for row in db.execute(
            "SELECT id, person_id FROM search_releases WHERE content_id=?", (index_id,)
        ).fetchall():
            if active_release and active_release["value"] == row["id"]:
                continue  # 生效组合受保护，正常不会指向非活动版本，防御性跳过
            db.execute("DELETE FROM search_releases WHERE id=?", (row["id"],))
            if row["person_id"] and not db.execute(
                "SELECT 1 FROM search_releases WHERE person_id=?", (row["person_id"],)
            ).fetchone():
                db.execute("DELETE FROM person_versions WHERE id=?", (row["person_id"],))
        db.execute("DELETE FROM index_versions WHERE id=?", (index_id,))
    return {"deleted": index_id}


def index_versions_rule(scan: Scan) -> CleanAction | None:
    _, _, retained = scan.media_state
    deletable = retained["deletable"]
    if not deletable:
        return None
    keep_recent = scan.config.clean.index_keep_versions
    items = []
    for version in deletable:
        members = scan.store.one(
            "SELECT count(*) AS n FROM index_members WHERE index_id=?", (version["id"],)
        )["n"]
        representations = scan.store.one(
            "SELECT count(*) AS n FROM representations WHERE index_id=?", (version["id"],)
        )["n"]
        items.append(CleanItem(version["id"], 0, {
            "members": members, "representations": representations,
            "created_at": version["created_at"],
        }))

    def execute() -> dict:
        return {"deleted": [delete_index_version(scan.store, v["id"])["deleted"] for v in deletable]}

    return CleanAction(
        category="index_versions", level="B", stage="pipeline",
        description=f"非活动索引版本（保留活动 + 最近 {keep_recent} 个 ready + 全部 building）",
        policy="删除 chroma 集合与 representations/index_members/index_versions 行、index_report "
               "键及关联检索组合；向量字节数无法按版本精确统计（以 chroma 整体大小为回收上限参考）；"
               "删除后失去该版本的回退与向量复用能力",
        items=items, kept={"versions": retained["keep"]}, execute=execute,
    )


RULES = [index_versions_rule]
