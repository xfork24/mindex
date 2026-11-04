"""工作台详情：页面与助手读取的展示视图（紧凑版本、检索组合依赖、冲突），不触发推理。"""

from __future__ import annotations

import copy

from mindex.shared.storage import Store
from mindex.studio import studio
from mindex.studio.material_digest import scope_parameters
from mindex.studio.platforms import snapshot_publish

from . import signatures
from .handlers.script import profile_changed, template_changed
from .plans import effective_voice, script_stale
from .stages import plan_shot_stages
from .versions import ordered_shots, plan, revision


def compact_version(item):
    """Keep editor/preview fields; diagnostics remain available on the version API."""
    data = item.get("data", {})
    def candidate(value):
        return {k: v for k, v in value.items() if k not in {"annotation", "matches", "channel_metadata"}}
    if item["stage"] == "compose":
        data = {k: v for k, v in data.items() if k != "graph"}
    elif item["stage"] == "recall":
        data = {**data, "results": [candidate(c) for c in data.get("results", [])]}
    elif item["stage"] == "select" and data.get("anchor"):
        data = {**data, "anchor": candidate(data["anchor"])}
    return {**item, "data": data, "compact": True}


def recall_closure(w, version_id):
    """某个版本（如成片合成）沿依赖链能到达的已完成召回版本，按 (index_id, person_release) 归组。"""
    groups, seen, pending = {}, set(), [version_id]
    while pending:
        item = w["versions"].get(pending.pop())
        if item is None or item["id"] in seen:
            continue
        seen.add(item["id"])
        if item["stage"] == "recall" and item["status"] == "complete":
            key = (item["parameters"].get("index_id"), item["parameters"].get("person_release"))
            groups.setdefault(key, set()).add(item["shot_id"])
        pending.extend(dep for dep in item["inputs"].values() if dep)
    return groups


def retrieval_dependencies(config, w, p, *, context):
    """检索组合依赖：当前生效组合、当前方案各分镜选用的召回版本依赖的组合，
    以及每个成片合成版本沿依赖链用到的组合。

    与召回签名同源：index_id / person_release 取自 settings(..., "recall")，
    召回版本依赖的组合取自其 parameters；二者不同的召回节点会提示选用版本不兼容。
    """
    store = context.database(config)
    releases = {}

    def release(release_id):
        if release_id not in releases:
            releases[release_id] = store.one(
                "SELECT content_id, person_id FROM search_releases WHERE id=?", (release_id,)
            ) if release_id else None
        return releases[release_id]

    def describe(content_id, release_id):
        row = release(release_id)
        return {"content_id": content_id, "release_id": release_id,
                "person_id": row["person_id"] if row else None, "known": bool(row)}

    current = signatures.settings(config, p, "recall", context=context)
    active = describe(current["index_id"], current["person_release"]) if current["index_id"] else None

    def summarize(groups):
        result = [
            {**describe(content_id, release_id), "shots": len(shots),
             "current": bool(active) and (content_id, release_id) == (active["content_id"], active["release_id"])}
            for (content_id, release_id), shots in groups.items()
        ]
        result.sort(key=lambda item: (not item["current"], -item["shots"]))
        return result

    selected = {}
    for sid in ordered_shots(w, p):
        vid = p["shots"][sid].get("recall")
        item = w["versions"].get(vid) if vid else None
        if item and item["status"] == "complete":
            key = (item["parameters"].get("index_id"), item["parameters"].get("person_release"))
            selected.setdefault(key, set()).add(sid)
    return {
        "active": active,
        "used": summarize(selected),
        "compose": {
            vid: summarize(recall_closure(w, vid))
            for vid, item in w["versions"].items() if item["stage"] == "compose"
        },
    }


def detail(config, job_id, w, *, store=None, compact=False):
    p = plan(w, w["active_plan"])
    context = signatures.DetailContext(store=store, display=True)
    from urllib.parse import quote

    references = None

    def display_version(item):
        """Return a view copy with display-only fields injected.

        ``w`` is re-read from disk every request and discarded afterwards, so the
        originals are never shared across requests. We still copy just the two
        nested structures we mutate here — per-file ``url`` and recall evidence
        ``reference_url`` — instead of deep-copying the whole workspace, which was
        the dominant cost of this response.
        """
        nonlocal references
        files = {}
        for name, metadata in item["files"].items():
            url = f"/api/studio/jobs/{job_id}/artifacts/{quote(metadata['path'], safe='/')}"
            files[name] = {**metadata, "url": url}
        copy_item = {**item, "files": files}
        if item["stage"] == "recall" and item.get("data", {}).get("results"):
            results = []
            for candidate in item["data"]["results"]:
                evidence_list = candidate.get("person_evidence")
                if not evidence_list:
                    results.append(candidate)
                    continue
                if references is None:
                    from mindex.persons.person_processing import EvidenceReferences
                    references = EvidenceReferences(config, store or Store(config.data_dir))
                results.append({
                    **candidate,
                    "person_evidence": [
                        {**evidence, "reference_url": references.url(evidence)}
                        for evidence in evidence_list
                    ],
                })
            copy_item = {**copy_item, "data": {**item["data"], "results": results}}
        return copy_item

    versions = {
        k: display_version(compact_version(v) if compact else v)
        for k, v in w["versions"].items()
    }
    result = {**w, "versions": versions}
    # Materialize legacy rules only in the response; revision and historical stage signatures stay unchanged.
    def display_plan(item):
        options = copy.deepcopy(item["options"])
        if options.get("publish"):
            options["publish"] = snapshot_publish(options["publish"], legacy=True)
        delivery = options.get("delivery")
        if delivery and delivery["config"].get("publish"):
            delivery["config"]["publish"] = snapshot_publish(delivery["config"]["publish"], legacy=True)
        return {**item, "options": options}
    result["plans"] = {key: display_plan(item) for key, item in w["plans"].items()}
    result["job"] = studio.load_job(config, job_id)
    result["material_scope"] = studio.scope_status(result["job"], context.database(config))
    result["revision"] = revision(p)
    result["workspace_revision"] = revision(w)
    result["script_stale"] = script_stale(p)
    # 所选文案模板（或提示词）与当前文案生成时不同：只提示可重新生成，不自动重写。
    result["script_template_changed"] = template_changed(config, w, p, store=context.database(config))
    # 从选题创建的任务同步过账号档案：已有文案按旧档案写成，只提示可重新生成。
    result["script_profile_changed"] = profile_changed(w, p)
    script = w["versions"].get(p.get("script")) or {}
    current_scope = scope_parameters(config, job_id, context.database(config)).get("material_scope")
    result["script_material_scope_changed"] = (script.get("data", {}).get("origin") == "generated"
        and script.get("parameters", {}).get("material_scope") != current_scope)
    result["tts_settings"] = {
        "backend": config.studio.tts.backend,
        "model": config.studio.tts.model,
        "default_voice": config.studio.tts.voice,
        "voice": effective_voice(config, p),
        "voice_mode": p["options"].get("voice_mode", "inherit"),
    }
    result["conflicts"] = {}
    result["retrieval"] = retrieval_dependencies(config, w, p, context=context)
    # 分镜编辑器「跟随方案」粒度默认值展示；数量默认直接读方案 recall_limit。
    result["recall_granularity"] = config.studio.recall.granularity
    # 参考文稿总字数上限与各画幅分辨率：创作页展示用。
    result["reference_limit"] = config.studio.max_reference_chars
    result["canvases"] = {name: list(config.studio.render.size(name)) for name in ("16:9", "9:16", "1:1")}
    from mindex.studio.studio_window import anchor_conflict

    # 每个分镜按画面来源实际包含的逐镜阶段（素材召回 = 全部四个；上传片段没有召回；图片只有配音与渲染）。
    result["shot_stages"] = {sid: list(plan_shot_stages(w, p, sid)) for sid in ordered_shots(w, p)}
    # 背景音乐归属：提示点分镜 start=True，顺延分镜 from=提示点分镜，停止提示点 stop=True。
    from .music import shot_music

    result["shot_music"] = shot_music(w, p)
    result["generators"] = {
        "video": {"backend": config.studio.video_generator.backend, "enabled": config.studio.video_generator.enabled},
        "image": {"backend": config.studio.image_generator.backend, "enabled": config.studio.image_generator.enabled},
        "music": {"backend": config.studio.music_generator.backend, "enabled": config.studio.music_generator.enabled},
    }
    result["anchor_conflicts"] = {sid: error for sid in ordered_shots(w, p)
                                  if (error := anchor_conflict(w, p, sid))}
    # Conflict detection reconstructs each shot's select-stage signature, which probes
    # the underlying shot media. On a cold probe cache this is the dominant cost of a
    # job's first open, so warm every referenced interval concurrently up front. The
    # serial loop below then reuses these results via the shared ShotMedia instance.
    if context.display:
        intervals = []
        for sid in ordered_shots(w, p):
            vid = p["shots"][sid].get("select")
            video = w["versions"].get(vid, {}).get("data", {}).get("event", {}).get("video") if vid else None
            if video and video.get("kind", "library") == "library":
                intervals.append((video["source_id"], video["start"], video["end"]))
        if len(intervals) > 1:
            try:
                context.media(config).prewarm(intervals)
            except Exception:  # noqa: BLE001 — 探测预热只是加速展示，真正读取时会带完整上下文重新报错
                pass
    for sid in ordered_shots(w, p):
        for stage in plan_shot_stages(w, p, sid):
            vid = p["shots"][sid].get(stage)
            if vid:
                result["conflicts"][f"{stage}:{sid}"] = signatures.conflicts(
                    config, job_id, w, p, stage, sid, vid, context=context,
                )
    return result
