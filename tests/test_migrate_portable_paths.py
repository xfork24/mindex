"""Task 5: 存量绝对路径迁移脚本的幂等性与正确性。"""
import importlib.util
import json
import sqlite3
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "migrate_portable_paths",
    Path(__file__).resolve().parents[1] / "scripts" / "migrate-portable-paths.py",
)
mig = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mig)


def _make_db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE source_tasks(id TEXT PRIMARY KEY, config TEXT)")
    for rid, cfg in rows:
        con.execute("INSERT INTO source_tasks(id, config) VALUES(?,?)", (rid, json.dumps(cfg)))
    con.commit()
    con.close()


def test_absolute_data_dir_and_manifest_become_relative(tmp_path):
    data_dir = tmp_path / "data"
    manifest = data_dir / "config-dependencies" / "abc" / "analyzer.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")
    db = data_dir / "metadata.sqlite3"
    _make_db(db, [("t1", {"data_dir": str(data_dir),
                          "analyzer": {"deployment_manifest": str(manifest)}})])

    report = mig.migrate_database(db, data_dir, apply=True)
    assert report, "应有改写"
    con = sqlite3.connect(db)
    cfg = json.loads(con.execute("SELECT config FROM source_tasks WHERE id='t1'").fetchone()[0])
    con.close()
    assert cfg["data_dir"] == "data"
    assert cfg["analyzer"]["deployment_manifest"] == "data/config-dependencies/abc/analyzer.json"


def test_stale_absolute_relocated_then_relativized(tmp_path):
    data_dir = tmp_path / "data"
    manifest = data_dir / "config-dependencies" / "abc" / "analyzer.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")
    db = data_dir / "metadata.sqlite3"
    # 失效的旧绝对路径（旧项目根 mindex2 已不存在），但片段在当前 data_dir 内存在。
    stale = "/Users/old/workspace/mindex2/data/config-dependencies/abc/analyzer.json"
    _make_db(db, [("t1", {"data_dir": str(data_dir), "analyzer": {"deployment_manifest": stale}})])

    mig.migrate_database(db, data_dir, apply=True)
    con = sqlite3.connect(db)
    cfg = json.loads(con.execute("SELECT config FROM source_tasks WHERE id='t1'").fetchone()[0])
    con.close()
    assert cfg["analyzer"]["deployment_manifest"] == "data/config-dependencies/abc/analyzer.json"


def test_external_path_preserved(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    db = data_dir / "metadata.sqlite3"
    external = "/opt/models/pin.json"
    _make_db(db, [("t1", {"person": {"model_root": "~/.insightface"},
                          "analyzer": {"deployment_manifest": external}})])
    mig.migrate_database(db, data_dir, apply=True)
    con = sqlite3.connect(db)
    cfg = json.loads(con.execute("SELECT config FROM source_tasks WHERE id='t1'").fetchone()[0])
    con.close()
    assert cfg["analyzer"]["deployment_manifest"] == external
    assert cfg["person"]["model_root"] == "~/.insightface"


def test_idempotent(tmp_path):
    data_dir = tmp_path / "data"
    manifest = data_dir / "config-dependencies" / "abc" / "analyzer.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")
    db = data_dir / "metadata.sqlite3"
    _make_db(db, [("t1", {"data_dir": str(data_dir),
                          "analyzer": {"deployment_manifest": str(manifest)}})])
    first = mig.migrate_database(db, data_dir, apply=True)
    assert first
    # 再次运行应无改写。
    second = mig.migrate_database(db, data_dir, apply=True)
    assert second == []
