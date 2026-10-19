import json
import shutil

import numpy as np
import pytest

from mindex.media import command, cut
from mindex.shot_media import ShotMedia
from mindex.shot_repair import adjusted, apply, audit, inspect_boundary
from mindex.storage import Store


def first_rgb(path):
    raw = command(['ffmpeg', '-v', 'error', '-i', str(path), '-frames:v', '1',
                   '-vf', 'scale=8:8', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
    return np.frombuffer(raw, np.uint8).reshape(-1, 3).mean(axis=0)


def test_repair_keeps_ids_files_evidence_and_redirects_legacy(config, video_file, monkeypatch):
    store = Store(config.data_dir)
    original = store.root / "original.mp4"
    shutil.copy2(video_file, original)
    store.execute('INSERT INTO sources(id,name,path,media,layout_key,prepared) VALUES(?,?,?,?,?,1)',
                  ('s', 'fixture', 'original.mp4', json.dumps({'duration': 10}), config.layout_key))
    for identifier, start, end, kind, parent in [
        ('a', 0, .96, 'shot', None), ('b', .96, 10, 'shot', None),
        ('m', .96, 4.96, 'moment', 'b')]:
        path = store.root / f'{identifier}.mp4'
        cut(video_file, start, end, path)
        store.execute('INSERT INTO segments VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (identifier, 's', kind, start, end, path.name, '', config.layout_key, parent, 'test'))
    plans = store.root / 'plans'
    plans.mkdir()
    (plans / 's.json').write_text(json.dumps({'source_id': 's', 'shots': [{'start': 0, 'end': .96}, {'start': .96, 'end': 10}]}))
    old = {p.name: p.read_bytes() for p in store.root.glob('*.mp4')}
    snapshot = store.one("SELECT * FROM segments WHERE id='m'") | {'annotation': {'description': 'preserve'}}
    store.execute("INSERT INTO index_versions(id,spec,status) VALUES('i','{}','active')")
    store.execute('INSERT INTO index_members VALUES(?,?,?,1)', ('i', 'm', json.dumps(snapshot)))
    store.execute("INSERT INTO person_evidence VALUES('e','s','b',NULL,NULL,NULL,1.5,2,'accepted','{}')")
    evidence = store.rows('SELECT * FROM person_evidence')
    plan = audit(store, workers=1)
    assert plan['sources'][0]['boundaries'][0]['new'] == 1
    assert first_rgb(store.root / 'b.mp4')[0] > 200
    from mindex import shot_repair
    commit_plan = shot_repair._commit_plan
    def interrupt(*args):
        raise RuntimeError('interrupted after metadata commit')
    monkeypatch.setattr(shot_repair, '_commit_plan', interrupt)
    with pytest.raises(RuntimeError, match='interrupted'):
        apply(config, plan)
    assert plan['sources'][0]['status'] == 'committed'
    monkeypatch.setattr(shot_repair, '_commit_plan', commit_plan)
    result = apply(config, plan)
    assert result['sources'][0]['status'] == 'verified'
    assert store.one("SELECT start FROM segments WHERE id='b'")['start'] == 1
    assert store.one("SELECT end FROM segments WHERE id='a'")['end'] == 1
    assert {r['id'] for r in store.segments()} == {'a', 'b', 'm'}
    assert store.rows('SELECT * FROM person_evidence') == evidence
    for name, content in old.items():
        assert (store.root / name).read_bytes() == content
    assert first_rgb(store.resolve_media('b.mp4'))[2] > 200
    assert first_rgb(store.resolve_media('m.mp4'))[2] > 200
    published = json.loads(store.one('SELECT snapshot FROM index_members')['snapshot'])
    assert published['start'] == 1 and published['annotation'] == snapshot['annotation']
    ShotMedia(store).register('m.mp4', 's', 1, 4.96)
    assert store.one("SELECT version FROM media_references WHERE path='m.mp4'")['version'] == 'shot-boundary-v1'
    assert apply(config, result)['sources'][0]['status'] == 'verified'
    assert inspect_boundary(video_file, 1)['new'] == 1
    repeated = audit(store)
    assert repeated['sources'][0]['already_corrected']
    assert apply(config, repeated)['no_changes']
    from mindex.pipeline import Pipeline
    from mindex.subtitle_archive import archive_path
    manifest = archive_path(store, 's')
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({'version': 1, 'tracks': [], 'utterances': {}, 'files': {}}))
    # Thumbnails are irrelevant to the resume-ID invariant; retain a preexisting thumbnail.
    for row in store.segments():
        store.execute('UPDATE segments SET thumbnail=? WHERE id=?', ('thumbnail.jpg', row['id']))
    (store.root / 'thumbnail.jpg').write_bytes(b'preexisting')
    import mindex.pipeline as pipeline_module
    monkeypatch.setattr(pipeline_module, 'thumbnail', lambda *args: None)
    Pipeline(config).split('s')
    assert {row['id'] for row in store.segments()} == {'a', 'b', 'm'}
    assert store.one("SELECT version FROM media_references WHERE path='m.mp4'")['version'] == 'shot-boundary-v1'
    source = store.one("SELECT * FROM sources WHERE id='s'")
    existing = store.one("SELECT * FROM segments WHERE id='b'")
    Pipeline(config)._segment(source, 'shot', 1, 10, config.layout_key, None, 'test')
    assert store.one("SELECT path FROM segments WHERE id='b'")['path'] == existing['path']
    assert len(store.segments()) == 3


def test_edge_adjustment_preserves_interior_absolute_times():
    parent = {'new_start': 1, 'new_end': 5}
    assert adjusted({'id': 'm', 'kind': 'moment', 'parent_id': 'p', 'start': .96, 'end': 4.96},
                    {'p': parent}, {.96: 1, 4.96: 5}) == (1, 5)
    assert adjusted({'id': 'm', 'kind': 'moment', 'parent_id': 'p', 'start': 2, 'end': 4},
                    {'p': parent}, {.96: 1}) == (2, 4)
    with pytest.raises(ValueError, match='collapses'):
        adjusted({'id': 'bad', 'kind': 'moment', 'parent_id': 'p', 'start': .96, 'end': .99},
                 {'p': parent}, {.96: 1})


def test_vfr_exclusive_boundary_holds_last_frame_without_relaxing_tolerance(tmp_path):
    from mindex.shot_repair import ensure_duration

    path = tmp_path / 'staged.mp4'
    command(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=blue:s=64x64:r=24',
             '-frames:v', '47', '-c:v', 'libx264', str(path)])
    before = path.read_bytes()
    info, held = ensure_duration(path, 2.00002, ['-c:v', 'libx264', '-preset', 'fast', '-crf', '20'])
    assert held == 1
    assert info['duration'] == pytest.approx(2, abs=1e-6)
    assert next(tmp_path.glob('*.unfilled-*.mp4')).read_bytes() == before
    assert first_rgb(path)[2] > 200
