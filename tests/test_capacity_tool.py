"""容量工装可执行性；小样本永远不能标成原定容量通过。"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest
import yaml
from test_cluster_process_flow import free_port, stop
from test_scaling_correctness import pg_config as pg_config
from test_scaling_correctness import s3_config as s3_config

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/scaling-capacity.py'


def module():
    spec = importlib.util.spec_from_file_location('capacity_tool', SCRIPT)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_failed_queries_are_included_in_latency_quantiles():
    result = module().summary([{'ok': True, 'elapsed_ms': 10}, {'ok': False, 'elapsed_ms': 5000}])
    assert result['requests'] == 2 and result['failures'] == 1
    assert result['p95_ms'] > 4000 and result['p99_ms'] > 4900


@pytest.mark.requires_postgres
@pytest.mark.requires_s3
@pytest.mark.requires_chroma_server
def test_capacity_seed_embedder_two_api_smoke(s3_config, tmp_path):
    chroma = os.environ.get('MINDEX_TEST_CHROMA_SERVER')
    if not chroma:
        pytest.skip('需要隔离 MINDEX_TEST_CHROMA_SERVER')
    s3_config.vector.backend = 'chroma_server'
    s3_config.vector.chroma_server.url = chroma
    s3_config.runtime_overrides = False
    s3_config.embedding.model = 'mindex-capacity-engineering-v1'
    s3_config.embedding.quantization = 'float32'
    s3_config.embedding.dimensions = 8
    model_port = free_port()
    s3_config.embedding.base_url = f'http://127.0.0.1:{model_port}'
    raw = s3_config.model_dump(mode='json')
    for field in ('database', 'vector', 'storage', 'queue', 'locks', 'gpu', 'server', 'uploads', 'agent', 'topics', 'local_services'):
        raw[field] = getattr(s3_config, field).model_dump(mode='json')
    raw['agent']['enabled'] = False
    raw['topics']['scheduler']['enabled'] = False
    raw['local_services']['enabled'] = False
    config = tmp_path / 'capacity.yaml'
    config.write_text(yaml.safe_dump(raw, allow_unicode=True))
    evidence = Path(os.environ.get('MINDEX_TEST_EVIDENCE_DIR', str(tmp_path))) / 'capacity-smoke'
    with ExitStack() as resources:
        def launch(name, command):
            log = resources.enter_context((tmp_path / f'{name}.log').open('w'))
            process = subprocess.Popen([sys.executable, *command], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            resources.callback(stop, process)
            return process
        seed = subprocess.run([sys.executable, str(SCRIPT), 'seed', '--config', str(config), '--smoke'],
                              cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)
        assert seed.returncode == 0, seed.stdout + seed.stderr
        model = launch('model', [str(SCRIPT), 'embedder', '--config', str(config), '--smoke', '--port', str(model_port)])
        apis, urls = [], []
        for number in range(2):
            raw['data_dir'] = str(tmp_path / f'api-{number}')
            path = tmp_path / f'api-{number}.yaml'
            path.write_text(yaml.safe_dump(raw, allow_unicode=True))
            port = free_port()
            apis.append(launch(f'api-{number}', ['-c', 'from mindex.cli import app; app()', '--config', str(path), 'serve', '--port', str(port)]))
            urls.append(f'http://127.0.0.1:{port}')
        with httpx.Client(timeout=2, trust_env=False) as client:
            for url in [s3_config.embedding.base_url + '/health', *(u + '/api/ready' for u in urls)]:
                until = time.monotonic() + 30
                while True:
                    try:
                        if client.get(url).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert all(p.poll() is None for p in [model, *apis]), '隔离容量进程异常退出'
                    assert time.monotonic() < until, '隔离容量服务未就绪'
                    time.sleep(.1)
        result = subprocess.run([sys.executable, str(SCRIPT), 'run', '--config', str(config), '--smoke',
            '--api-url', *urls, '--api-pid', *(str(p.pid) for p in apis), '--requests', '20', '--output', str(evidence)],
            cwd=ROOT, capture_output=True, text=True, timeout=90, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads((evidence / 'report.json').read_text())
        assert report['status'] == 'smoke_passed_not_acceptance' and not report['capacity_passed']
        assert report['all_requests']['requests'] == 44 and report['all_requests']['failures'] == 0
        assert report['source_unchanged'] and report['engineering_nearest_hit_at5'] == 1
