"""历史中文分析的英文覆盖不改变已发布数据或用户文字。"""
import copy
import json

from fastapi.testclient import TestClient

from mindex.api import create_app
from mindex.shared.analysis_translations import display_metadata, english_analysis_view, load_translations
from mindex.shared.jsonio import save


def test_display_metadata_covers_annotations_and_search_views(tmp_path):
    translations = {'仰视 low-angle view': 'low-angle view', '清新 lush': 'lush', '旧版描述': 'Legacy description'}
    save(tmp_path / 'analysis-translations.en.json', translations)
    data = {'annotation': {'angle': ['仰视 low-angle view']},
            'matched_text': '旧版描述\n角度：仰视 low-angle view\n氛围：清新 lush',
            'user_title': '仰视 low-angle view'}
    original = copy.deepcopy(data)
    result = display_metadata(data, tmp_path)
    assert result['_analysis_translations'] == translations
    assert {k: v for k, v in result.items() if k != '_analysis_translations'} == original
    assert data == original
    assert display_metadata({'text': 'unrelated'}, tmp_path) == {'text': 'unrelated'}


def test_missing_invalid_and_replaced_translations(tmp_path):
    assert load_translations(tmp_path) == {}
    path = tmp_path / 'analysis-translations.en.json'
    save(path, {'旧版': 'Legacy', '错误': 'English 中文', '空': '', '数组': []})
    assert load_translations(tmp_path) == {'旧版': 'Legacy'}
    save(path, {'旧版': 'Updated translation'})
    assert load_translations(tmp_path) == {'旧版': 'Updated translation'}


def test_api_attaches_display_translations(config):
    save(config.data_dir / 'analysis-translations.en.json', {'仰视 low-angle view': 'low-angle view'})
    app = create_app(config)

    @app.get('/api/test-analysis-translation')
    def example():
        return {'annotation': {'angle': ['仰视 low-angle view']}}

    with TestClient(app) as client:
        data = client.get('/api/test-analysis-translation').json()
    assert data['annotation']['angle'] == ['仰视 low-angle view']
    assert data['_analysis_translations'] == {'仰视 low-angle view': 'low-angle view'}
    assert 'low-angle view' in json.dumps(data)


def test_english_material_view_preserves_parser_labels():
    text = '旧版描述\n角度：仰视 low-angle view\n氛围：清新 lush；静谧 quiet'
    translations = {'旧版描述': 'Legacy description', '仰视 low-angle view': 'low-angle view',
                    '清新 lush': 'lush', '静谧 quiet': 'quiet'}
    assert english_analysis_view(text, translations) == (
        'Legacy description\n角度：low-angle view\n氛围：lush；quiet')
    assert english_analysis_view('城市夜景 / City lights\n动作：行走 / walking', {}) == (
        'City lights\n动作：walking')
