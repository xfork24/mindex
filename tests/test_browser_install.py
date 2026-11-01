"""AI 动画录制组件的检测与按需安装：用替代命令验证进度、成功与失败，不下载真实浏览器。"""

from __future__ import annotations

import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from mindex.api import create_app
from mindex.settings import browser_install
from mindex.settings.browser_install import BrowserInstaller
from mindex.studio import animation_capture

SUCCESS = [sys.executable, "-c",
           "import os, pathlib, sys\n"
           "root = pathlib.Path(os.environ['PLAYWRIGHT_BROWSERS_PATH'])\n"
           "print('Downloading Chrome Headless Shell 10%\\r 50%\\r 100%', flush=True)\n"
           "target = root / ('chromium_headless_shell-' + sys.argv[1])\n"
           "target.mkdir(parents=True)\n"
           "(target / 'INSTALLATION_COMPLETE').write_text('')\n"
           "print('done', flush=True)\n"]


def test_install_success_reports_progress_and_installed(monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
    monkeypatch.setattr(browser_install, "expected_revision", lambda: "9999")
    installer = BrowserInstaller([*SUCCESS, "9999"])
    before = installer.state()
    assert (before["installed"], before["task"]["status"], before["path"]) == (False, "idle", str(tmp_path / "browsers"))
    assert installer.start()["task"]["status"] == "running"
    installer.wait(30)
    state = installer.state()
    assert state["installed"] is True and state["task"]["status"] == "complete" and state["task"]["error"] is None
    # 探针：同一替身以 subprocess.run 读法对照，区分「替身 stdout 丢失」与「Popen 逐行读取丢失」。
    probe = subprocess.run([*SUCCESS, "9999"], capture_output=True, timeout=60, check=False)
    assert state["task"]["log"].splitlines() == ["100%", "done"], (state["task"], probe.stdout, probe.stderr)  # \r 进度只保留最后一段
    assert state["task"]["finished_at"]


def test_install_failure_and_concurrency(monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
    monkeypatch.setattr(browser_install, "expected_revision", lambda: "9999")
    failing = BrowserInstaller([sys.executable, "-c", "import sys, time; print('network down'); time.sleep(0.5); sys.exit(7)"])
    failing.start()
    with pytest.raises(RuntimeError, match="正在安装"):
        failing.start()
    failing.wait(30)
    state = failing.state()
    assert (state["installed"], state["task"]["status"]) == (False, "failed")
    assert "退出码 7" in state["task"]["error"] and "network down" in state["task"]["log"], state["task"]
    missing = BrowserInstaller(["/nonexistent/installer"])
    missing.start()
    missing.wait(30)
    assert missing.state()["task"]["status"] == "failed" and "无法安装" in missing.state()["task"]["error"]


def test_api_and_installed_detection(config, monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
    monkeypatch.setattr(browser_install, "expected_revision", lambda: "1234")
    monkeypatch.setattr(browser_install, "_INSTALLER", BrowserInstaller([*SUCCESS, "1234"]))
    client = TestClient(create_app(config))
    assert client.get("/api/settings/animation-browser").json()["installed"] is False
    assert client.post("/api/settings/animation-browser/install").status_code == 202
    browser_install.installer().wait(30)
    body = client.get("/api/settings/animation-browser").json()
    assert body["installed"] is True and body["revision"] == "1234" and body["available"] is True


def test_desktop_install_hint(monkeypatch):
    monkeypatch.delenv("MINDEX_DESKTOP", raising=False)
    assert animation_capture.install_hint() == animation_capture.INSTALL_HINT
    monkeypatch.setenv("MINDEX_DESKTOP", "1")
    assert animation_capture.install_hint() == animation_capture.DESKTOP_INSTALL_HINT
    assert "设置页" in animation_capture.DESKTOP_INSTALL_HINT
