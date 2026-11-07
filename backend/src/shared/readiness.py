"""有界、无写探针的就绪检查；复用当前上下文，控制与查询通道独立诊断。"""

from __future__ import annotations

import os
import time

from shared.admission import validate_protocol, validate_serving
from shared.db.migrate_pg import check_schema
from shared.migration_state import pending
from shared.query_budget import deadline, http_request


def readiness(context, config, *, gateway_token=None):
    started = time.monotonic()
    limit = config.server.readiness_timeout_seconds
    checks = []

    def probe(name, action):
        remaining = limit - (time.monotonic() - started)
        if remaining <= 0:
            checks.append({"check": name, "ok": False, "detail": "依赖探测总期限已耗尽"})
            return
        try:
            with deadline(min(remaining, max(0.05, limit / 6))):
                detail = action()
            checks.append({"check": name, "ok": True, "detail": detail or "可用"})
        except Exception as exc:  # noqa: BLE001 — 每项故障独立汇总，不回显连接串或凭据。
            checks.append({"check": name, "ok": False, "detail": f"依赖不可用或超时（{type(exc).__name__}）"})

    store = context.store

    def database():

        with store.connect() as db:
            if store.pg:
                check_schema(db)
            row = db.execute("SELECT value FROM settings WHERE key='library.id'").fetchone()
            if row is None or row[0] != context.library_id:
                raise RuntimeError("库身份不一致")
        validate_protocol(store)
        return "schema、协议及库身份一致"

    def control():
        if store.pg:
            with store.backend.connect(control=True) as db:
                db.execute("SELECT 1").fetchone()
        return "控制通道可用" if store.pg else "本地模式"

    def migration():

        with store.connect() as db:
            if pending(db):
                raise RuntimeError("迁移尚未激活")
        return "业务已开放"

    def storage():
        if config.storage.backend == "s3":
            media = context.media
            url = media.client.generate_presigned_url(
                "head_bucket", Params={"Bucket": media.bucket}, ExpiresIn=60
            )
            http_request("HEAD", url, timeout=limit).raise_for_status()
        return "S3 bucket 可读" if config.storage.backend == "s3" else "本地文件系统"

    def vectors():
        if config.vector.backend == "chroma_server":
            context.vectors.client.heartbeat()
        return config.vector.backend

    def admission():

        validate_serving(context)
        return "派生索引、分词器及空间一致"

    def gateway():
        if not config.agent.enabled:
            return "助手已显式禁用"
        if context.form == "cluster" and config.server.role == "api":
            if not config.agent.gateway_url:
                raise RuntimeError("缺少网关地址")
            token = (
                gateway_token if gateway_token is not None else os.environ.get("MINDEX_WEB_TOKEN", "")
            ).strip()
            response = http_request(
                "GET",
                config.agent.gateway_url.rstrip("/") + "/api/live",
                headers={"Authorization": "Bearer " + token} if token else {},
                timeout=limit,
                follow_redirects=False,
            )
            response.raise_for_status()
            if response.json().get("alive") is not True:
                raise RuntimeError("网关存活响应无效")
        return "单实例网关可用" if context.form == "cluster" else "本地助手运行时"

    for name, action in [
        ("database.query", database),
        ("database.control", control),
        ("storage.bucket", storage),
        ("vectors.server", vectors),
        ("database.migration", migration),
        ("runtime.admission", admission),
        ("agent.gateway", gateway),
    ]:
        probe(name, action)
    failed = sum(not check["ok"] for check in checks)
    return {
        "ok": failed == 0,
        "checks": checks,
        "elapsed_seconds": time.monotonic() - started,
        "timeout_seconds": limit,
        "summary": "全部检查通过" if not failed else f"{failed} 项需要修复",
    }
