"""构建执行：单节点执行（execute）、整次构建（run_build）、取消与服务重启恢复。

节点执行与复用判断经模块属性调用（``execute``、``signatures.match``），便于测试替换；
``execute`` 按 ``stages.REGISTRY`` 分派到阶段处理器的 ``run``，阶段显示名取处理器的 ``label``。
"""

from __future__ import annotations

import json
import re
from uuid import uuid4

from mindex.shared.trace import current
from mindex.studio import executor, studio
from mindex.studio.studio_artifacts import safe_path, sanitized
from mindex.studio.studio_logs import ExecutionLog, active_log, diagnostic_event

from . import signatures
from .persistence import load
from .plans import board_snapshot, choose
from .preflight import node_dependencies, preflight
from .stages import RunContext, handler
from .versions import add_version, file_info, persist, plan, root


class VersionDiagnostics:
    def __init__(self, directory):
        self.directory, self.sequence = directory, 0

    def record(self, kind, value):
        self.sequence += 1
        studio.save(
            self.directory / "debug" / f"{self.sequence:04d}-{kind}.json",
            {"at": studio.now(), "data": sanitized(value)},
        )
        diagnostic_event(kind, value)


def execute(config, job_id, w, p, stage, sid=None, *, speaker=None, storyboarder=None, embedder=None,
            writer=None, video_generator=None, animator=None, animation_capture=None, cancelled=None,
            image_generator=None, music_generator=None):
    stage_handler = handler(stage)
    stage_handler.prepare(config, w, p, sid)
    spec = signatures.specification(config, job_id, w, p, stage, sid)
    vid = uuid4().hex
    directory = root(config, job_id) / "versions" / vid
    directory.mkdir(parents=True)
    diagnostics = VersionDiagnostics(directory)
    diagnostics.record("inputs", {"stage": stage, "shot_id": sid, **spec})
    token = current.set(diagnostics)
    try:
        data, files = stage_handler.run(RunContext(
            config=config, job_id=job_id, w=w, p=p, sid=sid, spec=spec, directory=directory,
            version_id=vid, diagnostics=diagnostics,
            services={"speaker": speaker, "storyboarder": storyboarder, "embedder": embedder, "writer": writer,
                      "video_generator": video_generator, "animator": animator,
                      "animation_capture": animation_capture, "cancelled": cancelled,
                      "image_generator": image_generator, "music_generator": music_generator},
        ))
        add_version(config, job_id, w, stage, sid, data=data, files=files, version_id=vid, **spec)
        if stage_handler.selects:
            choose(w, p, stage, sid, vid)
        persist(config, job_id, w)
        return vid
    except Exception as exc:
        # Preserve partial files and diagnostics, but never select a failed version.
        diagnostics.record("error", {"type": type(exc).__name__, "message": str(exc)})
        partial = {
            f.relative_to(directory).as_posix(): file_info(config, job_id, f)
            for f in directory.rglob("*")
            if f.is_file()
        }
        add_version(
            config,
            job_id,
            w,
            stage,
            sid,
            status="failed",
            error=str(exc)[:2000],
            files=partial,
            version_id=vid,
            **spec,
        )
        persist(config, job_id, w)
        raise
    finally:
        current.reset(token)


CANCELLED = "已取消"


def cancel_remaining(nodes):
    """取消时尚未开始的节点一律阻塞并注明已取消；已有结果的节点保持原记录。"""
    count = 0
    for node in nodes:
        if node.get("status") in (None, "pending"):
            node.update(status="blocked", error=CANCELLED)
            count += 1
    return count


def run_build(config, job_id, build_id, *, speaker=None, storyboarder=None, embedder=None, writer=None,
              video_generator=None, animator=None, animation_capture=None, cancel_event=None,
              image_generator=None, music_generator=None):
    log = ExecutionLog(config, job_id, build_id)
    token = active_log.set(log)
    try:
        return _run_build(
            config, job_id, build_id, log, speaker=speaker, storyboarder=storyboarder, embedder=embedder,
            writer=writer, video_generator=video_generator, animator=animator,
            animation_capture=animation_capture, cancel_event=cancel_event,
            image_generator=image_generator, music_generator=music_generator,
        )
    finally:
        active_log.reset(token)


def _run_build(config, job_id, build_id, log, *, speaker=None, storyboarder=None, embedder=None,
               writer=None, video_generator=None, animator=None, animation_capture=None, cancel_event=None,
               image_generator=None, music_generator=None):
    w = load(config, job_id)
    build = next(b for b in w["builds"] if b["id"] == build_id)
    p = plan(w, build["plan_id"])
    failed = set()
    cancelled = False

    def cancel_check():
        # 长时间运行的节点（视频生成轮询）在节点内部调用，取消请求不必等到节点结束。
        return executor.cancel_requested(config, "build", job_id, build_id, cancel_event)

    log.emit("开始执行")
    for position, node in enumerate(build["nodes"]):
        stage, sid = node["stage"], node["shot_id"]
        # 在节点之间检查取消：已开始执行的节点照常完成（生成画面在轮询之间自行检查并以「已取消」失败）。auto_split 重新预检替换
        # 后续节点后，下一轮迭代立即在这里检查，新展开的节点同样会被取消。
        if cancel_check():
            cancelled = True
            count = cancel_remaining(build["nodes"][position:])
            log.stage = log.shot_id = None
            log.emit(f"已取消执行：{count} 个节点未执行", "warning")
            break
        log.stage, log.shot_id = stage, sid
        label = handler(stage).label
        deps = node_dependencies(w, p, stage, sid) if stage != "split" else []
        if node["action"] == "skip":
            node["status"] = "skipped"
        elif node["action"] == "blocked" or any(dep in failed for dep in deps):
            node["status"] = "blocked"
            node["error"] = node.get("reason") or "依赖失败"
            failed.add(node["key"])
        else:
            node.update(status="running", started_at=studio.now())
            persist(config, job_id, w)
            log.emit(f"开始{label}")
            try:
                found = None if build["force"] else signatures.match(config, job_id, w, p, stage, sid)
                if found:
                    choose(w, p, stage, sid, found)
                    node.update(status="reused", version=found)
                else:
                    vid = execute(
                        config,
                        job_id,
                        w,
                        p,
                        stage,
                        sid,
                        speaker=speaker,
                        storyboarder=storyboarder,
                        embedder=embedder,
                        writer=writer,
                        video_generator=video_generator,
                        animator=animator,
                        animation_capture=animation_capture,
                        cancelled=cancel_check,
                        image_generator=image_generator,
                        music_generator=music_generator,
                    )
                    node.update(status="complete", version=vid)
            except Exception as exc:  # noqa: BLE001 — 失败节点不得阻断互不依赖的节点
                # 节点内部响应取消（生成画面轮询中）：记为已取消，其余未执行节点在下一轮统一阻塞。
                interrupted = str(exc) == CANCELLED and cancel_check()
                node.update(status="cancelled" if interrupted else "failed", error=str(exc)[:2000])
                attempted = next(
                    (
                        v["id"]
                        for v in reversed(list(w["versions"].values()))
                        if v["stage"] == stage
                        and v["shot_id"] == sid
                        and v["status"] == "failed"
                        and v["created_at"] >= node["started_at"]
                    ),
                    None,
                )
                if attempted:
                    node["version"] = attempted
                failed.add(node["key"])
        if build.get("auto_split") and stage in ("script", "split"):
            if node["status"] not in ("complete", "reused"):
                for pending in build["nodes"][position + 1:]:
                    pending.update(action="blocked", reason="生成文案失败" if stage == "script" else "拆分镜失败")
            elif stage == "split":
                # 分镜刚落定：按新分镜重新预检，替换其后全部节点（之前的节点保持原记录）。
                checked = preflight(config, job_id, w, p, force=build["force"])
                build["nodes"][position + 1:] = [
                    {**n, "status": "pending"} for n in checked["nodes"] if n["stage"] not in ("script", "split")
                ]
        node["finished_at"] = studio.now()
        persist(config, job_id, w)
        labels = {
            "complete": "完成",
            "reused": "复用成功版本",
            "failed": "失败",
            "blocked": "阻塞",
            "skipped": "跳过",
            "cancelled": CANCELLED,
        }
        log.emit(
            f"{label}：{labels.get(node['status'], node['status'])}"
            + (f" · {node['error']}" if node.get("error") else ""),
            "error" if node["status"] == "failed" else ("warning" if node["status"] == "blocked" else "info"),
        )
    successes = any(n["status"] in ("complete", "reused") for n in build["nodes"])
    if cancelled:
        status = "cancelled"
    else:
        status = ("partial" if successes else "failed") if failed else "complete"
    build.update(status=status, finished_at=studio.now())
    # 记录本次构建完成后的分镜工作台状态，便于日后切回此文案版本时还原分镜选用。
    build["board_result"] = board_snapshot(p)
    log.stage = log.shot_id = None
    log.emit(
        "执行结束："
        + {"complete": "全部完成", "partial": "部分完成", "failed": "失败", "cancelled": CANCELLED}[status],
        "warning" if status == "cancelled" else "info",
    )
    persist(config, job_id, w)
    executor.clear_cancel(config, "build", job_id, build_id)


def recover(config, job_id, w):
    changed = False
    running = [b for b in w["builds"] if b["status"] == "running"]
    # A killed process cannot write a terminal version record. Recover its recorded
    # inputs and partial files without changing any plan selection.
    if running:
        for directory in (root(config, job_id) / "versions").glob("*"):
            if not re.fullmatch(r"[0-9a-f]{32}", directory.name) or (directory / "version.json").exists():
                continue
            try:
                safe_path(root(config, job_id), directory.relative_to(root(config, job_id)).as_posix())
                record = json.loads((directory / "debug/0001-inputs.json").read_text(encoding="utf-8"))
                data = record["data"]
                node = next(
                    (
                        n
                        for b in running
                        for n in b["nodes"]
                        if n.get("status") == "running"
                        and n["stage"] == data["stage"]
                        and n["shot_id"] == data["shot_id"]
                        and record["at"] >= b["created_at"]
                    ),
                    None,
                )
                if node is None:
                    continue
                files = {
                    f.relative_to(directory).as_posix(): file_info(config, job_id, f)
                    for f in directory.rglob("*")
                    if f.is_file()
                }
                node["version"] = add_version(
                    config,
                    job_id,
                    w,
                    data["stage"],
                    data["shot_id"],
                    inputs=data["inputs"],
                    parameters=data["parameters"],
                    signature=data["signature"],
                    files=files,
                    status="failed",
                    error="服务重启中断执行",
                    version_id=directory.name,
                )
            except (OSError, ValueError, KeyError):
                continue
    for build in w["builds"]:
        if build["status"] == "running":
            for node in build["nodes"]:
                if node.get("status") in ("pending", "running"):
                    node.update(status="blocked", error="服务重启中断，请重新预检续建")
            build.update(status="partial", finished_at=studio.now())
            # 重启前残留的取消请求已无意义：构建已按中断收尾。
            executor.clear_cancel(config, "build", job_id, build["id"])
            ExecutionLog(config, job_id, build["id"]).emit("服务中断，请重新提交执行", "error")
            changed = True
    if changed:
        persist(config, job_id, w)
