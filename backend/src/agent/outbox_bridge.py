"""agent-gateway 的 outbox 桥：轮询 PG 变更并幂等转入本地 ChangeFeed（plans §9）。

单实例网关启动后台线程，每 2 秒 drain 一次；ChangeFeed 的 seq 独立分配，
outbox id 仅用于去重与诊断。网关重启后从剩余未消费条目继续，不重放已转入的
历史（SSE 客户端按 Last-Event-ID 续传仍由 ChangeFeed 负责）。
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

__all__ = ["OutboxBridge"]

POLL_SECONDS = 2.0


class OutboxBridge:
    def __init__(self, store, feed, *, poll_seconds: float = POLL_SECONDS):
        self.store, self.feed, self.poll_seconds = store, feed, poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def drain_once(self) -> int:
        from mindex.shared.db.agent_outbox import drain

        items = drain(self.store)
        for item in items:
            try:
                self.feed.publish(item["kind"], item["data"], source=item["source"],
                                  session_id=item.get("session_id"), tab=item.get("tab"))
            except Exception:
                log.exception("outbox 条目 %s 转入 ChangeFeed 失败", item.get("outbox_id"))
        return len(items)

    def _loop(self):
        while not self._stop.wait(self.poll_seconds):
            try:
                self.drain_once()
            except Exception:
                log.exception("agent outbox 轮询失败")

    def start(self):
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._loop, daemon=True, name="agent-outbox")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
