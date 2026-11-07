"""Task 2/3: task 配置写入相对、读取按当前 data_dir 还原为绝对。

验证数据目录移动/改名后，冻结的任务配置仍能还原出当前有效的绝对路径。"""

import json

from admin.source_tasks import freeze_config, restore_config
from settings.runtime_settings import RuntimeConfig
from shared.storage import Store


def test_freeze_stores_relative_data_dir_and_manifest(config):
    store = Store(config.data_dir)
    config.analyzer.deployment_manifest = str(config.data_dir / "config-dependencies/abc/analyzer.json")
    frozen = json.loads(freeze_config(config, store))
    # data_dir 与内部清单以相对形式落库（相对 data_dir.parent）。
    assert frozen["data_dir"] == config.data_dir.name
    assert (
        frozen["analyzer"]["deployment_manifest"]
        == f"{config.data_dir.name}/config-dependencies/abc/analyzer.json"
    )
    assert "mindex" not in frozen["data_dir"]  # 不含任何绝对前缀


def test_restore_rebases_to_moved_data_dir(config, tmp_path):
    store = Store(config.data_dir)
    config.analyzer.deployment_manifest = str(config.data_dir / "config-dependencies/abc/analyzer.json")
    raw = freeze_config(config, store)

    # 模拟目录被移动/改名：用新的 data_dir 构造 runtime 回读。
    moved = tmp_path / "renamed" / "data"
    moved.mkdir(parents=True)
    moved_store = Store(moved)
    moved_config = config.model_copy(update={"data_dir": moved})
    restored = restore_config(raw, RuntimeConfig(moved_config, moved_store), moved_store)

    assert restored.data_dir == moved
    assert restored.analyzer.deployment_manifest == str(moved / "config-dependencies/abc/analyzer.json")


def test_restore_keeps_external_manifest_absolute(config, tmp_path):
    store = Store(config.data_dir)
    # 真正的外部路径：位于 data_dir.parent 之外，无法相对表达。
    external = "/opt/mindex-models/pin.json"
    config.analyzer.deployment_manifest = external
    raw = freeze_config(config, store)
    frozen = json.loads(raw)
    assert frozen["analyzer"]["deployment_manifest"] == external  # 外部路径保持绝对
    restored = restore_config(raw, RuntimeConfig(config, store), store)
    assert restored.analyzer.deployment_manifest == external
