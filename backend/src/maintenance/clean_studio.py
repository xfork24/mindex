"""studio 任务产物清理：预览缓存、旧 attempt 留档、旧导出 zip。

attempt「不可变留档」与磁盘压力的折衷：checkpoints.json 引用的 attempt 与各步骤当前
attempt 一律保留，其余按 mtime 过期删除；execution/step running 的任务整体跳过。
"""

from __future__ import annotations

import json
import shutil

from maintenance.clean import CleanAction, CleanItem, Scan, _tree_stats


def _jobs(scan: Scan) -> list[tuple[object, dict | None]]:
    """(任务目录, job.json 状态) 列表；job.json 缺失/损坏按 None 处理。"""
    root = scan.store.root / "studio"
    if not root.is_dir():
        return []
    jobs = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        manifest = directory / "job.json"
        state = None
        if manifest.is_file():
            try:
                state = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = None
        jobs.append((directory, state))
    return jobs


def _running(state: dict | None) -> bool:
    if not isinstance(state, dict):
        return False
    if state.get("execution", {}).get("status") == "running":
        return True
    steps = state.get("steps", {})
    return any(isinstance(info, dict) and info.get("status") == "running" for info in steps.values())


def studio_previews(scan: Scan) -> CleanAction | None:
    days = scan.config.clean.studio_preview_max_age_days
    items: list[CleanItem] = []
    targets = []
    for directory, _ in _jobs(scan):
        previews = directory / "previews"
        if not previews.is_dir():
            continue
        for path in sorted(previews.rglob("*")):
            if not path.is_file() or not scan.expired(path, days):
                continue
            targets.append(path)
            items.append(CleanItem(path.relative_to(scan.store.root).as_posix(), path.stat().st_size))

    def execute() -> dict:
        for path in targets:
            path.unlink(missing_ok=True)
        return {"deleted": len(targets)}

    if not items:
        return None
    return CleanAction(
        category="studio_previews",
        level="B",
        stage="studio",
        description="超过生命期的分镜/方案预览缓存",
        policy=f"mtime 早于 {days} 天；预览按请求指纹命名，可按需重生成",
        items=items,
        execute=execute,
    )


def studio_attempts(scan: Scan) -> CleanAction | None:
    days = scan.config.clean.studio_attempt_max_age_days
    items: list[CleanItem] = []
    targets: list[object] = []  # Path 列表（目录 + 结果 json）
    running_jobs: list[str] = []
    for directory, state in _jobs(scan):
        attempts = directory / "attempts"
        if not attempts.is_dir():
            continue
        if _running(state):
            running_jobs.append(directory.name)
            continue
        keep = _kept_attempts(directory, state)
        expired_dirs = []
        for child in sorted(attempts.iterdir()):
            if not child.is_dir() or child.name in keep:
                continue
            if not scan.expired(child, days):
                continue
            expired_dirs.append(child)
        for child in expired_dirs:
            targets.append(child)
            size, _ = _tree_stats(child)
            items.append(CleanItem(child.relative_to(scan.store.root).as_posix(), size))
            sidecar = child.with_name(child.name + ".json")
            if sidecar.is_file() and scan.expired(sidecar, days):
                targets.append(sidecar)
                items.append(
                    CleanItem(sidecar.relative_to(scan.store.root).as_posix(), sidecar.stat().st_size)
                )

    def execute() -> dict:
        deleted = 0
        for path in targets:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            deleted += 1
        return {"deleted": deleted}

    if not items and not running_jobs:
        return None
    return CleanAction(
        category="studio_attempts",
        level="B",
        stage="studio",
        description="未被检查点或当前 attempt 引用的过期 attempt 留档",
        policy=f"checkpoints.json 引用与各步骤当前 attempt 一律保留，其余树内最新文件早于 "
        f"{days} 天删除（含 edit-* 人工编辑归档）；running 任务整体跳过",
        items=items,
        kept={"running_jobs": running_jobs},
        execute=execute,
    )


def _kept_attempts(directory, state: dict | None) -> set[str]:
    """需要保留的 attempts/ 子目录名：各步骤当前 attempt + 检查点注册表引用。"""
    keep: set[str] = set()
    if isinstance(state, dict):
        for step, info in state.get("steps", {}).items():
            if isinstance(info, dict) and info.get("attempt"):
                keep.add(f"{step}-{info['attempt']}")
    try:
        registry = json.loads((directory / "checkpoints.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        registry = {}
    for item in registry.values() if isinstance(registry, dict) else []:
        if not isinstance(item, dict):
            continue
        parts = str(item.get("path", "")).split("/")
        if len(parts) >= 2 and parts[0] == "attempts":
            keep.add(parts[1])
    return keep


def studio_exports(scan: Scan) -> CleanAction | None:
    days = scan.config.clean.studio_export_max_age_days
    items: list[CleanItem] = []
    targets = []
    for directory, _ in _jobs(scan):
        exports = directory / "exports"
        if not exports.is_dir():
            continue
        for path in sorted(exports.iterdir()):
            if not path.is_file() or not (path.name.endswith(".zip") or path.name.endswith(".zip.partial")):
                continue
            if not scan.expired(path, days):
                continue
            targets.append(path)
            items.append(CleanItem(path.relative_to(scan.store.root).as_posix(), path.stat().st_size))

    def execute() -> dict:
        for path in targets:
            path.unlink(missing_ok=True)
        return {"deleted": len(targets)}

    if not items:
        return None
    return CleanAction(
        category="studio_exports",
        level="B",
        stage="studio",
        description="超过生命期的整包导出 zip",
        policy=f"mtime 早于 {days} 天；页面可随时重新打包导出",
        items=items,
        execute=execute,
    )


RULES = [studio_previews, studio_attempts, studio_exports]
