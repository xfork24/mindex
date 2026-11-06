"""原子文件提交：Windows 文件共享约束、工作集预约及失败清理。"""

from __future__ import annotations

import os
import tempfile
from contextlib import nullcontext
from pathlib import Path

import httpx
import pytest

from mindex.shared import work_budget
from mindex.shared.config import Config
from mindex.shared.storage import Store
from mindex.studio import studio
from mindex.studio.tools.artifacts import Artifact, download


@pytest.fixture
def windows_replace(monkeypatch):
    """在所有平台拒绝替换仍被打开的文件，重现 Windows 的 WinError 32。"""
    handles = []
    original_open = Path.open
    original_temporary = tempfile.NamedTemporaryFile
    original_replace = os.replace

    def tracked_open(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        handles.append((path.resolve(), handle))
        return handle

    def tracked_temporary(*args, **kwargs):
        handle = original_temporary(*args, **kwargs)
        handles.append((Path(handle.name).resolve(), handle))
        return handle

    def replace(source, target):
        paths = {Path(source).resolve(), Path(target).resolve()}
        if any(path in paths and not handle.closed for path, handle in handles):
            raise PermissionError("WinError 32：文件句柄尚未关闭")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", tracked_temporary)
    monkeypatch.setattr(os, "replace", replace)


@pytest.fixture
def budget_store(tmp_path):
    config = Config(data_dir=tmp_path / "staging", storage={"staging_quota_gb": 0.01})
    with Store.open(config) as store:
        yield store


@pytest.fixture
def streamed_download(monkeypatch):
    payload = b"x" * (1024 * 1024 + 17)

    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield payload[:1024 * 1024]
            yield payload[1024 * 1024:]

    original_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Chunks()))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs))

    def fetch(target, *, max_bytes=None):
        return download(Artifact(url="http://127.0.0.1/artifact"), target,
                        hosts={"127.0.0.1"}, max_bytes=len(payload) if max_bytes is None else max_bytes, timeout=5)

    return payload, fetch


@pytest.mark.usefixtures("windows_replace")
@pytest.mark.parametrize("budgeted", [False, True])
def test_download_closes_file_before_atomic_commit(budget_store, streamed_download, budgeted):
    payload, fetch = streamed_download
    target = budget_store._backend_context.staging.root / "artifact.bin"
    target.write_bytes(b"old")
    with work_budget.scope(budget_store) if budgeted else nullcontext():
        assert fetch(target) == target
    assert target.read_bytes() == payload
    assert not list(target.parent.glob(".*.partial"))
    assert budget_store._backend_context.staging.usage()["reserved"] == 0


@pytest.mark.usefixtures("windows_replace")
def test_budgeted_copy_closes_file_before_atomic_commit(budget_store):
    root = budget_store._backend_context.staging.root
    source, target = root / "source.bin", root / "target.bin"
    source.write_bytes(b"copied" * 200_000)
    target.write_bytes(b"old")
    with work_budget.scope(budget_store):
        assert work_budget.copy_file(source, target) == target
    assert target.read_bytes() == source.read_bytes()
    assert not list(target.parent.glob(".copy-*"))
    assert budget_store._backend_context.staging.usage()["reserved"] == 0


@pytest.mark.parametrize("operation", ["download", "copy"])
def test_failed_commit_preserves_target_and_releases_budget(budget_store, streamed_download, monkeypatch, operation):
    _payload, fetch = streamed_download
    root = budget_store._backend_context.staging.root
    source, target = root / "source.bin", root / "target.bin"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    original_replace = os.replace

    def fail_commit(partial, destination):
        if Path(destination).resolve() == target.resolve():
            raise PermissionError("提交被拒绝")
        return original_replace(partial, destination)

    monkeypatch.setattr(os, "replace", fail_commit)
    with work_budget.scope(budget_store), pytest.raises(PermissionError, match="提交被拒绝"):
        if operation == "download":
            fetch(target)
        else:
            work_budget.copy_file(source, target)
    assert target.read_bytes() == b"old"
    assert not list(target.parent.glob(".*.partial"))
    assert not list(target.parent.glob(".copy-*"))
    assert budget_store._backend_context.staging.usage()["reserved"] == 0


def test_stream_limit_preserves_target_and_releases_budget(budget_store, streamed_download):
    _payload, fetch = streamed_download
    target = budget_store._backend_context.staging.root / "artifact.bin"
    target.write_bytes(b"old")
    with work_budget.scope(budget_store), pytest.raises(studio.StudioError, match="大小上限"):
        fetch(target, max_bytes=1024 * 1024)
    assert target.read_bytes() == b"old"
    assert not list(target.parent.glob(".*.partial"))
    assert budget_store._backend_context.staging.usage()["reserved"] == 0
