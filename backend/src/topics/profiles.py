"""领域档案：增删改查、写作约束快照与内容哈希。

- 档案存 ``topic_profiles``（``body`` 为 ``ProfileBody`` 的 JSON，名称冗余一列便于排序）。
- 删除档案在一个事务里删除其数据源、条目、卡片、缺口与挖掘记录；有挖掘任务在排队或运行时拒绝（409）。
  已建的创作任务及其档案快照（任务目录 ``profile.json``）不受影响。
- ``snapshot`` 只取影响写作的字段（领域、子方向、受众、定位、禁区）；``profile_hash`` 是它的指纹，
  进入 script 阶段签名，所以改名称、关键词、权重或定时不会让已有文案显示为过期。
"""

from __future__ import annotations

from mindex.admin.curation import EntityNotFound
from mindex.shared.writing_profile import WRITING_KEYS, profile_hash
from mindex.topics.common import TopicConflict, check_id, dumps, loads, new_id, now
from mindex.topics.models import ProfileBody, ProfileChanges

CHILD_TABLES = ("topic_sources", "topic_items", "topic_cards", "topic_gaps")


def _view(row: dict) -> dict:
    body = ProfileBody.model_validate(loads(row["body"], {}))
    return {"id": row["id"], **body.model_dump(), "hash": profile_hash(snapshot_body(body)),
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def _row(store, profile_id: str) -> dict:
    check_id(profile_id, "tp", "领域档案")
    row = store.one("SELECT * FROM topic_profiles WHERE id=?", (profile_id,))
    if row is None:
        raise EntityNotFound("找不到领域档案")
    return row


def list_profiles(store) -> list[dict]:
    return [_view(row) for row in store.rows("SELECT * FROM topic_profiles ORDER BY created_at, rowid")]


def get_profile(store, profile_id: str) -> dict:
    return _view(_row(store, profile_id))


def body_of(store, profile_id: str) -> ProfileBody:
    return ProfileBody.model_validate(loads(_row(store, profile_id)["body"], {}))


def create_profile(store, body: ProfileBody) -> dict:
    validate_materials(store, body)
    profile_id, stamp = new_id("tp"), now()
    store.execute(
        "INSERT INTO topic_profiles(id,name,body,created_at,updated_at) VALUES(?,?,?,?,?)",
        (profile_id, body.name, dumps(body.model_dump()), stamp, stamp),
    )
    return get_profile(store, profile_id)


def update_profile(store, profile_id: str, changes: ProfileChanges) -> dict:
    current = body_of(store, profile_id)
    body = ProfileBody.model_validate({**current.model_dump(), **changes.values()})
    if {"source_ids", "source_group_ids"} & changes.values().keys():
        validate_materials(store, body)
    store.execute(
        "UPDATE topic_profiles SET name=?,body=?,updated_at=? WHERE id=?",
        (body.name, dumps(body.model_dump()), now(), profile_id),
    )
    return get_profile(store, profile_id)


def delete_profile(store, profile_id: str) -> dict:
    _row(store, profile_id)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        busy = db.execute(
            "SELECT 1 FROM topic_runs WHERE source_id=? AND status IN ('queued','running')", (profile_id,)
        ).fetchone()
        if busy:
            raise TopicConflict("该档案有挖掘任务正在运行，请等待完成或取消后再删除")
        counts = {"topic_calendar_events": db.execute(
            "DELETE FROM topic_calendar_events WHERE source_id IN (SELECT id FROM topic_sources WHERE profile_id=?)",
            (profile_id,)).rowcount}
        for table in CHILD_TABLES:
            counts[table] = db.execute(f"DELETE FROM {table} WHERE profile_id=?", (profile_id,)).rowcount
        counts["topic_runs"] = db.execute("DELETE FROM topic_runs WHERE source_id=?", (profile_id,)).rowcount
        db.execute("DELETE FROM topic_profiles WHERE id=?", (profile_id,))
    return {"deleted": profile_id, "removed": counts}


# ---------------------------------------------------------------- 写作约束


def snapshot_body(body: ProfileBody) -> dict:
    data = body.model_dump()
    return {key: data[key] for key in WRITING_KEYS}


def material_filters(body: ProfileBody):
    """素材组并集与手动素材取交集；空列表沿用不限语义。"""
    from mindex.shared.types import Filters

    return Filters(source_ids=body.source_ids, source_group_ids=body.source_group_ids)


def validate_materials(store, body: ProfileBody) -> None:
    unknown = set(body.source_ids) - store.source_ids()
    if unknown:
        raise ValueError("找不到素材：" + "、".join(sorted(unknown)))
    material_filters(body).effective_sources(store)


def material_state(body: ProfileBody) -> dict:
    return {"source_ids": body.source_ids or None, "source_group_ids": body.source_group_ids}


def material_signature(store, body: ProfileBody) -> dict:
    from mindex.shared.media_groups import Groups

    return {"source_ids": sorted(set(body.source_ids)),
            "groups": Groups(store).state("source", body.source_group_ids)}


def scoped_items(store, body: ProfileBody, items: list[dict]) -> list[dict]:
    scope = material_filters(body).effective_sources(store)
    return [item for item in items if scope is None or not item.get("dedup_key", "").startswith("local:source:")
            or item["dedup_key"].removeprefix("local:source:") in scope]
