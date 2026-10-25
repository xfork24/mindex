"""每日定时挖掘：``mindex serve`` 运行期间（Web 端常驻服务、桌面端窗口或托盘里的 sidecar）按档案的定时设置自动挖掘。

- ``due`` 是纯函数：档案开启定时、本地时间已过当天时刻、当天没有成功或进行中的自动任务、当天自动尝试少于
  ``MAX_ATTEMPTS`` 次时返回触发方式；服务启动后的第一次检查记为 ``catchup``（补跑错过的定时），之后为 ``schedule``。
- 线程每 ``topics.scheduler.interval_seconds`` 秒检查一次；提交经 ``TopicRuns``（全局 ``topics`` 锁），已有挖掘任务在运行
  时跳过，下一次检查再试；任何错误只记日志，不影响服务。
- 运行记录的 ``created_at`` 是本地时区 ISO 时间，"当天"按本地日期判断。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time, timedelta

from mindex.topics import profiles
from mindex.topics.common import parse_time

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
AUTO_TRIGGERS = ("schedule", "catchup")


def scheduled_at(schedule, day) -> datetime:
    hour, minute = (int(part) for part in schedule.time.split(":"))
    return datetime.combine(day, time(hour, minute)).astimezone()


def runs_on(store, profile_id: str, day) -> list[dict]:
    rows = store.rows("SELECT id,trigger,status,created_at FROM topic_runs WHERE source_id=? ORDER BY created_at DESC, rowid DESC",
                      (profile_id,))
    found = []
    for row in rows:
        created = parse_time(row["created_at"])
        if created is not None and created.astimezone().date() == day:
            found.append(row)
    return found


def due(schedule, now: datetime, runs_today: list[dict], *, startup: bool) -> str | None:
    if not schedule.enabled or now < scheduled_at(schedule, now.date()):
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
    today = scheduled_at(schedule, now.date())
    satisfied = due(schedule, now, runs_today, startup=False) is None and now >= today
    moment = today if now < today else (today + timedelta(days=1) if satisfied else now)
    return moment.isoformat(timespec="seconds")


def status(store, *, now: datetime | None = None, enabled: bool = True) -> dict:
    now = now or datetime.now().astimezone()
    items = []
    for profile in profiles.list_profiles(store):
        body = profiles.body_of(store, profile["id"])
        today = runs_on(store, profile["id"], now.date())
        cards = store.rows("SELECT created_at FROM topic_cards WHERE profile_id=?", (profile["id"],))
        made = sum(1 for c in cards if (parse_time(c["created_at"]) or now).astimezone().date() == now.date())
        latest = store.one("SELECT id,trigger,status,created_at,error FROM topic_runs WHERE source_id=? "
                           "ORDER BY created_at DESC, rowid DESC LIMIT 1", (profile["id"],))
        items.append({"profile_id": profile["id"], "name": profile["name"], "enabled": body.schedule.enabled,
                      "time": body.schedule.time, "count": body.schedule.count,
                      "next_run": next_run(body.schedule, now, today) if enabled else None,
                      "today": {"runs": len(today), "cards": made}, "last_run": latest})
    return {"scheduler": enabled, "now": now.isoformat(timespec="seconds"), "items": items}


class TopicScheduler:
    def __init__(self, store, runs, *, interval: float = 60, clock=None):
        self.store, self.runs, self.interval = store, runs, interval
        self.clock = clock or (lambda: datetime.now().astimezone())
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._first = True

    def tick(self) -> list[str]:
        """检查一次全部档案，返回本次提交的挖掘任务 ID。"""
        startup, self._first = self._first, False
        now = self.clock()
        submitted = []
        for profile in profiles.list_profiles(self.store):
            try:
                body = profiles.body_of(self.store, profile["id"])
                trigger = due(body.schedule, now, runs_on(self.store, profile["id"], now.date()), startup=startup)
                if trigger is None:
                    continue
                run = self.runs.start(profile["id"], trigger=trigger)
                submitted.append(run["id"])
                log.info("定时选题挖掘已启动：档案 %s（%s），任务 %s", profile["name"], trigger, run["id"])
            except Exception as exc:  # noqa: BLE001 — 调度失败只记日志，下次检查再试
                detail = getattr(exc, "detail", None) or exc
                log.info("定时选题挖掘暂未启动：档案 %s：%s", profile["name"], detail)
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

    scheduler = TopicScheduler(store, runs, interval=config.topics.scheduler.interval_seconds)
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
