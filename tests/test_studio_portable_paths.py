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


def test_llama_cpp_equivalent_endpoint_keeps_historical_recall(config):
    jid, w, p = setup_workspace(config)
    config.embedding.backend = 'llama_cpp'
    config.embedding.base_url = 'http://127.0.0.1:8888/v1'
    spec = ws.specification(config, jid, w, p, 'recall', 'a')
    vid = ws.add_version(config, jid, w, 'recall', 'a', **spec)
    p['shots']['a']['recall'] = vid
    for endpoint in ('http://127.0.0.1:8888', 'http://127.0.0.1:8888/v1/'):
        config.embedding.base_url = endpoint
        assert ws.conflicts(config, jid, w, p, 'recall', 'a', vid) == []
        assert ws.match(config, jid, w, p, 'recall', 'a', verify=False) == vid
    config.search.channel_weights['visual'] = 3
    assert '参数 召回权重（视觉）：原值 1，当前 3' in ws.conflicts(config, jid, w, p, 'recall', 'a', vid)
    config.search.channel_weights['visual'] = 1
    config.embedding.base_url = 'http://127.0.0.1:9999'
    assert ws.conflicts(config, jid, w, p, 'recall', 'a', vid)


def test_parameter_conflicts_identify_each_effective_change(config):
    from mindex.studio.workspace.signatures import parameter_conflicts

    previous = {'search': {'channel_weights': {'visual': 3}}, 'visibility': 'old-visibility',
                'index_id': 'old-index', 'embedding': {'backend': 'llama_cpp',
                'base_url': 'http://localhost:8888/v1', 'expected_space': {'model_sha256': 'old-model'}}}
    current = {'search': {'channel_weights': {'visual': 1}}, 'visibility': 'new-visibility',
               'index_id': 'new-index', 'embedding': {'backend': 'llama_cpp',
               'base_url': 'http://localhost:8888', 'expected_space': {'model_sha256': 'new-model'}}}
    messages = parameter_conflicts(config, previous, current)
    assert len(messages) == 4
    assert '参数 召回权重（视觉）：原值 3，当前 1' in messages
    assert '参数 素材可见性指纹：原值 "old-visibility"，当前 "new-visibility"' in messages
    assert '参数 活动索引版本：原值 "old-index"，当前 "new-index"' in messages
    assert any('embedding.expected_space.model_sha256' in message for message in messages)
    assert not any('base_url' in message or '嵌入服务地址' in message for message in messages)
    assert previous['embedding']['base_url'].endswith('/v1')
