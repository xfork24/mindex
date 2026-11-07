"""数据源配置（``topic_sources``）：增删改查、试抓与抓取入库。

- ``config`` 按来源类型的严格模型校验后存 JSON；``secret``（API Key 等）只写不回显，视图只给掩码。
- 试抓不入库；抓取对每个启用的来源独立执行，一个来源失败只记 ``last_error``，不影响其他来源。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, ValidationError, field_validator

from admin.curation import EntityNotFound
from shared.config import StrictModel
from topics import items as topic_items
from topics import profiles
from topics.common import check_id, dumps, loads, new_id, now
from topics.sources import base, calendar

SourceKind = Literal["rss", "hotlist", "brave", "calendar", "local"]
TEST_ITEMS = 20


class SourceBody(StrictModel):
    kind: SourceKind
    name: str = Field(min_length=1, max_length=40)
    enabled: bool = True
    config: dict = Field(default_factory=dict, description="按 kind 区分的配置，字段见 kinds[].schema")
    secret: str | None = Field(default=None, max_length=500, description="API Key 等密钥；只写不回显")

    @field_validator("name")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("不能为空")
        return value


class SourceChanges(StrictModel):
    """部分修改；``secret`` 为空串表示清除已保存的密钥，省略表示不变。``config`` 需整体给出。"""

    name: str | None = Field(default=None, min_length=1, max_length=40)
    enabled: bool | None = None
    config: dict | None = None
    secret: str | None = Field(default=None, max_length=500)


def validate_config(kind: str, config: dict) -> dict:
    source = base.get(kind)
    try:
        return source.config_model.model_validate(config or {}).model_dump()
    except ValidationError as exc:
        problems = "；".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'config'}：{e['msg']}" for e in exc.errors()
        )
        raise ValueError(f"{source.label}配置无效：{problems}") from exc


def mask(secret: str | None) -> str | None:
    if not secret:
        return None
    return "••••" + secret[-4:] if len(secret) > 8 else "••••"


def _view(row: dict, events: int | None = None) -> dict:
    source = base.get(row["kind"])
    extra = {"events": events} if row["kind"] == "calendar" else {}
    return {
        **extra,
        "id": row["id"],
        "profile_id": row["profile_id"],
        "kind": row["kind"],
        "kind_label": source.label,
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "config": loads(row["config"], {}),
        "third_party": source.third_party,
        "has_secret": bool(row["secret"]),
        "secret_mask": mask(row["secret"]),
        "last_fetch_at": row["last_fetch_at"],
        "last_error": row["last_error"],
        "last_count": row["last_count"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row(store, source_id: str) -> dict:
    check_id(source_id, "ts", "数据源")
    row = store.one("SELECT * FROM topic_sources WHERE id=?", (source_id,))
    if row is None:
        raise EntityNotFound("找不到数据源")
    return row


def event_counts(store, source_ids: list[str]) -> dict[str, int]:
    if not source_ids:
        return {}
    marks = ",".join("?" * len(source_ids))
    rows = store.rows(
        f"SELECT source_id, COUNT(*) AS n FROM topic_calendar_events WHERE source_id IN ({marks}) "
        "GROUP BY source_id",
        source_ids,
    )
    return {row["source_id"]: row["n"] for row in rows}


def list_sources(store, profile_id: str) -> list[dict]:
    profiles.get_profile(store, profile_id)
    rows = store.rows(
        "SELECT * FROM topic_sources WHERE profile_id=? ORDER BY created_at, rowid", (profile_id,)
    )
    counts = event_counts(store, [r["id"] for r in rows if r["kind"] == "calendar"])
    return [_view(row, counts.get(row["id"], 0)) for row in rows]


def get_source(store, source_id: str) -> dict:
    row = _row(store, source_id)
    return _view(row, event_counts(store, [source_id]).get(source_id, 0))


def create_source(store, profile_id: str, body: SourceBody) -> dict:
    profiles.get_profile(store, profile_id)
    config = validate_config(body.kind, body.config)
    if base.get(body.kind).secret and not body.secret:
        raise ValueError(f"{base.get(body.kind).label}需要{base.get(body.kind).secret}")
    source_id, stamp = new_id("ts"), now()
    store.execute(
        "INSERT INTO topic_sources(id,profile_id,kind,name,enabled,config,secret,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            source_id,
            profile_id,
            body.kind,
            body.name,
            int(body.enabled),
            dumps(config),
            body.secret or None,
            stamp,
            stamp,
        ),
    )
    return get_source(store, source_id)


def update_source(store, source_id: str, changes: SourceChanges) -> dict:
    row = _row(store, source_id)
    given = changes.model_dump(exclude_unset=True)
    name = given.get("name") or row["name"]
    enabled = row["enabled"] if given.get("enabled") is None else int(given["enabled"])
    config = (
        validate_config(row["kind"], given["config"])
        if given.get("config") is not None
        else loads(row["config"])
    )
    secret = row["secret"]
    if "secret" in given and given["secret"] is not None:
        secret = given["secret"] or None
    if base.get(row["kind"]).secret and not secret:
        raise ValueError(f"{base.get(row['kind']).label}需要{base.get(row['kind']).secret}")
    store.execute(
        "UPDATE topic_sources SET name=?,enabled=?,config=?,secret=?,updated_at=? WHERE id=?",
        (" ".join(name.split()), enabled, dumps(config), secret, now(), source_id),
    )
    return get_source(store, source_id)


def delete_source(store, source_id: str) -> dict:
    _row(store, source_id)
    with store.connect() as db:
        db.execute("DELETE FROM topic_calendar_events WHERE source_id=?", (source_id,))
        db.execute("DELETE FROM topic_sources WHERE id=?", (source_id,))
    return {"deleted": source_id}


# ---------------------------------------------------------------- 抓取


def _fetch_row(config, store, row: dict, budget: dict) -> list[base.Item]:
    source = base.get(row["kind"])
    cfg = source.config_model.model_validate(loads(row["config"], {}))
    ctx = base.FetchContext(
        config=config,
        profile=profiles.body_of(store, row["profile_id"]),
        store=store,
        secret=row["secret"],
        source_id=row["id"],
        budget=budget,
    )
    return source.fetch(ctx, cfg)


def try_source(config, store, source_id: str) -> dict:
    """试抓一次，不入库、不改来源状态。"""
    row = _row(store, source_id)
    try:
        found = _fetch_row(config, store, row, {})
    except base.SourceError as exc:
        return {"source_id": source_id, "ok": False, "error": str(exc), "count": 0, "items": []}
    return {
        "source_id": source_id,
        "ok": True,
        "error": None,
        "count": len(found),
        "items": [i.public() for i in found[:TEST_ITEMS]],
    }


def fetch_profile(
    config,
    store,
    profile_id: str,
    *,
    source_ids: list[str] | None = None,
    budget: dict | None = None,
    progress=None,
) -> dict:
    """抓取档案的启用来源（或指定来源）并入库；返回每个来源的结果与新增条数。"""
    profiles.get_profile(store, profile_id)
    rows = store.rows(
        "SELECT * FROM topic_sources WHERE profile_id=? ORDER BY created_at, rowid", (profile_id,)
    )
    if source_ids:
        wanted = set(source_ids)
        rows = [row for row in rows if row["id"] in wanted]
        missing = wanted - {row["id"] for row in rows}
        if missing:
            raise EntityNotFound("找不到数据源")
    else:
        rows = [row for row in rows if row["enabled"]]
    budget = {} if budget is None else budget
    results, added = [], 0
    for index, row in enumerate(rows):
        if progress:
            progress(
                {"stage": "fetch", "message": f"抓取数据源 {row['name']}", "done": index, "total": len(rows)}
            )
        try:
            found = _fetch_row(config, store, row, budget)
            error = None
        except base.SourceError as exc:
            found, error = [], str(exc)
        new = topic_items.store_items(store, profile_id, row["id"], found) if found else 0
        added += new
        store.execute(
            "UPDATE topic_sources SET last_fetch_at=?,last_error=?,last_count=? WHERE id=?",
            (now(), error, len(found), row["id"]),
        )
        results.append(
            {
                "source_id": row["id"],
                "name": row["name"],
                "kind": row["kind"],
                "count": len(found),
                "new": new,
                "error": error,
            }
        )
    purged = topic_items.purge(store, profile_id, config.topics.fetch.item_days)
    return {
        "profile_id": profile_id,
        "sources": results,
        "new": added,
        "purged": purged,
        "errors": sum(1 for r in results if r["error"]),
    }


# ---------------------------------------------------------------- 日历导入


def import_calendar(
    store, source_id: str, text: str, *, fmt: str | None = None, append: bool = False
) -> dict:
    """把 ICS/CSV 文本解析后存为该日历来源的事件（缺省替换已有事件）。"""

    row = _row(store, source_id)
    if row["kind"] != "calendar":
        raise ValueError("只有日历类型的数据源可以导入日历")
    if fmt not in (None, "ics", "csv"):
        raise ValueError("日历格式只支持 ics 或 csv")
    events, errors = calendar.parse_text(text, fmt)
    if not events:
        raise ValueError("没有解析到日历事件" + (f"：{errors[0]}" if errors else ""))
    total = calendar.replace_events(store, source_id, events, append=append)
    store.execute("UPDATE topic_sources SET updated_at=? WHERE id=?", (now(), source_id))
    return {"source_id": source_id, "imported": len(events), "total": total, "errors": errors[:20]}


def import_sample(store, source_id: str, name: str, *, append: bool = False) -> dict:

    return {
        **import_calendar(store, source_id, calendar.sample_text(name), fmt="csv", append=append),
        "sample": name,
    }
