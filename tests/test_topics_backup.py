"""选题挖掘 · 备份：topic_* 表随全量与部分备份整体导出/导入，数据源密钥默认脱敏，进行中的挖掘标记为中断。"""

from __future__ import annotations

import yaml
from test_backup import restore, seed

from mindex.maintenance.backup import export_backup
from mindex.maintenance.backup_db import store_at
from mindex.topics import datasources, profiles
from mindex.topics.models import ProfileBody

SECRET = "brave-secret-unique-98765"


def library(tmp_path, video_file):
    config, sources, _ = seed(tmp_path / "origin", video_file)
    config_path = tmp_path / "original.yaml"
    config_path.write_text(yaml.safe_dump({"data_dir": str(config.data_dir), "embedding": {"dimensions": 8}}))
    store = store_at(config.data_dir)
    pid = profiles.create_profile(store, ProfileBody(name="财经号", domain="金融", subtopics=["宏观"]))["id"]
    sid = datasources.create_source(store, pid, datasources.SourceBody(kind="brave", name="搜索", secret=SECRET))["id"]
    # topic_items/topic_runs 的 source_id 指向选题数据源与档案，不是素材库 sources。
    store.execute("INSERT INTO topic_items(id,profile_id,source_id,dedup_key,title,fetched_at) "
                  "VALUES('ti_000000000001',?,?,'k','降息',CURRENT_TIMESTAMP)", (pid, sid))
    store.execute("INSERT INTO topic_runs(id,source_id,status,config,options) "
                  "VALUES('tr_000000000001',?,'running','{}','{}')", (pid,))
    return config, sources, config_path, pid, sid


def test_topics_round_trip_full_and_partial(tmp_path, video_file):
    config, sources, config_path, pid, sid = library(tmp_path, video_file)
    for name, selected in (("full", None), ("partial", [sources[0]])):
        bundle = tmp_path / f"backup-{name}"
        export_backup(config, bundle, all_sources=selected is None, sources=selected, config_path=config_path)
        target = tmp_path / f"restored-{name}"
        restore(bundle, target)
        store = store_at(target)
        assert [r["id"] for r in store.rows("SELECT id FROM topic_profiles")] == [pid]
        source = store.one("SELECT * FROM topic_sources WHERE id=?", (sid,))
        assert source and not source["secret"]
        assert store.one("SELECT 1 FROM topic_items WHERE source_id=?", (sid,))
        assert store.one("SELECT status FROM topic_runs")["status"] == "interrupted"
        assert SECRET.encode() not in store.path.read_bytes()
