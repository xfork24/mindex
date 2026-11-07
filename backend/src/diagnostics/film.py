"""Offline film manifests and a portable review page; no model service required."""

from __future__ import annotations

import csv
import html
import json
from collections import Counter
from pathlib import Path
from statistics import median
from urllib.parse import quote

from shared.config import Config
from shared.export_media import copy_for_export
from shared.media import windows
from shared.review_style import review_style
from shared.storage import Store, dumps


def source_report(config: Config, source_id: str) -> dict:
    store = Store.open(config)
    source = store.one("SELECT * FROM sources WHERE id=?", (source_id,))
    if source is None:
        raise ValueError("找不到素材 ID")
    plan_path = store.root / "plans" / f"{source_id}.json"
    from shared.checkpoints import read_text

    plan_text = read_text(store, plan_path)
    plan = json.loads(plan_text) if plan_text else None
    media = json.loads(source["media"])
    segments = store.segments(source_id)
    shots = plan["shots"] if plan else [s for s in segments if s["kind"] == "shot"]
    durations = sorted(s["end"] - s["start"] for s in shots)
    v = config.video
    moments = sum(
        1
        if s["end"] - s["start"] <= v.short_shot_seconds
        else len(windows(s["start"], s["end"], v.window_seconds, v.stride_seconds))
        for s in shots
    )
    missing = [
        {"id": segment["id"], "field": field, "path": segment[field]}
        for segment in segments
        for field in ("path", "thumbnail")
        if not store.media_available(segment[field])
    ]
    return {
        "source_id": source_id,
        "name": store.source_names()[source_id],
        "media": media,
        "source_path": source["path"],
        "layout_key": source["layout_key"],
        "configuration_matches": source["layout_key"] == config.layout_key,
        "plan_path": str(plan_path) if plan else None,
        "prepared": bool(source["prepared"]),
        "planned_shots": len(shots),
        "planned_moments": moments if source["layout_key"] == config.layout_key else None,
        "materialized": dict(Counter(s["kind"] for s in segments)),
        "shot_seconds": {
            "min": durations[0],
            "median": median(durations),
            "p95": durations[min(len(durations) - 1, int(len(durations) * 0.95))],
            "max": durations[-1],
            "under_one_second": sum(d < 1 for d in durations),
        }
        if durations
        else None,
        "missing_artifacts": missing,
        "runs": store.rows("SELECT * FROM runs WHERE source_id=? ORDER BY id DESC LIMIT 10", (source_id,)),
    }


def export_source(config: Config, source_id: str, output: Path | None = None) -> dict:
    store = Store.open(config)
    report = source_report(config, source_id)
    output = (output or store.root / "reports" / source_id).resolve()
    output.mkdir(parents=True, exist_ok=True)
    segments = store.segments(source_id)
    if report["plan_path"]:
        report["shots"] = json.loads(Path(report["plan_path"]).read_text(encoding="utf-8"))["shots"]
    else:
        report["shots"] = [{"start": s["start"], "end": s["end"]} for s in segments if s["kind"] == "shot"]
    report["segments"] = segments
    (output / "manifest.json").write_text(dumps(report), encoding="utf-8")
    fields = ["id", "source_id", "kind", "start", "end", "path", "thumbnail", "parent_id", "reason"]
    with (output / "segments.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(segments)
    with (output / "shots.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, ["number", "start", "end", "duration"])
        writer.writeheader()
        for number, shot in enumerate(report.get("shots", []), 1):
            writer.writerow(
                {
                    "number": number,
                    "start": shot["start"],
                    "end": shot["end"],
                    "duration": shot["end"] - shot["start"],
                }
            )

    def url(relative):
        return html.escape(quote(copy_for_export(store, relative, output), safe="/"), quote=True)

    cards = []
    for s in segments:
        cards.append(
            f'<article data-kind="{html.escape(s["kind"])}"><h3>{html.escape(s["kind"])} '
            f"{s['start']:.3f}–{s['end']:.3f}s ({s['end'] - s['start']:.3f}s)</h3>"
            f'<video controls preload="none" poster="{url(s["thumbnail"])}" src="{url(s["path"])}"></video>'
            f"<p>{html.escape(s['id'])} · {html.escape(s['reason'])}</p></article>"
        )
    title = html.escape(report["name"])
    page = (
        """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>分镜检查</title>
<style>__STYLE__</style>
<h1>"""
        + title
        + f"""</h1><p>计划 {report["planned_shots"]} 个镜头 · {report["planned_moments"]} 个精细片段
 · 已落盘 {len(segments)} 个单元。时间为原片秒数；此页用于人工检查切点，不包含模型描述。</p>
<nav><button data-filter="shot">镜头</button><button data-filter="moment">精细片段</button>
<button data-filter="scene">场景</button><button data-filter="all">全部</button></nav>
<div class="pagination"><button id="previous">上一页</button><span id="page"></span><button
id="next">下一页</button></div>
<main></main><template id="cards">"""
        + "".join(cards)
        + """</template>
<script>const cards=[...document.querySelector('#cards').content.children];let selected=[],page=0;
function render(){document.querySelectorAll('main video').forEach(v=>v.pause());
document.querySelector('main').replaceChildren(...selected.slice(page*48,(page+1)*48).map(a=>a.cloneNode(true)));
document.querySelector('#page').textContent=`${page+1} / ${Math.max(1,Math.ceil(selected.length/48))} 页 ·
${selected.length} 个单元`;
document.querySelector('#previous').disabled=page===0;document.querySelector('#next').disabled=(page+1)*48>=selected.length}
function filter(kind){selected=cards.filter(a=>kind==='all'||a.dataset.kind===kind);page=0;render()}
document.querySelectorAll('[data-filter]').forEach(b=>b.onclick=()=>filter(b.dataset.filter));
document.querySelector('#previous').onclick=()=>{page--;render()};document.querySelector('#next').onclick=()=>{page++;render()};
filter('shot');</script></html>"""
    )
    (output / "review.html").write_text(page.replace("__STYLE__", review_style()), encoding="utf-8")
    return {
        "output": str(output),
        "review": str(output / "review.html"),
        "manifest": str(output / "manifest.json"),
        "segments": len(segments),
        "planned_shots": report["planned_shots"],
        "planned_moments": report["planned_moments"],
    }
