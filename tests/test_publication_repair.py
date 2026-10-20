"""Transient publication failures stay retryable; invalid inputs need attention."""
import runpy
from pathlib import Path

import pytest


@pytest.mark.parametrize('error,expected', [
    ('Connection refused', True),
    ('request timed out', True),
    ('HTTP 503 Service Unavailable', True),
    ('模型输出维度不匹配', False),
    ('模型内容安全策略拒绝', False),
    ('HTTP 401 Unauthorized', False),
    ('unclassified upstream failure', True),
])
def test_repair_distinguishes_transient_failures(error, expected):
    module = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/repair-source-publications.py'))
    assert module['retryable'](error) is expected
