"""选题卡片（``topic_cards``）：保存、视图、列表、修改，以及打分与大纲的编排。

- ``body`` JSON：angle、reason、item_ids、visuals、scores（五项）、coverage（覆盖报告）、outline、outline_origin
  （generated/manual）、template_id、warnings、scored_at、outlined_at。
- 状态：new（新）、dismissed（已忽略）、adopted（已建任务，只能由建任务产生）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from mindex.admin.curation import EntityNotFound
from mindex.models.adapters import ModelError
from mindex.shared.config import StrictModel
from mindex.studio import script_templates
from mindex.topics import coverage as topic_coverage
from mindex.topics import items as topic_items
from mindex.topics import outline as topic_outline
from mindex.topics import profiles, scoring
from mindex.topics.common import check_id, dumps, loads, new_id, now
from mindex.topics.sources.local import made_videos

STATUSES = ("new", "dismissed", "adopted")


class CardChanges(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=80)
    outline: str | None = Field(default=None, max_length=20000)
    template_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    status: Literal["new", "dismissed"] | None = None


def _row(store, card_id: str) -> dict:
    check_id(card_id, "tc", "选题卡片")
    row = store.one("SELECT * FROM topic_cards WHERE id=?", (card_id,))
    if row is None:
        raise EntityNotFound("找不到选题卡片")
    return row


def _coverage_summary(report: dict | None) -> dict | None:
    if not report:
        return None
    return {k: report.get(k) for k in ("status", "reason", "hits", "total", "ratio", "degraded")}


def view(row: dict, *, detail: bool = False, store=None) -> dict:
    body = loads(row["body"], {})
    if store is not None and body.get("coverage"):
        profile = profiles.body_of(store, row["profile_id"])
        current = profiles.material_signature(store, profile)
        previous = body["coverage"].get("material_scope", {"source_ids": [], "groups": []})
        if current != previous:
            body["coverage"] = {"status": "stale", "reason": "素材范围或组成员已变化，请重新打分",
                                "visuals": [], "hits": 0, "total": 0, "ratio": 0}
            body["scores"] = {}
        elif body["coverage"].get("verification_version") != topic_coverage.coverage_verify.VERSION:
            body["coverage"] = {"status": "stale", "reason": "旧报告未进行候选相关性核验，请重新打分",
                                "visuals": [], "hits": 0, "total": 0, "ratio": 0}
            body["scores"] = {}
        elif body["coverage"].get("coverage_overrides", {}) != profile.coverage.model_dump(exclude_none=True):
            body["coverage"] = {"status": "stale", "reason": "素材覆盖核验配置已变化，请重新打分",
                                "visuals": [], "hits": 0, "total": 0, "ratio": 0}
            body["scores"] = {}
    result = {"id": row["id"], "profile_id": row["profile_id"], "run_id": row["run_id"], "title": row["title"],
              "status": row["status"], "job_id": row["job_id"],
              "total": None if (body.get("coverage") or {}).get("status") == "stale" else row["total"],
              "angle": body.get("angle", ""), "reason": body.get("reason", ""), "visuals": body.get("visuals", []),
              "scores": body.get("scores") or {}, "outline": body.get("outline"),
              "outline_origin": body.get("outline_origin"), "template_id": body.get("template_id") or "base",
              "warnings": body.get("warnings", []), "item_ids": body.get("item_ids", []),
              "coverage": body.get("coverage") if detail else _coverage_summary(body.get("coverage")),
              "scored_at": body.get("scored_at"), "outlined_at": body.get("outlined_at"),
              "created_at": row["created_at"], "updated_at": row["updated_at"]}
    if detail and store is not None:
        names = {r["id"]: r["name"] for r in store.rows("SELECT id,name FROM topic_sources WHERE profile_id=?",
                                                          (row["profile_id"],))}
        result["items"] = [{k: item[k] for k in ("id", "title", "summary", "url", "published_at", "event_at", "hot")}
                           | {"source_name": names.get(item["source_id"])}
                           for item in topic_items.items_by_id(store, result["item_ids"])]
    return result


def get_card(store, card_id: str) -> dict:
    return view(_row(store, card_id), detail=True, store=store)


def list_cards(store, profile_id: str, *, status: str | None = "new", limit: int = 100) -> list[dict]:
    profiles.get_profile(store, profile_id)
    if status in (None, "all"):
        rows = store.rows("SELECT * FROM topic_cards WHERE profile_id=? ORDER BY total DESC, created_at DESC, id "
                          "LIMIT ?", (profile_id, limit))
    else:
        if status not in STATUSES:
            raise ValueError(f"未知的卡片状态：{status}")
        rows = store.rows("SELECT * FROM topic_cards WHERE profile_id=? AND status=? "
                          "ORDER BY total DESC, created_at DESC, id LIMIT ?", (profile_id, status, limit))
    return [view(row, store=store) for row in rows]


def counts(store, profile_id: str) -> dict:
    rows = store.rows("SELECT status, COUNT(*) AS n FROM topic_cards WHERE profile_id=? GROUP BY status", (profile_id,))
    found = {row["status"]: row["n"] for row in rows}
    return {status: found.get(status, 0) for status in STATUSES}


def save_body(store, card_id: str, body: dict, **columns) -> None:
    sets = ["body=?", "updated_at=?"]
    args = [dumps(body), now()]
    for key, value in columns.items():
        sets.append(f"{key}=?")
        args.append(value)
    store.execute(f"UPDATE topic_cards SET {','.join(sets)} WHERE id=?", (*args, card_id))


def create(store, profile_id: str, run_id: str | None, draft, members: list[dict]) -> str:
    card_id, stamp = new_id("tc"), now()
    body = {"angle": draft.angle, "reason": draft.reason, "item_ids": [m["id"] for m in members],
            "visuals": draft.visuals, "scores": {}, "coverage": None, "outline": None, "outline_origin": None,
            "template_id": script_templates.DEFAULT_TEMPLATE, "warnings": []}
    store.execute("INSERT INTO topic_cards(id,profile_id,run_id,title,body,total,status,created_at,updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?)", (card_id, profile_id, run_id, draft.title.strip(), dumps(body), 0, "new",
                                                stamp, stamp))
    return card_id


def discard(store, card_ids: list[str]) -> None:
    """删除卡片并从采集清单里撤回它们的缺口计数（挖掘任务取消或失败时调用）。"""
    if not card_ids:
        return
    wanted = set(card_ids)
    with store.connect() as db:
        for card_id in card_ids:
            db.execute("DELETE FROM topic_cards WHERE id=?", (card_id,))
        for row in db.execute("SELECT id,card_ids FROM topic_gaps").fetchall():
            ids = loads(row["card_ids"], [])
            kept = [i for i in ids if i not in wanted]
            if len(kept) == len(ids):
                continue
            if kept:
                db.execute("UPDATE topic_gaps SET card_ids=?,count=?,card_id=? WHERE id=?",
                           (dumps(kept), len(kept), kept[-1], row["id"]))
            else:
                db.execute("DELETE FROM topic_gaps WHERE id=?", (row["id"],))


def update_card(store, card_id: str, changes: CardChanges) -> dict:
    row = _row(store, card_id)
    body = loads(row["body"], {})
    given = changes.model_dump(exclude_unset=True)
    columns = {}
    if given.get("title"):
        columns["title"] = " ".join(given["title"].split())
    if "outline" in given and given["outline"] is not None:
        text = given["outline"].strip()
        body["outline"] = text or None
        body["outline_origin"] = "manual" if text else None
    if given.get("template_id"):
        if not script_templates.exists(store, given["template_id"]):
            raise EntityNotFound(f"找不到文案模板：{given['template_id']}")
        body["template_id"] = given["template_id"]
    if given.get("status"):
        if row["status"] == "adopted":
            raise ValueError("已建成创作任务的选题不能改状态")
        columns["status"] = given["status"]
    save_body(store, card_id, body, **columns)
    return get_card(store, card_id)


# ---------------------------------------------------------------- 打分


def evaluate(config, store, profile_id: str, card_ids: list[str], *, llm, embedder, progress=None) -> list[str]:
    """为卡片计算五项分数、覆盖报告与总分并保存；返回告警。"""
    if not card_ids:
        return []
    from mindex.topics import gaps as topic_gaps

    profile = profiles.body_of(store, profile_id)
    filters = profiles.material_filters(profile)
    filters.effective_sources(store)  # 失效引用必须在模型调用前报错
    weights = profile.weights.model_dump()
    rows = [_row(store, card_id) for card_id in card_ids]
    bodies = [loads(row["body"], {}) for row in rows]
    cards = [{"title": row["title"], **body} for row, body in zip(rows, bodies, strict=True)]
    warnings: list[str] = []
    retries = (config.topics.llm or config.studio.writer).split_retries
    try:
        audience, notes = scoring.audience(llm, profile, cards, retries=retries)
        warnings += notes
    except ModelError as exc:
        audience = [scoring.AUDIENCE_DEFAULT] * len(cards)
        warnings.append(f"受众匹配评分失败，按 {scoring.AUDIENCE_DEFAULT} 计：{exc}")
    references = [f"{v['title']}。{v['text']}" for v in made_videos(config)]
    adopted = store.rows("SELECT id,title,body FROM topic_cards WHERE profile_id=? AND status='adopted'", (profile_id,))
    references += [f"{r['title']}。{loads(r['body'], {}).get('angle', '')}" for r in adopted if r["id"] not in card_ids]
    texts = [f"{c['title']}。{c.get('angle', '')}" for c in cards]
    novelty, warning = scoring.novelty_scores(texts, references, embedder)
    if warning:
        warnings.append(warning)
    stamp = now()
    for index, (row, body) in enumerate(zip(rows, bodies, strict=True)):
        if progress:
            progress({"stage": "score", "message": f"打分与素材覆盖 {index + 1}/{len(rows)}", "done": index,
                      "total": len(rows)})
        report = topic_coverage.report(config, store, body.get("visuals") or [], embedder=embedder, filters=filters,
                                       llm=llm, title=row["title"], angle=body.get("angle", ""),
                                       limits=config.topics.coverage.model_copy(
                                           update=profile.coverage.model_dump(exclude_none=True)))
        report["material_scope"] = profiles.material_signature(store, profile)
        report["coverage_overrides"] = profile.coverage.model_dump(exclude_none=True)
        if report["status"] != "ok" and report["reason"] and report["reason"] not in warnings:
            warnings.append(report["reason"])
        members = topic_items.items_by_id(store, body.get("item_ids") or [])
        scores = {"timeliness": scoring.timeliness(members), "audience": audience[index],
                  "coverage": report["ratio"], "novelty": novelty[index], "cost": scoring.cost(report)}
        body.update(scores=scores, coverage=report, scored_at=stamp)
        save_body(store, row["id"], body, total=scoring.total(scores, weights))
        if report["status"] == "ok":
            topic_gaps.record(store, profile_id, row["id"], topic_coverage.gaps(report), embedder=embedder)
    return warnings


def rescore(config, store, card_id: str, *, llm, embedder) -> dict:
    row = _row(store, card_id)
    warnings = evaluate(config, store, row["profile_id"], [card_id], llm=llm, embedder=embedder)
    card = get_card(store, card_id)
    return {**card, "score_warnings": warnings}


# ---------------------------------------------------------------- 大纲


def write_outline(config, store, card_id: str, *, llm) -> dict:
    row = _row(store, card_id)
    body = loads(row["body"], {})
    profile = profiles.body_of(store, row["profile_id"])
    members = profiles.scoped_items(store, profile, topic_items.items_by_id(store, body.get("item_ids") or []))
    retries = (config.topics.llm or config.studio.writer).split_retries
    text, template_id, warnings = topic_outline.draft(llm, store, config, profile, {"title": row["title"], **body},
                                                      members, retries=retries)
    body.update(outline=text, outline_origin="generated", template_id=template_id, outlined_at=now())
    body["warnings"] = [w for w in body.get("warnings", []) if "文案模板" not in w] + warnings
    save_body(store, card_id, body)
    return get_card(store, card_id)
