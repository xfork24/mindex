"""Standalone worker: intentionally imports no mindex/chromadb or GPU runtime at startup."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path


def coalesce_alignment(items: list[dict]) -> list[dict]:
    """Merge zero-duration tokens using existing neighboring bounds; preserve all text."""
    rows, pending = [], []
    for item in items:
        start, end = float(item["start"]), float(item["end"])
        if not math.isfinite(start + end) or start < 0 or end < start:
            raise ValueError("ForcedAligner 返回无效时间范围")
        if end == start:
            pending.append(item)
            continue
        text = "".join(v["text"] for v in pending) + item["text"]
        rows.append({"start": min([start, *[float(v["start"]) for v in pending]]), "end": end, "text": text})
        pending = []
    if pending:
        if not rows:
            raise ValueError("全部对齐项均为零时长，需要人工复核原始转写")
        rows[-1]["text"] += "".join(v["text"] for v in pending)
        rows[-1]["end"] = max(rows[-1]["end"], *[float(v["end"]) for v in pending])
    return rows


def main():
    parser = argparse.ArgumentParser()
    for name in ("audio", "raw", "output", "model", "revision", "cache-dir"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    import torch
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError
    from qwen_asr import Qwen3ForcedAligner

    raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    rows = []
    if raw["text"].strip():
        try:
            model = snapshot_download(
                args.model, revision=args.revision, cache_dir=args.cache_dir, local_files_only=True
            )
        except LocalEntryNotFoundError:
            model = snapshot_download(args.model, revision=args.revision, cache_dir=args.cache_dir)
        aligner = Qwen3ForcedAligner.from_pretrained(model, dtype=torch.float32, device_map="cpu")
        result = aligner.align(audio=args.audio, text=raw["text"], language=raw["language"])
        rows = [
            {"text": item.text, "start": float(item.start_time), "end": float(item.end_time)}
            for item in result[0]
        ]
    target = Path(args.output)
    temporary = target.with_suffix(".partial")
    temporary.write_text(
        json.dumps(
            {
                "utterances": coalesce_alignment(rows),
                "raw_tokens": rows,
                "policy": "merge-zero-duration-adjacent-v1",
                "revision": args.revision,
            },
            ensure_ascii=False,
        ), encoding="utf-8"
    )
    temporary.replace(target)


if __name__ == "__main__":
    main()
