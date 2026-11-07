"""迁移阶段硬退出、首写前绑定、待激活准入、秘密与目标新写入后的反向闭环。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from maintenance.migration import activate_bundle, export_bundle, load_bundle, verify_bundle
from shared.backends import open_context
from shared.config import Config, GpuResourceGroup
from shared.db.maintenance_gate import MaintenanceBusy
from shared.storage import Store, dumps
from test_scaling_correctness import pg_config as pg_config
from test_scaling_correctness import s3_config as s3_config


def source_bundle(tmp_path, *, vectors=False, secrets=False):
    source = Config(data_dir=tmp_path / "source")
    with Store.open(source) as store:
        file = store.root / "sources/input.bin"
        file.parent.mkdir()
        file.write_bytes(b"authoritative-source")
        store.execute(
            "INSERT INTO sources(id,name,path,media,layout_key) "
            "VALUES('s','素材','sources/input.bin','{}','k')"
        )
        store.execute(
            "INSERT INTO source_tasks(id,source_id,work,status,config,options) "
            "VALUES('old','s','analysis','running',?,'{}')",
            (dumps({"api_key": "registered-secret", "api_key_env": "MODEL_ENV"}),),
        )
        if vectors:
            from processing.indexing import VectorIndex

            store.execute(
                "INSERT INTO index_versions(id,spec,status) VALUES('version','{\"format\":2}','ready')"
            )
            store.execute(
                "INSERT INTO representations(id,index_id,segment_id,view,document) "
                "VALUES('v','version','seg','video','d')"
            )
            VectorIndex(store.root, "version", create=True).upsert(
                "v", [1.0, 0.0], {"id": "seg", "source_id": "s", "kind": "shot"}, "video", "d"
            )
        if secrets:
            store.execute(
                "INSERT INTO source_curation(id,notes) VALUES('s',?)",
                (dumps({"password": "这是普通用户 JSON，必须原样保留"}),),
            )
    bundle = tmp_path / "bundle"
    manifest = export_bundle(source, bundle, with_media=True, with_vectors=vectors)
    return source, bundle, manifest


@pytest.mark.parametrize(
    "boundary",
    [
        "preflight",
        "identity",
        "media-pending",
        "media-written",
        "vectors",
        "metadata",
        "tasks",
        "validation",
        "activation",
    ],
)
def test_hard_kill_at_each_stage_resumes_same_migration(tmp_path, boundary):
    _, bundle, manifest = source_bundle(tmp_path)
    target = Config(data_dir=tmp_path / "target")
    code = r"""
import os,signal,sys
import mindex  # 自举:包目录加入 sys.path,后续短名导入
from pathlib import Path
from shared.config import Config
from maintenance.migration import load_bundle,activate_bundle
from maintenance.migration_journal import MigrationJournal
boundary=sys.argv[3]
original_stage=MigrationJournal.stage
original_object=MigrationJournal.object
def kill():
    os.kill(os.getpid(),signal.SIGKILL)
def stage(self,name,state,**kwargs):
    result=original_stage(self,name,state,**kwargs)
    if name==boundary and state=='complete':
        kill()
    return result
def object(self,kind,key,digest,*,state):
    if boundary=='media-written' and kind=='media' and state=='verified':
        kill()
    result=original_object(self,kind,key,digest,state=state)
    if boundary=='media-pending' and kind=='media' and state=='pending':
        kill()
    return result
MigrationJournal.stage=stage
MigrationJournal.object=object
cfg=Config(data_dir=Path(sys.argv[1]))
load_bundle(cfg,Path(sys.argv[2]))
if boundary=='activation':
    activate_bundle(cfg,Path(sys.argv[2]))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(target.data_dir), str(bundle), boundary],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == -signal.SIGKILL, result.stdout + result.stderr
    with Store.open(target) as store:
        row = store.one("SELECT * FROM migration_runs")
        if boundary != "preflight":
            assert row["migration_id"] == manifest["migration_id"]
            assert row["source_library_id"] == manifest["library_id"]
            assert row["source_sha256"] == manifest["database_sha256"] and row["manifest_sha256"]
        if (target.data_dir / "sources/input.bin").exists():
            assert row is not None  # 首份目标对象之前，身份与源清单已持久绑定。
    assert load_bundle(target, bundle)["consistent"]
    with Store.open(target) as store:
        assert store.one("SELECT status FROM source_tasks WHERE id='old'")["status"] == "interrupted"
        with pytest.raises(MaintenanceBusy):
            store.execute("INSERT INTO settings(key,value) VALUES('blocked','yes')")
    assert activate_bundle(target, bundle)["activated"]
    assert verify_bundle(target, bundle)["consistent"]


def test_vector_write_before_batch_record_resumes_without_reupsert(tmp_path):
    _, bundle, _ = source_bundle(tmp_path, vectors=True)
    target = Config(data_dir=tmp_path / "target")
    code = r"""
import os,signal,sys
import mindex  # 自举:包目录加入 sys.path,后续短名导入
from pathlib import Path
from shared.config import Config
from maintenance import vector_bundle
from maintenance.migration import load_bundle
original=vector_bundle._upsert
def kill(*args,**kwargs):
    original(*args,**kwargs)
    os.kill(os.getpid(),signal.SIGKILL)
vector_bundle._upsert=kill
load_bundle(Config(data_dir=Path(sys.argv[1])),Path(sys.argv[2]),with_vectors=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(target.data_dir), str(bundle)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == -signal.SIGKILL, result.stderr

    with patch(
        "maintenance.vector_bundle._upsert",
        side_effect=AssertionError("已存在且已复验的向量不能重复 upsert"),
    ):
        assert load_bundle(target, bundle, with_vectors=True)["consistent"]
    with Store.open(target) as store:
        batches = store.rows("SELECT state FROM migration_objects WHERE kind='vector-batch'")
        assert batches == [{"state": "verified"}]


def test_known_secrets_redacted_unknown_user_content_blocks_activation(tmp_path):
    _, bundle, manifest = source_bundle(tmp_path, secrets=True)
    assert b"registered-secret" not in (bundle / "metadata.sqlite3").read_bytes()
    assert any("source_curation" in location for location in manifest["unknown_secret_locations"])
    target = Config(data_dir=tmp_path / "target")
    assert load_bundle(target, bundle)["consistent"]
    with Store.open(target) as store:
        assert json.loads(store.one("SELECT config FROM source_tasks WHERE id='old'")["config"]) == {
            "api_key": "",
            "api_key_env": "MODEL_ENV",
        }
        assert (
            json.loads(store.one("SELECT notes FROM source_curation WHERE id='s'")["notes"])["password"]
            == "这是普通用户 JSON，必须原样保留"
        )
    with pytest.raises(ValueError, match="source_curation"):
        activate_bundle(target, bundle)


def test_activated_target_new_writes_reverse_export_and_old_bundle_replay_rejected(tmp_path):
    _, bundle, _ = source_bundle(tmp_path)
    target = Config(data_dir=tmp_path / "target")
    assert load_bundle(target, bundle)["consistent"]
    assert activate_bundle(target, bundle)["activated"]
    with Store.open(target) as store:
        store.execute("INSERT INTO settings(key,value) VALUES('new-business-write','preserve')")
    with pytest.raises(ValueError, match="已激活"):
        load_bundle(target, bundle)
    reverse = tmp_path / "reverse"
    export_bundle(target, reverse, with_media=True)
    restored = Config(data_dir=tmp_path / "reverse-target")
    assert load_bundle(restored, reverse)["consistent"]
    assert activate_bundle(restored, reverse)["activated"]
    with Store.open(restored) as store:
        assert store.one("SELECT value FROM settings WHERE key='new-business-write'")["value"] == "preserve"
        assert (store.root / "sources/input.bin").read_bytes() == b"authoritative-source"


@pytest.mark.requires_postgres
def test_pg_task_recovery_cannot_claim_or_write_before_atomic_activation(pg_config, tmp_path):

    from api import create_app
    from shared.db.queue_pg import PgTaskQueue
    from studio import studio

    _, bundle, manifest = source_bundle(tmp_path)
    assert load_bundle(pg_config, bundle)["consistent"]
    with open_context(pg_config) as context:
        store = context.store
        assert context.library_id == manifest["library_id"]
        task = store.one(
            "SELECT status,queue_attempt_id,queue_fencing_token FROM source_tasks WHERE id='old'"
        )
        assert task == {"status": "queued", "queue_attempt_id": None, "queue_fencing_token": 0}
        queue = PgTaskQueue(context.library_id)
        with store.backend.connect(control=True) as db:
            assert queue.claim(db, "too-early", ["source"]) is None
        with pytest.raises(MaintenanceBusy):
            store.execute("INSERT INTO settings(key,value) VALUES('blocked','1')")
        with pytest.raises(MaintenanceBusy):
            studio.create_job(pg_config, "迁移未激活时不能产生创作文件")
        with TestClient(create_app(pg_config)) as client:
            assert client.get("/api/live").status_code == 200
            ready = client.get("/api/ready")
            assert ready.status_code == 503 and any(
                c["check"] == "database.migration" and not c["ok"] for c in ready.json()["checks"]
            )
            assert client.post("/api/studio/jobs", json={"text": "不可写"}).status_code == 409
    assert activate_bundle(pg_config, bundle)["activated"]
    with Store.open(pg_config) as store, store.connect() as db:
        task = PgTaskQueue(store.library_id).claim(db, "new-attempt", ["source"])
        assert task and task.attempt_id and task.fencing_token == 1


@pytest.mark.requires_postgres
def test_pg_hard_kill_before_metadata_commit_resumes_and_event_sequence_advances(pg_config, tmp_path):
    source, _, _ = source_bundle(tmp_path)
    with Store.open(source) as store:
        store.execute("INSERT INTO studio_events(id,job_id,build_id,data) VALUES(42,'job','build','{}')")
    bundle = tmp_path / "with-events"
    export_bundle(source, bundle, with_media=True)
    code = (
        "\n"
        "import os,signal,sys\n"
        "import mindex  # 自举:包目录加入 sys.path,后续短名导入\n"
        "from pathlib import Path\n"
        "from shared.config import Config\n"
        "from shared.db import transfer\n"
        "from maintenance.migration import load_bundle\n"
        "original=transfer.copy_sqlite_to_pg\n"
        "def crash(*args,**kwargs):\n"
        "    original(*args,**kwargs)\n"
        "    os.kill(os.getpid(),signal.SIGKILL)\n"
        "transfer.copy_sqlite_to_pg=crash\n"
        "cfg=Config(data_dir=Path(sys.argv[1]),database={'backend':'postgres'},queue={'backend':'postgres'}"
        ",locks={'backend':'postgres'})\n"
        "load_bundle(cfg,Path(sys.argv[2]))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(pg_config.data_dir), str(bundle)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == -signal.SIGKILL, result.stderr
    with Store.open(pg_config) as store:
        assert not store.rows("SELECT * FROM sources")
        assert store.one("SELECT status FROM migration_runs")["status"] == "preparing"
    assert load_bundle(pg_config, bundle)["consistent"]
    assert activate_bundle(pg_config, bundle)["activated"]
    with Store.open(pg_config) as store, store.connect() as db:
        assert (
            db.execute(
                "INSERT INTO studio_events(job_id,build_id,data) VALUES('job','new','{}') RETURNING id"
            ).fetchone()[0]
            == 43
        )
    reverse = tmp_path / "reverse-events"
    export_bundle(pg_config, reverse, with_media=True)
    restored = Config(data_dir=tmp_path / "event-target")
    assert load_bundle(restored, reverse)["consistent"]
    with Store.open(restored) as store:
        assert [row["id"] for row in store.rows("SELECT id FROM studio_events ORDER BY id")] == [42, 43]


def test_migration_preserves_unknown_remote_execution_and_gpu_occupation(tmp_path):
    source, _, _ = source_bundle(tmp_path)
    with Store.open(source) as store:
        store.execute("INSERT INTO gpu_resource_groups(id,capacity) VALUES('physical-gpu',1)")
        store.execute(
            "INSERT INTO gpu_allocations(id,group_id,library_id,task_id,attempt_id,state) "
            "VALUES('allocation','physical-gpu',?,7,'old-attempt','active')",
            (store.library_id,),
        )
        store.execute(
            "INSERT INTO external_calls(id,resource,task_id,operation,state) "
            "VALUES('call','model','old','generate','dispatching')"
        )
    bundle = tmp_path / "remote-execution"
    export_bundle(source, bundle, with_media=True)
    target = Config(data_dir=tmp_path / "target")
    assert load_bundle(target, bundle)["consistent"]
    with Store.open(target) as store:
        assert store.one("SELECT state FROM external_calls WHERE id='call'")["state"] == "needs_review"
        assert store.one("SELECT status FROM source_tasks WHERE id='old'")["status"] == "failed"
        assert store.one("SELECT state FROM gpu_allocations WHERE id='allocation'")["state"] == "unknown"
    with pytest.raises(ValueError, match="物理资源组"):
        activate_bundle(target, bundle)
    target.gpu.groups = {"physical-gpu": GpuResourceGroup(capacity=1, queues=["source"])}
    assert activate_bundle(target, bundle)["activated"]
    with Store.open(target) as store:
        assert store.one("SELECT state FROM gpu_allocations WHERE id='allocation'")["state"] == "unknown"


@pytest.mark.requires_postgres
@pytest.mark.requires_s3
@pytest.mark.requires_chroma_server
@pytest.mark.parametrize("boundary", ["media", "vector-batch"])
def test_remote_write_before_record_hard_kill_verifies_without_overwrite(tmp_path, s3_config, boundary):

    _, bundle, _ = source_bundle(tmp_path, vectors=True)
    target = s3_config
    target.vector.backend = "chroma_server"
    target.vector.chroma_server.url = os.environ["MINDEX_TEST_CHROMA_SERVER"]
    target.gpu.cpu_queues = ["source"]
    config_file = tmp_path / "target.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "data_dir": str(target.data_dir),
                **{
                    name: getattr(target, name).model_dump()
                    for name in ("database", "storage", "vector", "queue", "locks", "gpu")
                },
            }
        )
    )
    code = r"""
import os,signal,sys
import mindex  # 自举:包目录加入 sys.path,后续短名导入
from pathlib import Path
from shared.config import load_config
from maintenance.migration import load_bundle
from maintenance.migration_journal import MigrationJournal
original=MigrationJournal.object
def object(self,kind,key,digest,*,state):
    if kind==sys.argv[3] and state=='verified':
        os.kill(os.getpid(),signal.SIGKILL)
    return original(self,kind,key,digest,state=state)
MigrationJournal.object=object
load_bundle(load_config(sys.argv[1]),Path(sys.argv[2]),with_vectors=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(config_file), str(bundle), boundary],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == -signal.SIGKILL, result.stderr
    with Store.open(target) as store:
        assert (
            store.one("SELECT state FROM migration_objects WHERE kind=?", (boundary,))["state"] == "pending"
        )
    symbol = (
        "shared.backends.s3.S3MediaStore.put_immutable"
        if boundary == "media"
        else "maintenance.vector_bundle._upsert"
    )
    with patch(symbol, side_effect=AssertionError("已存在的远端内容必须先核对，不能重复写入")):
        assert load_bundle(target, bundle, with_vectors=True)["consistent"]
    assert activate_bundle(target, bundle)["activated"]
