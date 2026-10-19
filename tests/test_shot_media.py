import json

import pytest

from mindex.media import MediaError, command, probe
from mindex.shot_media import ShotMedia
from mindex.storage import Store


@pytest.fixture
def library(tmp_path):
    store = Store(tmp_path)
    store.execute("INSERT INTO sources(id,name,path,media,layout_key) VALUES(?,?,?,?,?)",
                  ('s', 'source', 'missing-original.mp4', json.dumps({'duration': 2}), 'v'))
    for i, color in enumerate(('red', 'blue')):
        path = tmp_path / f'{i}.mp4'
        command(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                 f'color={color}:s=64x64:r=25:d=1', '-c:v', 'libx264', str(path)])
        store.execute('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (str(i), 's', 'shot', i, i + 1, path.name, '', 'v', None, 'test'))
    return store


def test_cross_shot_without_original(library, tmp_path):
    service = ShotMedia(library)
    refs = service.references('s', .5, 1.5)
    assert [r.start for r in refs] == [.5, 0]
    assert refs[1].logical(.2) == pytest.approx(.7)
    assert refs[1].absolute(.2) == pytest.approx(1.2)
    result = service.materialize('s', .5, 1.5)
    assert probe(result)['duration'] == pytest.approx(1, abs=.05)
    service.audio('s', tmp_path / 'audio.wav', .5, 1)
    service.frame('s', 1, tmp_path / 'frame.png')
    service.frame('s', 2, tmp_path / 'last.png')
    service.register('old/moment.mp4', 's', .5, 1.5)
    assert library.resolve_media('old/moment.mp4') == result
    assert service.materialize('s', 0, 1) == library.root / '0.mp4'


def test_missing_and_gapped_shots_fail(library):
    library.execute("UPDATE segments SET start=1.1 WHERE id='1'")
    with pytest.raises(MediaError, match='gap'):
        ShotMedia(library).references('s')
    library.execute("UPDATE segments SET start=1 WHERE id='1'")
    (library.root / '1.mp4').unlink()
    with pytest.raises(FileNotFoundError):
        ShotMedia(library).materialize('s')


def test_migration_preview_backup_and_recovery(library):
    from mindex.clips import materialize_all, migrate
    from mindex.config import Config
    from mindex.subtitle_archive import archive_path

    # Explicitly archive a source without reading a nonexistent original.
    manifest = archive_path(library, 's')
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({'version': 1, 'tracks': [], 'utterances': {}, 'files': {}}))
    library.execute('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)',
                    ('moment', 's', 'moment', .5, 1.5, 'clips/old.mp4', '', 'v', None, 'test'))
    config = Config(data_dir=library.root)
    before = library.path.read_bytes()
    report = migrate(config)
    assert not report['apply']
    assert before == library.path.read_bytes()
    assert not (library.root / 'backups').exists()
    report = migrate(config, apply=True, verify=True)
    assert report['sources'][0]['status'] == 'verified'
    assert report['protected_before'] == report['protected_after']
    assert __import__('pathlib').Path(report['backup']).is_file()
    assert materialize_all(config)['restored'] == ['clips/old.mp4']
    assert materialize_all(config)['restored'] == []


def test_audio_mix_frames_and_cache_lock(library, tmp_path):
    from filelock import FileLock

    # Add an actual tone to only the second shot.
    target = library.root / 'tone.mp4'
    command(['ffmpeg', '-v', 'error', '-y', '-i', str(library.root / '1.mp4'),
             '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:v', 'copy',
             '-c:a', 'aac', '-shortest', str(target)])
    library.execute("UPDATE segments SET path='tone.mp4' WHERE id='1'")
    service = ShotMedia(library)
    result = service.materialize('s', .5, 1.5)
    assert probe(result)['audio']
    frames = service.frames('s', .5, 1.5, 2)
    assert frames[0][0] >= .5 and frames[-1][0] < 1.5
    assert all(image.startswith('data:image/jpeg;base64,') for _, image in frames)
    with FileLock(str(result) + '.lock'):
        assert service.prune_cache(max_age=0, max_bytes=0) == []
        assert result.exists()
    assert service.prune_cache(max_age=0, max_bytes=0) == [result.name]


def test_business_modules_do_not_resolve_source_path():
    from pathlib import Path

    modules = ['api', 'admin', 'person_api', 'person_processing', 'dialogue', 'segment_details',
               'studio_workspace', 'studio_window', 'studio_steps', 'studio_api']
    root = Path(__file__).resolve().parents[1] / 'src' / 'mindex'
    for name in modules:
        source = (root / f'{name}.py').read_text()
        assert 'resolve_media(source["path"])' not in source, name
        assert 'resolve_media(segment["source_path"])' not in source, name


def test_text_subtitle_archive_survives_source_removal(config, video_file, tmp_path):
    from mindex.pipeline import Pipeline
    from mindex.subtitle_archive import load_archive

    subtitle = tmp_path / 'captions.srt'
    subtitle.write_text('1\n00:00:00,200 --> 00:00:00,800\nhello\n')
    source = tmp_path / 'subtitled.mp4'
    command(['ffmpeg', '-v', 'error', '-y', '-i', str(video_file), '-i', str(subtitle),
             '-map', '0:v', '-map', '1:s', '-c:v', 'copy', '-c:s', 'mov_text', str(source)])
    pipeline = Pipeline(config)
    sid = pipeline.prepare(source)
    row = pipeline.store.one('SELECT * FROM sources WHERE id=?', (sid,))
    pipeline.store.resolve_media(row['path']).unlink()
    archive = load_archive(pipeline.store, sid)
    track = archive['tracks'][0]
    assert archive['utterances'][str(track['index'])][0]['text'] == 'hello'
    assert archive['files']
    pipeline.split(sid)
    assert not any((pipeline.store.root / s['path']).exists()
                   for s in pipeline.store.segments(sid, 'moment'))


def test_http_playlist_range_and_head_without_original(library):
    from fastapi.testclient import TestClient

    from mindex.api import create_app
    from mindex.config import Config

    library.execute('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)',
                    ('moment', 's', 'moment', .5, 1.5, 'clips/missing.mp4', '', 'v', None, 'test'))
    client = TestClient(create_app(Config(data_dir=library.root)))
    playlist = client.get('/api/admin/segments/moment/playlist')
    assert playlist.status_code == 200
    items = playlist.json()['items']
    assert len(items) == 2 and items[1]['offset'] == .5
    assert client.head(items[0]['url']).status_code == 200
    response = client.get('/api/admin/segments/moment/media', headers={'Range': 'bytes=0-31'})
    assert response.status_code == 206 and len(response.content) == 32
    assert client.head('/api/admin/segments/moment/media').status_code == 200
    assert client.get('/api/admin/sources/s/media').status_code == 200


def test_migration_verification_retains_all_existing_media(library):
    from mindex.clips import migrate
    from mindex.config import Config
    from mindex.subtitle_archive import archive_path

    manifest = archive_path(library, 's')
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({'version': 1, 'tracks': [], 'utterances': {}, 'files': {}}))
    legacy = library.root / 'legacy.mp4'
    legacy.write_bytes(b'old entity bytes must remain unchanged until user acceptance')
    library.execute('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)',
                    ('moment', 's', 'moment', 0, 1, legacy.name, '', 'v', None, 'test'))
    before = legacy.read_bytes()
    report = migrate(Config(data_dir=library.root), apply=True, verify=True)
    assert report['physical_deletion_enabled'] is False
    assert report['released_bytes'] == 0
    assert report['sources'][0]['files'][0]['status'] == 'ready-for-deletion'
    assert legacy.read_bytes() == before


def test_concurrent_generation_is_atomic(library):
    from concurrent.futures import ThreadPoolExecutor

    service = ShotMedia(library)
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _: service.materialize('s', .2, 1.8), range(4)))
    assert len(set(paths)) == 1
    assert probe(paths[0])['duration'] == pytest.approx(1.6, abs=.04)
    assert not list(paths[0].parent.glob('*.partial.mp4'))


def test_playlist_revision_changes_when_shot_is_replaced(library):
    from mindex.shot_media import file_response

    service = ShotMedia(library)
    old = service.playlist('s')['items'][0]['url']
    assert '?v=' in old
    # A repaired shot retains its ID and public route, but must bypass cached old bytes.
    library.execute("UPDATE segments SET path='1.mp4' WHERE id='0'")
    new = service.playlist('s')['items'][0]['url']
    assert old.split('?')[0] == new.split('?')[0]
    assert old != new
    response = file_response(library.root / '1.mp4')
    assert response.headers['cache-control'] == 'no-cache'
    assert 'etag' not in response.headers  # FileResponse supplies validators during streaming.


def test_prune_verified_entities_preserves_shots_and_recovers(library):
    from mindex.clips import materialize_all, migrate, protected_digest, prune
    from mindex.config import Config
    from mindex.subtitle_archive import archive_path

    config = Config(data_dir=library.root)
    archive = archive_path(library, 's')
    archive.parent.mkdir(parents=True)
    archive.write_text(json.dumps({'version': 1, 'tracks': [], 'utterances': {}, 'files': {}}))
    library.execute('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)',
                    ('moment', 's', 'moment', .5, 1.5, 'clips/old.mp4', '', 'v', None, 'test'))
    migration = migrate(config, apply=True, verify=True)
    materialize_all(config)
    # Unlinking a redundant hardlink must retain the shot and report zero released blocks.
    legacy = library.root / 'clips/old.mp4'
    legacy.unlink()
    legacy.hardlink_to(library.root / '1.mp4')
    before = protected_digest(library)
    preview = prune(config, migration['report'])
    assert preview['estimated_released_allocated_bytes'] == 0
    assert preview['files'][0]['status'] == 'ready'
    assert (library.root / 'clips/old.mp4').exists()
    # A changed dependency must block deletion before any file is removed.
    library.execute("UPDATE clip_migration_files SET dependency_digest='stale'")
    with pytest.raises(ValueError, match='Stale'):
        prune(config, migration['report'], apply=True)
    assert (library.root / 'clips/old.mp4').exists()
    library.execute('UPDATE clip_migration_files SET dependency_digest=?',
                    (preview['files'][0]['dependency_digest'],))
    pinned = library.root / 'pin.json'
    pinned.write_text(json.dumps({'path': 'clips/old.mp4', 'sha256': 'fixed'}))
    assert prune(config, migration['report'], apply=True)['deleted_files'] == 0
    pinned.unlink()
    result = prune(config, migration['report'], apply=True)
    assert result['deleted_files'] == 1
    assert result['released_allocated_bytes'] == 0
    assert not (library.root / 'clips/old.mp4').exists()
    assert all((library.root / f'{i}.mp4').exists() for i in (0, 1))
    assert protected_digest(library) == before
    assert prune(config, migration['report'], apply=True)['deleted_files'] == 0
    assert materialize_all(config)['restored'] == ['clips/old.mp4']


def test_inference_interval_cleanup_on_failure(config, video_file):
    import pytest

    from mindex.pipeline import Pipeline
    from mindex.shot_media import ShotMedia

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    media = ShotMedia(pipeline.store, cache_probes=False)
    with pytest.raises(RuntimeError, match='inference failed'), media.inference_video(sid, .5, 2.5) as path:
        assert path.is_file()
        raise RuntimeError('inference failed')
    assert not path.exists()


def test_inference_with_audio_shots(config, video_file, tmp_path):
    from mindex.media import command, probe
    from mindex.pipeline import Pipeline
    from mindex.shot_media import ShotMedia

    video = tmp_path / 'audio.mp4'
    command(['ffmpeg', '-v', 'error', '-y', '-i', str(video_file), '-f', 'lavfi',
             '-i', 'sine=frequency=440:duration=10', '-c:v', 'copy', '-c:a', 'aac', str(video)])
    pipeline = Pipeline(config)
    sid = pipeline.prepare(video)
    with ShotMedia(pipeline.store, cache_probes=False).inference_video(sid, .5, 2.5) as path:
        info = probe(path)
        assert abs(info['duration'] - 2) < .1
        assert not info['audio']
    assert not path.exists()
