"""版本记录层：任务目录、workspace.json 落盘、方案与版本查找、不可变版本与文件校验。

工作台的最底层，其余模块都建立在这里；不依赖包内其他模块。
"""

from __future__ import annotations

import json
from uuid import uuid4

from mindex.shared.config import fingerprint
from mindex.studio import studio
from mindex.studio.studio_artifacts import digest, safe_path, sanitized

STAGES = ("recall", "tts", "select", "render")
# 方案级前置阶段：文案（script）→ 拆分镜（split）；二者都不属于单个分镜。
SCRIPT_ORIGINS = ("generated", "manual", "legacy")


def root(config, job_id):
    return studio.job_dir(config, job_id)


def path(config, job_id):
    return root(config, job_id) / "workspace.json"


class RevisionConflict(studio.StudioError):
    """工作区/方案已被其他操作修改（条件发布失败，映射为 409）。"""


def current_revision(config, job_id):
    """磁盘上工作区的当前修订；不存在或损坏返回 None。"""
    file = path(config, job_id)
    if not file.is_file():
        return None
    try:
        return revision(json.loads(file.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def persist(config, job_id, workspace, *, expected_revision=None):
    """条件写入工作区：``expected_revision`` 与磁盘当前修订不符时抛 RevisionConflict。

    读-比对-写在进程内与跨进程资源锁内完成（``workspace:<job_id>``），因此并发
    写者不会把旧快照覆盖到新状态；调用方已持 studio 锁时也不会自锁（不同资源）。
    """
    from mindex.shared import lock_backend

    with lock_backend.resource(config, f"workspace:{job_id}", timeout=None):
        if expected_revision is not None:
            current = current_revision(config, job_id)
            if current != expected_revision:
                raise RevisionConflict("工作区已被其他操作修改，请刷新后重试")
        workspace["updated_at"] = studio.now()
        studio.save(path(config, job_id), workspace)


def version(workspace, version_id):
    result = workspace["versions"].get(version_id)
    if result is None:
        raise studio.StudioError("找不到产物版本")
    return result


def plan(workspace, plan_id):
    result = workspace["plans"].get(plan_id)
    if result is None:
        raise studio.StudioError("找不到构建方案")
    return result


def revision(p):
    return fingerprint(p)


def require_revision(p, expected):
    if expected != revision(p):
        raise studio.StudioError("方案已变化，请刷新后重新预检或提交")


def require_workspace_revision(w, expected):
    """条件发布（If-Match）：``expected`` 为 None 时不校验；不符抛 RevisionConflict（409）。"""
    if expected is not None and expected != revision(w):
        raise RevisionConflict("工作区已被其他操作修改，请刷新后重试")


def add_version(
    config,
    job_id,
    w,
    stage,
    shot_id=None,
    *,
    data=None,
    inputs=None,
    parameters=None,
    signature=None,
    files=None,
    status="complete",
    error=None,
    incomplete=False,
    version_id=None,
):
    vid = version_id or uuid4().hex
    item = {
        "id": vid,
        "stage": stage,
        "shot_id": shot_id,
        "created_at": studio.now(),
        "status": status,
        "inputs": inputs or {},
        "parameters": sanitized(parameters or {}),
        "signature": signature,
        "data": data or {},
        "files": files or {},
        "error": error,
        "incomplete": incomplete,
    }
    target = root(config, job_id) / "versions" / vid / "version.json"
    if target.exists():
        raise studio.StudioError("不可变版本已存在")
    studio.save(target, item)
    w["versions"][vid] = item
    return vid


def file_info(config, job_id, target):
    safe_path(root(config, job_id), target.relative_to(root(config, job_id)).as_posix())
    return {
        "path": target.relative_to(root(config, job_id)).as_posix(),
        "sha256": digest(target),
        "bytes": target.stat().st_size,
    }


def file_path(config, job_id, item, name):
    metadata = item["files"].get(name)
    if not metadata:
        raise studio.StudioError(f"版本 {item['id']} 缺少 {name}")
    target = safe_path(root(config, job_id), metadata["path"])
    if not target.is_file() or digest(target) != metadata["sha256"]:
        raise studio.StudioError(f"版本 {item['id']} 文件损坏或缺失")
    return target


def valid_files(config, job_id, item):
    try:
        for name in item["files"]:
            file_path(config, job_id, item, name)
        return True
    except (OSError, studio.StudioError):
        return False


def ordered_shots(w, p):
    if not p["board"]:
        return []
    return version(w, p["board"])["data"]["order"]
