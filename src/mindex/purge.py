"""素材物理删除：清向量、清库、清派生文件，唯独保留源视频文件。

- 向量：该素材在所有索引版本集合中的表示逐一删除（集合已被清理的旧版本跳过）；
- 库：segments 及其派生行（representations/index_members/annotations/curation）、
  transcripts、scene_plans、runs、sources 级联删除，事务内完成；
- 文件：clips/<id>/、thumbnails、plans/<id>.json、transcripts/<id>/、reports/<id>/、
  context/<segment>.mp4；sources/ 下的原片保留，重新导入无需重新拷贝。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .indexing import VectorIndex
from .storage import Store

logger = logging.getLogger(__name__)


class PurgeRefused(RuntimeError):
    """删除被拒绝：素材仍被创作任务引用，删除会破坏任务的可重跑性。"""


def studio_jobs_using(store: Store, source_id: str) -> list[str]:
    """扫描创作任务清单，返回仍引用该素材的任务目录名。

    source id 是全局唯一指纹，直接对 job.json 文本做包含匹配即可，
    不依赖任务状态机的具体结构。
    """
    jobs: list[str] = []
    studio_root = store.root / "studio"
    if not studio_root.is_dir():
        return jobs
    for directory in sorted(studio_root.iterdir()):
        manifest = directory / "job.json"
        if not manifest.is_file():
            continue
        try:
            text = manifest.read_text(encoding="utf-8")
        except OSError:
            continue
        if f'"{source_id}"' in text:
            jobs.append(directory.name)
    return jobs


def _count_files(path: Path) -> int:
    return sum(1 for item in path.rglob("*") if item.is_file()) if path.is_dir() else 0


def _remove_tree(path: Path) -> int:
    if not path.is_dir():
        return 0
    removed = _count_files(path)
    import shutil

    shutil.rmtree(path)
    return removed


def _remove_file(path: Path) -> int:
    if path.is_file():
        path.unlink()
        return 1
    return 0


def purge_source(store: Store, source_id: str) -> dict:
    """物理删除素材（保留源视频文件）；返回删除统计。"""
    source = store.one("SELECT * FROM sources WHERE id=?", (source_id,))
    if source is None:
        raise KeyError(source_id)

    jobs = studio_jobs_using(store, source_id)
    if jobs:
        raise PurgeRefused(f"素材仍被创作任务引用：{'、'.join(jobs)}；请先删除相关任务再删除素材")

    segments = store.rows("SELECT id, path, thumbnail FROM segments WHERE source_id=?", (source_id,))
    segment_ids = [segment["id"] for segment in segments]
    representations: list[dict] = []
    placeholders = ""
    if segment_ids:
        placeholders = ",".join("?" * len(segment_ids))
        representations = store.rows(
            f"SELECT id, index_id FROM representations WHERE segment_id IN ({placeholders})", segment_ids
        )

    by_index: dict[str, list[str]] = {}
    for row in representations:
        by_index.setdefault(row["index_id"], []).append(row["id"])
    vectors = 0
    for index_id, rep_ids in by_index.items():
        try:
            index = VectorIndex(store.root, index_id)
        except Exception as exc:  # noqa: BLE001 — 旧版本集合可能已被清理，向量天然不存在
            logger.warning("索引 %s 的向量集合不可用，跳过其中 %d 条向量的删除：%s", index_id, len(rep_ids), exc)
            continue
        index.delete(rep_ids)
        vectors += len(rep_ids)

    placeholders = ",".join("?" * len(segment_ids))
    with store.connect() as db:
        if segment_ids:
            db.execute(f"DELETE FROM representations WHERE segment_id IN ({placeholders})", segment_ids)
            db.execute(f"DELETE FROM index_members WHERE segment_id IN ({placeholders})", segment_ids)
            db.execute(f"DELETE FROM annotations WHERE segment_id IN ({placeholders})", segment_ids)
            db.execute(f"DELETE FROM segment_curation WHERE id IN ({placeholders})", segment_ids)
            db.execute("DELETE FROM segments WHERE source_id=?", (source_id,))
        db.execute("DELETE FROM transcripts WHERE source_id=?", (source_id,))
        db.execute("DELETE FROM channel_data WHERE source_id=?", (source_id,))
        db.execute("DELETE FROM dialogue_tasks WHERE source_id=?", (source_id,))
        db.execute("DELETE FROM source_tasks WHERE source_id=?", (source_id,))
        db.execute("DELETE FROM scene_plans WHERE source_id=?", (source_id,))
        db.execute("DELETE FROM source_curation WHERE id=?", (source_id,))
        db.execute("DELETE FROM runs WHERE source_id=?", (source_id,))
        from .persons import purge_person_source

        purge_person_source(db, source_id)
        db.execute("DELETE FROM sources WHERE id=?", (source_id,))

    files = 0
    files += _remove_tree(store.root / "clips" / source_id)
    files += _remove_tree(store.root / "transcripts" / source_id)
    files += _remove_tree(store.root / "dialogue" / source_id)
    files += _remove_tree(store.root / "analysis" / source_id)
    files += _remove_tree(store.root / "subtitles" / source_id)
    files += _remove_tree(store.root / "persons" / "frames" / source_id)
    files += _remove_tree(store.root / "reports" / source_id)
    files += _remove_file(store.root / "plans" / f"{source_id}.json")
    for segment in segments:
        files += _remove_file(store.root / "context" / f"{segment['id']}.mp4")
        try:
            files += _remove_file(store.resolve_media(segment["thumbnail"]))
        except (FileNotFoundError, ValueError):
            pass  # 缩略图路径异常不阻塞删除

    return {
        "deleted": {"segments": len(segments), "vectors": vectors, "files": files},
        "kept": {"source_file": Path(source["path"]).name},
    }
