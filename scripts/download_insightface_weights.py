"""下载官方 model-zoo 的完整 cheetah_l V2 模型包，按包内 manifest 校验 SHA-256；可重复执行。

仅用标准库，任意 Python 3.11+ 均可运行（不依赖 .venv-person）。安装位置由
MINDEX_PERSON_MODEL_ROOT 决定，默认 <仓库>/models/insightface，包落在其 models/cheetah_l/。
损坏的已有包会被改名保留为 .invalid-<hex>，不会自动删除。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen

MODEL = "cheetah_l"
ARCHIVE_URL = f"https://github.com/deepinsight/insightface/releases/download/model-zoo/{MODEL}.zip"
REQUIRED_TASKS = ("person_detection", "person_reid", "detection", "recognition")


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def download(url, path):
    request = Request(url, headers={"User-Agent": "mindex-insightface-setup"})
    with urlopen(request, timeout=120) as response, path.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
    print("已下载：", path.name)


def extract(archive, destination):
    """拒绝越界、符号链接或重复条目后再解压（与上游 SDK 的安全检查一致）。"""
    with zipfile.ZipFile(archive) as bundle:
        seen = set()
        for member in bundle.infolist():
            name = member.orig_filename
            entry = PurePosixPath(name)
            if (
                not entry.parts
                or entry.is_absolute()
                or ".." in entry.parts
                or "\\" in name
                or ":" in name
                or "\x00" in name
                or stat.S_ISLNK(member.external_attr >> 16)
                or entry in seen
            ):
                raise ValueError(f"不安全的压缩条目：{name!r}")
            seen.add(entry)
        bundle.extractall(destination)


def verify(package):
    """按服务的同一套规则校验包：唯一 manifest、V2、四类任务、逐文件 SHA-256。"""
    manifests = list(package.glob("*manifest*.json"))
    if len(manifests) != 1:
        raise ValueError(f"需要唯一的 manifest JSON，找到 {len(manifests)} 个")
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    tasks = manifest.get("tasks", {})
    if manifest.get("manifest_version") != 2 or not set(REQUIRED_TASKS) <= tasks.keys():
        raise ValueError("模型包缺少 V2 manifest 或四类人脸/人体任务")
    if manifest.get("model_id", MODEL) != MODEL:
        raise ValueError("manifest 的 model_id 与包名不符")
    hashes = {}
    for task in REQUIRED_TASKS:
        declared = tasks[task]
        file = (package / declared["file"]).resolve()
        if not file.is_relative_to(package.resolve()) or not file.is_file() or not declared.get("sha256"):
            raise ValueError(f"模型文件缺失或未声明 sha256：{task}")
        actual = digest(file)
        if actual != declared["sha256"]:
            raise ValueError(f"模型文件摘要不符：{task}")
        hashes[task] = actual
    return {"manifest_sha256": digest(manifests[0]), "tasks": hashes}


def main():
    root = (
        Path(
            os.environ.get(
                "MINDEX_PERSON_MODEL_ROOT", str(Path(__file__).resolve().parent.parent / "models/insightface")
            )
        )
        .expanduser()
        .resolve()
    )
    package = root / "models" / MODEL
    if package.is_dir():
        try:
            print("已校验：", verify(package))
            return
        except ValueError as error:
            aside = package.with_name(f"{package.name}.invalid-{time.strftime('%Y%m%d%H%M%S')}")
            package.rename(aside)
            print(f"已有包校验失败（{error}），已改名保留：{aside.name}")

    package.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{MODEL}-install-", dir=package.parent) as temporary:
        staging = Path(temporary)
        archive = staging / f"{MODEL}.zip"
        download(ARCHIVE_URL, archive)
        unpacked = staging / "unpacked"
        extract(archive, unpacked)
        candidate = unpacked if (unpacked / "manifest.json").is_file() else unpacked / MODEL
        result = verify(candidate)
        candidate.rename(package)
        provenance = {
            "repository": "https://github.com/deepinsight/insightface",
            "url": ARCHIVE_URL,
            "archive_sha256": digest(archive),
            **result,
        }
        (package.parent / "mindex-install.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print("cheetah_l V2 模型包安装完成：", package)
    print("启动服务：./scripts/run-person-service.sh（由 setup-person-service.sh 完成引擎验证）")


if __name__ == "__main__":
    main()
