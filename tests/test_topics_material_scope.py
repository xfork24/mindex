"""选题与写作共用素材范围：动态组、覆盖、候选、继承与失效。"""
from __future__ import annotations

import json

import pytest

from mindex.shared.media_groups import Groups
from mindex.shared.storage import Store, dumps
from mindex.shared.types import Filters
from mindex.studio import studio
from mindex.studio.material_digest import build_digest, safe_digest, scope_parameters
from mindex.topics import cards, coverage, create_job, profiles
from mindex.topics.models import ProfileBody, ProfileChanges
from mindex.topics.summarize import CardDraft


@pytest.fixture
def materials(config):
    store = Store(config.data_dir)
    for sid in ('a', 'b', 'c'):
        store.execute('INSERT INTO sources(id,name,path,media,layout_key) VALUES(?,?,?,?,?)',
                      (sid, sid, f'sources/{sid}.mp4', dumps({'duration': 5}), 'layout'))
    groups = Groups(store)
    first = groups.save('source', '第一组')['id']
    second = groups.save('source', '第二组')['id']
    empty = groups.save('source', '空组')['id']
    groups.change_members(first, ['a', 'b'])
    groups.change_members(second, ['b', 'c'])
    return store, groups, first, second, empty


def test_coverage_union_intersection_empty_and_deleted(config, materials):
    store, groups, first, second, empty = materials

    class Engine:
        calls = 0

        def text(self, query):
            self.calls += 1
            assert query.filters.effective_sources(store) == {'b', 'c'}
            return {'degraded': True, 'results': [
                {'id': sid, 'source_id': sid, 'score': 1} for sid in ('a', 'b', 'c')]}

    engine = Engine()
    report = coverage.report(config, store, ['画面'], engine=engine,
                             filters=Filters(source_group_ids=[first, second], source_ids=['b', 'c']))
    assert report['ratio'] == 1
    assert [clip['source_id'] for clip in report['visuals'][0]['clips']] == ['b', 'c']
    report = coverage.report(config, store, ['画面'], engine=engine, filters=Filters(source_group_ids=[empty]))
    assert report['ratio'] == 0 and engine.calls == 1
    groups.delete(first)
    with pytest.raises(ValueError, match='已删除'):
        coverage.report(config, store, ['画面'], engine=engine, filters=Filters(source_group_ids=[first]))
    assert engine.calls == 1


def test_scope_saved_stale_and_job_inherits(config, materials):
    store, groups, first, second, _ = materials
    profile = profiles.create_profile(store, ProfileBody(name='测试', domain='科技', source_ids=['b'],
                                                        source_group_ids=[first]))
    body = profiles.body_of(store, profile['id'])
    local_items = [{'dedup_key': f'local:source:{sid}'} for sid in ('a', 'b', 'c')]
    assert profiles.scoped_items(store, body, local_items) == [local_items[1]]
    cid = cards.create(store, profile['id'], None,
                       CardDraft(cluster=0, title='标题', angle='角度', reason='理由', visuals=['画面一', '画面二']), [])
    row = store.one('SELECT body FROM topic_cards WHERE id=?', (cid,))
    card_body = json.loads(row['body'])
    card_body['coverage'] = {'status': 'ok', 'ratio': 1, 'visuals': [],
                             'material_scope': profiles.material_signature(store, body)}
    cards.save_body(store, cid, card_body)
    task = create_job.create_from_card(config, store, cid)
    job_id = task['job_id']
    state = studio.load_job(config, job_id)
    assert state['source_ids'] == ['b'] and state['source_group_ids'] == [first]
    before = scope_parameters(config, job_id, store)
    groups.change_members(second, ['a'])
    assert cards.get_card(store, cid)['coverage']['status'] == 'ok'
    groups.change_members(first, ['c'])
    assert cards.get_card(store, cid)['coverage']['status'] == 'stale'
    assert cards.list_cards(store, profile['id'], status='all')[0]['coverage']['status'] == 'stale'
    assert before != scope_parameters(config, job_id, store)
    assert studio.load_job(config, job_id) == state
    groups.delete(first)
    with pytest.raises(ValueError, match='已删除'):
        safe_digest(config, job_id, store=store)
    with pytest.raises(ValueError, match='找不到素材'):
        profiles.update_profile(store, profile['id'], ProfileChanges(source_ids=['missing']))


def test_digest_filters_task_and_shot_and_empty(config, video_file):
    from topics_fixtures import analyzed_library

    analyzed_library(config, video_file)
    store = Store(config.data_dir)
    sid = next(iter(store.source_ids()))
    group = Groups(store).save('source', '摘要范围')['id']
    assert build_digest(config, store, {'source_group_ids': [group]}).empty
    Groups(store).change_members(group, [sid])
    digest = build_digest(config, store, {'source_group_ids': [group]})
    assert not digest.empty and {s.source_id for s in digest.sources} == {sid}
    assert build_digest(config, store, {'source_group_ids': [group]},
                        filters=Filters(source_ids=['excluded'])).empty


def test_material_scope_rest_cli_and_agent(config, materials, tmp_path):
    from fastapi.testclient import TestClient
    from test_topics_profiles import run_tool
    from typer.testing import CliRunner

    from mindex import cli
    from mindex.api import create_app

    store, _, first, _, _ = materials
    app = create_app(config)
    with TestClient(app) as client:
        response = client.post('/api/topics/profiles', json={
            'name': '范围档案', 'domain': '科技', 'source_ids': ['b'], 'source_group_ids': [first]})
        assert response.status_code == 201, response.text
        pid = response.json()['id']
    preview, out = run_tool(app, 'save_topic_profile', {'topic_profile_id': pid, 'source_ids': ['a']})
    assert out.value['source_ids'] == ['a'] and out.value['source_group_ids'] == [first]
    assert any(row['label'] == '素材范围' for row in preview.changes)
    config_path = tmp_path / 'scope.yaml'
    config_path.write_text(f'data_dir: {config.data_dir}\n')
    result = CliRunner().invoke(cli.app, ['--config', str(config_path), 'topics', 'profiles', 'update', pid,
                                         '--material-source', 'b', '--source-group', first])
    assert result.exit_code == 0, result.output
    assert profiles.body_of(store, pid).source_ids == ['b']
