"""ffmpeg/ffprobe 定位：``MINDEX_FFMPEG``/``MINDEX_FFPROBE`` 覆盖 PATH，且不影响签名。"""

from __future__ import annotations

import shutil
import stat
import sys
from pathlib import Path

import pytest

from shared import media
from studio import workspace as ws
from test_studio_ai_media import FakeImageGenerator, run, setup, shot


def wrapper(directory: Path, name: str, log: Path) -> Path:
    """记录每次调用后转交给真实二进制的包装脚本（绝对路径，不依赖 PATH）。"""
    real = shutil.which(name)
    assert real, f"测试需要 {name}"
    path = directory / f"wrapped-{name}"
    if sys.platform == "win32":
        # Windows 没有 shebang 机制：包装脚本用批处理（%* 原样转交全部参数）。
        path = path.with_suffix(".bat")
        path.write_text(f'@echo off\r\necho {name} %* >> "{log}"\r\n"{real}" %*\r\n')
        return path
    path.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexec "{real}" "$@"\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_defaults_unchanged(monkeypatch):
    monkeypatch.delenv("MINDEX_FFMPEG", raising=False)
    monkeypatch.delenv("MINDEX_FFPROBE", raising=False)
    assert media.ffmpeg_bin() == "ffmpeg" and media.ffprobe_bin() == "ffprobe"
    assert media.resolve_tool(["ffprobe", "-v"]) == ["ffprobe", "-v"]
    assert media.resolve_tool(["llama-server"]) == ["llama-server"]
    monkeypatch.setenv("MINDEX_FFMPEG", "  ")  # 空白视为未设置
    assert media.ffmpeg_bin() == "ffmpeg"


def test_missing_configured_binary_reports_path(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDEX_FFPROBE", str(tmp_path / "nope"))
    with pytest.raises(media.MediaError, match="nope"):
        media.command(["ffprobe", "-version"])


@pytest.mark.slow
def test_build_uses_configured_binaries_without_path(media_config, monkeypatch, tmp_path):
    """整条构建（配音→生成图片→渲染→合成）只经配置的二进制调用 ffmpeg/ffprobe，PATH 中没有它们也能完成，
    且签名与未配置时相同。"""
    shots = [
        shot("a", "红色开场。", source={"kind": "aigc_image"}),
        shot("b", "蓝色结尾。", source={"kind": "aigc_image"}),
    ]
    job_id, w, p = setup(media_config, shots)
    before = ws.specification(media_config, job_id, w, p, "compose")["signature"]
    log = tmp_path / "calls.log"
    monkeypatch.setenv("MINDEX_FFMPEG", str(wrapper(tmp_path, "ffmpeg", log)))
    monkeypatch.setenv("MINDEX_FFPROBE", str(wrapper(tmp_path, "ffprobe", log)))
    # 空 PATH：Linux CI 的 ffmpeg 就装在 /usr/bin，不能用系统目录构造「PATH 里没有 ffmpeg」。
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert shutil.which("ffmpeg") is None and shutil.which("ffprobe") is None
    assert ws.specification(media_config, job_id, w, p, "compose")["signature"] == before
    w, p = run(media_config, job_id, w, p, images=FakeImageGenerator())
    assert w["builds"][-1]["status"] == "complete", f"{w['builds'][-1]}"
    calls = log.read_text().splitlines()
    assert any(line.startswith("ffmpeg ") for line in calls) and any(
        line.startswith("ffprobe ") for line in calls
    )


@pytest.fixture
def media_config(config):
    config.studio.render.width = 160
    config.studio.render.height = 120
    config.studio.render.preset = "ultrafast"
    config.studio.render.fps = 25
    config.studio.image_generator.enabled = True
    return config
