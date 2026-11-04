"""每日定时挖掘：``mindex serve`` 运行期间（Web 端常驻服务、桌面端窗口或托盘里的 sidecar）按档案的定时设置自动挖掘。

- ``due`` 是纯函数：档案开启定时、库内时间已过当天时刻、当天没有成功或进行中的自动任务、当天自动尝试少于
  ``MAX_ATTEMPTS`` 次时返回触发方式；服务启动后的第一次检查记为 ``catchup``（补跑错过的定时），之后为 ``schedule``。
- 线程每 ``topics.scheduler.interval_seconds`` 秒检查一次；提交经 ``TopicRuns``（全局 ``topics`` 锁），已有挖掘任务在运行
  时跳过，下一次检查再试；任何错误只记日志，不影响服务。
- 运行记录的 ``created_at`` 是带时区 ISO 时间，"当天"按库绑定时区判断（本地缺省用系统时区）。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from mindex.topics import profiles
from mindex.topics.common import parse_time

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
AUTO_TRIGGERS = ("schedule", "catchup")
LEADER_NAME = "scheduler:topics"
TIMEZONE_KEY = "topics.scheduler.timezone"


def library_timezone(store, requested=None, *, persist=False):
    if persist and (store.pg or requested is not None):
        with store.connect() as db:
            db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING",
                       (TIMEZONE_KEY, requested or "UTC"))
    row = store.one("SELECT value FROM settings WHERE key=?", (TIMEZONE_KEY,))
    value = row["value"] if row else requested or ("UTC" if store.pg else None)
    if requested is not None and value != requested:
        raise ValueError("节点调度时区与素材库已绑定时区不一致；请统一配置后再启动")
    return ZoneInfo(value) if value is not None else None


def scheduled_at(schedule, day, *, tz=None) -> datetime:
    hour, minute = (int(part) for part in schedule.time.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=tz).astimezone(tz)


def runs_on(store, profile_id: str, day, *, tz=None) -> list[dict]:
    rows = store.rows("SELECT id,trigger,status,created_at FROM topic_runs WHERE source_id=? ORDER BY created_at DESC, rowid DESC",
                      (profile_id,))
    found = []
    for row in rows:
        created = parse_time(row["created_at"])
        if created is not None and created.astimezone(tz).date() == day:
            found.append(row)
    return found


def due(schedule, now: datetime, runs_today: list[dict], *, startup: bool) -> str | None:
    if not schedule.enabled or now < scheduled_at(schedule, now.date(), tz=now.tzinfo):
        return None
    automatic = [r for r in runs_today if r["trigger"] in AUTO_TRIGGERS]
    if any(r["status"] in ("queued", "running", "complete") for r in automatic):
        return None
    if len(automatic) >= MAX_ATTEMPTS:
        return None
    return "catchup" if startup else "schedule"


def next_run(schedule, now: datetime, runs_today: list[dict]) -> str | None:
    if not schedule.enabled:
        return None
    today = scheduled_at(schedule, now.date(), tz=now.tzinfo)
    satisfied = due(schedule, now, runs_today, startup=False) is None and now >= today
    moment = today if now < today else (today + timedelta(days=1) if satisfied else now)
    return moment.isoformat(timespec="seconds")


def status(store, *, now: datetime | None = None, enabled: bool = True) -> dict:
    now = now or datetime.now().astimezone()
    tz = library_timezone(store)
    if tz is not None:
        now = now.astimezone(tz)
    items = []
    for profile in profiles.list_profiles(store):
        body = profiles.body_of(store, profile["id"])
        today = runs_on(store, profile["id"], now.date(), tz=now.tzinfo)
        cards = store.rows("SELECT created_at FROM topic_cards WHERE profile_id=?", (profile["id"],))
        made = sum(1 for c in cards if (parse_time(c["created_at"]) or now).astimezone(now.tzinfo).date() == now.date())
        latest = store.one("SELECT id,trigger,status,created_at,error FROM topic_runs WHERE source_id=? "
                           "ORDER BY created_at DESC, rowid DESC LIMIT 1", (profile["id"],))
        items.append({"profile_id": profile["id"], "name": profile["name"], "enabled": body.schedule.enabled,
                      "time": body.schedule.time, "count": body.schedule.count,
                      "next_run": next_run(body.schedule, now, today) if enabled else None,
                      "today": {"runs": len(today), "cards": made}, "last_run": latest})
    return {"scheduler": enabled, "now": now.isoformat(timespec="seconds"), "items": items}


class TopicScheduler:
    def __init__(self, store, runs, *, interval: float = 60, clock=None, leader=None, timezone=None):
        self.store, self.runs, self.interval = store, runs, interval
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.timezone = library_timezone(store, timezone, persist=True)
        # leader 租约（locks.backend=postgres 时注入）：同一时刻只有一个调度角色
        # 真正触发，避免多副本重复提交（plans §7.2）；本地形态为 None（topics 文件
        # 锁已互斥）。租约以调度间隔为 TTL，拿不到即跳过本轮。
        self._leader = leader
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._first = True

    def tick(self) -> list[str]:
        """检查一次全部档案，返回本次提交的挖掘任务 ID。"""
        startup, self._first = self._first, False
        now = self.clock()
        if self.timezone is not None:
            now = now.astimezone(self.timezone)
        submitted = []
        lease = None
        if self._leader is not None:
            lease = self._leader.lease(LEADER_NAME, mode="exclusive",
                                       ttl=max(self.interval * 2, 60), wait=0)
            if lease is None:
                log.info("调度 leader 租约被其他实例持有，本轮跳过")
                return submitted
        try:
            for profile in profiles.list_profiles(self.store):
                try:
                    body = profiles.body_of(self.store, profile["id"])
                    today = runs_on(self.store, profile["id"], now.date(), tz=now.tzinfo)
                    trigger = due(body.schedule, now, today, startup=startup)
                    if trigger is None:
                        continue
                    if hasattr(self.runs, "start_scheduled"):
                        ordinal = 1 + sum(r["trigger"] in AUTO_TRIGGERS for r in today)
                        run = self.runs.start_scheduled(profile["id"], period=now.date().isoformat(),
                                                        ordinal=ordinal, trigger=trigger, created_at=now.isoformat())
                    else:
                        run = self.runs.start(profile["id"], trigger=trigger)
                    submitted.append(run["id"])
                    log.info("定时选题挖掘已启动：档案 %s（%s），任务 %s", profile["name"], trigger, run["id"])
                except Exception as exc:  # noqa: BLE001 — 调度失败只记日志，下次检查再试
                    detail = getattr(exc, "detail", None) or exc
                    log.info("定时选题挖掘暂未启动：档案 %s：%s", profile["name"], detail)
        finally:
            if lease is not None:
                lease.release()
        return submitted

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("定时选题挖掘检查失败")
            self._stop.wait(self.interval)

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="topics-scheduler")
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def install(app, store, runs, config) -> TopicScheduler:
    """随应用启停调度器（包装 ``app.router.lifespan_context``）。"""
    import contextlib

    leader = None
    if getattr(config, "locks", None) and config.locks.backend == "postgres":
        from mindex.shared.db.locks_pg import PgLockProvider

        library_id = getattr(store, "library_id", None) or (
            store.one("SELECT value FROM settings WHERE key='library.id'") or {"value": ""})["value"]
        leader = PgLockProvider(store.backend, library_id=library_id)
    scheduler = TopicScheduler(store, runs, interval=config.topics.scheduler.interval_seconds,
                               leader=leader, timezone=config.topics.scheduler.timezone)
    previous = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(application):
        scheduler.start()
        try:
            async with previous(application) as state:
                yield state
        finally:
            scheduler.stop()

    app.router.lifespan_context = lifespan
    app.state.topic_scheduler = scheduler
    return scheduler
