from __future__ import annotations

from pathlib import Path
from time import perf_counter

import numpy as np

from mindex.models.adapters import ModelError, make_embedder, request
from mindex.shared.config import Config, fingerprint
from mindex.shared.media import probe


def model_check(config: Config, video: Path, text: str, repeats: int = 3) -> dict:
    duration = probe(video)["duration"]
    if duration > config.video.short_shot_seconds + 0.05:
        raise ValueError("模型联调请使用不超过 short_shot_seconds 的真实短片段")
    embedder = make_embedder(config)
    space = embedder.identity()
    rows = []
    for index in range(repeats):
        started = perf_counter()
        query = embedder.text([text], role="query")[0]
        text_ms = (perf_counter() - started) * 1000
        started = perf_counter()
        visual = embedder.video(video)
        video_ms = (perf_counter() - started) * 1000
        rows.append(
            {
                "iteration": index,
                "text_ms": text_ms,
                "video_ms": video_ms,
                "text_dimensions": len(query),
                "video_dimensions": len(visual),
                "text_norm": float(np.linalg.norm(query)),
                "video_norm": float(np.linalg.norm(visual)),
                "cosine_similarity": float(np.dot(query, visual)),
            }
        )
    try:
        gpu = request(
            "GET",
            config.embedding.base_url.rstrip("/") + "/metrics",
            timeout=3,
            attempts=1,
            headers=config.embedding.headers(),
        )
    except ModelError:
        gpu = None
    return {
        "space": space,
        "space_id": fingerprint(space),
        "input": {"video": str(video), "text": text},
        "measurements": rows,
        "gpu_metrics": gpu,
        "note": "First iteration is cold inference in an already loaded service; later rows are warm. "
        "Latency includes media upload/decode. Cosine is not a quality acceptance score.",
    }
