"""部署诊断（plans §11/§13 阶段 5）：启动前拒绝不完整组合，逐后端连通与身份检查。

``mindex doctor`` 输出 JSON 概览并按退出码判定（0 可用 / 1 需修复）；API 的
``/api/health`` 携带同一概览的轻量版本。不执行 DDL；S3 能力检查仅创建并清理独立随机探针，不修改 bucket 配置或业务对象。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindex.shared.config import Config

log = logging.getLogger(__name__)

__all__ = ["doctor"]


def doctor(config: Config, *, serving=False) -> dict:
    """逐项检查并汇总；任何一项失败都给出中文修复提示。"""
    checks: list[dict] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    def first_line(exc: Exception) -> str:
        text = str(exc).strip()
        return text.splitlines()[0] if text else type(exc).__name__

    try:
        from mindex.shared.backends import open_context

        context = open_context(config)
        record("backends.combination", True,
               f"form={context.form} library={context.library_id[:8]}…")
    except Exception as exc:  # noqa: BLE001 — 组合校验失败即整体不可用
        record("backends.combination", False, str(exc))
        return {"ok": False, "checks": checks, "summary": "后端组合校验失败，不能启动"}

    store = context.store
    try:
        version = store.one("SELECT value FROM settings WHERE key='library.id'")
        record("database.identity", version is not None,
               "" if version else "缺少 library.id（先 mindex migrate）")
    except Exception as exc:  # noqa: BLE001
        record("database.identity", False, first_line(exc))

    if config.vector.backend == "chroma_server":
        try:
            client = context.vectors.client
            client.heartbeat()
            record("vectors.server", True, config.vector.chroma_server.url)
        except Exception as exc:  # noqa: BLE001
            record("vectors.server", False, first_line(exc))
    else:
        record("vectors.server", True, "chroma_local（本地嵌入）")

    if config.storage.backend == "s3":
        try:
            media = context.media
            media.client.head_bucket(Bucket=media.bucket)
            record("storage.bucket", True, f"{media.section.endpoint_url}/{media.bucket}")
            supported = True if serving else media.verify_conditional_delete()
            record("storage.conditional_delete", supported,
                   ("readiness 不执行写入探针；GC 独立核验能力" if serving else "安全删除能力探针通过") if supported else "后端未通过条件删除/版本删除核验，自动 GC 已停止")
            if config.storage.s3.browser_origins and not serving:
                from mindex.shared.s3_multipart import probe_browser

                try:
                    record("storage.browser", True, probe_browser(media, config.storage.s3.browser_origins))
                except Exception as exc:  # noqa: BLE001 — 每项故障独立报告
                    record("storage.browser", False, first_line(exc))
            elif not serving:
                record("storage.browser", config.uploads.default_transport != "s3",
                       "未配置 browser_origins；浏览器直传须显式配置允许的 origin 并核验 CORS")
        except Exception as exc:  # noqa: BLE001
            record("storage.bucket", False, first_line(exc))
    else:
        record("storage.bucket", True, "local（单机文件系统）")

    if config.queue.backend == "postgres":
        try:
            with store.backend.connect() as db:
                db.execute("SELECT 1 FROM task_queue LIMIT 1").fetchone()
            record("queue.table", True, "task_queue 可读")
        except Exception as exc:  # noqa: BLE001
            record("queue.table", False, f"先 mindex migrate：{first_line(exc)}")
    if config.locks.backend == "postgres":
        try:
            with store.backend.connect() as db:
                db.execute("SELECT 1 FROM lock_resources LIMIT 1").fetchone()
            record("locks.table", True, "lock_resources 可读")
        except Exception as exc:  # noqa: BLE001
            record("locks.table", False, f"先 mindex migrate：{first_line(exc)}")

    if context.form == "cluster":
        try:
            from mindex.shared.admission import validate_serving

            validate_serving(context)
            record("runtime.admission", True, "运行协议、库身份与派生索引一致")
        except Exception as exc:  # noqa: BLE001 — 诊断必须汇总故障
            record("runtime.admission", False, first_line(exc))
        try:
            with store.backend.connect(control=True) as db:
                db.execute("SELECT 1").fetchone()
            record("database.control", True, "控制通道可用")
        except Exception as exc:  # noqa: BLE001 — 诊断必须汇总故障
            record("database.control", False, first_line(exc))
    context.close()
    failed = [item for item in checks if not item["ok"]]
    return {"ok": not failed, "checks": checks,
            "summary": "全部检查通过" if not failed else f"{len(failed)} 项需要修复"}
