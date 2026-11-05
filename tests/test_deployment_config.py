"""部署设置修订、只读与运行边界；保存不能改变当前进程后端/身份。"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from mindex.api import create_app
from mindex.shared.config import Config, load_config


def test_deployment_validation_revision_conflict_and_restart_boundary(tmp_path):
    config_file = tmp_path / 'config.yaml'
    config_file.write_text('data_dir: node\nruntime_overrides: false\n')
    config = load_config(config_file)
    with TestClient(create_app(config)) as client:
        url = '/api/settings/deployment'
        first = client.get(url).json()
        assert first['writable'] and first['revision'] and not first['restart_required']
        assert not first['fields']['server.library_id']['editable']
        patch = {'revision': first['revision'], 'config': {'database': {'postgres': {'pool_max': 4}}}}
        validation = client.post(url + '/validate', json=patch)
        assert validation.status_code == 200 and validation.json()['restart_required']
        assert config.database.postgres.pool_max == 8
        saved = client.put(url, json=patch)
        assert saved.status_code == 200, saved.text
        assert saved.json()['revision'] != first['revision'] and saved.json()['restart_required']
        assert config.database.postgres.pool_max == 8
        assert load_config(config_file).database.postgres.pool_max == 4
        assert client.put(url, json=patch).status_code == 409
        current = client.get(url).json()
        for bad in [
            {'database': {'postgres': {'pool_max': 99}}},
            {'server': {'library_id': 'another-library'}},
            {'database': {'postgres': {'dsn': 'postgresql://secret'}}},
            {'storage': {'s3': {'secret_access_key': 'secret'}}},
            {'agent': {'gateway_url': 'http://user:password@host'}},
            {'agent': {'gateway_url': 'https://host?token=secret'}},
            {'queue': {'backend': 'postgres'}},
        ]:
            rejected = client.post(url + '/validate', json={'config': bad})
            assert rejected.status_code in {409, 422}, rejected.text
            assert 'secret' not in rejected.text and 'password@' not in rejected.text
        assert client.get(url).json()['revision'] == current['revision']


def test_deployment_no_config_file_and_read_only_state(tmp_path):
    with TestClient(create_app(Config(data_dir=tmp_path / 'memory'))) as client:
        assert client.get('/api/settings/deployment').json()['writable'] is False
        assert client.put('/api/settings/deployment', json={'config': {}, 'revision': 'x'}).status_code == 409
    file = tmp_path / 'read-only.yaml'
    file.write_text('data_dir: readonly\nruntime_overrides: false\n')
    file.chmod(0o444)
    try:
        with TestClient(create_app(load_config(file))) as client:
            assert client.get('/api/settings/deployment').json()['writable'] is False
            assert client.put('/api/settings/deployment', json={'config': {}, 'revision': 'x'}).status_code == 409
    finally:
        file.chmod(0o600)


def test_deployment_credential_references_only_and_no_plaintext_echo(tmp_path, monkeypatch):
    file = tmp_path / 'config.yaml'
    file.write_text('data_dir: node\nruntime_overrides: false\nagent:\n  gateway_url: http://legacy:never-echo-this@localhost\n')
    monkeypatch.setenv('KNOWN_PG_REFERENCE', 'postgresql://private:never-echo-this@host/db')
    config = load_config(file)
    with TestClient(create_app(config)) as client:
        data = client.get('/api/settings/deployment').json()
        assert 'never-echo-this' not in json.dumps(data)
        response = client.post('/api/settings/deployment/validate', json={'config': {
            'database': {'backend': 'postgres', 'postgres': {'dsn_env': 'KNOWN_PG_REFERENCE'}}}})
        assert response.status_code == 200 and response.json()['migration_required']
        assert 'never-echo-this' not in response.text
        assert client.post('/api/settings/deployment/validate', json={'config': {
            'database': {'backend': 'postgres', 'postgres': {'dsn_env': 'MISSING_PG_REFERENCE'}}}}).status_code == 422
