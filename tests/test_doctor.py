"""部署诊断（plans §11 阶段 5）：组合校验、逐后端连通与失败退出语义。"""

from __future__ import annotations

import pytest

from diagnostics.doctor import doctor
from shared.config import Config


def test_local_defaults_pass(tmp_path):
    report = doctor(Config(data_dir=tmp_path / "data"))
    assert report["ok"] and report["summary"] == "全部检查通过"
    names = {item["check"] for item in report["checks"]}
    assert {"backends.combination", "database.identity", "vectors.server", "storage.bucket"} <= names


def test_invalid_combination_reports_failure(tmp_path):
    report = doctor(Config(data_dir=tmp_path / "data", queue={"backend": "postgres"}))
    assert not report["ok"]
    assert "sqlite 单机形态" in report["checks"][0]["detail"]


def test_unreachable_chroma_server_fails_closed(tmp_path, monkeypatch):
    import os

    if os.environ.get("MINDEX_TEST_POSTGRES_DSN"):
        monkeypatch.setenv("MINDEX_DATABASE_DSN", os.environ["MINDEX_TEST_POSTGRES_DSN"])
    else:
        pytest.skip("需要 MINDEX_TEST_POSTGRES_DSN（chroma_server 属共享后端组合）")
    report = doctor(
        Config(
            data_dir=tmp_path / "data",
            database={"backend": "postgres"},
            vector={
                "backend": "chroma_server",
                "chroma_server": {"url": "http://127.0.0.1:59999", "token_env": "MINDEX_NO_TOKEN"},
            },
        )
    )
    assert not report["ok"]
    vector = next(item for item in report["checks"] if item["check"] == "vectors.server")
    assert not vector["ok"]  # 连不通明确失败，不假数据降级（plans §12）


def test_missing_dsn_is_actionable(tmp_path, monkeypatch):
    monkeypatch.delenv("MINDEX_DATABASE_DSN", raising=False)
    report = doctor(Config(data_dir=tmp_path / "data", database={"backend": "postgres"}))
    assert not report["ok"]
    assert "MINDEX_DATABASE_DSN" in report["checks"][0]["detail"]


@pytest.mark.requires_postgres
def test_pg_single_doctor_live(tmp_path, monkeypatch):
    import os

    dsn = os.environ.get("MINDEX_TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("需要 MINDEX_TEST_POSTGRES_DSN")
    monkeypatch.setenv("MINDEX_DATABASE_DSN", dsn)
    report = doctor(Config(data_dir=tmp_path / "data", database={"backend": "postgres"}))
    assert report["ok"]
    combination = report["checks"][0]
    assert "form=pg-single" in combination["detail"]
