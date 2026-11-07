"""选题挖掘任务：抓取 → 文本嵌入 → 聚类 → LLM 归纳卡片 → 打分与素材覆盖 → 前 N 名写大纲。

- ``discover`` 是流水线本身（CLI ``--wait`` 在本进程直接调用它，服务端在后台线程里调用）。
- ``TopicRuns`` 复用 ``BackgroundTaskRunner``：任务表 ``topic_runs``（``source_id`` 存档案 ID），持久化进度，
  可取消、重试；全局 ``topics`` 锁保证同一时间只运行一个挖掘任务（含定时触发与 CLI），提交冲突返回 409。
- ``created_at`` 写本地时区带偏移的 ISO 时间（定时判断"今天是否已运行"以它为准）；``updated_at`` 沿用基类。
- 候选条目：本档案未被任何卡片使用、发布（或抓取）时间在近 7 天内，或事件在今天起 14 天内的条目。
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta

from fastapi import HTTPException
from filelock import Timeout
from pydantic import Field

from models import adapters
from models.adapters import ModelError
from shared import lock_backend
from shared.background_tasks import BackgroundTaskRunner, public_task
from shared.config import StrictModel, fingerprint
from shared.queue_dispatch import insert_ledger, ledger_transaction
from topics import cards as topic_cards
from topics import cluster, datasources, profiles
from topics.common import RUN_ID_PATTERN, TopicConflict, now, parse_time
from topics.llm import make_llm
from topics.scheduler import AUTO_TRIGGERS, MAX_ATTEMPTS, library_timezone, runs_on
from topics.summarize import summarize

log = logging.getLogger(__name__)
RECENT_DAYS = 7
EVENT_DAYS = 14
MAX_CANDIDATES = 200
TRIGGERS = ("manual", "schedule", "catchup")


class RunOptions(StrictModel):
    skip_fetch: bool = Field(default=False, description="不重新抓取，只用已有的候选条目")
    outline_count: int | None = Field(
        default=None, ge=0, le=50, description="为前 N 名写大纲；缺省取档案定时设置的条数"
    )


def candidate_items(store, profile_id: str, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now().astimezone()
    used = set()
    for row in store.rows("SELECT body FROM topic_cards WHERE profile_id=?", (profile_id,)):
        used.update(json.loads(row["body"]).get("item_ids") or [])
    found = []
    for row in store.rows("SELECT * FROM topic_items WHERE profile_id=?", (profile_id,)):
        if row["id"] in used:
            continue
        event = parse_time(row["event_at"])
        if event is not None:
            if now.date() <= event.date() <= (now + timedelta(days=EVENT_DAYS)).date():
                found.append(row)
            continue
        moment = parse_time(row["published_at"]) or parse_time(row["fetched_at"])
        if moment is not None and now - moment <= timedelta(days=RECENT_DAYS):
            found.append(row)
    found = profiles.scoped_items(store, profiles.body_of(store, profile_id), found)
    # 先按时间倒序，再按热度倒序（稳定排序：同热度保持时间顺序）。
    found.sort(key=lambda r: str(r["published_at"] or r["event_at"] or r["fetched_at"]), reverse=True)
    found.sort(key=lambda r: -(r["hot"] or 0))
    return found[:MAX_CANDIDATES]


def discover(
    config,
    store,
    profile_id: str,
    *,
    llm=None,
    embedder=None,
    options: RunOptions | None = None,
    run_id: str | None = None,
    progress=None,
) -> dict:
    options = options or RunOptions()
    progress = progress or (lambda value: None)
    llm = make_llm(config, llm)
    if embedder is None:
        embedder = adapters.make_embedder(config)
    profile = profiles.body_of(store, profile_id)
    retries = (config.topics.llm or config.studio.writer).split_retries
    warnings: list[str] = []
    fetched = None
    if not options.skip_fetch:
        fetched = datasources.fetch_profile(config, store, profile_id, progress=progress)
        warnings += [f"数据源「{s['name']}」抓取失败：{s['error']}" for s in fetched["sources"] if s["error"]]
    candidates = candidate_items(store, profile_id)
    if not candidates:
        raise ValueError("没有可用的候选条目：请先配置并启用数据源，或检查数据源的抓取错误")
    progress({"stage": "embed", "message": f"计算 {len(candidates)} 条候选条目的文本向量"})
    threshold = config.topics.cluster.threshold
    try:
        vectors = cluster.embed(embedder, [cluster.item_text(item) for item in candidates])
        groups = cluster.greedy(vectors, threshold)
    except Exception as exc:  # 嵌入不可用时退化为按标题分组，不伪造向量
        log.warning("选题嵌入不可用，候选条目按标题分组", exc_info=True)
        warnings.append(f"嵌入不可用，候选条目按标题分组：{exc}")
        groups = cluster.by_title([item["title"] for item in candidates])
    groups = cluster.rank(groups, candidates, config.topics.cluster.max_clusters)
    progress({"stage": "cluster", "message": f"{len(candidates)} 条候选条目归为 {len(groups)} 个候选簇"})
    names = {
        r["id"]: r["name"]
        for r in store.rows("SELECT id,name FROM topic_sources WHERE profile_id=?", (profile_id,))
    }
    drafts = summarize(
        llm,
        profile,
        [[candidates[i] for i in group] for group in groups],
        source_names=names,
        batch=config.topics.cluster.batch,
        retries=retries,
        progress=progress,
    )
    card_ids = [topic_cards.create(store, profile_id, run_id, draft, members) for draft, members in drafts]
    try:
        return _finish(
            config,
            store,
            profile_id,
            profile,
            card_ids,
            llm=llm,
            embedder=embedder,
            options=options,
            progress=progress,
            warnings=warnings,
            fetched=fetched,
            candidates=len(candidates),
            clusters=len(groups),
        )
    except BaseException:
        # 取消或失败时撤回本次产出的卡片，重试时这些候选条目仍可再用。
        topic_cards.discard(store, card_ids)
        raise


def _finish(
    config,
    store,
    profile_id,
    profile,
    card_ids,
    *,
    llm,
    embedder,
    options,
    progress,
    warnings,
    fetched,
    candidates,
    clusters,
) -> dict:
    if not card_ids:
        warnings.append("模型没有产出选题：候选资料可能与档案无关或触犯禁区")
    progress(
        {"stage": "score", "message": f"为 {len(card_ids)} 张选题卡片打分", "done": 0, "total": len(card_ids)}
    )
    warnings += topic_cards.evaluate(
        config, store, profile_id, card_ids, llm=llm, embedder=embedder, progress=progress
    )
    count = profile.schedule.count if options.outline_count is None else options.outline_count
    ranked = [
        card["id"]
        for card in sorted(
            (topic_cards.get_card(store, cid) for cid in card_ids), key=lambda card: -card["total"]
        )
    ][:count]
    outlined = 0
    for index, card_id in enumerate(ranked):
        progress(
            {
                "stage": "outline",
                "message": f"为前 {len(ranked)} 名写大纲 {index + 1}/{len(ranked)}",
                "done": index,
                "total": len(ranked),
            }
        )
        try:
            topic_cards.write_outline(config, store, card_id, llm=llm)
            outlined += 1
        except ModelError as exc:
            warnings.append(f"选题 {card_id} 的大纲生成失败：{exc}")
    progress({"stage": "done", "message": f"产出 {len(card_ids)} 张选题卡片，{outlined} 张已写大纲"})
    return {
        "profile_id": profile_id,
        "fetch": fetched,
        "candidates": candidates,
        "clusters": clusters,
        "cards": len(card_ids),
        "card_ids": card_ids,
        "outlined": outlined,
        "warnings": warnings,
    }


class TopicRuns(BackgroundTaskRunner):
    table = "topic_runs"
    label = "挖掘任务"
    busy = "已有选题挖掘任务在运行"
    not_retryable = "只有失败、中断或已取消的挖掘任务可以重试"
    thread_prefix = "topics"

    def __init__(self, runtime, store, embedder=None, *, llm=None):
        self.llm = llm
        super().__init__(runtime, store, embedder)

    def lock(self, *args, **kwargs):
        return lock_backend.named(self.store, "topics")

    def row_lock(self, row):
        return self.lock()

    def recover(self):
        if getattr(self.store, "queue_backend", "inline") == "postgres":
            return
        lock = self.lock()
        try:
            lock.acquire()
        except Timeout:
            return  # 另一进程正在挖掘，不改它的状态
        try:
            self.store.execute(
                "UPDATE topic_runs SET "
                "status='interrupted',error='进程已中断；请重试',updated_at=CURRENT_TIMESTAMP "
                "WHERE status IN ('queued','running')"
            )
        finally:
            lock.release()

    def restore(self, row):
        return self.runtime.current()

    def execute(self, task_id, row, config, save):
        options = RunOptions.model_validate(json.loads(row["options"] or "{}"))
        result = discover(
            config,
            self.store,
            row["source_id"],
            llm=self.llm,
            embedder=self.embedder,
            options=options,
            run_id=task_id,
            progress=lambda value: save("progress", value),
        )
        save("result", result)

    def start(self, profile_id: str, options: RunOptions | None = None, *, trigger: str = "manual") -> dict:
        options = options or RunOptions()
        if trigger not in TRIGGERS:
            raise ValueError(f"未知的触发方式：{trigger}")

        def create():
            profiles.get_profile(self.store, profile_id)
            task_id = uuid.uuid4().hex
            insert_ledger(
                self.store,
                self.table,
                task_id,
                "INSERT INTO topic_runs(id,source_id,trigger,status,config,options,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (task_id, profile_id, trigger, "queued", "{}", options.model_dump_json(), now()),
            )
            return task_id, self.runtime.current()

        return self.submit(self.lock(), create)

    def start_scheduled(self, profile_id, *, period, ordinal, trigger, created_at):
        """周期占位、账本与入队同事务；leader 重放返回既有任务，不重新执行终态任务。"""

        if trigger not in AUTO_TRIGGERS or not 1 <= ordinal <= MAX_ATTEMPTS:
            raise ValueError("无效的定时触发或周期尝试编号")
        moment = datetime.fromisoformat(created_at)
        if moment.tzinfo is None:
            raise ValueError("定时周期必须与带时区的触发时间一致")
        tz = library_timezone(self.store)
        if tz is not None:
            moment = moment.astimezone(tz)
        if moment.date().isoformat() != period:
            raise ValueError("定时周期必须与素材库时区中的触发日期一致")
        key = fingerprint(["topic-period-v1", profile_id, period, ordinal])
        task_id = key[:32]
        lock = self.lock()
        self.acquire(lock, "调度")
        try:
            profiles.get_profile(self.store, profile_id)
            existing = self.store.one(
                "SELECT run_id FROM scheduled_runs WHERE profile_id=? AND period=? AND ordinal=?",
                (profile_id, period, ordinal),
            )
            if existing:
                lock.release()
                return self.get(existing["run_id"])
            today = runs_on(self.store, profile_id, moment.date(), tz=moment.tzinfo)
            automatic = [row for row in today if row["trigger"] in AUTO_TRIGGERS]
            active = next(
                (row for row in automatic if row["status"] in {"queued", "running", "complete"}), None
            )
            if active:
                lock.release()
                return self.get(active["id"])
            if ordinal != len(automatic) + 1:
                raise ValueError("定时尝试编号已变化，请重新检查周期")
            config = self.runtime.current()
            with ledger_transaction(self.store, self.table, task_id) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT INTO topic_runs(id,source_id,trigger,status,config,options,created_at) "
                    "VALUES(?,?,?,'queued','{}','{}',?)",
                    (task_id, profile_id, trigger, created_at),
                )
                db.execute(
                    "INSERT INTO scheduled_runs(period_key,profile_id,period,ordinal,run_id) "
                    "VALUES(?,?,?,?,?)",
                    (key, profile_id, period, ordinal, task_id),
                )
            self.launch(task_id, config, lock)
            return self.get(task_id)
        except BaseException:
            lock.release()
            raise

    def run_here(
        self, profile_id: str, options: RunOptions | None = None, *, trigger: str = "manual"
    ) -> dict:
        """在当前线程同步执行（CLI ``--wait``）；拿不到锁时抛 ``TopicConflict``。"""
        if getattr(self.store, "queue_backend", "inline") == "postgres":
            task = self.start(profile_id, options, trigger=trigger)
            while task["status"] in {"queued", "running"}:
                time.sleep(0.5)
                task = self.get(task["id"])
            return task
        lock = self.lock()
        try:
            lock.acquire()
        except Timeout as exc:
            raise TopicConflict(f"{self.busy}，请稍后再试") from exc
        try:
            profiles.get_profile(self.store, profile_id)
            task_id = uuid.uuid4().hex
            self.store.execute(
                "INSERT INTO topic_runs(id,source_id,trigger,status,config,options,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    task_id,
                    profile_id,
                    trigger,
                    "queued",
                    "{}",
                    (options or RunOptions()).model_dump_json(),
                    now(),
                ),
            )
        except BaseException:
            lock.release()
            raise
        self.run(task_id, self.runtime.current(), lock)  # run 负责终态与释放锁
        return self.get(task_id)


def run_view(row: dict) -> dict:
    value = public_task(row)
    value["profile_id"] = value.pop("source_id")
    value.pop("publication", None)
    return value


def list_runs(store, profile_id: str, limit: int = 20) -> list[dict]:
    profiles.get_profile(store, profile_id)
    rows = store.rows(
        "SELECT * FROM topic_runs WHERE source_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (profile_id, limit),
    )
    return [run_view(row) for row in rows]


def get_run(store, run_id: str) -> dict:
    if not re.match(RUN_ID_PATTERN, run_id or ""):
        raise HTTPException(404, "找不到挖掘任务")
    row = store.one("SELECT * FROM topic_runs WHERE id=?", (run_id,))
    if row is None:
        raise HTTPException(404, "找不到挖掘任务")
    return run_view(row)
