"""Loading paths must not read frame evidence or full publication snapshots."""
import io

from fastapi.testclient import TestClient
from PIL import Image

from mindex.api import create_app
from mindex.persons.person_models import Entity
from mindex.persons.persons import Persons
from mindex.shared.storage import Store, dumps


def test_search_filters_are_lightweight_and_respect_relationships(config, monkeypatch):
    store = Store(config.data_dir)
    people = Persons(store)
    for sid in ('visible', 'hidden'):
        store.execute('INSERT INTO sources(id,name,path,media,layout_key) VALUES(?,?,?,?,?)',
                      (sid, sid, 'video.mp4', '{}', 'layout'))
    work = people.work_for('visible')
    actor = people.save(Entity(kind='actor', name='Actor', aliases=['Alias']))
    role = people.save(Entity(kind='role', name='Role', actor_ids=[actor['id']], work_ids=[work]))
    store.execute("INSERT INTO source_curation(id,display_name) VALUES('visible','Renamed')")
    store.execute("INSERT INTO source_curation(id,hidden) VALUES('hidden',1)")
    app = create_app(config)
    def unexpected(*args, **kwargs):
        raise AssertionError('Heavy catalogue data requested for filters')
    for name in ('references', 'review_summary', 'published'):
        monkeypatch.setattr(Persons, name, unexpected)
    with TestClient(app) as client:
        response = client.get('/api/persons/filters')
        store.execute("DELETE FROM person_sources WHERE source_id='visible'")
        assert client.get('/api/persons/filters').json()['sources'][0]['work_id'] == work
    assert response.status_code == 200
    result = response.json()
    assert result['sources'] == [{'id': 'visible', 'name': 'Renamed', 'work_id': work}]
    assert {e['id'] for e in result['entities']} == {actor['id'], role['id']}
    assert next(e for e in result['entities'] if e['id'] == role['id'])['actor_ids'] == [actor['id']]
    assert set(result) == {'entities', 'sources'}


def test_cast_gallery_thumbnail_cached_and_original_preserved(config, monkeypatch):
    store = Store(config.data_dir)
    people = Persons(store)
    data = io.BytesIO()
    Image.new('RGBA', (1368, 1776), (255, 0, 0, 128)).save(data, 'PNG')
    image = {'id': 'image', **people.image(data.getvalue(), 'imports')}
    store.execute('INSERT INTO person_imports(id,data) VALUES(?,?)',
                  ('draft', dumps({'images': [image], 'entries': []})))
    app = create_app(config)
    with TestClient(app) as client:
        url = '/api/persons/imports/draft/images/image'
        preview = client.get(url + '?thumbnail=true')
        assert preview.status_code == 200
        assert preview.headers['content-type'] == 'image/jpeg'
        assert 'max-age=' in preview.headers['cache-control']
        with Image.open(io.BytesIO(preview.content)) as small:
            assert small.width <= 340 and small.height <= 192
            assert small.mode == 'RGB'
        assert client.get(url).content == (store.root / image['path']).read_bytes()
        def unexpected(*args, **kwargs):
            raise AssertionError('Cached thumbnail decoded again')
        monkeypatch.setattr('mindex.persons.person_thumbnails.Image.open', unexpected)
        assert client.get(url + '?thumbnail=true').content == preview.content
        client.delete(url)
        assert client.get(url + '?thumbnail=true').status_code in (404, 422)


def test_summary_status_omits_expensive_coverage(config, monkeypatch):
    app = create_app(config)
    def unexpected(*args, **kwargs):
        raise AssertionError('Full index coverage requested for summary')
    monkeypatch.setattr(Store, 'active_index', lambda self: {'id': 'active', 'spec': {}})
    monkeypatch.setattr('mindex.search.search.channel_coverage', unexpected)
    monkeypatch.setattr(Persons, 'published', unexpected)
    with TestClient(app) as client:
        result = client.get('/api/status?summary=true')
    assert result.status_code == 200
    assert result.json()['channel_coverage'] == {}
    assert result.json()['person_coverage'] == []


def test_person_catalogue_compact_coverage_preserves_row_counts(config, monkeypatch):
    store = Store(config.data_dir)
    # The UI shows per-source coverage counts; it consumes neither frame data nor per-row records.
    app = create_app(config)
    original = store.rows
    def rows(self, sql, args=()):
        if sql in ('SELECT * FROM person_coverage', 'SELECT source_id FROM person_coverage'):
            raise AssertionError('Coverage rows loaded for compact catalogue')
        if sql == 'SELECT source_id,COUNT(*) AS n FROM person_coverage GROUP BY source_id':
            return [{'source_id': 'a', 'n': 2}, {'source_id': 'b', 'n': 1}]
        return original(sql, args)
    monkeypatch.setattr(Store, 'rows', rows)
    monkeypatch.setattr(Persons, 'review_summary', lambda self: (_ for _ in ()).throw(AssertionError('Unused review summary')))
    with TestClient(app) as client:
        response = client.get('/api/persons?compact=true')
    assert response.status_code == 200
    assert response.json()['coverage_counts'] == {'a': 2, 'b': 1}
    assert 'coverage' not in response.json()
