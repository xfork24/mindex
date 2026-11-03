"""索引版本保留与删除：活动、未超期 building 与最近 N 个非活动 ready 一律保留。

删除一个版本 = chroma 集合（含 segment HNSW 目录）+ representations/index_members/
index_versions 行 + index_report 设置键 + 关联检索组合（search_releases，其人物快照
无其他引用则连带删除）。删除后该版本不再可用于 indexes activate 回退，也失去增量
发布的向量复用来源；超期 building 版本被删后，同 spec 的跨运行续跑
（INSERT OR IGNORE 确定性 id）同样失效。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta

from mindex.maintenance.clean import CleanAction, CleanItem, Scan

log = logging.getLogger(__name__)

# chroma 顶层 segment 目录名为 UUID；非 UUID 形态的目录与库文件不进孤儿判定
UUID_DIR = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _pinned_versions(store, *, task_cutoff=None):
    pinned = set()
    releases = {row['id']: row['content_id']
                for row in store.rows('SELECT id,content_id FROM search_releases')}
    files = [*store.root.glob('studio/*/workspace.json'), *store.root.glob('studio/*/job.json')]
    for path in files:
        try:
            stack = [json.loads(path.read_text(encoding='utf-8'))]
        except (OSError, ValueError):
            log.warning('无法核对创作索引引用，保留全部索引：%s', path)
            return None
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {'index_id', 'content_index_id', 'content_id'} and isinstance(item, str):
                        pinned.add(item)
                    elif key in {'release_id', 'search_release_id', 'person_release'} and isinstance(item, str):
                        if item in releases:
                            pinned.add(releases[item])
                    elif isinstance(item, (dict, list)):
                        stack.append(item)
            elif isinstance(value, list):
                stack.extend(item for item in value if isinstance(item, (dict, list)))
    for row in store.rows(
        "SELECT progress FROM source_tasks WHERE status IN ('queued','running') OR "
        "(status IN ('failed','interrupted','cancelled') AND (? IS NULL OR updated_at>=?))",
        (task_cutoff, task_cutoff),
    ):
        try:
            value = json.loads(row['progress'])
            if not isinstance(value, dict):
                raise ValueError('任务进度不是对象')
        except (TypeError, ValueError):
            log.warning('无法核对素材任务索引引用，保留全部索引')
            return None
        if value.get('index_id'):
            pinned.add(value['index_id'])
    active_release = store.one("SELECT value FROM settings WHERE key='active.release'")
    if active_release and active_release['value'] in releases:
        pinned.add(releases[active_release['value']])
    return pinned


def retained_versions(store, keep_recent: int, *, building_max_age_days: int | None = None,
                      now: datetime | None = None) -> dict:
    """返回 {"active", "keep", "deletable"}：keep = 活动 ∪ 未超期 building ∪ 其他非
    ready 状态 ∪ 最近 N 个其余 ready；deletable = 其余 ready + 超期 building
    （按 created_at 新到旧）。building_max_age_days 为 None 时 building 全保留（旧行为）。"""
    active = store.one("SELECT value FROM settings WHERE key='active'")
    active_id = active["value"] if active else None
    versions = store.rows(
        "SELECT id, status, created_at FROM index_versions ORDER BY created_at DESC, id"
    )
    stale_cutoff = None
    if building_max_age_days is not None:
        cutoff = (now or datetime.now(UTC)) - timedelta(days=building_max_age_days)
        stale_cutoff = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    stale_building = [
        v for v in versions
        if v["status"] == "building" and stale_cutoff is not None and v["created_at"] < stale_cutoff
    ]
    inactive_ready = [v for v in versions if v["status"] == "ready" and v["id"] != active_id]
    keep = {v["id"] for v in versions if v["status"] != "ready"} - {v["id"] for v in stale_building}
    keep |= {v["id"] for v in inactive_ready[: max(keep_recent, 0)]}
    if active_id:
        keep.add(active_id)
    pinned = _pinned_versions(store, task_cutoff=stale_cutoff)
    keep.update(pinned if pinned is not None else (v['id'] for v in versions))
    deletable = [v for v in inactive_ready + stale_building if v["id"] not in keep]
    return {"active": active_id, "keep": sorted(keep), "deletable": deletable}


def _chroma_db(store) -> object | None:
    database = store.root / "chroma" / "chroma.sqlite3"
    if not database.is_file():
        return None
    return database


def _collection_segment_dirs(store, collection_name: str) -> list[str]:
    """集合名 → 其 segment 目录名列表（删除集合前读取，删完连带回收）。"""
    database = _chroma_db(store)
    if database is None:
        return []
    try:
        with sqlite3.connect(database) as db:
            return [row[0] for row in db.execute(
                "SELECT s.id FROM segments s JOIN collections c ON s.collection=c.id "
                "WHERE c.name=?", (collection_name,))]
    except sqlite3.Error:
        return []


def _orphan_segment_dirs(store) -> list[tuple[str, int]]:
    """(目录名, 字节) 列表：chroma 顶层 UUID 目录不在 segments 表中即孤儿。"""
    root = store.root / "chroma"
    database = _chroma_db(store)
    if database is None or not root.is_dir():
        return []
    try:
        with sqlite3.connect(database) as db:
            live = {row[0] for row in db.execute("SELECT id FROM segments")}
    except sqlite3.Error:
        return []
    orphans = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or not UUID_DIR.match(entry.name) or entry.name in live:
            continue
        total = sum(path.stat().st_size for path in entry.rglob("*") if path.is_file())
        orphans.append((entry.name, total))
    return orphans


def delete_index_version(store, index_id: str) -> dict:
    """删除单个非活动索引版本；集合缺失时容错跳过（与 purge 同先例）。

    调用方（clean 执行器）持有 index_write_lock，与索引构建互斥：并发写同一
    chroma 目录会用各自内存态覆盖 HNSW 落盘文件，制造「元数据有、HNSW 无」的
    孤儿向量（复用时 chroma 报 Error finding id）。
    """
    import chromadb
    from chromadb.config import Settings

    active = store.one("SELECT value FROM settings WHERE key='active'")
    if active and active["value"] == index_id:
        raise ValueError("活动索引版本不可删除")
    client = chromadb.PersistentClient(
        path=str(store.root / "chroma"), settings=Settings(anonymized_telemetry=False)
    )
    # delete_collection 只删 sqlite 行不回收 HNSW 目录：先记目录，集合删成后连带删除
    stale_dirs = _collection_segment_dirs(store, "idx-" + index_id)
    try:
        client.delete_collection("idx-" + index_id)
    except Exception as exc:  # noqa: BLE001 — 旧集合可能已被清理
        log.warning("索引 %s 的向量集合删除跳过：%s", index_id, exc)
    for name in stale_dirs:
        shutil.rmtree(store.root / "chroma" / name, ignore_errors=True)
    active_release = store.one("SELECT value FROM settings WHERE key='active.release'")
    with store.connect() as db:
        db.execute("DELETE FROM representations WHERE index_id=?", (index_id,))
        db.execute("DELETE FROM index_members WHERE index_id=?", (index_id,))
        # 该版本的派生词法索引一并删除（plans §6.1：派生数据随所属索引生命周期）。
        for table in ("lexical_terms", "lexical_docs", "search_segments", "lexical_index_meta"):
            db.execute(f"DELETE FROM {table} WHERE index_id=?", (index_id,))
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
    building_days = scan.config.clean.index_building_max_age_days
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
            "stale_building": version["status"] == "building",
        }))

    def execute() -> dict:
        # 预览后可能有任务恢复、版本激活或创作引用新增；持锁后按新事实复核。
        current = retained_versions(scan.store, keep_recent, building_max_age_days=building_days,
                                    now=scan.now)
        allowed = {v['id'] for v in current['deletable']}
        return {"deleted": [delete_index_version(scan.store, v["id"])["deleted"]
                            for v in deletable if v['id'] in allowed]}

    return CleanAction(
        category="index_versions", level="B", stage="pipeline",
        description=f"非活动索引版本（保留活动 + 最近 {keep_recent} 个 ready + "
                    f"{building_days} 天内的 building）",
        policy="删除 chroma 集合（含 segment HNSW 目录）与 representations/index_members/"
               "index_versions 行、index_report 键及关联检索组合；向量字节数无法按版本精确统计"
               "（以 chroma 整体大小为回收上限参考）；删除后失去该版本的回退与向量复用能力，"
               f"超期 building（{building_days} 天）另失去同 spec 跨运行续跑",
        items=items, kept={"versions": retained["keep"]}, execute=execute,
    )


def chroma_orphan_segments(scan: Scan) -> CleanAction | None:
    """chroma 删除集合不回收 HNSW segment 目录的残留（A 级兜底）。

    历史上 delete_collection 只删 sqlite 行：集合已不存在而目录仍在的，即孤儿。
    与 index_versions 同 stage，执行段持 index-write 锁与索引构建互斥；正在写入
    的集合其 segments 行先于目录落盘，绝不会被误判。
    """
    orphans = _orphan_segment_dirs(scan.store)
    if not orphans:
        return None

    def execute() -> dict:
        # 扫描在持锁前完成：执行段在锁内重读引用，期间刚被复活的目录不删
        removed = [name for name, _ in _orphan_segment_dirs(scan.store)]
        for name in removed:
            shutil.rmtree(scan.store.root / "chroma" / name, ignore_errors=True)
        return {"deleted": len(removed)}

    return CleanAction(
        category="chroma_orphan_segments", level="A", stage="pipeline",
        description="chroma 孤儿 HNSW segment 目录",
        policy="顶层 UUID 目录不在 chroma segments 表中即集合删除后的残留；apply 段持 "
               "index-write 锁重读引用后删除；非 UUID 目录与 chroma.sqlite3 不触碰",
        items=[CleanItem(f"chroma/{name}", size) for name, size in orphans],
        execute=execute,
    )


RULES = [index_versions_rule, chroma_orphan_segments]
