"""集群验收入口的覆盖门禁与启动失败证据回归。"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def verifier():
    spec = importlib.util.spec_from_file_location("scaling_verifier", ROOT / "scripts/verify-scaling.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backend_coverage_ignores_ci_rule_examples_and_docs(verifier, tmp_path):
    (tmp_path / "test_ci_selection.py").write_text(
        (ROOT / "tests/test_ci_selection.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "test_docs.py").write_text(
        '"""说明 pytest.mark.requires_postgres 与 MINDEX_TEST_S3_ENDPOINT。"""\n', encoding="utf-8"
    )
    assert verifier.external_test_files(tmp_path) == set()


@pytest.mark.parametrize(
    "source",
    [
        "pytestmark = pytest.mark.requires_postgres",
        "@pytest.mark.requires_s3\ndef test_objects(): pass",
        "pytestmark = [pytest.mark.requires_chroma_server]",
        'dsn = os.environ.get("MINDEX_TEST_POSTGRES_DSN", "")',
        'url = os.getenv("MINDEX_TEST_S3_ENDPOINT")',
        'url = os.environ["MINDEX_TEST_CHROMA_SERVER"]',
    ],
)
def test_backend_coverage_detects_real_markers_and_gates(verifier, tmp_path, source):
    (tmp_path / "test_new_backend.py").write_text(source, encoding="utf-8")
    assert verifier.external_test_files(tmp_path) == {"test_new_backend.py"}


def test_repository_backend_coverage_is_complete(verifier):
    covered = set(verifier.TESTS) | set(verifier.CPU_TESTS) | set(verifier.FAULT_TESTS)
    assert verifier.external_test_files(ROOT / "tests") <= covered


def test_isolated_environment_preserves_browser_tooling_only(verifier, monkeypatch):
    monkeypatch.setenv("MINDEX_PLAYWRIGHT_MODULE", "/tmp/browser/node_modules/playwright-core")
    monkeypatch.setenv("MINDEX_CONFIG", "/private/config.yaml")
    monkeypatch.setenv("MINDEX_DATABASE_DSN", "private-database")
    monkeypatch.setenv("MINIO_ROOT_PASSWORD", "private-password")
    env = verifier.isolated_environment()
    assert env["MINDEX_PLAYWRIGHT_MODULE"] == "/tmp/browser/node_modules/playwright-core"
    assert "MINDEX_CONFIG" not in env
    assert "MINDEX_DATABASE_DSN" not in env
    assert "MINIO_ROOT_PASSWORD" not in env


@pytest.mark.parametrize("missing", ["dependency", "coverage"])
def test_preflight_failure_keeps_report_without_starting_services(verifier, monkeypatch, tmp_path, missing):
    tests = tmp_path / "tests"
    tests.mkdir()
    if missing == "coverage":
        (tests / "test_new_backend.py").write_text(
            "pytestmark = pytest.mark.requires_postgres\n", encoding="utf-8"
        )
    output = tmp_path / "evidence"
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    monkeypatch.setattr(verifier.platform, "platform", lambda: "测试平台")
    monkeypatch.setattr(
        verifier.shutil, "which", lambda _: None if missing == "dependency" else sys.executable
    )
    monkeypatch.setattr(
        sys, "argv", ["verify-scaling.py", "--minio-binary", sys.executable, "--output", str(output)]
    )

    def no_process(*args, **kwargs):
        pytest.fail("预检失败时不应启动容器或服务")

    monkeypatch.setattr(verifier.subprocess, "Popen", no_process)
    with pytest.raises(SystemExit) as exc:
        verifier.main()
    assert exc.value.code == 2
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "preflight_failed"
    assert ("需要 Docker" if missing == "dependency" else "test_new_backend.py") in report["error"]
    assert report["finished_at"] >= report["started_at"]
