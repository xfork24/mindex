"""构建跟踪：提交构建后轮询状态，终态时写入 ``plugin:mindex/build-finished`` 并排队一轮 followup 总结。

跟踪关系记录在会话日志里（``plugin:mindex/build-watch``），服务重启后 ``resume_all`` 会继续跟踪
尚未出现 build-finished 的构建；构建若已在重启期间结束，会立即补写结果并触发总结。
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter

from mindex.agent.ids import short

log = logging.getLogger(__name__)
STAGE = {"script": "文案", "split": "拆分镜", "recall": "召回", "tts": "配音", "select": "选片", "render": "渲染",
         "compose": "合成"}


class BuildWatcher:
    def __init__(self, runtime, client_factory, *, interval: float = 1.0):
        self.runtime = runtime
        self.client_factory = client_factory
        self.interval = interval
        self.tasks: dict[tuple[str, str], asyncio.Task] = {}

    def watch(self, sid: str, target: dict) -> None:
        key = (sid, target["build_id"])
        if key in self.tasks and not self.tasks[key].done():
            return
        self.tasks[key] = asyncio.create_task(self._run(sid, target))

    def forget(self, sid: str) -> None:
        for key, task in list(self.tasks.items()):
            if key[0] == sid:
                task.cancel()
                self.tasks.pop(key, None)

    def resume_all(self) -> None:
        store = self.runtime.store
        for row in store.list():
            watched, finished = {}, set()
            for event in store.events(row["id"], types=("plugin:mindex/build-watch", "plugin:mindex/build-finished")):
                if event["type"] == "plugin:mindex/build-watch":
                    watched[event["data"]["build_id"]] = event["data"]
                else:
                    finished.add(event["data"]["build_id"])
            for build_id, target in watched.items():
                if build_id not in finished:
                    self.watch(row["id"], target)

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
            if final and build["status"] != "failed" else []
        return {"job_id": job, "build_id": build_id, "plan_id": build["plan_id"], "status": build["status"],
                "counts": counts, "problems": problems[:20], "error": build.get("error"),
                "final": short(final), "media": media}

    @staticmethod
    def followup_text(summary: dict) -> str:
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
