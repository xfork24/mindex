"""生成画面阶段（逐镜，aigc 来源）：向视频生成服务提交任务、轮询（退避 + 总超时）、下载并留档。

- 时长：配音时长 ÷ 方案变速，向上取整到 ``studio.video_generator.durations`` 中的允许值（都更短时取最大值，
  不足部分由选片阶段冻结末帧补足）；
- 签名：提示词（缺省画面意象，再缺省召回查询）、反向提示词、seed、参考图摘要、时长、画幅尺寸，以及生成服务的
  backend/model/允许时长（ComfyUI 另含 fps、工作流摘要与节点映射）；不含密钥、超时与轮询参数；
- 轮询之间检查构建取消：取消时尽力撤销远端任务并以「已取消」失败；
- 产物：``versions/<vid>/generated.mp4`` 与 ``request.json``（脱敏请求、任务 ID 与最后状态，无密钥）。
  失败时 request.json 与诊断随失败版本保留。
"""

from __future__ import annotations

import time

from mindex.models.adapters import ModelError, make_video_generator
from mindex.models.video_generators import VideoContentRejected, VideoRequest, allowed_seconds
from mindex.shared.media import MediaError, atomic_media, probe
from mindex.studio import studio
from mindex.studio.studio_artifacts import sanitized
from mindex.studio.studio_logs import active_log

from .. import sources
from ..plans import canvas_render
from ..stages import StageHandler, register, shot_content
from ..versions import file_info

GENERATE_VERSION = 1
CANCELLED = "已取消"
NOT_CONFIGURED = "未配置视频生成服务，请在设置页激活视频生成档案"
POLL_ERRORS = 3  # 轮询连续失败（网络抖动等）的容忍次数；审核拒绝立即失败
STATE_LABELS = {"queued": "排队中", "running": "生成中", "succeeded": "已完成", "failed": "失败"}


MODE_LABELS = {"text": "文生视频", "first_frame": "首帧图生视频", "last_frame": "尾帧图生视频",
               "first_last": "首尾帧生视频", "reference": "主体参考生视频"}
# 只有 MiniMax H3（V2 多模态）支持尾帧、首尾帧与主体参考；其余后端只支持文生与首帧图生。
BASIC_MODES = ("text", "first_frame")


def source_mode(source) -> str:
    if source.get("mode"):
        return source["mode"]
    return "first_frame" if source.get("image_id") else "text"


def unavailable(config, source=None) -> str | None:
    """视频生成服务不可用的原因；可用返回 None。给出 ``source`` 时同时检查后端是否支持其生成方式。"""
    cfg = config.studio.video_generator
    if not cfg.enabled:
        return NOT_CONFIGURED
    if cfg.backend == "comfyui":
        if cfg.comfyui_workflow is None or not cfg.comfyui_mapping:
            return "ComfyUI 视频生成档案未导入工作流或节点映射，请在设置页补全"
    elif not cfg.model.strip() and cfg.backend != "minimax_h3":
        return "视频生成档案未填写模型名，请在设置页补全"
    if source is not None and cfg.backend != "minimax_h3" and source_mode(source) not in BASIC_MODES:
        return (f"当前视频生成档案（{cfg.backend}）不支持{MODE_LABELS[source_mode(source)]}，"
                "请在设置页激活 MiniMax H3 视频生成档案，或把生成方式改为文生/首帧图生视频")
    return None


def generation_request(config, w, p, sid) -> dict:
    """签名载荷与提交参数的唯一来源；配音尚未完成时 seconds 为 None。"""
    selected, content = shot_content(w, p, sid)
    source = content.get("source") or {}
    prompt = source.get("prompt") or (content.get("visual") or "").strip() or content["query"]
    speech = w["versions"].get(selected.get("tts"))
    seconds = None
    if speech and speech["status"] == "complete" and speech["data"].get("duration"):
        speed = p["options"]["atempo"] or 1.0
        seconds = allowed_seconds(config.studio.video_generator.durations, speech["data"]["duration"] / speed)
        if source.get("duration"):
            seconds = float(source["duration"])  # 逐镜指定时长（MiniMax H3）：不足部分由选片冻结末帧补足
    render = canvas_render(config, p)[0]
    request = {
        "prompt": prompt,
        "negative": source.get("negative"),
        "seed": source.get("seed"),
        "image": source.get("image_id"),
        "seconds": seconds,
        "width": render.width,
        "height": render.height,
    }
    # MiniMax H3 的逐镜选项只在设置时加键：老 aigc 分镜的签名与旧版逐字节一致。
    for key, value in (("mode", source.get("mode")), ("last_image", source.get("last_image_id")),
                       ("references", [[item["kind"], item["media_id"]] for item in source.get("references") or []]),
                       ("model", source.get("model")), ("resolution", source.get("resolution")),
                       ("ratio", source.get("ratio"))):
        if value:
            request[key] = value
    return request


def wait_for(generator, task_id, cfg, cancelled, notify, *, clock=time.monotonic, sleep=time.sleep):
    """轮询到终态：间隔从 poll_interval 起按 1.5 倍退避（上限 4 倍），总时长不超过 max_wait。

    等待期间每 0.2 秒检查一次取消；取消与超时都会尽力撤销远端任务。"""
    deadline = clock() + cfg.max_wait
    interval = cfg.poll_interval
    polls = errors = 0

    def stop(message):
        try:
            generator.cancel(task_id)
        except Exception:  # noqa: BLE001 — 撤销只是尽力而为，不能掩盖取消/超时本身
            pass
        raise studio.StudioError(message)

    while True:
        if cancelled():
            stop(CANCELLED)
        try:
            status = generator.poll(task_id)
            errors = 0
        except VideoContentRejected:
            raise
        except ModelError:
            errors += 1
            if errors >= POLL_ERRORS:
                raise
            status = None
        polls += 1
        if status is not None:
            notify(status, polls)
            if status.state == "succeeded":
                return status, polls
            if status.state == "failed":
                raise studio.StudioError(f"视频生成失败：{status.error or '服务未说明原因'}")
        remaining = deadline - clock()
        if remaining <= 0:
            stop(f"视频生成超时：{cfg.max_wait:g} 秒内未完成，已尝试撤销任务")
        end = clock() + min(interval, remaining)
        while (left := end - clock()) > 0:
            if cancelled():
                stop(CANCELLED)
            sleep(min(0.2, left))
        interval = min(interval * 1.5, cfg.poll_interval * 4)


def normalize(download, target):
    """下载结果统一为 MP4：能直接封装就不重编码，否则转 H.264（ComfyUI 可能输出 webm/gif/webp）。"""
    maps = ["-map", "0:v:0", "-map", "0:a:0?"]
    try:
        atomic_media(["-i", str(download), *maps, "-c", "copy", "-movflags", "+faststart"], target)
    except MediaError:
        atomic_media(["-i", str(download), *maps, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                      "-crf", "18", "-c:a", "aac", "-movflags", "+faststart"], target)


class GenerateHandler(StageHandler):
    name, label, scope = "generate", "生成画面", "shot"

    def settings(self, config, p, *, store=None, display=False):
        return {"generator": config.studio.video_generator.signature()}

    def specification(self, config, job_id, w, p, sid, params, resources):
        selected, _ = shot_content(w, p, sid)
        inputs = {"content": selected["content"], "tts": selected.get("tts")}
        payload = {**generation_request(config, w, p, sid), "generate_version": GENERATE_VERSION}
        return payload, inputs, params

    def dependencies(self, w, p, sid, *, resplit=False):
        return [f"tts:{sid}"]

    def preflight(self, config, job_id, w, p, sid, spec, found, node):
        if found:
            return
        source = sources.plan_source(w, p, sid) or {}
        reason = unavailable(config, source)
        if reason is None:
            reason = sources.missing_reference(config, job_id, source)
        if reason:
            node.update(action="blocked", reason=reason)

    def run(self, ctx):
        cfg = ctx.config.studio.video_generator
        source = sources.plan_source(ctx.w, ctx.p, ctx.sid) or {}
        generator = ctx.services.get("video_generator")
        if generator is None:
            if reason := unavailable(ctx.config, source):
                raise studio.StudioError(reason)
            generator = make_video_generator(ctx.config)
        cancelled = ctx.services.get("cancelled") or (lambda: False)
        request = generation_request(ctx.config, ctx.w, ctx.p, ctx.sid)
        if request["seconds"] is None:
            raise studio.StudioError("缺少成功配音，无法确定生成时长")
        image = sources.reference_image(ctx.config, ctx.job_id, source)
        last_image = sources.reference_image(ctx.config, ctx.job_id, source, "last_image_id")
        references = tuple((item["kind"], sources.reference_file(ctx.config, ctx.job_id, item["kind"], item["media_id"]))
                           for item in source.get("references") or [])
        log = active_log.get()
        record_path = ctx.directory / "request.json"
        record = {"request": request, "generator": ctx.spec["parameters"].get("generator"),
                  "task_id": None, "status": None, "polls": 0, "submitted_at": None, "finished_at": None}

        def save(**update):
            record.update(update)
            studio.save(record_path, sanitized(record))

        save()
        task_id = generator.submit(VideoRequest(
            prompt=request["prompt"], negative_prompt=request["negative"], image=image,
            seconds=request["seconds"], width=request["width"], height=request["height"], seed=request["seed"],
            mode=source.get("mode"), last_image=last_image, references=references, model=source.get("model"),
            resolution=source.get("resolution"), ratio=source.get("ratio"),
        ))
        save(task_id=task_id, submitted_at=studio.now())
        if log:
            log.emit(f"已提交视频生成任务（{request['seconds']:g} 秒）")
        last = {"state": None}

        def notify(status, polls):
            snapshot = {"state": status.state, "error": status.error, "raw": status.raw}
            save(status=snapshot, polls=polls)
            if status.state != last["state"]:
                last["state"] = status.state
                ctx.diagnostics.record("generation-status", {"task_id": task_id, **snapshot})
                if log:
                    log.emit(f"视频生成：{STATE_LABELS.get(status.state, status.state)}")

        try:
            status, polls = wait_for(generator, task_id, cfg, cancelled, notify)
        except Exception as exc:
            save(finished_at=studio.now(), error=str(exc)[:2000])
            raise
        download = ctx.directory / "download.bin"
        generator.fetch(status, download)
        target = ctx.directory / "generated.mp4"
        try:
            normalize(download, target)
            info = probe(target)
        except MediaError as exc:
            save(finished_at=studio.now(), error=f"生成结果无法解码：{exc}"[:2000])
            raise studio.StudioError(f"生成的视频无法解码：{exc}") from exc
        download.unlink(missing_ok=True)
        save(finished_at=studio.now())
        data = {
            "task_id": task_id,
            "prompt": request["prompt"],
            "seconds": request["seconds"],
            "duration": info["duration"],
            "width": info["width"],
            "height": info["height"],
            "fps": info["fps"],
            "audio": info["audio"],
            "polls": polls,
        }
        files = {"generated": file_info(ctx.config, ctx.job_id, target),
                 "request": file_info(ctx.config, ctx.job_id, record_path)}
        return data, files

    def conflict_labels(self, config, w, item, expected, messages):
        found = []
        before = item["parameters"].get("generator") or {}
        after = expected["parameters"].get("generator") or {}
        for field, label in (("backend", "生成服务"), ("model", "生成模型")):
            if before.get(field) != after.get(field):
                found.append(f"{label}已变化：{before.get(field) or '未记录'} → {after.get(field) or '未设置'}")
        if before.get("workflow") != after.get("workflow"):
            found.append("ComfyUI 工作流已变化")
        return found


register(GenerateHandler())
