"""Task 1: 统一路径可移植化工具 relativize_config_paths / absolutize_config_paths。

验证：data_dir / deployment_manifest / font_file 相对化到 data_dir.parent；
data_dir 外路径保持绝对；相对↔绝对往返一致；历史后缀兼容。"""

from pathlib import Path

from shared.config import absolutize_config_paths, relativize_config_paths


def test_relativize_internal_paths(config):
    base = config.data_dir.parent
    snapshot = {
        "data_dir": str(config.data_dir),
        "analyzer": {"deployment_manifest": str(config.data_dir / "config-dependencies/abc/analyzer.json")},
        "studio": {"subtitle": {"font_file": str(base / "fonts/subtitle.ttf")}},
    }
    result = relativize_config_paths(config.data_dir, snapshot)
    assert result["data_dir"] == config.data_dir.name  # e.g. "data"
    assert (
        result["analyzer"]["deployment_manifest"]
        == f"{config.data_dir.name}/config-dependencies/abc/analyzer.json"
    )
    assert result["studio"]["subtitle"]["font_file"] == "fonts/subtitle.ttf"


def test_external_paths_stay_absolute(config):
    external = "/opt/models/.insightface/pin.json"
    snapshot = {
        "person": {"model_root": "~/.insightface"},  # 非可移植键，原样保留
        "analyzer": {"deployment_manifest": external},  # data_dir 外，保持绝对
    }
    result = relativize_config_paths(config.data_dir, snapshot)
    assert result["person"]["model_root"] == "~/.insightface"
    assert result["analyzer"]["deployment_manifest"] == external


def test_roundtrip_relative_then_absolute(config):
    manifest = config.data_dir / "config-dependencies/abc/analyzer.json"
    snapshot = {
        "data_dir": str(config.data_dir),
        "analyzer": {"deployment_manifest": str(manifest)},
    }
    relative = relativize_config_paths(config.data_dir, snapshot)
    restored = absolutize_config_paths(config.data_dir, relative)
    assert Path(restored["data_dir"]) == config.data_dir
    assert Path(restored["analyzer"]["deployment_manifest"]) == manifest


def test_absolutize_rebases_to_moved_data_dir(config, tmp_path):
    # 模拟目录改名：相对值按新 data_dir 还原，而不是旧绝对路径。
    relative = {
        "data_dir": "data",
        "analyzer": {"deployment_manifest": "data/config-dependencies/abc/analyzer.json"},
    }
    moved = tmp_path / "renamed" / "data"
    restored = absolutize_config_paths(moved, relative)
    assert Path(restored["data_dir"]) == moved
    assert (
        Path(restored["analyzer"]["deployment_manifest"])
        == moved.parent / "data/config-dependencies/abc/analyzer.json"
    )


def test_historical_suffix_preserved_for_fingerprint_stability(config):
    # 历史绝对路径指向已不存在的旧项目根，但相对后缀与当前一致 → 沿用旧相对拼写。
    current_rel = "data/config-dependencies/abc/analyzer.json"
    snapshot = {"analyzer": {"deployment_manifest": "/previous/project/" + current_rel}}
    reference = {"analyzer": {"deployment_manifest": current_rel}}
    result = relativize_config_paths(config.data_dir, snapshot, reference)
    assert result["analyzer"]["deployment_manifest"] == current_rel


def test_already_relative_paths_untouched(config):
    snapshot = {"data_dir": "data", "analyzer": {"deployment_manifest": "data/x/m.json"}}
    assert relativize_config_paths(config.data_dir, snapshot) == snapshot


def test_absolutize_leaves_absolute_external_untouched(config):
    external = "/opt/models/pin.json"
    snapshot = {"analyzer": {"deployment_manifest": external}}
    assert absolutize_config_paths(config.data_dir, snapshot)["analyzer"]["deployment_manifest"] == external
