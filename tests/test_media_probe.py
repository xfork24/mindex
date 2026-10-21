"""media.probe 的有界缓存：同一（路径、大小、mtime）只探测一次，失败与文件变更不粘住。"""

from __future__ import annotations

import os
import shutil

import pytest

from mindex.shared import media


def test_probe_cache_hits_and_invalidates(video_file, tmp_path, monkeypatch):
    calls = []
    original = media.command

    def counting(args, **kwargs):
        calls.append(args[0])
        return original(args, **kwargs)

    monkeypatch.setattr(media, "command", counting)
    clip = tmp_path / "clip.mp4"
    shutil.copyfile(video_file, clip)
    first = media.probe(clip)
    second = media.probe(clip)
    assert second == first and second is not first  # 返回副本，调用方改不动缓存。
    assert calls.count("ffprobe") == 1
    os.utime(clip, ns=(0, 0))  # mtime 变化 → 缓存失效重新探测。
    media.probe(clip)
    assert calls.count("ffprobe") == 2


def test_probe_failure_not_cached(video_file, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not a video")
    with pytest.raises(media.MediaError):
        media.probe(clip)
    shutil.copyfile(video_file, clip)  # 大小与 mtime 已变，必须能重新探测成功。
    assert media.probe(clip)["duration"] > 0
