"""创作检查点与不可变 attempt 产物；当前逻辑路径保持向后兼容。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from mindex.shared.config import fingerprint, portable_parameters
from mindex.studio import studio


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    target = root / relative
    if Path(relative).is_absolute() or not target.resolve().is_relative_to(root.resolve()):
        raise studio.StudioError("产物路径必须位于任务目录内")
    return target


def sanitized(value):
    if isinstance(value, dict):
        return {
            k: sanitized(v)
            for k, v in value.items()
            if k.lower() not in {"authorization", "headers", "api_key", "password", "token"}
        }
    if isinstance(value, (list, tuple)):
        return [sanitized(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        url = urlsplit(value)
        return urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))
    return value


class ArtifactSession:
    def __init__(self, config, job_id: str, step: str, state: dict, *, mode="resume", shot_ids=None):
        self.root = studio.job_dir(config, job_id)
        self.step, self.mode, self.shot_ids = step, mode, shot_ids
        self.attempt = state["steps"][step]["attempt"]
        self.directory = safe_path(self.root, f"attempts/{step}-{self.attempt}")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.registry_path = safe_path(self.root, "checkpoints.json")
        try:
            self.registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.registry = {}
        self.outputs = {}
        self.sequence = 0
        self.record(
            "config",
            {
                "studio": portable_parameters(config, config.studio.model_dump()),
                "search": config.search.model_dump(),
                "video": config.video.model_dump(),
                "embedding": portable_parameters(config, config.embedding.model_dump()),
                "options": state["options"],
                "mode": mode,
                "shot_ids": shot_ids,
            },
        )

    def record(self, kind: str, value):
        self.sequence += 1
        studio.save(
            self.directory / "debug" / f"{self.sequence:04d}-{kind}.json",
            {"at": studio.now(), "data": sanitized(value)},
        )

    def forced(self, logical: str) -> bool:
        shot_id = Path(logical).name.split(".")[0]
        return self.mode == "force" and (self.shot_ids is None or shot_id in self.shot_ids)

    def reuse(self, logical: str, key: str) -> Path | None:
        if self.forced(logical):
            return None
        item = self.registry.get(logical)
        if not item or item.get("key") != key:
            return None
        try:
            path = safe_path(self.root, item["path"])
            if not path.is_file() or digest(path) != item["sha256"]:
                return None
        except (OSError, KeyError, studio.StudioError):
            return None
        self._alias(logical, path)
        self.outputs[logical] = item
        self.record("reuse", {"logical": logical, **item})
        return path

    def target(self, logical: str) -> Path:
        path = safe_path(self.directory, logical)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _alias(self, logical: str, path: Path):
        alias = safe_path(self.root, logical)
        alias.parent.mkdir(parents=True, exist_ok=True)
        temporary = alias.with_name(f".{alias.name}-{uuid4().hex}.partial")
        try:
            try:
                os.link(path, temporary)
            except OSError:
                shutil.copy2(path, temporary)
            temporary.replace(alias)
        finally:
            temporary.unlink(missing_ok=True)

    def publish(self, logical: str, path: Path, key: str, **metadata) -> Path:
        target = self.target(logical)
        if path.resolve() != target.resolve():
            shutil.copy2(path, target)
        item = {
            "key": key,
            "path": target.relative_to(self.root).as_posix(),
            "sha256": digest(target),
            "bytes": target.stat().st_size,
            "step": self.step,
            "attempt": self.attempt,
            **metadata,
        }
        self._alias(logical, target)
        self.registry[logical] = item
        self.outputs[logical] = item
        studio.save(self.registry_path, self.registry)
        self.record("output", {"logical": logical, **item})
        return target

    def json(self, logical: str, value, key: str | None = None) -> Path:
        path = self.target(logical)
        studio.save(path, value)
        return self.publish(logical, path, key or fingerprint(value))

    def finish(self):
        studio.save(
            self.directory / "manifest.json",
            {"step": self.step, "attempt": self.attempt, "outputs": self.outputs},
        )


def visibility_key(store) -> str:
    return fingerprint(
        {
            "sources": store.rows("SELECT id,hidden FROM source_curation ORDER BY id"),
            "segments": store.rows("SELECT id,hidden FROM segment_curation ORDER BY id"),
        }
    )


def visibility_key_cached(store, ttl: float = 1.0) -> str:
    """``visibility_key`` 的短 TTL 缓存版本：两次全表扫描在只读展示热路径上去重。"""
    cached = getattr(store, "_hot_cached", None)
    if cached is None:
        return visibility_key(store)
    return cached("visibility_key", ttl, lambda: visibility_key(store))
