"""集群文字检索：人物、提及与普通渠道共用可分页 eligible 关系。"""

from __future__ import annotations

from persons.person_names import _occurrences, coverage_missing, overlaps_for, resolve_names
from persons.persons import Persons


def text(engine, query, started):
    store = engine.store
    catalogue = Persons(store)
    active = engine._active()
    snapshots, projected = engine._eligible_rows(active, query.granularity, query.filters)
    release = catalogue.release_for(
        active["id"],
        pinned=engine.index_id is not None,
        release_id=engine.release_id or active.get("search_release_id"),
    )
    published = catalogue.published_entities(release)
    entities = list({e["id"]: e for e in [*published["entities"], *catalogue.entities()]}.values())
    ids, recognized, remaining, variant_groups = resolve_names(query, entities)
    mode = "onscreen" if query.person_mode == "auto" and ids else query.person_mode
    meta = {
        "person_mode": mode,
        "person_ids": ids,
        "recognized_people": [p for p in recognized if p.get("usage") != "reference"],
        "release_id": release["id"] if release else None,
        "person_version": release["person_id"] if release else None,
    }
    references = [p for p in recognized if p.get("usage") == "reference"]
    if references:
        meta["referenced_people"] = references
    if mode == "onscreen" and not ids:
        raise ValueError("人物出镜模式需要选择人物或输入已登记姓名")
    if mode == "onscreen":
        if published.get("format", 1) >= 2:
            cast_roles = {}
            for entity in published["entities"]:
                if entity.get("kind") == "role":
                    for actor in entity.get("actor_ids", []):
                        cast_roles.setdefault(actor, []).append(entity["id"])
            variant_groups = [
                list(
                    dict.fromkeys(
                        role for identifier in group for role in cast_roles.get(identifier, [identifier])
                    )
                )
                for group in variant_groups
            ]
        names = {e["id"]: e["name"] for e in entities}
        identifiers = {identifier for group in variant_groups for identifier in group}
        missing = set()

        def enrich(page):
            evidence = catalogue.evidence_for(
                release, sources={s["source_id"] for s in page}, identifiers=identifiers, intervals=page
            )
            missing.update(coverage_missing(page, evidence["coverage"]))
            index = evidence.get("evidence_index") or evidence["evidence"]
            for segment in page:
                matches = overlaps_for(segment, index, variant_groups, query.person_relation)
                if not matches:
                    continue
                enriched = [
                    {
                        **item,
                        "data": {k: v for k, v in item["data"].items() if k not in {"body_feature", "space"}},
                        "actor_name": names.get(item["actor_id"]),
                        "role_name": names.get(item["role_id"]),
                        "image_url": "/api/persons/evidence/" + item["id"] + "/image"
                        if item["data"].get("image_path")
                        else None,
                        "reference_url": None,
                        "start": max(item["start"], segment["start"]),
                        "end": min(item["end"], segment["end"]),
                    }
                    for item in matches
                ]
                intervals = sorted((e["start"], e["end"]) for e in enriched)
                total, end = 0, -1
                for a, b in intervals:
                    total += max(0, b - max(a, end))
                    end = max(end, b)
                quality = max(e["data"].get("score", 0) for e in enriched)
                yield {**segment, "person_evidence": enriched, "person_quality": total + quality * 0.001}

        snapshots = snapshots.transform(enrich)
        meta["person_coverage_missing"] = sorted(missing)
        if not remaining:
            from search.search import time_iou

            ranked, selected, offset = [], [], 0
            count = engine.config.search.candidates
            while len(ranked) < query.limit:
                page = snapshots.person_ranked(
                    engine.config.search, query.target_duration, count, offset=offset
                )
                if not page:
                    break
                candidates = snapshots.lookup(hit["id"] for hit in page)
                for hit in page:
                    row = candidates[hit["id"]]
                    if any(time_iou(row, prior) > 0.5 for prior in selected):
                        continue
                    selected.append(row)
                    ranked.append(hit)
                    if len(ranked) >= query.limit:
                        break
                offset += len(page)
            lookup = engine._hydrate(active, [hit["id"] for hit in ranked])
            projected_rows = snapshots.lookup(lookup)
            for key in lookup:
                lookup[key]["person_evidence"] = projected_rows[key]["person_evidence"]
            result = engine._result(
                active,
                snapshots,
                {},
                query.limit,
                started,
                ranked=ranked,
                lookup=lookup,
                eligible_count=len(snapshots),
            )
            result["score_type"] = (
                "duration_blend"
                if query.target_duration and engine.config.search.duration_weight
                else "person_coverage_quality"
            )
        elif not snapshots:
            result = engine._result(
                active, snapshots, {}, query.limit, started, target_duration=query.target_duration
            )
        else:
            query = query.model_copy(update={"text": remaining})
            result = engine._channel_text(
                query, active, snapshots, engine._vectors(active["id"]), started, projection=True
            )
        from persons.person_processing import EvidenceReferences

        reference_images = EvidenceReferences(engine.config, store)
        for hit in result["results"]:
            for item in hit.get("person_evidence", []):
                item["reference_url"] = reference_images.url(item)
        return {**result, **meta}
    if mode == "mentioned":
        names = {e["id"]: [e["name"], *e.get("aliases", [])] for e in entities}
        if ids:
            from search.lexical_query import dialogue_projection

            def mentioned(page):
                dialogue = dialogue_projection(store, active["id"], [s["id"] for s in page])
                for segment in page:
                    literal = dialogue.get(segment["id"], "")
                    checks = [
                        any(_occurrences(n, literal) for i in group for n in names[i])
                        for group in variant_groups
                    ]
                    if any(checks) if query.person_relation == "any" else all(checks):
                        yield segment

            snapshots = snapshots.transform(mentioned)
        query = query.model_copy(
            update={"channels": ["dialogue"], "text": query.text or " ".join(names[i][0] for i in ids)}
        )
    if mode == "mentioned" and not snapshots:
        result = engine._result(
            active, snapshots, {}, query.limit, started, target_duration=query.target_duration
        )
    else:
        result = engine._channel_text(
            query, active, snapshots, engine._vectors(active["id"]), started, projection=projected
        )
    return {**result, **meta}
