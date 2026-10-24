"""磁盘清理：A 级无条件安全项每次删除，B 级按策略过期删除；默认预览，--apply 才执行。

分级判定、锁行为与待调整清单详见 docs/cleanup.md。规则为数据驱动的类别列表：
扫描阶段绝不删除；apply 阶段分两段持锁执行——先独占 pipeline.lock + index-write.lock
（管线与索引类），释放后再取 studio.lock（创作类），与 admin 删除、CLI index 同序避免死锁。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mindex.shared.config import Config
from mindex.shared.storage import Store

log = logging.getLogger(__name__)

ITEM_PREVIEW_LIMIT = 20

# TemporaryDirectory 写在 data 根下的已知前缀；正常退出自动消失，存在即崩溃残留。
CRASH_TEMP_PREFIXES = (
    "asr-", "align-", "embed-", "dialogue-", "query-", "query-window-", "query-image-",
)

# C 级子树：clean 只在 notes 中报告大小，绝不进入任何删除候选。
PROTECTED_SUBTREES = (
    "model-cache", "studio-verification", "e2e", "verification", "chroma",
)

PROTECTED_NOTES = (
    ("model-cache", "强制对齐模型缓存（C 级待调整）：删除后下次 align 重新下载"),
    ("studio-verification", "一次性人工验证产物（C 级待调整）：归档或删除请人工决定"),
    ("e2e", "e2e 验证产物（C 级待调整）：请人工决定"),
    ("verification", "最新一次验证快照（C 级待调整）：会被下次验证覆盖"),
)


class CleanBusy(RuntimeError):
    """清理无法安全进行：所需锁被占用。"""


@dataclass(frozen=True)
class CleanItem:
    path: str
    bytes: int
    detail: dict = field(default_factory=dict)


@dataclass
class CleanAction:
    """一个清理类别：扫描产物 + 执行回调；result/error/skipped 由执行段回填。"""

    category: str
    level: str
    stage: str  # "pipeline" | "studio"，决定执行段与所持锁
    description: str
    policy: str
    items: list[CleanItem]
    kept: dict = field(default_factory=dict)
    result: dict | None = None
    error: str | None = None
    skipped: str | None = None
    execute: Callable[[], dict] | None = None

    @property
    def total_bytes(self) -> int:
        return sum(item.bytes for item in self.items)

    def report(self) -> dict:
        return {
            "category": self.category,
            "level": self.level,
            "description": self.description,
            "policy": self.policy,
            "item_count": len(self.items),
            "bytes": self.total_bytes,
            "items": [
                {"path": item.path, "bytes": item.bytes, **item.detail}
                for item in self.items[:ITEM_PREVIEW_LIMIT]
            ],
            "kept": self.kept,
            "result": self.result,
            "error": self.error,
            "skipped": self.skipped,
        }


class Scan:
    """单次扫描上下文：缓存跨规则共享的引用集，避免重复解析大快照。"""

    def __init__(self, config: Config, store: Store, now: datetime):
        self.config = config
        self.store = store
        self.now = now
        self._media: tuple[set[str], set[str], dict] | None = None

    @property
    def media_state(self) -> tuple[set[str], set[str], dict]:
        """(clips/thumbnails 引用集, segment id 集, retained_versions 划分)。

        引用集与 check_index 同口径：segments 行 + 保留索引版本 complete 成员快照的
        path/thumbnail；被保留快照引用的文件绝不判孤儿。
        """
        if self._media is None:
            from mindex.maintenance.clean_index import retained_versions

            retained = retained_versions(self.store, self.config.clean.index_keep_versions)
            references: set[str] = set()
            segment_ids: set[str] = set()
            for row in self.store.rows("SELECT id, path, thumbnail FROM segments"):
                segment_ids.add(row["id"])
                references.add(row["path"])
                references.add(row["thumbnail"])
            for index_id in retained["keep"]:
                for snapshot in self.store.snapshots(index_id):
                    if not isinstance(snapshot, dict):
                        continue
                    for key in ("path", "thumbnail"):
                        value = snapshot.get(key)
                        if isinstance(value, str):
                            references.add(value)
            self._media = (references, segment_ids, retained)
        return self._media

    def expired(self, path: Path, days: int) -> bool:
        """目录按树内最新文件 mtime 判过期（目录自身 mtime 不可靠）。"""
        _, newest = _tree_stats(path)
        if newest == 0.0:
            return True  # 空目录视为过期
        return datetime.fromtimestamp(newest, UTC) < self.now - timedelta(days=days)


# ---------------------------------------------------------------- 通用遍历与删除工具


def _iter_files(root: Path, *, skip: tuple[str, ...] = ()) -> Iterable[Path]:
    """深度优先遍历文件，整棵跳过 skip 中的相对子树（C 级保护与性能）。"""
    if not root.is_dir():
        return
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if any(relative == item or relative.startswith(item + "/") for item in skip):
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(path)
            elif entry.is_file(follow_symlinks=False):
                yield path


def _tree_stats(path: Path) -> tuple[int, float]:
    """(总字节, 树内最新 mtime)；单文件直接统计，缺失路径返回 (0, 0)。"""
    if path.is_file():
        stat = path.stat()
        return stat.st_size, stat.st_mtime
    if not path.is_dir():
        return 0, 0.0
    total, newest = 0, 0.0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                stat = entry.stat()
                total += stat.st_size
                newest = max(newest, stat.st_mtime)
    return total, newest


def _prune_empty_parents(path: Path, stop: Path) -> None:
    """删除文件后自底向上清理空父目录，直到 stop（不含）。"""
    parent = path.parent
    while parent != stop and parent.is_dir():
        try:
            next(parent.iterdir())
            return
        except StopIteration:
            pass
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


# ---------------------------------------------------------------- A 级：每次都删


def temp_crash(scan: Scan) -> CleanAction | None:
    """崩溃残留的临时目录与根级 .partial 文件；apply 段独占管线锁，无 mtime 门槛。"""
    items, targets = [], []
    try:
        entries = list(os.scandir(scan.store.root))
    except OSError:
        return None
    for entry in entries:
        name = entry.name
        crashed = name.startswith(CRASH_TEMP_PREFIXES) or (
            name.endswith(".partial") and entry.is_file(follow_symlinks=False)
        )
        if not crashed or not (entry.is_dir(follow_symlinks=False) or entry.is_file(follow_symlinks=False)):
            continue
        path = Path(entry.path)
        size, _ = _tree_stats(path)
        targets.append(path)
        items.append(CleanItem(path.relative_to(scan.store.root).as_posix(), size))

    def execute() -> dict:
        deleted = 0
        for path in targets:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            deleted += 1
        return {"deleted": deleted}

    if not items:
        return None
    return CleanAction(
        category="temp_crash", level="A", stage="pipeline",
        description="崩溃残留的临时目录与未完成写入",
        policy="TemporaryDirectory 正常退出即消失，存在即残留；apply 时独占管线锁保证无在跑管线，存在即删",
        items=items, execute=execute,
    )


def ds_store(scan: Scan) -> CleanAction | None:
    items = [
        CleanItem(path.relative_to(scan.store.root).as_posix(), path.stat().st_size)
        for path in _iter_files(scan.store.root, skip=PROTECTED_SUBTREES)
        if path.name == ".DS_Store"
    ]

    def execute() -> dict:
        deleted = 0
        for item in items:
            (scan.store.root / item.path).unlink(missing_ok=True)
            deleted += 1
        return {"deleted": deleted}

    if not items:
        return None
    return CleanAction(
        category="ds_store", level="A", stage="pipeline",
        description="macOS Finder 产物 .DS_Store",
        policy="无任何用途；C 级子树内的不触碰", items=items, execute=execute,
    )


def uploads_residue(scan: Scan) -> CleanAction | None:
    grace = timedelta(hours=scan.config.clean.temp_grace_hours)
    items: list[CleanItem] = []
    stale: list[Path] = []
    empty_dirs: list[Path] = []
    kept = 0
    for name in ("sources/.uploads", "subtitles/.uploads"):
        directory = scan.store.root / name
        if not directory.is_dir():
            continue
        files = [child for child in directory.iterdir() if child.is_file()]
        for path in files:
            if datetime.fromtimestamp(path.stat().st_mtime, UTC) < scan.now - grace:
                stale.append(path)
                items.append(CleanItem(f"{name}/{path.name}", path.stat().st_size))
            else:
                kept += 1  # 宽限期内：可能正是进行中的上传
        if not files and not any(child.is_dir() for child in directory.iterdir()):
            empty_dirs.append(directory)
            items.append(CleanItem(name, 0))

    def execute() -> dict:
        for path in stale:
            path.unlink(missing_ok=True)
        for directory in empty_dirs:
            directory.rmdir()
        return {"deleted": len(stale) + len(empty_dirs)}

    if not items:
        return None
    return CleanAction(
        category="uploads_residue", level="A", stage="pipeline",
        description="上传暂存残留（sources/.uploads、subtitles/.uploads）",
        policy=f"上传失败/中断的残留；宽限 {scan.config.clean.temp_grace_hours} 小时内的文件视为进行中而保留",
        items=items, kept={"within_grace": kept}, execute=execute,
    )


def orphan_clips(scan: Scan) -> CleanAction | None:
    references, _, _ = scan.media_state
    items = [
        CleanItem(relative, size)
        for relative, size in _orphan_files(scan, scan.store.root / "clips", references)
    ]
    return _orphan_action(scan, "orphan_clips", "无任何 segment 行或保留索引快照引用的切片",
                          "clips", items)


def orphan_thumbnails(scan: Scan) -> CleanAction | None:
    references, _, _ = scan.media_state
    items = [CleanItem(relative, size) for relative, size in _orphan_files(
        scan, scan.store.root / "thumbnails", references)]
    return _orphan_action(scan, "orphan_thumbnails", "无任何 segment 行或保留索引快照引用的缩略图",
                          "thumbnails", items)


def orphan_context(scan: Scan) -> CleanAction | None:
    _, segment_ids, _ = scan.media_state
    directory = scan.store.root / "context"
    items: list[CleanItem] = []
    targets: list[Path] = []
    if not directory.is_dir():
        return None
    for path in sorted(directory.iterdir()):
        if not path.is_file() or not path.name.endswith(".mp4"):
            continue
        if path.stem in segment_ids:
            continue  # 有 segment 行的属于 B 级 context_cache，按 mtime 过期
        targets.append(path)
        items.append(CleanItem(path.relative_to(scan.store.root).as_posix(), path.stat().st_size))

    def execute() -> dict:
        deleted = 0
        for path in targets:
            path.unlink(missing_ok=True)
            deleted += 1
        return {"deleted": deleted}

    if not items:
        return None
    return CleanAction(
        category="orphan_context", level="A", stage="pipeline",
        description="segment 已不存在的播放转码缓存",
        policy="文件名即 segment id，无对应行即孤儿；下次访问按需重转码",
        items=items, execute=execute,
    )


def _orphan_files(scan: Scan, directory: Path, references: set[str]) -> list[tuple[str, int]]:
    found = []
    for path in _iter_files(directory):
        relative = path.relative_to(scan.store.root).as_posix()
        if relative not in references:
            found.append((relative, path.stat().st_size))
    return found


def _orphan_action(scan: Scan, category: str, description: str, root_name: str,
                   items: list[CleanItem]) -> CleanAction | None:
    if not items:
        return None
    root = scan.store.root / root_name

    def execute() -> dict:
        deleted = 0
        for item in items:
            path = scan.store.root / item.path
            path.unlink(missing_ok=True)
            _prune_empty_parents(path, root)
            deleted += 1
        return {"deleted": deleted}

    return CleanAction(
        category=category, level="A", stage="pipeline",
        description=description,
        policy="引用集 = segments 行 + 保留索引版本（活动、building、最近 ready）快照，"
               "与 check_index 校验同口径；切分布局不可变，正常文件不会被误判",
        items=items, execute=execute,
    )


def person_frames(scan: Scan) -> CleanAction | None:
    from mindex.persons.persons import retained_frame_paths

    def collect():
        retained = set().union(*retained_frame_paths(scan.store).values(), set())
        return [CleanItem(path.relative_to(scan.store.root).as_posix(), path.stat().st_size)
                for path in _iter_files(scan.store.root / "persons/frames")
                if path.name != ".DS_Store" and path.relative_to(scan.store.root).as_posix() not in retained]

    items = collect()
    if not items:
        return None

    def execute():
        # The scan precedes lock acquisition. Recompute under the exclusive lock.
        current = {item.path for item in collect()}
        deleted = 0
        for item in items:
            if item.path in current:
                path = scan.store.root / item.path
                path.unlink(missing_ok=True)
                _prune_empty_parents(path, scan.store.root / "persons/frames")
                deleted += 1
        return {"deleted": deleted}

    return CleanAction(category="person_frames", level="A", stage="pipeline",
                       description="人物冗余展示帧", policy="已确认镜头每身份保留一帧；未确认镜头全部保留；"
                       "旧库先运行 persons migrate-frames 获得压缩收益", items=items, execute=execute)


def orphan_person_png(scan: Scan) -> CleanAction | None:
    digests = {row["digest"] for row in scan.store.rows("SELECT digest FROM person_references")}
    for row in scan.store.rows("SELECT data FROM person_imports"):
        try:
            images = json.loads(row["data"]).get("images", [])
        except ValueError:
            continue
        digests |= {image.get("digest") for image in images if isinstance(image, dict)}
    items: list[CleanItem] = []
    for namespace in ("references", "imports"):
        directory = scan.store.root / "persons" / namespace
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.png")):
            if path.stem not in digests:
                items.append(CleanItem(
                    path.relative_to(scan.store.root).as_posix(), path.stat().st_size))

    def execute() -> dict:
        deleted = 0
        for item in items:
            (scan.store.root / item.path).unlink(missing_ok=True)
            deleted += 1
        return {"deleted": deleted}

    if not items:
        return None
    return CleanAction(
        category="orphan_person_png", level="A", stage="pipeline",
        description="无引用计数的人物参考图/导入图 PNG",
        policy="与 delete_import_image 同口径：person_references.digest ∪ person_imports.images[].digest",
        items=items, execute=execute,
    )


def orphan_voices(scan: Scan) -> CleanAction | None:
    directory = scan.store.root / "voices"
    if not directory.is_dir():
        return None
    referenced = {
        row["sample_path"] for row in scan.store.rows("SELECT sample_path FROM voices")
        if row["sample_path"]
    }
    items = [
        CleanItem(path.relative_to(scan.store.root).as_posix(), path.stat().st_size)
        for path in sorted(directory.iterdir())
        if path.is_file() and path.relative_to(scan.store.root).as_posix() not in referenced
    ]

    def execute() -> dict:
        deleted = 0
        for item in items:
            (scan.store.root / item.path).unlink(missing_ok=True)
            deleted += 1
        return {"deleted": deleted}

    if not items:
        return None
    return CleanAction(
        category="orphan_voices", level="A", stage="pipeline",
        description="无 voices 行引用的声音样本（pending 残留与试听文件）",
        policy="voices.sample_path 为准；试听与失败残留可再生或已无用", items=items, execute=execute,
    )


def orphan_sources(scan: Scan) -> CleanAction | None:
    directory = scan.store.root / "sources"
    if not directory.is_dir():
        return None
    registered = {row["path"] for row in scan.store.rows("SELECT path FROM sources")}
    grace = timedelta(hours=scan.config.clean.temp_grace_hours)
    items: list[CleanItem] = []
    targets: list[Path] = []
    kept = 0
    for entry in sorted(os.scandir(directory), key=lambda item: item.name):
        if not entry.is_file(follow_symlinks=False) or entry.name.startswith("."):
            continue  # 锁文件与子目录（已注册原片所在）不碰
        relative = Path(entry.path).relative_to(scan.store.root).as_posix()
        if relative in registered:
            continue
        if datetime.fromtimestamp(entry.stat().st_mtime, UTC) >= scan.now - grace:
            kept += 1  # 宽限期内：可能是正在登记的原片
            continue
        targets.append(Path(entry.path))
        items.append(CleanItem(relative, entry.stat().st_size))

    def execute() -> dict:
        for path in targets:
            path.unlink(missing_ok=True)
        return {"deleted": len(targets)}

    if not items:
        return None
    return CleanAction(
        category="orphan_sources", level="A", stage="pipeline",
        description="sources 根下未注册且超过宽限期的文件",
        policy=f"不在 sources 表且 mtime 超过 {scan.config.clean.temp_grace_hours} 小时；"
               "已注册原片永不触碰",
        items=items, kept={"within_grace": kept}, execute=execute,
    )


# ---------------------------------------------------------------- B 级：策略过期


def transcript_checkpoints(scan: Scan) -> CleanAction | None:
    current = scan.config.asr_key
    return _profile_dirs(scan, scan.store.root / "transcripts", {current},
                         scan.config.clean.transcripts_max_age_days,
                         "transcript_checkpoints", "旧 asr_key 的转写块检查点",
                         "当前 asr_key 永不删；旧指纹除非配置改回否则永不命中，删除代价 = ASR 重听")


def analysis_checkpoints(scan: Scan) -> CleanAction | None:
    from mindex.processing.pipeline import Pipeline

    pipeline = Pipeline(scan.config)
    keep: dict[str, set[str]] = {}
    base = scan.store.root / "analysis"
    if base.is_dir():
        for source_dir in base.iterdir():
            if source_dir.is_dir():
                keep[source_dir.name] = {pipeline.annotation_profile(source_dir.name)}
    return _profile_dirs(scan, base, keep, scan.config.clean.analysis_max_age_days,
                         "analysis_checkpoints", "旧分析 profile 的场景分组检查点",
                         "当前 annotation_profile（分析配置 + 对白内容版本）永不删；旧 profile 删除代价 = LLM 重算")


def dialogue_checkpoints(scan: Scan) -> CleanAction | None:
    keep: dict[str, set[str]] = {}
    for row in scan.store.rows(
        "SELECT DISTINCT source_id, profile FROM channel_data WHERE channel='dialogue'"
    ):
        keep.setdefault(row["source_id"], set()).add(row["profile"])
    return _profile_dirs(scan, scan.store.root / "dialogue", keep,
                         scan.config.clean.dialogue_max_age_days,
                         "dialogue_checkpoints", "旧对白 profile 的增强检查点",
                         "保留集 = channel_data 现行 dialogue profile（含 processing 行，保护进行中任务）")


def _profile_dirs(scan: Scan, base: Path, keep: str | dict[str, set[str]], days: int,
                  category: str, description: str, policy: str) -> CleanAction | None:
    """按 profile 目录过期；keep 为全局集合或 {source_id: profiles}。"""
    if not base.is_dir():
        return None
    items: list[CleanItem] = []
    targets: list[Path] = []
    for source_dir in sorted(base.iterdir()):
        if not source_dir.is_dir():
            continue
        current = keep if isinstance(keep, set) else keep.get(source_dir.name, set())
        for profile_dir in sorted(source_dir.iterdir()):
            if not profile_dir.is_dir() or profile_dir.name in current:
                continue
            if not scan.expired(profile_dir, days):
                continue
            size, _ = _tree_stats(profile_dir)
            targets.append(profile_dir)
            items.append(CleanItem(
                profile_dir.relative_to(scan.store.root).as_posix(), size))

    def execute() -> dict:
        for path in targets:
            shutil.rmtree(path)
        return {"deleted": len(targets)}

    if not items:
        return None
    return CleanAction(
        category=category, level="B", stage="pipeline",
        description=description,
        policy=f"{policy}；树内最新文件早于 {days} 天才删除", items=items, execute=execute,
    )


def context_cache(scan: Scan) -> CleanAction | None:
    from filelock import FileLock, Timeout

    directory = scan.store.root / "context"
    if not directory.is_dir():
        return None
    days = scan.config.clean.context_max_age_days
    items: list[CleanItem] = []
    pairs: list[tuple[Path, Path | None]] = []
    kept = 0
    for path in sorted(directory.iterdir()):
        if not path.is_file() or not path.name.endswith(".mp4"):
            continue
        if not scan.expired(path, days):
            continue
        lock_file = path.with_name(path.name + ".lock")
        if lock_file.is_file():
            try:
                with FileLock(str(lock_file), timeout=0):
                    pass
            except Timeout:
                kept += 1  # 正在被写入，跳过
                continue
        pairs.append((path, lock_file if lock_file.is_file() else None))
        items.append(CleanItem(path.relative_to(scan.store.root).as_posix(), path.stat().st_size))

    def execute() -> dict:
        deleted = 0
        for path, lock_file in pairs:
            path.unlink(missing_ok=True)
            if lock_file is not None:
                lock_file.unlink(missing_ok=True)
            deleted += 1
        return {"deleted": deleted}

    if not items:
        return None
    return CleanAction(
        category="context_cache", level="B", stage="pipeline",
        description="超过生命期的在线播放转码缓存",
        policy=f"mtime 早于 {days} 天；下次访问秒级重转码；写入中的（锁被持）跳过",
        items=items, kept={"locked": kept}, execute=execute,
    )


def shot_media_cache(scan: Scan) -> CleanAction | None:
    from filelock import FileLock, Timeout

    directory = scan.store.root / "cache" / "shot-media"
    if not directory.is_dir():
        return None
    days = scan.config.clean.shot_media_max_age_days
    items: list[CleanItem] = []
    pairs: list[tuple[Path, Path | None]] = []
    kept = 0
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name.startswith(".") or path.name.endswith(".lock"):
            continue  # 锁文件随对应媒体同批处理
        if not scan.expired(path, days):
            continue
        lock_file = path.with_name(path.name + ".lock")
        if lock_file.is_file():
            try:
                with FileLock(str(lock_file), timeout=0):
                    pass
            except Timeout:
                kept += 1  # 播放/渲染持有缓存租约，跳过
                continue
        pairs.append((path, lock_file if lock_file.is_file() else None))
        items.append(CleanItem(path.relative_to(scan.store.root).as_posix(), path.stat().st_size))

    def execute() -> dict:
        deleted = 0
        for path, lock_file in pairs:
            path.unlink(missing_ok=True)
            if lock_file is not None:
                lock_file.unlink(missing_ok=True)
            deleted += 1
        return {"deleted": deleted}

    if not items:
        return None
    return CleanAction(
        category="shot_media_cache", level="B", stage="pipeline",
        description="超过生命期的 shot 媒体合成与探测缓存（cache/shot-media）",
        policy=f"mtime 早于 {days} 天；按内容指纹可由现行 shot 重新合成；读取中的（缓存租约锁被持）跳过",
        items=items, kept={"locked": kept}, execute=execute,
    )


def reports_exports(scan: Scan) -> CleanAction | None:
    directory = scan.store.root / "reports"
    if not directory.is_dir():
        return None
    days = scan.config.clean.reports_max_age_days
    items: list[CleanItem] = []
    targets: list[Path] = []
    for path in sorted(directory.iterdir()):
        if not scan.expired(path, days):
            continue
        size, _ = _tree_stats(path)
        targets.append(path)
        items.append(CleanItem(path.relative_to(scan.store.root).as_posix(), size))

    def execute() -> dict:
        deleted = 0
        for path in targets:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            deleted += 1
        return {"deleted": deleted}

    if not items:
        return None
    return CleanAction(
        category="reports_exports", level="B", stage="pipeline",
        description="超过生命期的导出产物（reports/）",
        policy=f"树内最新文件早于 {days} 天；可随时重新导出", items=items, execute=execute,
    )


def subtitle_cache(scan: Scan) -> CleanAction | None:
    base = scan.store.root / "subtitles"
    if not base.is_dir():
        return None
    days = scan.config.clean.subtitles_max_age_days
    items: list[CleanItem] = []
    targets: list[Path] = []
    for source_dir in sorted(base.iterdir()):
        if not source_dir.is_dir() or source_dir.name.startswith("."):
            continue
        for path in source_dir.iterdir():
            if not path.is_file() or not (
                path.name.endswith(".json") or path.name.endswith(".json.lock")
                or path.name.endswith(".json.partial")
            ):
                continue  # uploads/ 子目录与其它文件不碰
            if not scan.expired(path, days):
                continue
            targets.append(path)
            items.append(CleanItem(path.relative_to(scan.store.root).as_posix(), path.stat().st_size))

    def execute() -> dict:
        for path in targets:
            path.unlink(missing_ok=True)
        return {"deleted": len(targets)}

    if not items:
        return None
    return CleanAction(
        category="subtitle_cache", level="B", stage="pipeline",
        description="超过生命期的字幕轨道解析缓存",
        policy=f"mtime 早于 {days} 天；uploads/ 用户原件永不触碰；缓存键含原片字节与 mtime，按需重提取",
        items=items, execute=execute,
    )


def agent_attachments(scan: Scan) -> CleanAction | None:
    """助手附件：过期记录（连同目录）、无记录的孤儿 token 目录、.uploads/ 上传暂存残留。

    agent.sqlite3 不存在时不创建它：此时没有任何登记记录，只按宽限期回收孤儿与暂存残留。
    """
    from mindex.agent.attachments import DIRNAME, TOKEN_RE, AttachmentStore, token

    root = scan.store.root / DIRNAME
    database = scan.store.root / "agent.sqlite3"
    has_db = database.is_file()
    if not root.is_dir() and not has_db:
        return None
    attachments = AttachmentStore(scan.store.root) if has_db else None
    grace_hours = scan.config.clean.temp_grace_hours
    cutoff = scan.now - timedelta(hours=grace_hours)
    stamp = int(scan.now.timestamp() * 1000)

    def stale(path: Path) -> bool:
        _, newest = _tree_stats(path)
        return newest == 0.0 or datetime.fromtimestamp(newest, UTC) < cutoff

    items: list[CleanItem] = []
    expired: list[str] = []
    orphans: list[Path] = []
    staging: list[Path] = []
    kept = {"within_grace": 0}

    for meta in attachments.expired(stamp) if attachments else []:
        folder = root / token(meta["id"])
        size, _ = _tree_stats(folder)
        expired.append(meta["id"])
        items.append(CleanItem(f"{DIRNAME}/{folder.name}", size,
                               {"reason": "expired", "filename": meta["filename"]}))
    registered = attachments.tokens() if attachments else set()
    if root.is_dir():
        for entry in sorted(os.scandir(root), key=lambda item: item.name):
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False) and TOKEN_RE.fullmatch(entry.name):
                if entry.name in registered:
                    continue  # 有记录：未过期保留，过期的已在上面列出
                if not stale(path):
                    kept["within_grace"] += 1  # 可能正在登记（目录先建、记录后写）
                    continue
                size, _ = _tree_stats(path)
                orphans.append(path)
                items.append(CleanItem(f"{DIRNAME}/{entry.name}", size, {"reason": "orphan"}))
            elif entry.name == ".uploads" and entry.is_dir(follow_symlinks=False):
                for child in sorted(path.iterdir()):
                    if not child.is_file():
                        continue
                    if not stale(child):
                        kept["within_grace"] += 1  # 进行中的上传
                        continue
                    staging.append(child)
                    items.append(CleanItem(f"{DIRNAME}/.uploads/{child.name}", child.stat().st_size,
                                           {"reason": "staging"}))

    def execute() -> dict:
        deleted = 0
        for ref in expired:
            try:
                attachments.delete(ref)  # 记录与目录一起删除
            except Exception:  # noqa: BLE001 — 扫描后已被会话删除级联清掉
                continue
            deleted += 1
        # 扫描在持锁前完成：执行时重新读取登记集，期间刚登记的目录不删。
        current = attachments.tokens() if attachments else set()
        for path in orphans:
            if path.name in current:
                continue
            shutil.rmtree(path, ignore_errors=True)
            deleted += 1
        for path in staging:
            path.unlink(missing_ok=True)
            deleted += 1
        return {"deleted": deleted}

    if not items:
        return None
    return CleanAction(
        category="agent_attachments", level="B", stage="pipeline",
        description="过期助手附件",
        policy=f"记录超过 agent.attachment_ttl_hours（{scan.config.agent.attachment_ttl_hours} 小时）"
               f"有效期即删除记录与文件；无记录的附件目录与 .uploads/ 暂存残留超过 "
               f"{grace_hours} 小时宽限期才删除；agent.sqlite3 其余内容不触碰",
        items=items, kept=kept, execute=execute,
    )


def runs_history(scan: Scan) -> CleanAction | None:
    days = scan.config.clean.runs_max_age_days
    cutoff = (scan.now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    stale = scan.store.rows(
        "SELECT id FROM runs WHERE status IN ('complete','failed') "
        "AND finished_at IS NOT NULL AND finished_at < ?", (cutoff,)
    )
    if not stale:
        return None
    ids = [row["id"] for row in stale]

    def execute() -> dict:
        with scan.store.connect() as db:
            db.executemany("DELETE FROM runs WHERE id=? AND status != 'running'", [(i,) for i in ids])
        return {"deleted": len(ids)}

    return CleanAction(
        category="runs_history", level="B", stage="pipeline",
        description="runs 审计表中超期的已完成记录",
        policy=f"finished_at 早于 {days} 天且非 running；行删除不释放库文件空间，需 VACUUM（见 notes）",
        items=[CleanItem(f"runs#{row}", 0) for row in ids], execute=execute,
    )


# ---------------------------------------------------------------- 入口


RULES: list[Callable[[Scan], CleanAction | None]] = [
    temp_crash,
    ds_store,
    uploads_residue,
    orphan_clips,
    orphan_thumbnails,
    orphan_context,
    orphan_person_png,
    person_frames,
    orphan_voices,
    orphan_sources,
    transcript_checkpoints,
    analysis_checkpoints,
    dialogue_checkpoints,
    context_cache,
    shot_media_cache,
    reports_exports,
    subtitle_cache,
    agent_attachments,
    runs_history,
]


def _all_rules() -> list[Callable[[Scan], CleanAction | None]]:
    from mindex.maintenance.clean_index import RULES as index_rules
    from mindex.maintenance.clean_studio import RULES as studio_rules

    return RULES + index_rules + studio_rules


def clean_library(config: Config, *, apply: bool = False, now: datetime | None = None) -> dict:
    """扫描（并可选执行）全部清理规则；返回统一 JSON 报告。"""
    store = Store(config.data_dir)
    scan = Scan(config, store, now or datetime.now(UTC))
    actions: list[CleanAction] = []
    for rule in _all_rules():
        try:
            action = rule(scan)
        except Exception as exc:  # noqa: BLE001 — 单规则失败不拖垮整体扫描
            log.warning("清理规则 %s 扫描失败：%s", getattr(rule, "__name__", rule), exc)
            continue
        if action is not None:
            actions.append(action)
    if apply:
        with store.run("clean"):
            _apply(config, store, actions)
    return _report(store, actions, applied=apply, scanned_at=scan.now)


def _apply(config: Config, store: Store, actions: list[CleanAction]) -> None:
    """两段持锁执行：管线+索引段 → 释放 → studio 段；锁冲突抛 CleanBusy 或标记 skipped。"""
    from filelock import FileLock, Timeout

    from mindex.shared.processing_lock import SharedFileLock, index_write_lock

    pipeline_actions = [action for action in actions if action.stage == "pipeline"]
    studio_actions = [action for action in actions if action.stage == "studio"]

    guard = SharedFileLock(config.data_dir / "pipeline.lock", exclusive=True)
    try:
        guard.acquire()
    except Timeout:
        raise CleanBusy("有离线处理任务正在运行，无法安全清理；请等待管线空闲后重试") from None
    try:
        # 独占 pipeline.lock 已与全部 ProcessingLock 用户互斥；index-write 短等待兜底
        # 未走 ProcessingLock 的写入方。
        write = index_write_lock(store.root)
        write.timeout = 10
        try:
            write.acquire()
        except Timeout:
            raise CleanBusy("索引正在构建或发布（index-write.lock 被持有），请稍后重试") from None
        try:
            for action in pipeline_actions:
                _execute(action)
        finally:
            write.release()
    finally:
        guard.release()

    if not studio_actions:
        return
    from mindex.studio.studio import lock_path

    studio_lock = FileLock(str(lock_path(config)), timeout=0, thread_local=False)
    try:
        studio_lock.acquire()
    except Timeout:
        for action in studio_actions:
            action.skipped = "创作任务正在执行，已跳过 studio 清理"
        return
    try:
        for action in studio_actions:
            _execute(action)
    finally:
        studio_lock.release()


def _execute(action: CleanAction) -> None:
    if action.execute is None:
        return
    try:
        action.result = action.execute()
    except Exception as exc:  # noqa: BLE001 — 单类别失败不中断其余类别
        action.error = str(exc) or type(exc).__name__
        log.warning("清理类别 %s 执行失败：%s", action.category, action.error)


def _report(store: Store, actions: list[CleanAction], *, applied: bool,
            scanned_at: datetime) -> dict:
    return {
        "applied": applied,
        "scanned_at": scanned_at.isoformat(),
        "categories": [action.report() for action in actions],
        "notes": _notes(store),
        "total": {
            "bytes": sum(action.total_bytes for action in actions),
            "item_count": sum(len(action.items) for action in actions),
            "categories": len(actions),
        },
    }


def _notes(store: Store) -> list[dict]:
    """C 级待调整项：只报告大小，clean 永不自动删除。"""
    notes = []
    for name, advice in PROTECTED_NOTES:
        directory = store.root / name
        if not directory.exists():
            continue
        size, _ = _tree_stats(directory)
        notes.append({"path": name, "bytes": size, "advice": advice})
    for path in sorted(store.root.glob("metadata.sqlite3.bak-*")):
        size, _ = _tree_stats(path)
        notes.append({
            "path": path.relative_to(store.root).as_posix(), "bytes": size,
            "advice": "人工数据库备份（C 级待调整）：确认无需回退后可手动删除",
        })
    if (store.root / "metadata.sqlite3").is_file():
        notes.append({
            "path": "metadata.sqlite3", "bytes": 0,
            "advice": "大量删除后可停服执行 sqlite3 metadata.sqlite3 'VACUUM' 回收库文件空间（需独占库）",
        })
    return notes
