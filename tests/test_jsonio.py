"""shared/jsonio：唯一的原子写实现——并发写同一目标不互相破坏、UTF-8 落盘、失败不留临时文件。"""

from __future__ import annotations

import json
import threading

import pytest

from mindex.shared import jsonio
from mindex.studio import studio


def test_concurrent_writes_to_same_file_stay_valid(tmp_path):
    target = tmp_path / "job.json"
    errors = []

    def writer(number):
        try:
            for round_ in range(40):
                jsonio.save(target, {"writer": number, "round": round_, "payload": "中文" * 500})
        except Exception as exc:  # noqa: BLE001 — 线程内异常收集回主线程断言  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    value = json.loads(target.read_text(encoding="utf-8"))
    assert value["payload"] == "中文" * 500 and value["round"] == 39
    assert [p.name for p in tmp_path.iterdir()] == ["job.json"]


def test_studio_save_delegates_and_writes_utf8_bytes(tmp_path):
    target = tmp_path / "nested" / "storyboard.json"
    studio.save(target, {"text": "旁白"})
    raw = target.read_bytes()
    assert "旁白".encode() in raw and b"\\u" not in raw
    studio.save_bytes(tmp_path / "a.bin", b"\x00\xff")
    assert (tmp_path / "a.bin").read_bytes() == b"\x00\xff"
    jsonio.save_text(tmp_path / "t.txt", "文案")
    assert (tmp_path / "t.txt").read_bytes() == "文案".encode()


def test_sibling_targets_use_distinct_temporary_names(tmp_path, monkeypatch):
    seen = []
    real = jsonio.tempfile.mkstemp

    def spy(*args, **kwargs):
        descriptor, name = real(*args, **kwargs)
        seen.append(name)
        return descriptor, name

    monkeypatch.setattr(jsonio.tempfile, "mkstemp", spy)
    jsonio.save(tmp_path / "a.json", {})
    jsonio.save(tmp_path / "a.yaml", {})
    assert len(set(seen)) == 2
    assert all(name.endswith(".partial") for name in seen)  # clean 的崩溃残留规则仍能识别


def test_failed_write_removes_temporary_and_keeps_old_content(tmp_path, monkeypatch):
    target = tmp_path / "job.json"
    jsonio.save(target, {"v": 1})

    def broken_replace(*args):
        raise OSError("模拟替换失败")

    monkeypatch.setattr(jsonio.os, "replace", broken_replace)
    with pytest.raises(OSError, match="模拟替换失败"):
        jsonio.save(target, {"v": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["job.json"]


def test_save_upload_removes_partial_file_on_failure(tmp_path):
    import asyncio
    import io

    from fastapi import HTTPException
    from starlette.datastructures import UploadFile

    from mindex.shared.uploads import save_upload

    target = tmp_path / "upload.bin"
    with pytest.raises(HTTPException) as too_big:
        asyncio.run(save_upload(UploadFile(io.BytesIO(b"x" * 3000)), target, 1024))
    assert too_big.value.status_code == 413 and not target.exists()
    with pytest.raises(HTTPException) as empty:
        asyncio.run(save_upload(UploadFile(io.BytesIO(b"")), target, 1024))
    assert empty.value.status_code == 422 and not target.exists()
    asyncio.run(save_upload(UploadFile(io.BytesIO(b"ok")), target, 1024))
    assert target.read_bytes() == b"ok"
