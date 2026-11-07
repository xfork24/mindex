"""运行指标快照（plans §12）：连接池等待、租约到期、队列深度、staging 水位。

只读、短查询：供 ``/api/health``（轻量）与 ``/api/metrics``（抓取）共用。共享依赖
故障时对应键缺席而不是抛错——readiness 由调用方（health 的 backends 校验）决定。
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING, cast

from shared.db.pg_backend import PgBackend

if TYPE_CHECKING:
    from shared.backends import BackendContext
    from shared.config import Config

log = logging.getLogger(__name__)

__all__ = ["metrics_snapshot"]


def metrics_snapshot(backends: BackendContext, config: Config) -> dict:

    out: dict = {"form": backends.form, "pid": os.getpid()}
    try:
        # 延迟导入：平台专属标准库仅在对应平台的受保护路径中加载。
        import resource

        out["peak_rss_bytes"] = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * (
            1 if sys.platform == "darwin" else 1024
        )
    except ImportError:  # Windows 的实际安装验收使用平台专用进程指标
        out["peak_rss_bytes"] = None
    if config.database.backend == "postgres":
        backend = cast(PgBackend, backends.store.backend)
        try:
            pool = backend.metrics()
            out["pool"] = pool
            out["pool_wait"] = int(pool.get("requests_waiting", 0) or 0)
        except Exception as exc:  # noqa: BLE001 — 指标缺失不改变就绪语义
            log.debug("连接池指标不可用：%s", exc)
        try:
            with backend.connect() as db:
                row = db.execute(
                    "SELECT count(*) AS total,"
                    " count(*) FILTER (WHERE expires_at < clock_timestamp()) AS expired"
                    " FROM lock_holders WHERE library_id=?",
                    (backends.library_id,),
                ).fetchone()
            out["lease_expired"] = int(row["expired"])
            out["lease_holders"] = int(row["total"])
        except Exception as exc:  # noqa: BLE001
            log.debug("租约指标不可用：%s", exc)
        if config.queue.backend == "postgres":
            try:
                # 延迟导入：保持异常捕获范围，依赖加载失败时仍走原有降级路径。
                from shared.db.queue_pg import PgTaskQueue

                with backend.connect() as db:
                    out["queue"] = PgTaskQueue(backends.library_id).stats(db)
            except Exception as exc:  # noqa: BLE001
                log.debug("队列指标不可用：%s", exc)
    staging = getattr(backends, "staging", None)
    if staging is not None:
        try:
            out["staging"] = staging.usage()
        except Exception as exc:  # noqa: BLE001
            log.debug("staging 指标不可用：%s", exc)
    return out
