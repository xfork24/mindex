"""浏览器的增量摘要、已登记分片重用与正式登记协议。"""
import subprocess
from pathlib import Path


def test_browser_upload_protocol_and_incremental_hash():
    root = Path(__file__).resolve().parents[1]
    subprocess.run(["node", str(root / "tests/upload_sessions_ui.cjs")], cwd=root, check=True, capture_output=True)
