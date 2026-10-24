"""任务跟踪：提交创作构建或后台任务后轮询状态，终态时写入结果事件并排队一轮 followup 总结。

两类跟踪目标：

- 创作构建 ``{"job_id", "build_id", "plan_id"?}``（``kind`` 省略或为 ``build``）：跟踪关系记为
  ``plugin:mindex/build-watch``，结束写 ``plugin:mindex/build-finished``（面板构建卡片依赖这两个事件）。
- 后台任务 ``{"kind": "source"|"dialogue"|"person", "task_id", "source_id"?, "work"?}``：跟踪关系记为
  ``plugin:mindex/task-watch``，结束写 ``plugin:mindex/task-finished``。

服务重启后 ``resume_all`` 会继续跟踪尚未出现结束事件的目标；目标若已在重启期间结束，会立即补写结果
并触发总结。
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter

from mindex.agent.ids import short

log = logging.getLogger(__name__)
STAGE = {"script": "文案", "split": "拆分镜", "recall": "召回", "tts": "配音", "select": "选片", "render": "渲染",
         "compose": "合成"}
# 后台任务类别 → (状态接口, 中文名)。
TASK_KINDS = {
    "source": ("/api/admin/source-tasks/{}", "素材任务"),
    "dialogue": ("/api/admin/dialogue-tasks/{}", "对白任务"),
    "person": ("/api/persons/tasks/{}", "人物任务"),
}
WATCH_KINDS = ("build", *TASK_KINDS)
# 仍在进行中的任务状态；其余（complete、partial、failed、cancelled、interrupted 及未知值）都视为终态。
ACTIVE = frozenset({"queued", "running"})
WORK = {"shots": "分镜", "analysis": "分析", "dialogue": "对白", "persons": "人物", "publish": "发布"}
BUILD_EVENTS = ("plugin:mindex/build-watch", "plugin:mindex/build-finished")
TASK_EVENTS = ("plugin:mindex/task-watch", "plugin:mindex/task-finished")


def watch_target(target: dict) -> dict:
    """规范化跟踪目标；``kind`` 省略视为构建。字段缺失或类别未知时抛 ValueError。"""
    kind = target.get("kind") or "build"
    if kind not in WATCH_KINDS:
        raise ValueError(f"未知的跟踪类别：{kind}")
    if kind == "build":
        if not target.get("job_id") or not target.get("build_id"):
            raise ValueError("构建跟踪需要 job_id 与 build_id")
        return {k: v for k, v in target.items() if k != "kind"}
    if not target.get("task_id"):
        raise ValueError("后台任务跟踪需要 task_id")
    return {"kind": kind, "task_id": target["task_id"],
            **{k: target[k] for k in ("source_id", "work") if target.get(k)}}


def _key(sid: str, target: dict) -> tuple[str, str]:
    kind = target.get("kind") or "build"
    return (sid, target["build_id"]) if kind == "build" else (sid, f"{kind}:{target['task_id']}")


class TaskWatcher:
    def __init__(self, runtime, client_factory, *, interval: float = 1.0):
        self.runtime = runtime
        self.client_factory = client_factory
        self.interval = interval
        self.tasks: dict[tuple[str, str], asyncio.Task] = {}

    def register(self, sid: str, target: dict) -> None:
        """登记跟踪关系（写入会话日志，供重启后恢复）并开始跟踪。"""
        target = watch_target(target)
        event = "plugin:mindex/task-watch" if "kind" in target else "plugin:mindex/build-watch"
        self.runtime._append(sid, event, target)
        self.watch(sid, target)

    def watch(self, sid: str, target: dict) -> None:
        target = watch_target(target)
        key = _key(sid, target)
        if key in self.tasks and not self.tasks[key].done():
            return
        run = self._run_task if "kind" in target else self._run
        self.tasks[key] = asyncio.create_task(run(sid, target))

    def forget(self, sid: str) -> None:
        for key, task in list(self.tasks.items()):
            if key[0] == sid:
                task.cancel()
                self.tasks.pop(key, None)

    def resume_all(self) -> None:
        store = self.runtime.store
        for row in store.list():
            watched, finished = {}, set()
            for event in store.events(row["id"], types=BUILD_EVENTS + TASK_EVENTS):
                data = event["data"]
                try:
                    key = _key(row["id"], data)
                except KeyError:
                    continue
                if event["type"] in ("plugin:mindex/build-watch", "plugin:mindex/task-watch"):
                    watched[key] = data
                else:
                    finished.add(key)
            for key, target in watched.items():
                if key not in finished:
                    try:
                        self.watch(row["id"], target)
                    except ValueError:
                        log.warning("忽略无法恢复的跟踪目标：%s", target)

    # ------------------------------------------------------------ 创作构建
    async def _run(self, sid: str, target: dict) -> None:
        client = self.client_factory(None)
        job, build_id = target["job_id"], target["build_id"]
        try:
            while True:
                result = await client.get(f"/api/studio/jobs/{job}/builds/{build_id}/logs", after=10**9)
                if not result["ok"]:
                    summary = {"job_id": job, "build_id": build_id, "status": "unknown", "error": result["error"]}
                    break
                if result["data"]["status"] != "running":
                    summary = await self._summary(client, job, build_id)
                    break
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 跟踪失败也要告知会话
            log.exception("构建跟踪失败")
            summary = {"job_id": job, "build_id": build_id, "status": "unknown", "error": str(exc)}
        self.runtime._append(sid, "plugin:mindex/build-finished", summary)
        await self.runtime.followup(sid, self.followup_text(summary))

    async def _summary(self, client, job: str, build_id: str) -> dict:
        data = await client.get(f"/api/studio/jobs/{job}/workspace", compact="true")
        build = next((b for b in data["data"]["builds"] if b["id"] == build_id), None) if data["ok"] else None
        if build is None:
            return {"job_id": job, "build_id": build_id, "status": "unknown"}
        counts = dict(Counter(n.get("status", "pending") for n in build["nodes"]))
        problems = [
            {"stage": n["stage"], "shot_id": n["shot_id"], "status": n["status"], "error": (n.get("error") or "")[:300]}
            for n in build["nodes"] if n.get("status") in ("failed", "blocked")
        ]
        from mindex.agent.tools.media import clean, version_media

        final = (data["data"]["plans"].get(build["plan_id"]) or {}).get("final")
        media = clean(version_media(data["data"]["versions"].get(final), f"成片 {short(final)}", collapsed=False)) \
            if final and build["status"] not in ("failed", "cancelled") else []
        return {"job_id": job, "build_id": build_id, "plan_id": build["plan_id"], "status": build["status"],
                "counts": counts, "problems": problems[:20], "error": build.get("error"),
                "final": short(final), "media": media}

    # ------------------------------------------------------------ 后台任务
    async def _run_task(self, sid: str, target: dict) -> None:
        client = self.client_factory(None)
        path = TASK_KINDS[target["kind"]][0].format(target["task_id"])
        try:
            while True:
                result = await client.get(path)
                if not result["ok"]:
                    summary = {**target, "status": "unknown", "error": result["error"]}
                    break
                if result["data"].get("status") not in ACTIVE:
                    summary = self.task_summary(target, result["data"])
                    break
                await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 跟踪失败也要告知会话
            log.exception("后台任务跟踪失败")
            summary = {**target, "status": "unknown", "error": str(exc)}
        self.runtime._append(sid, "plugin:mindex/task-finished", summary)
        await self.runtime.followup(sid, self.followup_text(summary))

    @staticmethod
    def task_summary(target: dict, task: dict) -> dict:
        """任务接口返回值 → 结束事件数据：只保留状态、错误、进度与链式发布信息，不带大块结果。"""
        summary = {**target, "status": task.get("status") or "unknown",
                   "source_id": task.get("source_id") or target.get("source_id")}
        if task.get("work") or target.get("work"):
            summary["work"] = task.get("work") or target.get("work")
        if task.get("error"):
            summary["error"] = str(task["error"])[:1000]
        for key in ("progress", "publication"):
            if isinstance(task.get(key), dict):
                summary[key] = task[key]
        return summary

    # ------------------------------------------------------------ followup
    @staticmethod
    def followup_text(summary: dict) -> str:
        if summary.get("kind") in TASK_KINDS:
            return TaskWatcher.task_followup_text(summary)
        lines = [f"[系统通知] 构建 {summary['build_id']}（任务 {summary['job_id']}）已结束，状态：{summary['status']}。"]
        if summary.get("counts"):
            lines.append(f"节点统计：{summary['counts']}")
        if summary.get("error"):
            lines.append(f"错误：{summary['error']}")
        for item in summary.get("problems", []):
            lines.append(f"- {STAGE.get(item['stage'], item['stage'])} {short(item['shot_id']) or ''}"
                         f" {item['status']}：{item['error']}")
        lines.append("请向用户总结结果（重建、复用、失败数量）；如有失败或阻塞，说明原因并建议下一步。")
        return "\n".join(lines)

    @staticmethod
    def task_followup_text(summary: dict) -> str:
        kind = summary["kind"]
        scope = [f"素材 {summary['source_id']}"] if summary.get("source_id") else []
        if summary.get("work"):
            scope.append(f"工作 {WORK.get(summary['work'], summary['work'])}")
        where = f"（{'，'.join(scope)}）" if scope else ""
        lines = [f"[系统通知] {TASK_KINDS[kind][1]} {summary['task_id']}{where}已结束，状态：{summary['status']}。"]
        if summary.get("error"):
            lines.append(f"错误：{summary['error']}")
        if summary.get("progress") and summary["status"] != "complete":
            lines.append(f"最后进度：{summary['progress']}")
        publication = summary.get("publication") or {}
        if publication.get("task_ids"):
            lines.append(f"已链式创建发布任务：{', '.join(publication['task_ids'])}"
                         "（可用 get_task_status(kind=source) 查询）")
        elif publication.get("status") and publication["status"] != "not_requested":
            lines.append(f"发布状态：{publication['status']}")
        lines.append(f"请向用户总结结果；如失败、中断或已取消，说明原因并建议下一步（可用 retry_task(kind={kind}) 从断点重试）。")
        return "\n".join(lines)


# 兼容旧名称：构建跟踪是 TaskWatcher 的一个类别。
BuildWatcher = TaskWatcher
