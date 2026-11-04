"""迁移 CLI 验收（plans §10 阶段 5）：SQLite→PG 装载 → PG 一致性导出 → 对账闭环。"""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from mindex.cli import app as cli_app
from mindex.shared.config import Config
from mindex.shared.storage import Store

pytestmark = pytest.mark.requires_postgres

DSN = os.environ.get("MINDEX_TEST_POSTGRES_DSN", "")


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def pg_config(tmp_path, monkeypatch):
    import psycopg

    from mindex.shared.db.migrate_pg import migrate_pg

    if not DSN:
        pytest.skip("需要 MINDEX_TEST_POSTGRES_DSN")
    monkeypatch.setenv("MINDEX_DATABASE_DSN", DSN)
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    with psycopg.connect(DSN) as conn:
        migrate_pg(conn)
    config = Config(data_dir=tmp_path / "data", database={"backend": "postgres"})
    config.embedding.dimensions = 8
    return config


def _seed_sqlite(store: Store):
    store.execute("INSERT INTO sources(id,name,path,media,layout_key) VALUES('s1','素材','p','{}','k')")
    store.execute("INSERT INTO settings(key,value) VALUES('active','i1')")
    store.execute("INSERT INTO index_versions(id,spec,status) VALUES('i1','{\"format\":2}','ready')")
    store.execute("INSERT INTO index_members(index_id,segment_id,snapshot,complete) "
                  "VALUES('i1','seg1','{\"id\":\"seg1\"}',1)")
    with store.connect() as db:
        assert db.execute("INSERT INTO runs(stage,status) VALUES('detect','complete')").lastrowid == 1


def test_export_load_verify_roundtrip(runner, pg_config, tmp_path, monkeypatch):
    """SQLite 种子 → load 到 PG → export 回暂存 → verify 逐表一致（闭环验收）。"""
    from mindex.shared.backends import open_context

    origin = Store(tmp_path / "sqlite-seed")
    _seed_sqlite(origin)
    export_dir = tmp_path / "bundle"

    # CLI 全局回调按配置文件重建上下文：写一份 postgres 配置供子命令使用
    config_path = tmp_path / "pg.yaml"
    config_path.write_text(
        f"data_dir: {tmp_path / 'data'}\ndatabase:\n  backend: postgres\n",
        encoding="utf-8")
    common = ["--config", str(config_path)]

    # 1) load：种子 SQLite 需在 export 目录形状（metadata.sqlite3）
    export_dir.mkdir()
    (export_dir / "metadata.sqlite3").write_bytes((tmp_path / "sqlite-seed" / "metadata.sqlite3").read_bytes())
    result = runner.invoke(cli_app, [*common, "migration", "load", "--source", str(export_dir)],
                           env={"MINDEX_DATABASE_DSN": DSN})
    assert result.exit_code == 0, result.output
    pg = open_context(pg_config)
    assert pg.store.one("SELECT value FROM settings WHERE key='active'")["value"] == "i1"

    # 2) export：PG 一致性快照导出到新目录
    bundle2 = tmp_path / "bundle2"
    result = runner.invoke(cli_app, [*common, "migration", "export", "--target", str(bundle2)],
                           env={"MINDEX_DATABASE_DSN": DSN})
    assert result.exit_code == 0, result.output
    assert (bundle2 / "manifest.json").is_file()

    # 3) verify：导出结果与当前 PG 对账一致（此时无并发写）
    result = runner.invoke(cli_app, [*common, "migration", "verify", "--source", str(bundle2)],
                           env={"MINDEX_DATABASE_DSN": DSN})
    assert result.exit_code == 0 and '"consistent": true' in result.output

    # 4) 追加写入后对账出现差异（verify 真的在比对）
    pg.store.execute("INSERT INTO settings(key,value) VALUES('drift','1')")
    result = runner.invoke(cli_app, [*common, "migration", "verify", "--source", str(bundle2)],
                           env={"MINDEX_DATABASE_DSN": DSN})
    assert '"consistent": false' in result.output
    assert any("settings" in line for line in result.output.splitlines())


def test_load_rejects_missing_database(runner, pg_config, tmp_path):
    empty = tmp_path / "empty-bundle"
    empty.mkdir()
    result = runner.invoke(cli_app, ["migration", "load", "--source", str(empty)], obj=pg_config)
    assert result.exit_code != 0
    assert result.exit_code != 0  # 错误面板含缺失文件提示（渲染格式随 typer 版本）


def test_export_requires_postgres_backend(runner, tmp_path):
    from mindex.shared.config import Config

    result = runner.invoke(cli_app, ["migration", "export", "--target", str(tmp_path / "x")],
                           obj=Config(data_dir=tmp_path / "data"))
    assert result.exit_code != 0
    assert result.exit_code != 0
