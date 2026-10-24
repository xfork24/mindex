"""mindex clean 的 B 级规则 agent_attachments 与备份排除 agent-attachments/。

过期附件按记录 expires_at 判定（直接改库伪造过期），孤儿目录与 .uploads/ 暂存按 mtime 宽限期判定。"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from mindex.agent.attachments import AttachmentStore
from mindex.cli import app
from mindex.maintenance.backup_files import excluded

runner = CliRunner()
OLD = datetime(2026, 1, 1, tzinfo=UTC).timestamp()


def make_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(f"data_dir: {tmp_path / 'data'}\nruntime_overrides: false\n", encoding="utf-8")
    return path


def clean(config_path: Path, *args) -> dict:
    result = runner.invoke(app, ["--config", str(config_path), "clean", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def rule(report: dict) -> dict | None:
    return next((c for c in report["categories"] if c["category"] == "agent_attachments"), None)


def age(path: Path) -> Path:
    os.utime(path, (OLD, OLD))
    return path


def put(store: AttachmentStore, tmp_path: Path, name: str) -> str:
    upload = tmp_path / f"upload-{name}"
    upload.write_bytes(b"payload-" + name.encode())
    return store.put(upload, filename=name, content_type="text/plain", owner="mcp",
                     session_id=None, ttl_hours=1)["id"]


def seed(tmp_path: Path) -> dict:
    data = tmp_path / "data"
    store = AttachmentStore(data)
    expired = put(store, tmp_path, "old.txt")
    fresh = put(store, tmp_path, "new.txt")
    with sqlite3.connect(data / "agent.sqlite3") as db:
        db.execute("UPDATE agent_attachments SET expires_at=0 WHERE id=?", (expired.removeprefix(store.PREFIX),))
    orphan_old = store.root / ("a" * 32)
    age(_write(orphan_old / "x.bin"))
    orphan_new = store.root / ("b" * 32)
    _write(orphan_new / "y.bin")
    staging_old = age(_write(store.root / ".uploads" / ("c" * 32)))
    staging_new = _write(store.root / ".uploads" / ("d" * 32))
    return {"data": data, "store": store, "expired": expired, "fresh": fresh, "orphan_old": orphan_old,
            "orphan_new": orphan_new, "staging_old": staging_old, "staging_new": staging_new}


def _write(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 10)
    return path


def test_preview_lists_expired_orphan_and_staging_without_deleting(tmp_path):
    case = seed(tmp_path)
    config_path = make_config(tmp_path)
    expired_dir = case["store"].root / case["expired"].removeprefix("attachment://")

    action = rule(clean(config_path))

    assert action["level"] == "B" and action["description"] == "过期助手附件"
    reasons = {item["path"]: item["reason"] for item in action["items"]}
    assert reasons == {
        f"agent-attachments/{expired_dir.name}": "expired",
        f"agent-attachments/{'a' * 32}": "orphan",
        f"agent-attachments/.uploads/{'c' * 32}": "staging",
    }
    assert action["kept"] == {"within_grace": 2}
    assert expired_dir.is_dir() and case["orphan_old"].is_dir() and case["staging_old"].is_file()
    assert case["store"].get(case["expired"])["filename"] == "old.txt"


def test_apply_deletes_expired_records_and_stale_files_keeping_the_rest(tmp_path):
    case = seed(tmp_path)
    config_path = make_config(tmp_path)
    store = case["store"]
    expired_dir = store.root / case["expired"].removeprefix("attachment://")

    action = rule(clean(config_path, "--apply"))

    assert action["result"] == {"deleted": 3}
    assert not expired_dir.exists() and not case["orphan_old"].exists() and not case["staging_old"].exists()
    assert store.tokens() == {case["fresh"].removeprefix("attachment://")}
    assert store.resolve(case["fresh"], session_id=None, owner="mcp").read_bytes() == b"payload-new.txt"
    assert case["orphan_new"].is_dir() and case["staging_new"].is_file()
    # 再次扫描没有可回收项。
    assert rule(clean(config_path)) is None


def test_missing_agent_database_is_not_created(tmp_path):
    config_path = make_config(tmp_path)
    data = tmp_path / "data"

    assert rule(clean(config_path, "--apply")) is None
    assert not (data / "agent.sqlite3").exists()

    # 没有库时仍按宽限期回收孤儿目录，且不建库。
    age(_write(data / "agent-attachments" / ("e" * 32) / "z.bin"))
    action = rule(clean(config_path, "--apply"))
    assert action["result"] == {"deleted": 1}
    assert not (data / "agent-attachments" / ("e" * 32)).exists()
    assert not (data / "agent.sqlite3").exists()


def test_backup_excludes_agent_attachments_at_data_root_only():
    assert excluded(f"agent-attachments/{'a' * 32}/photo.png")
    assert excluded("agent-attachments/.uploads/partial")
    assert excluded("sources/agent-attachments/clip.mp4") is None
