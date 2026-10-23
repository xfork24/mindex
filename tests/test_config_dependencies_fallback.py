"""Task 4: _config_dependencies 对失效绝对路径按 config-dependencies 片段回退。"""
import hashlib

import pytest

from mindex.maintenance.backup_files import _config_dependencies


def _write_manifest(path, content=b'{"file_identity": "x"}'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()[:24]


def test_resolves_stale_absolute_via_data_root(tmp_path):
    data_root = tmp_path / "mindex" / "data"
    digest = _write_manifest(data_root / "config-dependencies" / "abc" / "analyzer.json")
    payload = tmp_path / "payload"
    payload.mkdir()
    # 配置里残留的是旧的、已不存在的绝对路径（模拟改名前的 mindex2），
    # 但其 config-dependencies/<dir>/<name> 片段与当前数据目录内的文件一致。
    stale = "/Users/old/workspace/mindex2/data/config-dependencies/abc/analyzer.json"
    configs = [{"analyzer": {"deployment_manifest": stale}}]

    paths, _missing, _env = _config_dependencies(
        configs, base_dir=data_root.parent, payload=payload, data_root=data_root
    )
    # 成功定位并打包，映射里保留原始字符串以便数据库重定位。
    assert stale in paths
    assert (payload / paths[stale]).is_file()
    assert digest  # 文件内容摘要存在（打包目录名基于内容摘要重算）


def test_resolves_relative_dependency(tmp_path):
    data_root = tmp_path / "data"
    _write_manifest(data_root / "config-dependencies" / "zzz" / "font.ttf", b"FONTDATA")
    payload = tmp_path / "payload"
    payload.mkdir()
    # 迁移后以相对 data_dir.parent 形式存储。
    relative = "data/config-dependencies/zzz/font.ttf"
    configs = [{"studio": {"subtitle": {"font_file": relative}}}]
    paths, _, _ = _config_dependencies(configs, base_dir=data_root.parent, payload=payload, data_root=data_root)
    assert relative in paths
    assert (payload / paths[relative]).is_file()


def test_truly_missing_still_raises(tmp_path):
    data_root = tmp_path / "data"
    data_root.mkdir(parents=True)
    payload = tmp_path / "payload"
    payload.mkdir()
    configs = [{"analyzer": {"deployment_manifest": "/nowhere/config-dependencies/xx/gone.json"}}]
    with pytest.raises(ValueError, match="配置依赖文件缺失"):
        _config_dependencies(configs, base_dir=data_root.parent, payload=payload, data_root=data_root)
