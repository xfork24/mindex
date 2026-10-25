"""召回阶段（逐镜）：按分镜查询在活动索引中召回候选片段，结果写入 candidates.json。"""

from __future__ import annotations

from mindex.studio import studio
from mindex.studio.studio_artifacts import sanitized, visibility_key

from .. import signatures
from ..plans import needs_recall
from ..stages import StageHandler, register, shot_content
from ..versions import file_info


class RecallHandler(StageHandler):
    name, label, scope = "recall", "召回", "shot"
    uses_store = True

    def settings(self, config, p, *, store=None, display=False):
        o = p["options"]
        # 展示路径用短 TTL 缓存减少重复整表扫描；构建路径（display=False）始终读新值。
        if display:
            from mindex.studio.studio_artifacts import visibility_key_cached
            active = store.active_index_cached()
            visibility = visibility_key_cached(store)
        else:
            active = store.active_index()
            visibility = visibility_key(store)
        # Text recall is implicitly affected by the active person release; cover it in the key.
        return {
            "index_id": active["id"] if active else None,
            "person_release": active.get("search_release_id") if active else None,
            "limit": o["recall_limit"],
            "recall": {**config.studio.recall.model_dump(), "limit": o["recall_limit"]},
            "search": config.search.model_dump(),
            "embedding": sanitized(config.embedding.model_dump()),
            "visibility": visibility,
        }

    def specification(self, config, job_id, w, p, sid, params, resources):
        selected, content = shot_content(w, p, sid)
        inputs = {"content": selected["content"]}
        payload = {"query": content["query"]}
        # 分镜级覆盖仅在实际设置时进签名，保证未覆盖分镜与历史版本签名逐字节一致。
        if content.get("granularity"):
            payload["granularity"] = content["granularity"]
        if content.get("recall_limit"):
            payload["limit"] = content["recall_limit"]
        # 只在有限制时加键，保证不限任务的签名与历史版本逐字节一致。
        if scope := studio.material_scope(studio.load_job(config, job_id)):
            params = {**params, "sources": sorted(scope)}
        recall = signatures.recall_signature(content.get("recall"))
        if recall is not None:
            payload["recall"] = recall
        from mindex.persons.person_names import AUTO_PERSON_QUERY_VERSION, has_document_reference

        value = content.get("recall") or {}
        if (
            value.get("mode", "text") == "text"
            and value.get("person_mode", "auto") == "auto"
            and has_document_reference(content["query"])
        ):
            payload["auto_person_query_version"] = AUTO_PERSON_QUERY_VERSION
        return payload, inputs, params

    def preflight(self, config, job_id, w, p, sid, spec, found, node):
        if spec["parameters"]["index_id"] is None and needs_recall(p, sid):
            node.update(action="blocked", reason="尚未发布索引")
        # Explicit ranges do not need recall and can be built without an index.
        if not needs_recall(p, sid) and not found:
            node.update(action="skip", reason="人工选片不依赖召回")

    def run(self, ctx):
        from mindex.search.search import SearchEngine
        from mindex.shared.types import Filters, TextSearch

        config, job_id = ctx.config, ctx.job_id
        params = ctx.spec["parameters"]
        if not params["index_id"]:
            raise studio.StudioError("尚未发布索引")
        _, content = shot_content(ctx.w, ctx.p, ctx.sid)
        recall = studio.ShotRecall.model_validate(content["recall"]) if content.get("recall") else None
        engine = SearchEngine(
            config, ctx.services.get("embedder"), index_id=params["index_id"],
            release_id=params["person_release"],
        )
        mode = recall.mode if recall else "text"
        meta = {}
        # 分镜级覆盖优先，未设置时跟随方案数量与服务配置粒度。
        limit = content.get("recall_limit") or params["limit"]
        granularity = content.get("granularity") or config.studio.recall.granularity
        # 任务素材范围是硬上限，回显的 recall 仍是用户原始设置。
        scope = studio.material_scope(studio.load_job(config, job_id))
        if mode == "text":
            result = engine.text(
                TextSearch(
                    text=content["query"],
                    granularity=granularity,
                    limit=limit,
                    person_ids=recall.person_ids if recall else [],
                    person_mode=recall.person_mode if recall else "auto",
                    person_relation=recall.person_relation if recall else "all",
                    filters=studio.scoped_filters(recall.filters if recall else Filters(), scope),
                )
            )
            meta = {
                key: result[key]
                for key in (
                    "person_mode",
                    "person_ids",
                    "recognized_people",
                    "referenced_people",
                    "person_coverage_missing",
                    "person_version",
                    "score_type",
                )
                if key in result
            }
        else:
            search = engine.image if mode == "image" else engine.video
            result = search(
                signatures.query_media(config, job_id, recall),
                granularity=granularity,
                limit=limit,
                filters=studio.scoped_filters(recall.filters, scope),
            )
        entries = [
            {
                **item,
                "segment_id": item["id"],
                "rank": rank,
                "media_url": f"/api/admin/segments/{item['id']}/media",
                "thumbnail_url": f"/api/admin/segments/{item['id']}/thumbnail",
            }
            for rank, item in enumerate(result["results"], 1)
        ]
        data = {
            "query": content["query"],
            "mode": mode,
            "recall": recall.model_dump() if recall else None,
            "meta": meta,
            "results": entries,
            "elapsed_ms": result["elapsed_ms"],
        }
        target = ctx.directory / "candidates.json"
        studio.save(target, data)
        return data, {"candidates": file_info(config, job_id, target)}

    def conflict_labels(self, config, w, item, expected, messages):
        return ["查询、召回方式或召回设置已变化"]


register(RecallHandler())
