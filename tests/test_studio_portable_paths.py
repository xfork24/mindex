from test_studio_workspace import setup_workspace

from mindex.shared.config import portable_parameters
from mindex.studio import workspace as ws


def test_historical_absolute_manifest_reuses_version_after_move(config):
    jid, w, p = setup_workspace(config)
    relative = 'data/config-dependencies/release/model.json'
    config.embedding.deployment_manifest = str(config.data_dir.parent / relative)
    spec = ws.specification(config, jid, w, p, 'recall', 'a')
    assert spec['parameters']['embedding']['deployment_manifest'] == relative
    historical = {**spec['parameters'], 'embedding': {
        **spec['parameters']['embedding'],
        'deployment_manifest': '/previous/project/' + relative,
    }}
    legacy = ws.specification(config, jid, w, p, 'recall', 'a', parameters=historical)
    vid = ws.add_version(config, jid, w, 'recall', 'a', **legacy)
    p['shots']['a']['recall'] = vid
    assert ws.conflicts(config, jid, w, p, 'recall', 'a', vid) == []
    assert ws.match(config, jid, w, p, 'recall', 'a', verify=False) == vid
    # Query and genuine dependency changes still invalidate the historical result.
    content = w['versions'][p['shots']['a']['content']]['data']
    content['query'] += 'changed'
    messages = ws.conflicts(config, jid, w, p, 'recall', 'a', vid)
    assert messages == ['查询、召回方式或召回设置已变化']
    content['query'] = content['query'].removesuffix('changed')
    config.embedding.deployment_manifest = str(config.data_dir.parent / 'other/model.json')
    assert ws.conflicts(config, jid, w, p, 'recall', 'a', vid)


def test_font_and_manifest_snapshots_are_relative(config):
    params = {'subtitle': {'font_file': str(config.data_dir.parent / 'fonts/subtitle.ttf')},
              'analyzer': {'deployment_manifest': str(config.data_dir / 'pins/analyzer.json')}}
    result = portable_parameters(config, params)
    assert result['subtitle']['font_file'] == 'fonts/subtitle.ttf'
    assert result['analyzer']['deployment_manifest'] == config.data_dir.name + '/pins/analyzer.json'
