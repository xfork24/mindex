from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import FixtureEmbedder
from PIL import Image
from typer.testing import CliRunner

from mindex.adapters import ModelError
from mindex.backup import (
    _saved_secrets,
    export_backup,
    import_backup,
    inspect_bundle,
    journal_path,
    recover_import,
    source_list,
    vector_worker,
)
from mindex.backup_db import merge_database, store_at
from mindex.cli import app
from mindex.config import Config, fingerprint
from mindex.curation import CurationPatch
from mindex.processing_lock import SharedFileLock
from mindex.storage import Store, dumps


def seed(root, video, names=('a', 'b'), *, space='test'):
    config = Config(data_dir=root)
    config.embedding.dimensions = 8
    store = Store(root)
    snapshots = []
    for name in names:
        sid = fingerprint(name)
        relative = f'sources/{sid}/video.mp4'
        target = root / relative
        target.parent.mkdir(parents=True)
        shutil.copy2(video, target)
        thumbnail = f'thumbnails/{sid}/shot.jpg'
        (root / thumbnail).parent.mkdir(parents=True)
        Image.new('RGB', (20, 20), color='red').save(root / thumbnail)
        segment = {'id': fingerprint(['shot', name]), 'source_id': sid, 'kind': 'shot', 'start': 0,
                   'end': 10, 'path': relative, 'thumbnail': thumbnail, 'generation': 'test',
                   'parent_id': None, 'reason': ''}
        store.execute('INSERT INTO sources(id,name,path,media,layout_key,prepared) VALUES(?,?,?,?,?,1)',
                      (sid, name, relative, dumps({'duration': 10}), config.layout_key))
        store.execute('INSERT INTO segments(' + ','.join(segment) + ') VALUES('
                      + ','.join('?' for _ in segment) + ')', tuple(segment.values()))
        snapshot = {**segment, 'source_path': relative, 'source_name': name, 'source_duration': 10,
                    'views': {}, 'indexed_views': ['video'], 'representation_ids': [fingerprint([name, 'video'])]}
        snapshots.append(snapshot)
        archive = root / 'subtitle-archives' / sid / 'manifest.json'
        archive.parent.mkdir(parents=True)
        archive.write_text(dumps({'files': {}, 'version': 1}))
        (root / 'plans').mkdir(exist_ok=True)
        (root / 'plans' / f'{sid}.json').write_text(dumps({'source_id': sid}))
    identity = FixtureEmbedder(config).identity()
    if space != 'test':
        identity['revision'] = space
    spec = {'format': 2, 'space': identity, 'preprocessing': config.video.model_dump(),
            'mode': 'auto', 'manifest': fingerprint(snapshots)}
    index = fingerprint(spec)
    store.execute('INSERT INTO index_versions(id,spec,status) VALUES(?,?,?)', (index, dumps(spec), 'ready'))
    vectors = []
    for s in snapshots:
        rid = fingerprint([index, s['representation_ids'][0]])
        store.execute('INSERT INTO index_members VALUES(?,?,?,1)', (index, s['id'], dumps(s)))
        store.execute('INSERT INTO representations VALUES(?,?,?,?,?,?)', (rid, index, s['id'], 'video', '', None))
        vectors.append({'id': rid, 'embedding': [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                        'document': 'video', 'metadata': {'segment_id': s['id'], 'source_id': s['source_id'],
                                                        'granularity': 'shot', 'view': 'video'}})
    batches = root.parent / (root.name + '-seed')
    batches.mkdir()
    (batches / 'batch.json').write_text(dumps(vectors))
    vector_worker('load', root, batches, collections=[{'index_id': index, 'batches': ['batch.json']}],
                  index_map={index: index}, rep_map={})
    store.activate(index)
    return config, [s['source_id'] for s in snapshots], index


@pytest.fixture
def library(tmp_path, video_file):
    config, sources, index = seed(tmp_path / 'origin', video_file)
    config_path = tmp_path / 'original.yaml'
    config_path.write_text(yaml.safe_dump({'data_dir': str(config.data_dir), 'embedding': {'dimensions': 8}}))
    return config, sources, index, config_path


def export(library, path, **kwargs):
    config, _, _, config_path = library
    return export_backup(config, path, all_sources=not kwargs.get('sources'), config_path=config_path, **kwargs)


def restore(bundle, target, **kwargs):
    return import_backup(Config(data_dir=target), bundle, all_sources=not kwargs.get('sources'),
                         config_path=target.parent / (target.name + '.yaml'), **kwargs)


def search_results(root):
    script = '''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from conftest import FixtureEmbedder
from mindex.config import Config
from mindex.search import SearchEngine
from mindex.types import TextSearch
config = Config(data_dir=Path(sys.argv[1]))
config.embedding.dimensions = 8
print(json.dumps(SearchEngine(config, FixtureEmbedder(config)).text(
    TextSearch(text='红色', granularity='shot'))['results'], ensure_ascii=False))
'''
    result = subprocess.run([sys.executable, '-c', script, str(root), str(Path(__file__).parent)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_full_restore_relocated_and_cross_process_vectors(library, tmp_path):
    config, sources, index, _ = library
    store = store_at(config.data_dir)
    store.execute('INSERT INTO settings VALUES(?,?)', ('search.limit', '12'))
    history = config.data_dir / 'historical-cache.txt'
    history.write_text('保留全量历史文件')
    before = search_results(config.data_dir)
    bundle = tmp_path / 'backup'
    exported = export(library, bundle)
    assert exported['includes_secrets'] is False
    target = tmp_path / 'different-location' / 'data'
    result = restore(bundle, target)
    assert result['searchable_sources'] == sorted(sources)
    assert store_at(target).active_index()['id'] == index
    assert store_at(target).one("SELECT value FROM settings WHERE key='search.limit'")['value'] == '12'
    assert (target / history.name).read_text() == history.read_text()
    assert search_results(target) == before
    script = ('from pathlib import Path; from mindex.indexing import VectorIndex; '
              'import sys; v=VectorIndex(Path(sys.argv[1]),sys.argv[2]); '
              'print(v.collection.query(query_embeddings=[[1.,0.,0.,0.,0.,0.,0.,0.]],n_results=2)["ids"])')
    proc = subprocess.run([sys.executable, '-c', script, str(target), index], capture_output=True,
                          text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert len(json.loads(proc.stdout.replace("'", '"'))[0]) == 2
    restored_config = yaml.safe_load((target.parent / 'data.yaml').read_text())
    assert restored_config['data_dir'] == str(target)


@pytest.mark.parametrize('at_export', [True, False])
def test_selected_sources_leave_no_rows_or_files(library, tmp_path, at_export):
    _, sources, old, _ = library
    bundle = tmp_path / 'backup'
    export(library, bundle, **({'sources': [sources[0]]} if at_export else {}))
    target = tmp_path / 'restored'
    result = restore(bundle, target, **({} if at_export else {'sources': [sources[0]]}))
    store = store_at(target)
    assert [s['id'] for s in store.sources()] == [sources[0]]
    assert result['searchable_sources'] == [sources[0]]
    assert store.active_index()['id'] != old
    for path in target.rglob('*'):
        assert sources[1] not in str(path)
        if path.suffix in {'.json', '.yaml'}:
            assert sources[1] not in path.read_text()
    assert sources[1].encode() not in store.path.read_bytes()


def test_compatible_merge_and_skip_existing(library, tmp_path, video_file):
    bundle = tmp_path / 'backup'
    export(library, bundle)
    target = tmp_path / 'target'
    _, target_ids, old = seed(target, video_file, names=('c',))
    result = restore(bundle, target)
    assert result['config_action'] == '保留目标配置'
    assert result['searchable_sources'] == sorted([*library[1], *target_ids])
    assert old != result['active_index']
    before = store_at(target).rows('SELECT * FROM source_curation')
    # 重复全部跳过后，不能重新带入原素材的历史成员。
    skipped = restore(bundle, target, skip_existing=True)
    assert skipped['skipped_sources'] == sorted(library[1])
    assert store_at(target).rows('SELECT * FROM source_curation') == before
    assert {r['source_id'] for r in search_results(target)} == set(library[1]) | set(target_ids)


@pytest.mark.parametrize('problem', ['space', 'duplicate', 'shared'])
def test_conflict_leaves_target_unchanged(library, tmp_path, video_file, problem):
    bundle = tmp_path / 'backup'
    if problem == 'shared':
        store_at(library[0].data_dir).execute('INSERT INTO person_entities VALUES(?,?,?)',
                                            ('shared', 'actor', dumps({'name': '甲'})))
    export(library, bundle)
    target = tmp_path / 'target'
    seed(target, video_file, names=('a',) if problem == 'duplicate' else ('c',),
         space='other' if problem == 'space' else 'test')
    if problem == 'shared':
        store_at(target).execute('INSERT INTO person_entities VALUES(?,?,?)',
                                 ('shared', 'actor', dumps({'name': '乙'})))
    before = store_at(target).rows('SELECT * FROM sources')
    active = store_at(target).active_index()
    with pytest.raises(ValueError, match={'space': '不兼容', 'duplicate': '素材已存在', 'shared': '内容冲突'}[problem]):
        restore(bundle, target)
    assert store_at(target).rows('SELECT * FROM sources') == before
    assert store_at(target).active_index() == active
    assert not journal_path(target).exists()


@pytest.mark.parametrize('include', [False, True])
def test_secret_redaction_including_snapshot_pages(library, tmp_path, monkeypatch, include):
    config, sources, _, _ = library
    secret = 'saved-secret-unique-12345'
    store = store_at(config.data_dir)
    store.execute('INSERT INTO model_profiles(role,name,provider,config,api_key) VALUES(?,?,?,?,?)',
                  ('embedding', '配置', 'mindex', '{}', secret))
    store.execute('INSERT INTO source_tasks(id,source_id,work,status,config,options) VALUES(?,?,?,?,?,?)',
                  ('task', sources[0], 'analysis', 'running', dumps({'api_key': secret}), '{}'))
    monkeypatch.setenv('MINDEX_MODEL_API_KEY', 'environment-secret-must-never-export')
    # 旧数据库备份也不得绕过脱敏。
    from mindex.backup_db import snapshot
    snapshot(store.path, config.data_dir / 'old.sqlite3')
    history = sqlite3.connect(config.data_dir / 'old.sqlite3')
    history.execute('PRAGMA journal_mode=WAL')
    history.execute('DROP TABLE person_features')  # 模拟旧 schema 的历史备份
    history.commit()
    assert (config.data_dir / 'old.sqlite3-wal').exists()
    bundle = tmp_path / 'backup'
    try:
        export(library, bundle, include_secrets=include)
    finally:
        history.close()
    assert not list(bundle.rglob('*-wal'))
    assert not list(bundle.rglob('*-shm'))
    for path in bundle.rglob('*'):
        if path.is_file():
            assert b'environment-secret-must-never-export' not in path.read_bytes()
            if not include:
                assert secret.encode() not in path.read_bytes(), path
    target = tmp_path / 'target'
    restore(bundle, target)
    assert store_at(target).one('SELECT api_key FROM model_profiles')['api_key'] == (secret if include else None)
    task = store_at(target).one('SELECT status FROM source_tasks')
    assert task['status'] == 'interrupted'


def test_dry_run_and_cli_noninteractive(library, tmp_path):
    bundle = tmp_path / 'backup'
    export(library, bundle, dry_run=True)
    assert not bundle.exists()
    export(library, bundle)
    target = tmp_path / 'target'
    restore(bundle, target, dry_run=True)
    assert not target.exists()
    runner = CliRunner()
    result = runner.invoke(app, ['--config', str(library[3]), 'backup', 'export', str(tmp_path / 'other'), '--json'])
    assert result.exit_code == 1, result.output
    assert '非交互环境' in json.loads(result.output)['error']
    result = runner.invoke(app, ['--config', str(tmp_path / 'absent.yaml'), 'backup', 'inspect', str(bundle), '--json'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['version'] == 1


@pytest.mark.parametrize('attack', ['corrupt', 'traversal', 'symlink', 'missing_vectors'])
def test_invalid_bundle_fails_without_target(library, tmp_path, attack):
    bundle = tmp_path / 'backup'
    export(library, bundle)
    path = bundle / 'manifest.json'
    manifest = json.loads(path.read_text())
    if attack == 'corrupt':
        (bundle / 'config/base.yaml').write_text('corrupt')
    elif attack == 'traversal':
        manifest['files'][0]['path'] = '../../outside'
        path.write_text(dumps(manifest))
    elif attack == 'symlink':
        (bundle / 'escape').symlink_to(tmp_path)
    else:
        (bundle / manifest['collections'][0]['batches'][0]).unlink()
    with pytest.raises(ValueError):
        restore(bundle, tmp_path / 'target')
    assert not (tmp_path / 'target').exists()


def test_lock_conflict_and_export_inside_data(library, tmp_path):
    with SharedFileLock(library[0].data_dir / 'pipeline.lock'), pytest.raises(ValueError, match='锁被占用'):
        export(library, tmp_path / 'backup')
    with pytest.raises(ValueError, match='内部'):
        export(library, library[0].data_dir / 'backup')


def test_switch_failure_rolls_back_data_and_config(library, tmp_path, video_file, monkeypatch):
    bundle = tmp_path / 'backup'
    export(library, bundle)
    target = tmp_path / 'target'
    seed(target, video_file, names=('c',))
    original = store_at(target).active_index()
    original_rename = Path.rename
    def fail_stage(self, destination):
        if self.name.startswith('.target.migration-stage-'):
            raise OSError('模拟切换中断')
        return original_rename(self, destination)
    monkeypatch.setattr(Path, 'rename', fail_stage)
    with pytest.raises(OSError, match='模拟切换中断'):
        restore(bundle, target)
    assert store_at(target).active_index() == original
    assert not journal_path(target).exists()
    assert recover_import(target, config_path=tmp_path / 'target.yaml')['recovered'] is False


def test_missing_media_refused(library, tmp_path):
    (library[0].data_dir / store_at(library[0].data_dir).sources()[0]['path']).unlink()
    with pytest.raises(ValueError, match='必需媒体'):
        export(library, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


def test_profile_voice_audit_and_credential_reference_remapping(library, tmp_path, video_file):
    config, sources, _, _ = library
    origin = store_at(config.data_dir)
    origin.execute('INSERT INTO model_profiles(id,role,name,provider,config,api_key) VALUES(?,?,?,?,?,?)',
                   (1, 'embedding', '默认', 'custom', dumps({'model': 'origin', 'api_key_env': 'CUSTOM_KEY'}), 'key-one'))
    origin.execute('INSERT INTO settings VALUES(?,?)', ('active.profile.embedding', '1'))
    origin.execute('INSERT INTO voices(id,name,kind,provider,voice_id) VALUES(?,?,?,?,?)',
                   (1, '默认', 'custom', 'test', 'voice-origin'))
    origin.execute('INSERT INTO runs(id,stage,source_id,status) VALUES(1,?,?,?)', ('test', sources[0], 'complete'))
    snapshot = {'_credential_profiles': {'embedding': '1', 'asr': {'value': '1'}},
                'profile_id': 1, 'voice_pk': 1, 'run_id': 1}
    origin.execute('INSERT INTO source_tasks(id,source_id,work,status,config,options) VALUES(?,?,?,?,?,?)',
                   ('task', sources[0], 'analysis', 'queued', dumps(snapshot), '{}'))
    bundle = tmp_path / 'backup'
    result = export(library, bundle, include_secrets=True)
    assert 'CUSTOM_KEY' in result['required_environment']
    target = tmp_path / 'target'
    seed(target, video_file, names=('c',))
    dst = store_at(target)
    dst.execute('INSERT INTO model_profiles(id,role,name,provider,config,api_key) VALUES(?,?,?,?,?,?)',
                (1, 'embedding', '默认', 'custom', dumps({'model': 'target'}), 'key-two'))
    dst.execute('INSERT INTO voices(id,name,kind,provider,voice_id) VALUES(?,?,?,?,?)',
                (1, '默认', 'custom', 'test', 'voice-target'))
    dst.execute('INSERT INTO runs(id,stage,status) VALUES(1,?,?)', ('test', 'complete'))
    imported = restore(bundle, target, restore_config=True)
    maps = imported['integer_id_maps']
    assert maps['model_profiles'][1] != 1
    assert maps['voices'][1] != 1
    assert maps['runs'][1] != 1
    actual = json.loads(dst.one("SELECT config FROM source_tasks WHERE id='task'")['config'])
    assert actual['_credential_profiles']['embedding'] == str(maps['model_profiles'][1])
    assert actual['_credential_profiles']['asr']['value'] == str(maps['model_profiles'][1])
    assert actual['run_id'] == maps['runs'][1]
    assert actual['voice_pk'] == maps['voices'][1]
    assert dst.one("SELECT value FROM settings WHERE key='active.profile.embedding'")['value'] == str(
        maps['model_profiles'][1])
    assert dst.one('SELECT api_key FROM model_profiles WHERE id=1')['api_key'] == 'key-two'


def publish_person(root, evidence_id):
    from mindex.persons import Persons

    store = store_at(root)
    source = store.sources()[0]
    segment = store.segments(source['id'])[0]
    store.execute('INSERT INTO person_entities VALUES(?,?,?)', ('actor', 'actor', dumps({'name': '演员'})))
    store.execute('INSERT INTO person_evidence(id,source_id,shot_id,actor_id,start,end,status,data) '
                  'VALUES(?,?,?,?,?,?,?,?)',
                  (evidence_id, source['id'], segment['id'], 'actor', 0, 1, 'accepted', '{}'))
    Persons(store).publish()
    return store, source, segment


def test_person_union_uses_published_evidence_only(library, tmp_path, video_file):
    store, source, segment = publish_person(library[0].data_dir, 'published-origin')
    store.execute('INSERT INTO person_evidence(id,source_id,shot_id,actor_id,start,end,status,data) '
                  'VALUES(?,?,?,?,?,?,?,?)',
                  ('unpublished-change', source['id'], segment['id'], 'actor', 2, 3, 'accepted', '{}'))
    bundle = tmp_path / 'backup'
    export(library, bundle)
    target = tmp_path / 'target'
    seed(target, video_file, names=('c',))
    publish_person(target, 'published-target')
    restore(bundle, target)
    dst = store_at(target)
    active = dst.active_index()
    published = json.loads(dst.one('SELECT data FROM person_versions WHERE id=?', (active['person_version'],))['data'])
    assert {e['id'] for e in published['evidence']} == {'published-origin', 'published-target'}
    assert dst.one("SELECT id FROM person_evidence WHERE id='unpublished-change'")


def test_partial_studio_dependencies_paths_and_releases(library, tmp_path):
    config, sources, old, _ = library
    publish_person(config.data_dir, 'evidence')
    store = store_at(config.data_dir)
    old_release = store.active_index()['search_release_id']
    for suffix, scope in [('aaaaaa', [sources[0]]), ('bbbbbb', sources), ('cccccc', None)]:
        directory = config.data_dir / 'studio' / f'20261020-120000-{suffix}'
        directory.mkdir(parents=True)
        (directory / 'job.json').write_text(dumps({
            'id': directory.name, 'source_ids': scope, 'config_snapshot': {'index_id': old, 'release_id': old_release},
            'data_dir': str(config.data_dir), 'execution': {'status': 'running'},
        }))
        (directory / 'checkpoint.json').write_text(dumps({'source_id': sources[0],
                                                        'path': str(config.data_dir / 'plans' / f'{sources[0]}.json')}))
    bundle = tmp_path / 'backup'
    result = export(library, bundle, sources=[sources[0]])
    assert len(result['excluded_jobs']) == 2
    target = tmp_path / 'target'
    restore(bundle, target)
    state = json.loads((target / 'studio/20261020-120000-aaaaaa/job.json').read_text())
    assert state['config_snapshot']['index_id'] != old
    assert state['config_snapshot']['release_id'] != old_release
    assert state['execution']['status'] == 'interrupted'
    assert state['data_dir'] == str(target)
    checkpoint = json.loads((target / 'studio/20261020-120000-aaaaaa/checkpoint.json').read_text())
    assert checkpoint['path'].startswith(str(target))
    assert not (target / 'studio/20261020-120000-bbbbbb').exists()


def test_base_config_assets_and_runtime_overrides_are_separate(library, tmp_path):
    config, _, _, config_path = library
    manifest = tmp_path / 'deployment.json'
    manifest.write_text(dumps({'file_identity': {'content_id': 'test'}, 'files': {}}))
    font = tmp_path / 'font.ttf'
    font.write_bytes(b'test-font')
    base = {'data_dir': str(config.data_dir), 'embedding': {'dimensions': 8,
                                                          'deployment_manifest': 'deployment.json'},
            'studio': {'subtitle': {'font_file': 'font.ttf'}}}
    config_path.write_text(yaml.safe_dump(base))
    from mindex.config import load_config
    config = load_config(config_path)
    store_at(config.data_dir).execute('INSERT INTO settings VALUES(?,?)', ('override.enforce_gpu_lock', 'true'))
    bundle = tmp_path / 'backup'
    export_backup(config, bundle, all_sources=True, config_path=config_path)
    target = tmp_path / 'new' / 'data'
    restore(bundle, target)
    restored = yaml.safe_load((target.parent / 'data.yaml').read_text())
    assert Path(restored['embedding']['deployment_manifest']).is_file()
    assert Path(restored['studio']['subtitle']['font_file']).read_bytes() == b'test-font'
    assert 'enforce_gpu_lock' not in restored
    assert store_at(target).one("SELECT value FROM settings WHERE key='override.enforce_gpu_lock'")['value'] == 'true'


def test_crash_after_data_switch_recovers_in_another_process(library, tmp_path, video_file):
    bundle, target = tmp_path / 'backup', tmp_path / 'target'
    export(library, bundle)
    _, original_ids, _ = seed(target, video_file, names=('c',))
    configuration = tmp_path / 'target.yaml'
    configuration.write_text('data_dir: target\n')
    script = '''
import os, sys
from pathlib import Path
from mindex.backup import import_backup
from mindex.config import Config
rename = Path.rename
def crash(self, destination):
    result = rename(self, destination)
    if self.name.startswith('.target.migration-stage-'):
        os._exit(23)
    return result
Path.rename = crash
import_backup(Config(data_dir=Path(sys.argv[2])), Path(sys.argv[1]), all_sources=True,
              config_path=Path(sys.argv[3]), restore_config=True)
'''
    result = subprocess.run([sys.executable, '-c', script, str(bundle), str(target), str(configuration)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 23, result.stdout + result.stderr
    assert journal_path(target).exists()
    assert set(store_at(target).source_ids()) == set(library[1]) | set(original_ids)
    result = subprocess.run([sys.executable, '-m', 'mindex.cli', '--config', str(configuration),
                             '--data-dir', str(target), 'backup', 'recover', '--json'],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['recovered'] is True
    assert store_at(target).source_ids() == set(original_ids)
    assert configuration.read_text() == 'data_dir: target\n'
    assert not journal_path(target).exists()
    assert not list(tmp_path.glob('.target.migration-*/'))


def test_interactive_search_and_multiselect(monkeypatch):
    from mindex.backup_cli import choose

    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    answers = iter(['甲', '1', '乙', '1', '/完成'])
    monkeypatch.setattr('typer.prompt', lambda *a, **k: next(answers))
    assert choose([{'id': 'a', 'name': '甲'}, {'id': 'b', 'name': '乙'}], False, None) == ['a', 'b']


@pytest.mark.parametrize('custom_name', ['最新素材名', None, ''])
def test_backup_list_uses_live_curated_name(tmp_path, custom_name):
    root = tmp_path / 'data'
    store = Store(root)
    store.execute('INSERT INTO sources(id,name,path,media,layout_key) VALUES(?,?,?,?,?)',
                  ('source', '原始素材名.mp4', 'sources/source/video.mp4', '{}', 'layout'))
    store.execute('INSERT INTO source_curation(id,display_name) VALUES(?,?)', ('source', custom_name))
    expected = custom_name or '原始素材名.mp4'
    assert source_list(root)[0]['name'] == expected
    runner = CliRunner()
    args = ['--config', str(tmp_path / 'config.yaml'), '--data-dir', str(root), 'backup', 'list']
    result = runner.invoke(app, [*args, '--json'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['sources'][0]['name'] == expected
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert expected in result.output
    if custom_name:
        assert '原始素材名.mp4' not in result.output
    # 再次改名应立即生效；源文件名和媒体路径仍保留原值。
    renamed = CurationPatch(display_name=' 再次改名 ').display_name
    store.execute('UPDATE source_curation SET display_name=? WHERE id=?', (renamed, 'source'))
    assert source_list(root)[0]['name'] == '再次改名'
    assert store.sources()[0]['name'] == '原始素材名.mp4'
    assert source_list(root)[0]['path'] == 'sources/source/video.mp4'


def test_backup_curated_names_roundtrip_and_legacy_manifest(library, tmp_path):
    config, sources, _, _ = library
    store_at(config.data_dir).execute('INSERT INTO source_curation(id,display_name) VALUES(?,?)',
                                     (sources[0], '改名后的素材'))
    bundle = tmp_path / 'backup'
    result = export(library, bundle)
    assert {s['id']: s['name'] for s in result['sources']}[sources[0]] == '改名后的素材'
    manifest = inspect_bundle(bundle)
    assert {s['id']: s['name'] for s in manifest['sources']}[sources[0]] == '改名后的素材'
    # 升级前导出的 v1 清单仍能检查和导入，并保留 SQLite 中的最新人工命名。
    manifest['sources'] = source_list(bundle / 'data', original_names=True)
    (bundle / 'manifest.json').write_text(dumps(manifest))
    inspected = inspect_bundle(bundle)
    assert {s['id']: s['name'] for s in inspected['sources']}[sources[0]] == '改名后的素材'
    target = tmp_path / 'restored'
    restored = restore(bundle, target)
    assert {s['id']: s['name'] for s in restored['sources']}[sources[0]] == '改名后的素材'
    assert {s['id']: s['name'] for s in source_list(target)}[sources[0]] == '改名后的素材'


def test_plain_bracket_text_survives_secret_scan_and_merge(tmp_path):
    root = tmp_path / 'origin'
    store = Store(root)
    store.execute('INSERT INTO sources(id,name,path,media,layout_key) VALUES(?,?,?,?,?)',
                  ('source', '[高清] 原片.mp4', 'sources/source/video.mp4', '{}', 'layout'))
    store.execute('INSERT INTO source_curation(id,display_name,notes) VALUES(?,?,?)',
                  ('source', '[新版] 素材名', '{这是普通备注，不是 JSON}'))
    store.execute('INSERT INTO runs(stage,source_id,status,message) VALUES(?,?,?,?)',
                  ('test', 'source', 'complete', '[INFO] 普通任务日志'))
    store.execute('INSERT INTO source_tasks(id,source_id,work,status,config,options,error) VALUES(?,?,?,?,?,?,?)',
                  ('task', 'source', 'analysis', 'failed', '{}', '{}', '[Errno 2] 找不到文件'))
    store.execute('INSERT INTO model_profiles(role,name,provider,config,api_key) VALUES(?,?,?,?,?)',
                  ('embedding', '档案', 'custom', ' \n {"api_key":"nested-saved-key"}', 'saved-key'))
    assert set(_saved_secrets(root)) == {'saved-key', 'nested-saved-key'}
    target = tmp_path / 'target'
    Store(target)
    merge_database(target, root, restore_config=False)
    restored = store_at(target)
    assert restored.sources()[0]['name'] == '[高清] 原片.mp4'
    assert restored.one('SELECT notes FROM source_curation')['notes'] == '{这是普通备注，不是 JSON}'
    assert restored.one('SELECT message FROM runs')['message'] == '[INFO] 普通任务日志'
    assert restored.one('SELECT error FROM source_tasks')['error'] == '[Errno 2] 找不到文件'


def test_selected_export_preserves_plain_bracket_text(library, tmp_path):
    config, sources, _, _ = library
    store = store_at(config.data_dir)
    store.execute('INSERT INTO runs(stage,source_id,status,message) VALUES(?,?,?,?)',
                  ('test', sources[0], 'complete', '[INFO] 普通任务日志'))
    store.execute('INSERT INTO source_tasks(id,source_id,work,status,config,options,error) VALUES(?,?,?,?,?,?,?)',
                  ('task', sources[0], 'analysis', 'failed', '{}', '{}', '[Errno 2] 找不到文件'))
    description = '这是一段很长的镜头分析描述。' * 100 + '0.7秒处有人影入画，画面呈红色/蓝色。'
    segment = store.segments(sources[0])[0]
    store.execute('INSERT INTO annotations(segment_id,profile,data) VALUES(?,?,?)',
                  (segment['id'], 'test', dumps({'annotation': {'description': description}})))
    bundle = tmp_path / 'backup'
    export(library, bundle, sources=[sources[0]], dry_run=True)
    assert not bundle.exists()
    export(library, bundle, sources=[sources[0]])
    target = tmp_path / 'restored'
    restore(bundle, target)
    assert store_at(target).one('SELECT message FROM runs')['message'] == '[INFO] 普通任务日志'
    assert store_at(target).one('SELECT error FROM source_tasks')['error'] == '[Errno 2] 找不到文件'
    assert json.loads(store_at(target).one('SELECT data FROM annotations')['data'])[
        'annotation']['description'] == description


def test_partial_backup_excludes_unrelated_cast_and_reference_files(library, tmp_path):
    from mindex.backup_db import filter_database

    root = library[0].data_dir
    store = store_at(root)
    for name in ('wanted-work', 'other-work'):
        store.execute('INSERT INTO person_entities VALUES(?,?,?)',
                      (name, 'work', dumps({'kind': 'work', 'name': name})))
    for source, work in zip(library[1], ('wanted-work', 'other-work')):
        store.execute('INSERT INTO person_sources VALUES(?,?)', (source, work))
    store.execute('INSERT INTO person_entities VALUES(?,?,?)',
                  ('other-actor', 'actor', dumps({'kind': 'actor', 'name': '其他演员'})))
    store.execute('INSERT INTO person_entities VALUES(?,?,?)',
                  ('other-role', 'role', dumps({'kind': 'role', 'name': '其他角色',
                                              'work_ids': ['other-work'], 'actor_ids': ['other-actor']})))
    image = root / 'persons/references/unrelated.jpg'
    image.parent.mkdir(parents=True)
    Image.new('RGB', (20, 20)).save(image)
    store.execute('INSERT INTO person_references(id,actor_id,path,digest,data,role_id) VALUES(?,?,?,?,?,?)',
                  ('reference', 'other-actor', str(image.relative_to(root)), 'digest', '{}', 'other-role'))
    store.execute('INSERT INTO person_imports(id,data,status) VALUES(?,?,?)',
                  ('import', dumps({'work_id': 'other-work', 'images': [{'path': str(image.relative_to(root))}]}), 'draft'))
    entities = [{'id': r['id'], **json.loads(r['data'])} for r in store.rows('SELECT * FROM person_entities')]
    history = {'format': 1, 'entities': entities, 'evidence': [], 'coverage': []}
    person = fingerprint(history)
    store.execute('INSERT INTO person_versions(id,data) VALUES(?,?)', (person, dumps(history)))
    release = fingerprint([library[2], person])
    store.execute('INSERT INTO search_releases(id,content_id,person_id) VALUES(?,?,?)', (release, library[2], person))
    store.activate_release(release)
    bundle = tmp_path / 'partial-cast'
    export(library, bundle, sources=[library[1][0]])
    kept = store_at(bundle / 'data')
    assert [r['id'] for r in kept.rows('SELECT id FROM person_entities')] == ['wanted-work']
    assert not kept.rows('SELECT * FROM person_references')
    assert not kept.rows('SELECT * FROM person_imports')
    version = json.loads(kept.rows('SELECT data FROM person_versions')[0]['data'])
    assert [item['id'] for item in version['entities']] == ['wanted-work']
    assert not (bundle / 'data/persons/references/unrelated.jpg').exists()
    # 源数据库始终保持完整，重复裁剪也不会把全局演员表带回来。
    assert len(store.rows('SELECT * FROM person_entities')) == 4
    filter_database(kept.path, {library[1][0]})
    assert len(kept.rows('SELECT * FROM person_entities')) == 1
    full = tmp_path / 'full-cast'
    export(library, full)
    target = tmp_path / 'cast-target'
    restore(full, target, sources=[library[1][0]])
    assert [r['id'] for r in store_at(target).rows('SELECT id FROM person_entities')] == ['wanted-work']
    assert not (target / 'persons/references/unrelated.jpg').exists()


def test_partial_backup_keeps_associated_cast_without_person_evidence(library, tmp_path):
    root = library[0].data_dir
    store = store_at(root)
    store.execute('INSERT INTO person_sources VALUES(?,?)', (library[1][0], 'work'))
    for identifier, kind, extra in [('work', 'work', {}), ('actor', 'actor', {}),
                                    ('role', 'role', {'work_ids': ['work'], 'actor_ids': ['actor']})]:
        store.execute('INSERT INTO person_entities VALUES(?,?,?)',
                      (identifier, kind, dumps({'kind': kind, 'name': identifier, **extra})))
    bundle = tmp_path / 'associated-cast'
    export(library, bundle, sources=[library[1][0]])
    assert {r['id'] for r in store_at(bundle / 'data').rows('SELECT id FROM person_entities')} == {'work', 'actor', 'role'}


def test_partial_roundtrip_preserves_compatibility_evidence_bytes(library, tmp_path, video_file):
    import hashlib

    from mindex.compatibility import load_baseline

    config, sources, index = library[:3]
    root = config.data_dir
    space = {'schema': 2, 'dimensions': 8, 'video': {'version': 1}, 'numerical_implementation': 'fixture'}
    spec = json.loads(store_at(root).one('SELECT spec FROM index_versions WHERE id=?', (index,))['spec'])
    spec['space'] = space
    store_at(root).execute('UPDATE index_versions SET spec=? WHERE id=?', (dumps(spec), index))
    directory = root / 'capabilities/v2' / fingerprint(space)
    directory.mkdir(parents=True)
    samples = []
    for number in range(6):
        path = directory / 'samples' / f'{number}.mp4'
        path.parent.mkdir(exist_ok=True)
        shutil.copy2(video_file, path)
        text = f'sample {number}'
        samples.append({'text': text, 'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                        'path': str(path.relative_to(root)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    deployment = {'server_path': str(root / 'models/weights.gguf')}
    baseline = {'schema': 2, 'space': space, 'space_id': fingerprint(space), 'deployment': deployment,
                'deployment_id': fingerprint(deployment), 'preprocessing': space['video'], 'samples': samples,
                'inputs_id': fingerprint(samples), 'raw_text_vectors': [[1., 0., 0., 0., 0., 0., 0., 0.]] * 6,
                'raw_video_vectors': [[1., 0., 0., 0., 0., 0., 0., 0.]] * 6}
    gate = directory / 'deployments' / (fingerprint(deployment) + '.json')
    gate.parent.mkdir()
    gate_bytes = dumps({'schema': 2, 'passed': False, 'deployment': deployment}).encode()
    gate.write_bytes(gate_bytes)
    gate.with_suffix('.sha256').write_text(hashlib.sha256(gate_bytes).hexdigest() + '\n')
    original = dumps(baseline).encode()
    (directory / 'baseline.json').write_bytes(original)
    (directory / 'baseline.sha256').write_text(hashlib.sha256(original).hexdigest() + '\n')
    load_baseline(config, space)
    bundle = tmp_path / 'compatibility-backup'
    export(library, bundle, sources=[sources[0]])
    target = tmp_path / 'different-root'
    restore(bundle, target)
    restored = load_baseline(Config(data_dir=target), space)
    assert restored == baseline
    assert (target / gate.relative_to(root)).read_bytes() == gate_bytes
    assert (target / gate.with_suffix('.sha256').relative_to(root)).read_bytes() == gate.with_suffix('.sha256').read_bytes()
    assert (target / directory.relative_to(root) / 'baseline.json').read_bytes() == original
    (directory / 'samples/0.mp4').unlink()
    with pytest.raises(ModelError, match='无法核验本地文件'):
        export(library, tmp_path / 'missing-proof', sources=[sources[0]])


@pytest.mark.parametrize('mode', ['auto', 'enriched'])
def test_merge_ignores_analysis_and_other_model_contracts(tmp_path, mode):
    from types import SimpleNamespace

    from mindex.backup import check_compatible

    def unexpected_snapshot_read(_):
        raise AssertionError('合并兼容性不能读取分析/ASR 来源配置')

    store = SimpleNamespace(root=tmp_path, snapshots=unexpected_snapshot_read)
    spec = {'space': {'model': 'WeMM', 'weights_sha256': 'same'}, 'format': 2,
            'mode': mode, 'preprocessing': {'fps': 4}}
    first = {'id': 'target', 'spec': {**spec, 'analysis_key': 'old-analysis-profile'}}
    second = {'id': 'incoming', 'spec': {**spec, 'analysis_key': 'another-analysis-profile'}}
    check_compatible(first, second, store, store)


@pytest.mark.parametrize('field,value', [('space', {'model': 'different-WeMM'}), ('format', 1),
                                        ('preprocessing', {'fps': 8}), ('mode', 'visual')])
def test_merge_still_rejects_embedding_and_index_contract_changes(field, value):
    from mindex.backup import check_compatible

    spec = {'space': {'model': 'WeMM'}, 'format': 2, 'preprocessing': {'fps': 4}, 'mode': 'auto'}
    with pytest.raises(ValueError, match=f'活动索引不兼容：{field}'):
        check_compatible({'spec': spec}, {'spec': {**spec, field: value}}, None, None)


def test_finder_metadata_is_ignored_without_weakening_backup_validation(library, tmp_path):
    config = library[0]
    (config.data_dir / '.DS_Store').write_bytes(b'Finder metadata')
    bundle = tmp_path / 'finder-backup'
    exported = export(library, bundle)
    assert not (bundle / 'data/.DS_Store').exists()
    assert any(item['path'] == '.DS_Store' for item in exported['excluded'])
    for relative in ('.DS_Store', 'data/.DS_Store', 'vectors/.DS_Store'):
        (bundle / relative).write_bytes(b'Added by Finder after export')
    assert inspect_bundle(bundle)['sources'] == exported['sources']
    extra = bundle / 'data/unregistered.txt'
    extra.write_text('Unregistered business data must still be rejected')
    with pytest.raises(ValueError, match='备份含未登记文件：data/unregistered.txt'):
        inspect_bundle(bundle)
    extra.unlink()
    metadata = bundle / '.DS_Store'
    metadata.unlink()
    metadata.symlink_to(config.data_dir / '.DS_Store')
    with pytest.raises(ValueError, match='符号链接'):
        inspect_bundle(bundle)
    metadata.unlink()
    with (bundle / 'data/metadata.sqlite3').open('ab') as stream:
        stream.write(b'corrupted')
    with pytest.raises(ValueError, match='校验失败'):
        inspect_bundle(bundle)


@pytest.mark.parametrize('text', ['123456789012345678e12345', '1e3', '1.00', 'null', 'true'])
@pytest.mark.parametrize('include_secrets', [False, True])
def test_rewrite_preserves_sql_scalar_text_and_relocates_json(tmp_path, text, include_secrets):
    from mindex.backup_db import rewrite_database

    store = Store(tmp_path / 'data')
    store.execute('INSERT INTO sources(id,name,path,media,layout_key) VALUES(?,?,?,?,?)',
                  (text, text, '/old/video.mp4', dumps({'source_id': text, 'path': '/old/video.mp4',
                                                    'api_key': 'saved-secret'}), 'layout'))
    store.execute('INSERT INTO settings VALUES(?,?)', ('literal.identifier', text))
    store.execute('INSERT INTO settings VALUES(?,?)', ('override.path', dumps('/old/font.ttf')))
    rewrite_database(store.path, lambda value: value.replace('/old/', '/new/'), secrets=include_secrets)
    source = store.sources()[0]
    assert source['id'] == text
    assert source['name'] == text
    assert source['path'] == '/new/video.mp4'
    media = json.loads(store.one('SELECT media FROM sources')['media'])
    assert media['source_id'] == text
    assert media['path'] == '/new/video.mp4'
    assert media['api_key'] == ('saved-secret' if include_secrets else '')
    assert store.one("SELECT value FROM settings WHERE key='literal.identifier'")['value'] == text
    assert json.loads(store.one("SELECT value FROM settings WHERE key='override.path'")['value']) == '/new/font.ttf'


def test_shared_scalar_text_is_not_compared_as_json_numbers():
    from mindex.backup_db import same_value

    assert not same_value('1e3', '1000')
    assert not same_value('1e999', '2e999')
    assert not same_value('1.00', '1.0')
    assert same_value('{"a":1,"b":2}', '{ "b": 2, "a": 1 }')


@pytest.mark.parametrize('old', ['123456789012345678901234', '123456789012345678e12345'])
def test_rewrite_remaps_numeric_looking_text_ids_and_setting_references(tmp_path, old):
    from mindex.backup_db import rewrite_database

    store = Store(tmp_path / 'data')
    new = 'abcdef0123456789abcdef01'
    store.execute('INSERT INTO sources(id,name,path,media,layout_key) VALUES(?,?,?,?,?)',
                  (old, '素材', 'video.mp4', dumps({'source_id': old}), 'layout'))
    store.execute('INSERT INTO settings VALUES(?,?)', ('raw.reference', old))
    store.execute('INSERT INTO settings VALUES(?,?)', ('json.reference', dumps(old)))
    rewrite_database(store.path, lambda value: {old: new}.get(value, value), secrets=True)
    assert store.sources()[0]['id'] == new
    assert json.loads(store.one('SELECT media FROM sources')['media'])['source_id'] == new
    assert store.one("SELECT value FROM settings WHERE key='raw.reference'")['value'] == new
    assert json.loads(store.one("SELECT value FROM settings WHERE key='json.reference'")['value']) == new
