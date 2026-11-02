"""素材覆盖必须有镜头证据与核验结论，近邻和 BM25 都不是覆盖证明。"""
from __future__ import annotations

import json

import pytest

from mindex.models.adapters import ModelError
from mindex.shared.storage import Store
from mindex.topics import cards, coverage, coverage_verify, profiles
from mindex.topics.models import ProfileBody, ProfileChanges
from mindex.topics.summarize import CardDraft


def candidate(identifier, description, similarity=0.9, duration=2):
    return {"id": identifier, "source_id": "src", "start": 0, "end": duration,
            "annotation": {"description": description} if description else {},
            "matches": [{"route": "video", "cosine_similarity": similarity}]}


class Engine:
    def __init__(self, candidates, degraded=False):
        self.candidates, self.degraded, self.queries = candidates, degraded, []

    def text(self, query):
        self.queries.append(query)
        return {"results": self.candidates, "degraded": self.degraded}


def judge(system, parts):
    assert "素材候选相关性核验" in system
    payload = json.loads(parts[0]["text"].split("\n", 1)[1])
    decisions = []
    for item in payload["candidates"]:
        matched = '显示Claude对话窗口及聊天文字' in item['evidence']
        decisions.append({"candidate": item['candidate'], "verdict": "matched" if matched else "rejected",
                          "reason": "有具体对话内容证据" if matched else "仅有灯光，没有聊天内容",
                          "quote": "显示Claude对话窗口及聊天文字" if matched else ""})
    return json.dumps({"decisions": decisions}, ensure_ascii=False)


def test_only_verified_candidates_count_and_lower_rank_can_pass(config):
    engine = Engine([candidate('airport', '机场中庭发光装置'),
                     candidate('screen', '屏幕显示Claude对话窗口及聊天文字')])
    report = coverage.report(config, Store(config.data_dir), ['Claude对话窗口'], engine=engine, llm=judge,
                             title='几千个Claude智能体的任务：绕过训练数据监控器')
    row = report['visuals'][0]
    assert report['hits'] == 1 and [c['segment_id'] for c in row['clips']] == ['screen']
    assert [c['verification']['verdict'] for c in row['candidates']] == ['rejected', 'matched']
    assert engine.queries[0].filters.min_duration == 1


@pytest.mark.parametrize('degraded', [False, True])
def test_airport_not_coverage_even_with_high_score_or_bm25(config, degraded):
    report = coverage.report(config, Store(config.data_dir), ['Claude对话窗口'],
                             engine=Engine([candidate('airport', '机场中庭发光装置')], degraded), llm=judge)
    assert report['ratio'] == 0 and not report['visuals'][0]['clips']
    assert report['degraded'] is degraded


def test_missing_evidence_and_short_transitions_never_reach_model(config):
    def forbidden(*args):
        raise AssertionError('不应对缺证据或不可用的片段调用模型')

    report = coverage.report(config, Store(config.data_dir), ['Claude对话窗口'], llm=forbidden,
                             engine=Engine([candidate('missing', ''), candidate('short', '紫粉色失焦色块', duration=0.26)]))
    assert report['hits'] == 0
    assert [c['verification']['verdict'] for c in report['visuals'][0]['candidates']] == ['uncertain', 'rejected']


@pytest.mark.parametrize('failure', ['model', 'missing', 'duplicate', 'quote'])
def test_verification_errors_never_fall_back_to_recall_hit(config, failure):
    config.studio.writer.split_retries = 0

    def invalid(system, parts):
        if failure == 'model':
            raise ModelError('测试模型不可用')
        decision = {'candidate': 1, 'verdict': 'matched', 'reason': '相符', 'quote': '并不存在的画面证据'}
        return json.dumps({'decisions': [] if failure == 'missing' else [decision] * (2 if failure == 'duplicate' else 1)})

    report = coverage.report(config, Store(config.data_dir), ['Claude对话窗口'], llm=invalid,
                             engine=Engine([candidate('airport', '机场中庭发光装置')]))
    assert report['ratio'] == 0 and report['status'] == 'unverified'
    assert report['visuals'][0]['candidates'][0]['verification']['verdict'] == 'error'


def test_batches_have_unique_complete_decisions():
    candidates = [{'candidate': i, 'visual': 'Claude对话窗口', 'evidence': '显示Claude对话窗口及聊天文字'}
                  for i in range(1, 26)]
    decisions = coverage_verify.verify(judge, candidates, title='标题', angle='', retries=0)
    assert set(decisions) == set(range(1, 26))
    assert all(d['verdict'] == 'matched' for d in decisions.values())


def test_legacy_reports_and_changed_profile_settings_are_stale(config):
    store = Store(config.data_dir)
    profile = profiles.create_profile(store, ProfileBody(name='科技', domain='科技'))
    cid = cards.create(store, profile['id'], None, CardDraft(cluster=1, title='测试', visuals=['画面一', '画面二']), [])
    body = json.loads(store.one('SELECT body FROM topic_cards WHERE id=?', (cid,))['body'])
    body['coverage'] = {'status': 'ok', 'ratio': 1, 'hits': 2, 'total': 2, 'visuals': [],
                        'material_scope': profiles.material_signature(store, profiles.body_of(store, profile['id']))}
    body['scores'] = {'coverage': 1}
    cards.save_body(store, cid, body)
    assert cards.get_card(store, cid)['coverage']['status'] == 'stale'
    assert cards.list_cards(store, profile['id'])[0]['scores'] == {}
    body['coverage']['verification_version'] = coverage_verify.VERSION
    cards.save_body(store, cid, body)
    assert cards.get_card(store, cid)['coverage']['status'] == 'ok'
    profiles.update_profile(store, profile['id'], ProfileChanges(coverage={'min_similarity': 0.6}))
    assert cards.get_card(store, cid)['coverage']['status'] == 'stale'
