"""下载固定上游版本和完整 SavedModel，校验 Git LFS SHA256；可重复执行。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

REVISION = "85cef72af9a916bdfd7cc94a670c9cdfbf12d1ed"
BASE = f"https://raw.githubusercontent.com/soCzech/TransNetV2/{REVISION}"
MEDIA = f"https://media.githubusercontent.com/media/soCzech/TransNetV2/{REVISION}"
WEIGHTS = (
    "transnetv2-weights/saved_model.pb",
    "transnetv2-weights/variables/variables.index",
    "transnetv2-weights/variables/variables.data-00000-of-00001",
)


def fetch(url):
    return urlopen(Request(url, headers={"User-Agent": "mindex-transnet-setup"}), timeout=120)


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def download(url, path, expected=None, size=None):
    if path.is_file() and expected and digest(path) == expected:
        print("已校验：", path.name)
        return expected
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            with fetch(url) as response:
                while chunk := response.read(1024 * 1024):
                    handle.write(chunk)
        actual = digest(temporary)
        if expected and actual != expected or size is not None and temporary.stat().st_size != size:
            raise ValueError(f"下载校验失败：{path.name}")
        temporary.replace(path)
        print("已下载：", path.name)
        return actual
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def main():
    root = Path(__file__).resolve().parent.parent
    inference = Path(
        os.environ.get("MINDEX_TRANSNET_DIR", str(root / "models/TransNetV2/inference"))
    ).resolve()
    files = {}
    files["transnetv2.py"] = download(
        f"{BASE}/inference/transnetv2.py",
        inference / "transnetv2.py",
        "f55b3a75727d1502438707ac15e8f6257a736817e713e2113e4b84176500ca65",
    )
    download(f"{BASE}/LICENSE", inference.parent / "LICENSE")
    for name in WEIGHTS:
        with fetch(f"{BASE}/inference/{name}") as response:
            pointer = response.read().decode("utf-8")
        fields = dict(line.split(" ", 1) for line in pointer.splitlines())
        if fields.get("version") != "https://git-lfs.github.com/spec/v1":
            raise ValueError(f"上游权重指针格式发生变化：{name}")
        expected = fields["oid"].removeprefix("sha256:")
        files[name] = download(f"{MEDIA}/inference/{name}", inference / name, expected, int(fields["size"]))
    manifest = {"repository": "https://github.com/soCzech/TransNetV2", "revision": REVISION, "sha256": files}
    (inference.parent / "mindex-install.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("上游权重完整校验通过：", inference)


if __name__ == "__main__":
    main()
