"""The one-off maintenance tool must preserve selected reviews and candidates."""
import importlib.util
import json
import sqlite3
from pathlib import Path


def test_prune_reviews_by_actual_frame_index(tmp_path, video_file, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        'prune_person_frames', Path(__file__).parents[1] / 'scripts/prune-person-review-frames.py'
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    selected = module.sampled_pts(video_file, 0, 1, 15)
    all_pts = sorted(module.sampled_pts(video_file, 0, 1, 1))
    assert selected == set(all_pts[::15])
    db = sqlite3.connect(tmp_path / 'metadata.sqlite3')
    db.executescript('''
        CREATE TABLE segments(id,source_id,path,start,end,kind);
        CREATE TABLE person_evidence(id PRIMARY KEY,source_id,shot_id,status,start,data);
        CREATE TABLE person_features(kind,data);
        CREATE TABLE model_profiles(id,role,config);
    ''')
    db.execute('INSERT INTO segments VALUES(?,?,?,?,?,?)', ('shot', 'source', str(video_file), 0, 1, 'shot'))
    expected = {}
    for i, t in enumerate(all_pts):
        for status in ('accepted', 'rejected', 'candidate'):
            identifier = f'{i}-{status}'
            db.execute('INSERT INTO person_evidence VALUES(?,?,?,?,?,?)',
                       (identifier, 'source', 'shot', status, t, json.dumps({'cache_id': 'old'})))
            if status == 'candidate' or t in selected:
                expected[identifier] = status
    db.execute('INSERT INTO person_evidence VALUES(?,?,?,?,?,?)',
               ('marker', 'source', 'shot', 'accepted', 0, '{}'))
    expected['marker'] = 'accepted'
    db.execute('INSERT INTO person_features VALUES(?,?)', ('matching', '[]'))
    db.execute('INSERT INTO model_profiles VALUES(?,?,?)', (1, 'person', '{}'))
    db.commit()
    monkeypatch.setattr('sys.argv', ['prune', '--data-dir', str(tmp_path), '--apply', '--interval', '15'])
    module.main()
    assert dict(db.execute('SELECT id,status FROM person_evidence')) == expected
    assert db.execute('SELECT COUNT(*) FROM person_features').fetchone()[0] == 0
    assert json.loads(db.execute('SELECT config FROM model_profiles').fetchone()[0])['frame_interval'] == 15
    db.close()
