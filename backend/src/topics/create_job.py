"""选题 → 创作任务，以及任务的档案快照（读取与同步）。

建任务：
1. ``studio.create_job``：大纲取卡片大纲（没有时用标题、角度与关键画面拼成）；
2. 在 studio 锁内设置当前方案的文案模板与画幅，把来源条目（标题、摘要、链接）登记为一份参考文稿，
   并在方案上记录档案快照哈希 ``profile_hash``（进入 script 阶段签名）；
3. 任务目录写 ``profile.json``（档案快照），``job.json`` 写 ``topic``（卡片、档案与哈希）；
4. 卡片置为 adopted 并记录 job_id。任一步失败删除刚建的任务目录，卡片保持原状。

同步：用当前档案重写 ``profile.json`` 并更新该任务全部方案的 ``profile_hash``；
已有文案按现有机制提示参数变化，
不自动重写。
"""

from __future__ import annotations

import json
import shutil

from admin.curation import EntityNotFound
from shared import lock_backend
from studio import script_templates, studio
from topics import cards as topic_cards
from topics import items as topic_items
from topics import profiles
from topics.common import TopicConflict, loads, now

PROFILE_FILE = "profile.json"
REFERENCE_NAME = "选题来源资料"
STUDIO_BUSY = "已有创作步骤在执行，请稍候再试"


def profile_path(config, job_id: str):
    return studio.job_dir(config, job_id) / PROFILE_FILE


def read_snapshot(config, job_id: str) -> dict | None:
    path = profile_path(config, job_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_snapshot(config, job_id: str, profile_id: str, name: str, snapshot: dict) -> dict:
    value = {
        "profile_id": profile_id,
        "name": name,
        "hash": profiles.profile_hash(snapshot),
        "snapshot": snapshot,
        "synced_at": now(),
    }
    studio.save(profile_path(config, job_id), value, config=config)
    return value


def fallback_outline(card: dict) -> str:
    lines = [card["title"]]
    if card.get("angle"):
        lines.append(f"角度：{card['angle']}")
    if card.get("reason"):
        lines.append(f"为什么现在做：{card['reason']}")
    if card.get("visuals"):
        lines.append("需要的画面：" + "；".join(card["visuals"]))
    return "\n".join(lines)


def reference_text(card: dict, items: list[dict]) -> str:
    lines = [f"选题：{card['title']}"]
    if card.get("angle"):
        lines.append(f"角度：{card['angle']}")
    for item in items:
        lines.append("")
        lines.append(f"【{item['title']}】")
        if item.get("published_at") or item.get("event_at"):
            lines.append(f"时间：{item.get('event_at') or item.get('published_at')}")
        if item.get("url"):
            lines.append(f"链接：{item['url']}")
        if item.get("summary"):
            lines.append(item["summary"])
    return "\n".join(lines)


def studio_lock(config):
    lock = lock_backend.named(config, "studio")
    try:
        return lock.acquire()
    except Exception as exc:  # filelock.Timeout
        raise TopicConflict(STUDIO_BUSY) from exc


def create_from_card(config, store, card_id: str, *, force: bool = False, language: str = "zh") -> dict:
    from studio import workspace as ws

    card = topic_cards.get_card(store, card_id)
    if card["status"] == "adopted" and not force:
        raise TopicConflict(f"该选题已建成创作任务 {card['job_id']}；需要再建一份时使用 force")
    if not script_templates.exists(store, card["template_id"]):
        raise EntityNotFound(f"找不到文案模板：{card['template_id']}")
    body = profiles.body_of(store, card["profile_id"])
    snapshot = profiles.snapshot_body(body)
    outline = (card.get("outline") or "").strip() or fallback_outline(card)
    outline = outline[: config.studio.max_input_chars]
    profiles.validate_materials(store, body)
    items = profiles.scoped_items(store, body, topic_items.items_by_id(store, card["item_ids"]))
    state = studio.create_job(
        config,
        outline,
        card["title"][:60],
        store=store,
        original_input=f"来自选题：{card['title']}",
        language=language,
        options=profiles.material_state(body),
    )
    job_id = state["id"]
    try:
        lock = studio_lock(config)
        try:
            w = ws.load(config, job_id)
            p = ws.plan(w, w["active_plan"])
            options = ws.PlanOptions.model_validate(
                {**p["options"], "template_id": card["template_id"], "canvas": body.canvas}
            ).model_dump()
            p["options"] = {**options, "references": ws.references.references(p)}
            if items or card.get("angle"):
                ws.references.register(config, job_id, p, REFERENCE_NAME, reference_text(card, items))
            saved = write_snapshot(config, job_id, card["profile_id"], body.name, snapshot)
            p["profile_hash"] = saved["hash"]
            ws.persist(config, job_id, w)
            job = studio.load_job(config, job_id)
            job["topic"] = {
                "card_id": card_id,
                "profile_id": card["profile_id"],
                "profile_hash": saved["hash"],
                "title": card["title"],
            }
            studio.save(studio.job_path(config, job_id), job, config=config)
        finally:
            lock.release()
        row_body = loads(store.one("SELECT body FROM topic_cards WHERE id=?", (card_id,))["body"], {})
        topic_cards.save_body(store, card_id, row_body, status="adopted", job_id=job_id)
    except BaseException:
        shutil.rmtree(studio.job_dir(config, job_id), ignore_errors=True)
        raise
    return {
        "job_id": job_id,
        "card": topic_cards.get_card(store, card_id),
        "job": studio.load_job(config, job_id),
    }


# ---------------------------------------------------------------- 任务档案


def job_profile(config, store, job_id: str) -> dict:
    job = studio.load_job(config, job_id)
    topic = job.get("topic")
    saved = read_snapshot(config, job_id)
    if not topic or saved is None:
        return {
            "job_id": job_id,
            "topic": None,
            "snapshot": None,
            "hash": None,
            "current_hash": None,
            "outdated": False,
            "profile_exists": False,
            "profile_name": None,
            "card": None,
        }
    current_hash, exists, name = None, True, saved.get("name")
    try:
        current = profiles.get_profile(store, saved["profile_id"])
        current_hash, name = current["hash"], current["name"]
    except EntityNotFound:
        exists = False
    card_row = store.one("SELECT id,title,status FROM topic_cards WHERE id=?", (topic.get("card_id"),))
    return {
        "job_id": job_id,
        "topic": topic,
        "snapshot": saved["snapshot"],
        "hash": saved["hash"],
        "synced_at": saved.get("synced_at"),
        "current_hash": current_hash,
        "outdated": exists and current_hash != saved["hash"],
        "profile_exists": exists,
        "profile_name": name,
        "card": dict(card_row) if card_row else None,
    }


def sync_job_profile(config, store, job_id: str) -> dict:
    from studio import workspace as ws

    view = job_profile(config, store, job_id)
    if not view["topic"]:
        raise ValueError("该任务不是从选题创建的，没有账号档案快照")
    if not view["profile_exists"]:
        raise ValueError("原领域档案已删除，无法同步；任务继续使用创建时的档案快照")
    profile_id = view["topic"]["profile_id"]
    body = profiles.body_of(store, profile_id)
    lock = studio_lock(config)
    try:
        saved = write_snapshot(config, job_id, profile_id, body.name, profiles.snapshot_body(body))
        w = ws.load(config, job_id)
        for p in w["plans"].values():
            p["profile_hash"] = saved["hash"]
        ws.persist(config, job_id, w)
        job = studio.load_job(config, job_id)
        job["topic"] = {**job["topic"], "profile_hash": saved["hash"]}
        studio.save(studio.job_path(config, job_id), job, config=config)
    finally:
        lock.release()
    return job_profile(config, store, job_id)
