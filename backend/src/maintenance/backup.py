"""离线、可验证的库迁移；发布前的一切工作只发生在同文件系统暂存区。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import yaml

from maintenance.backup_db import (
    filter_database,
    interrupt_tasks,
    merge_database,
    redact,
    remap_local_refs,
    rewrite_database,
    snapshot,
    store_at,
    upgrade_database,
    validate_database,
    walk,
)
from maintenance.backup_files import (
    _config_dependencies,
    _copy_payload_file,
    _fsync_directory,
    _saved_secrets,
    _sync_tree,
    compatibility_files,
    digest,
    excluded,
    file_owners,
    files_under,
    finder_metadata,
    immutable_evidence,
    job_selection,
    offline,
    referenced_files,
    remove_tree,
    safe_path,
    selection,
    source_list,
    temporary_directory,
    validate_media,
    vector_worker,
    verification_mode,
    verification_report,
)
from shared.config import Config, fingerprint
from shared.storage import dumps, require_sqlite
from shared.work_budget import copy_file

FORMAT_VERSION = 1


def build_bundle(
    config,
    output,
    *,
    selected,
    include_secrets,
    config_path,
    allow_legacy_representations=False,
    verification="full",
):

    require_sqlite(config, "业务备份导出（PostgreSQL 一致性导出在阶段 5 交付）")
    root = config.data_dir.resolve()
    payload = output / "data"
    payload.mkdir(parents=True)
    known = source_list(root)
    chosen = {s["id"] for s in known} if selected is None else selected
    jobs, omitted = job_selection(root, selected, {s["id"] for s in known})
    snapshot(store_at(root).path, payload / "metadata.sqlite3")
    maps = filter_database(
        payload / "metadata.sqlite3", selected, allow_legacy_representations=allow_legacy_representations
    )
    ids = {k: v for mapping in maps.values() for k, v in mapping.items()}
    transform = lambda value: ids.get(value, value) if isinstance(value, str) else value
    secrets = _saved_secrets(root)
    rewrite_database(payload / "metadata.sqlite3", transform, secrets=include_secrets)
    base = (
        yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if config_path.is_file()
        else config.model_dump(mode="json")
    )
    base = base or {}
    if not include_secrets:
        base = redact(base)

        # Service.api_key 允许字符串；被删除的 YAML 字段直接去掉。
        def clean(value):
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items() if v is not None or k != "api_key"}
            return value

        base = clean(base)
    configs = [config.model_dump(mode="json"), base]
    for table in ("model_profiles", "dialogue_tasks", "source_tasks", "person_tasks"):
        configs.extend(
            json.loads(row["config"]) for row in store_at(payload).rows(f"SELECT config FROM {table}")
        )
    for path in (root / "studio").glob("*/job.json"):
        if path.parent.name in jobs:
            configs.append(json.loads(path.read_text(encoding="utf-8")))
    deps, missing, env = _config_dependencies(configs, config_path.parent, payload, data_root=root)
    owners = file_owners(root, known)
    references = referenced_files(root, payload / "metadata.sqlite3", available_files=owners)
    technical = compatibility_files(root, payload / "metadata.sqlite3")
    exclusions = []
    global_dirs = ("voices/", "config-dependencies/")
    for origin in files_under(root):
        relative = origin.relative_to(root).as_posix()
        reason = "macOS AppleDouble 文件元数据" if finder_metadata(origin) else excluded(relative)
        assigned = owners.get(relative, set())
        if relative in technical:
            reason = None
        elif selected is not None and not reason:
            if relative.startswith("studio/"):
                parts = Path(relative).parts
                if len(parts) < 3 or parts[1] not in jobs:
                    reason = "创作任务依赖不完整或未知"
            elif assigned - chosen:
                reason = "文件引用未选素材"
            elif not (assigned or relative in references or relative.startswith(global_dirs)):
                reason = "部分备份不包含归属未知的历史文件或缓存"
        if reason:
            exclusions.append({"path": relative, "reason": reason})
            continue
        if relative in technical or immutable_evidence(relative):
            destination = payload / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            # 证据摘要和部署 ID 绑定原始字节，禁止重写已验证内容。
            if origin.suffix == ".json" and not include_secrets:
                text = origin.read_text(encoding="utf-8")
                value = json.loads(text)
                if redact(value) != value or any(secret in text for secret in secrets):
                    raise ValueError(f"兼容性证据含凭据，不能脱敏后沿用验证：{relative}")
            copy_file(origin, destination)
            continue
        _copy_payload_file(
            origin,
            payload / relative,
            transform=transform,
            include_secrets=include_secrets,
            secrets=secrets,
            selected=selected,
            allow_legacy_representations=allow_legacy_representations,
        )
    (output / "config").mkdir()
    (output / "config/base.yaml").write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
    validate_database(payload, basic=verification == "basic")
    validate_media(payload)
    collections = vector_worker(
        "dump",
        payload,
        output,
        origin=str(root),
        full=selected is None,
        original_indexes={v: k for k, v in maps["indexes"].items()},
        original_representations={v: k for k, v in maps["representations"].items()},
        verification=verification,
    )
    manifest = {
        "format": "mindex-backup",
        "version": FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "full": selected is None,
        "includes_secrets": include_secrets,
        "export_verification": verification,
        "legacy_representation_sources": sorted(selected) if allow_legacy_representations else [],
        "origin": {"data_dir": str(root), "config": str(config_path)},
        "sources": [s for s in known if s["id"] in chosen],
        "collections": collections,
        "id_maps": maps,
        "path_map": deps,
        "jobs": jobs,
        "excluded_jobs": omitted,
        "excluded": exclusions,
        "required_environment": env,
        "missing_dependencies": missing,
        "files": [],
    }
    for path in files_under(output):
        if finder_metadata(path):
            continue
        relative = path.relative_to(output).as_posix()
        manifest["files"].append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": digest(path),
                "sources": sorted(owners.get(relative.removeprefix("data/"), set()) & chosen),
            }
        )
    (output / "manifest.json").write_text(dumps(manifest), encoding="utf-8")
    return manifest


def inspect_bundle(bundle, *, verification="full"):
    verification_mode(verification)
    try:
        return _inspect_bundle(bundle, verification=verification)
    except json.JSONDecodeError as exc:
        raise ValueError(f"备份 JSON 无效：{exc}") from exc
    except (KeyError, TypeError, AttributeError, IndexError, sqlite3.DatabaseError) as exc:
        raise ValueError(f"备份结构或数据库无效：{exc}") from exc


def _inspect_bundle(bundle, *, verification="full"):
    bundle = bundle.resolve()
    manifest = json.loads(safe_path(bundle, "manifest.json").read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != "mindex-backup"
        or type(manifest.get("version")) is not int
        or manifest.get("version") != FORMAT_VERSION
    ):
        raise ValueError("不支持的备份格式或版本")
    listed = set()
    for item in manifest["files"]:
        path = safe_path(bundle, item["path"])
        if path.name.endswith(("-wal", "-shm", "-journal")):
            raise ValueError("备份不接受 SQLite 事务旁路文件")
        if item["path"] in listed:
            raise ValueError(f"清单包含重复路径：{item['path']}")
        listed.add(item["path"])
        if (
            not path.is_file()
            or path.stat().st_size != item["bytes"]
            or verification == "full"
            and digest(path) != item["sha256"]
        ):
            raise ValueError(f"备份文件缺失或校验失败：{item['path']}")
    if not {"data/metadata.sqlite3", "config/base.yaml"} <= listed:
        raise ValueError("备份缺少数据库或基础配置")
    actual = {p.relative_to(bundle).as_posix() for p in files_under(bundle)} - {"manifest.json"}
    unregistered = sorted(path for path in actual - listed if not finder_metadata(bundle / path))
    if unregistered:
        preview = "、".join(unregistered[:10])
        remaining = (
            f"（另有 {len(unregistered) - 10} 个，共 {len(unregistered)} 个）"
            if len(unregistered) > 10
            else ""
        )
        raise ValueError("备份含未登记文件：" + preview + remaining)
    indexes = {r["id"] for r in store_at(bundle / "data").rows("SELECT id FROM index_versions")}
    if {item["index_id"] for item in manifest["collections"]} != indexes:
        raise ValueError("向量集合与数据库的索引版本不一致")
    if len(manifest["collections"]) != len(indexes):
        raise ValueError("备份含重复向量集合")
    for collection in manifest["collections"]:
        if any(name not in listed for name in collection["batches"]):
            raise ValueError("备份缺少向量批次")
        if verification == "basic":
            count = store_at(bundle / "data").one(
                "SELECT count(*) AS n FROM representations WHERE index_id=?", (collection["index_id"],)
            )["n"]
            if collection["count"] != count:
                raise ValueError("向量清单数量与数据库不一致")
            continue
        identifiers = set()
        count = 0
        for name in collection["batches"]:
            rows = json.loads(safe_path(bundle, name).read_text(encoding="utf-8"))
            count += len(rows)
            identifiers.update(r["id"] for r in rows)
        if count != collection["count"] or len(identifiers) != count:
            raise ValueError("向量批次数量或 ID 不一致")
        expected = {
            r["id"]
            for r in store_at(bundle / "data").rows(
                "SELECT id FROM representations WHERE index_id=?", (collection["index_id"],)
            )
        }
        if identifiers != expected:
            raise ValueError("备份缺少必需向量或包含未知表示")
    for relative in manifest["path_map"].values():
        safe_path(bundle / "data", relative)
        if "data/" + relative not in listed:
            raise ValueError("备份缺少配置依赖")
    current_sources = {row["id"]: row for row in source_list(bundle / "data")}
    original_sources = {row["id"]: row for row in source_list(bundle / "data", original_names=True)}
    # v1 早期备份的清单使用原始文件名；不改变其数据库中的人工命名。
    recorded_sources = manifest["sources"]
    if (
        len(recorded_sources) != len(current_sources)
        or len({row["id"] for row in recorded_sources}) != len(recorded_sources)
        or any(
            row not in (current_sources.get(row["id"]), original_sources.get(row["id"]))
            for row in recorded_sources
        )
    ):
        raise ValueError("备份素材清单与数据库不一致")
    validate_database(bundle / "data", basic=verification == "basic", legacy=True)
    validate_media(bundle / "data")
    manifest["sources"] = list(current_sources.values())
    return manifest


def summary(manifest):
    return {
        key: manifest[key]
        for key in (
            "version",
            "full",
            "includes_secrets",
            "sources",
            "collections",
            "id_maps",
            "jobs",
            "excluded_jobs",
            "required_environment",
            "missing_dependencies",
        )
    } | {
        "files": len(manifest["files"]),
        "bytes": sum(f["bytes"] for f in manifest["files"]),
        "legacy_representation_sources": manifest.get("legacy_representation_sources", []),
        "excluded": manifest["excluded"],
        "configuration": "基础 YAML 与 SQLite 运行时覆盖层分别保存",
    }


def _combined_maps(original, current, selected):
    combined = {category: dict(mapping) for category, mapping in current.items()}
    for category, mapping in original.items():
        for old, intermediate in mapping.items():
            if selected is None or intermediate in current[category]:
                combined[category][old] = current[category].get(intermediate, intermediate)
    return combined


def export_backup(
    config,
    destination,
    *,
    all_sources=False,
    sources=None,
    include_secrets=False,
    dry_run=False,
    config_path=None,
    progress=None,
    allow_legacy_representations=False,
    verification="full",
):
    verification_mode(verification)
    root, destination = config.data_dir.resolve(), destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("导出目标必须不存在")
    if destination.resolve().is_relative_to(root):
        raise ValueError("不能备份到数据目录内部")
    config_path = (config_path or Path("config.local.yaml")).resolve()
    selected = selection(source_list(root), all_sources, sources)
    if allow_legacy_representations and (selected is None or len(selected) != 1):
        raise ValueError("--allow-legacy-representations 仅允许显式选择单个临时素材")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with offline(root), temporary_directory(prefix=".mindex-export-", dir=destination.parent) as temporary:
        if progress:
            if verification == "basic":
                progress("基础校验模式：省去 SHA256 复算、向量归属复核和批次重复解析。")
            progress("正在创建 SQLite 一致快照、计算素材依赖并导出向量…")
        staging = Path(temporary) / "bundle"
        staging.mkdir()
        manifest = build_bundle(
            config,
            staging,
            selected=selected,
            include_secrets=include_secrets,
            config_path=config_path,
            allow_legacy_representations=allow_legacy_representations,
            verification=verification,
        )
        inspect_bundle(staging, verification=verification)
        if not dry_run:
            # 再检查一次，避免构建期间意外覆盖他人创建的目录。
            if destination.exists():
                raise ValueError("导出目标已存在")
            _sync_tree(staging)
            staging.rename(destination)
            _fsync_directory(destination.parent)
        return summary(manifest) | {
            "ok": True,
            "dry_run": dry_run,
            "destination": str(destination),
            **verification_report(verification, exporting=True),
        }


def _relocator(origin, target, path_map, ids):
    def transform(value):
        if not isinstance(value, str):
            return value
        if value in ids:
            return ids[value]
        if value in path_map:
            return str(target / path_map[value])
        if value == origin or value.startswith(origin + "/"):
            return str(target) + value[len(origin) :]
        return value

    return transform


def _portable_base_config(base, root, config_path):
    """项目内客户端路径相对配置文件保存，外置数据目录保留绝对路径。"""
    parent = config_path.parent

    def visit(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key in {"data_dir", "deployment_manifest", "font_file"} and isinstance(item, str):
                    path = Path(item)
                    if path.is_absolute() and path.is_relative_to(root) and path.is_relative_to(parent):
                        item = path.relative_to(parent).as_posix()
                result[key] = visit(item)
            return result
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    return visit(base)


def _filter_payload(bundle, incoming, selected, manifest):
    # 备份可能是较早结构：先按核心表快照，暂存副本随后升级到当前 schema 再过滤。
    snapshot(bundle / "data/metadata.sqlite3", incoming / "metadata.sqlite3", allow_legacy=True)
    upgrade_database(incoming / "metadata.sqlite3")
    maps = filter_database(incoming / "metadata.sqlite3", selected)
    kept = {s["id"] for s in source_list(incoming)}
    jobs, omitted = job_selection(bundle / "data", selected, {s["id"] for s in manifest["sources"]})
    owners = {
        f["path"].removeprefix("data/"): set(f["sources"])
        for f in manifest["files"]
        if f["path"].startswith("data/")
    }
    references = referenced_files(bundle / "data", incoming / "metadata.sqlite3")
    technical = compatibility_files(bundle / "data", incoming / "metadata.sqlite3")
    for item in manifest["files"]:
        if not item["path"].startswith("data/") or item["path"] == "data/metadata.sqlite3":
            continue
        relative = item["path"].removeprefix("data/")
        assigned = owners.get(relative, set())
        if selected is not None and relative not in technical:
            if relative.startswith("studio/"):
                if Path(relative).parts[1] not in jobs:
                    continue
            elif assigned - kept or not (
                assigned or relative in references or relative.startswith(("voices/", "config-dependencies/"))
            ):
                continue
        target = safe_path(incoming, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        copy_file(safe_path(bundle, item["path"]), target)
    return maps, omitted


def _merge_published_persons(store, origins, target):
    values = {"format": 1, "entities": [], "evidence": [], "coverage": []}
    for category, key in [("entities", "id"), ("evidence", "id"), ("coverage", "cache_id")]:
        unique = {}
        for origin in origins:
            person = origin.get("person_version")
            row = store.one("SELECT data FROM person_versions WHERE id=?", (person,)) if person else None
            for item in json.loads(row["data"]).get(category, []) if row else []:
                if item[key] in unique and unique[item[key]] != item:
                    raise ValueError(f"已发布人物数据冲突：{category}/{item[key]}")
                unique[item[key]] = item
        values[category] = sorted(unique.values(), key=lambda item: item[key])
    person = fingerprint(values) if any(o.get("person_version") for o in origins) else None
    if person:
        store.execute("INSERT OR IGNORE INTO person_versions(id,data) VALUES(?,?)", (person, dumps(values)))
    release = fingerprint([target, person])
    store.execute(
        "INSERT OR IGNORE INTO search_releases(id,content_id,person_id) VALUES(?,?,?)",
        (release, target, person),
    )
    store.activate_release(release)


def union_index(stage, originals):
    store = store_at(stage)
    first, second = originals
    check_compatible(first, second, store, store)
    members = {}
    for version in originals:
        for member in store.rows("SELECT * FROM index_members WHERE index_id=?", (version["id"],)):
            if member["segment_id"] in members:
                raise ValueError("联合索引含重复素材表示")
            members[member["segment_id"]] = member
    spec = dict(first["spec"])
    spec["manifest"] = fingerprint([json.loads(members[key]["snapshot"]) for key in sorted(members)])
    identifier = fingerprint(spec)
    store.execute(
        "INSERT INTO index_versions(id,spec,status) VALUES(?,?,?)", (identifier, dumps(spec), "ready")
    )
    for member in members.values():
        store.execute(
            "INSERT INTO index_members VALUES(?,?,?,?)",
            (identifier, member["segment_id"], member["snapshot"], member["complete"]),
        )
    vector_worker("union", stage, stage, index_id=identifier, origins=[v["id"] for v in originals])
    _merge_published_persons(store, originals, identifier)
    return identifier


def check_compatible(first, second, first_store, second_store):
    # 合并既有向量只约束嵌入空间及索引结构；分析/ASR 等来源配置保留作历史记录。
    for key in ("space", "format", "preprocessing", "mode"):
        if first["spec"].get(key) != second["spec"].get(key):
            raise ValueError(f"活动索引不兼容：{key}")


def _write_journal(path, value):
    temporary = path.with_suffix(".tmp")
    if path.is_symlink() or temporary.is_symlink():
        raise ValueError("迁移日志不能是符号链接")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(dumps(value))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    _fsync_directory(path.parent)


def journal_path(root):
    return root.parent / f".{root.name}.migration.json"


def recover_import(root, *, config_path):
    root, config_path = root.resolve(), config_path.resolve()
    with offline(root):
        return _recover(root, config_path)


def _recover(root, config_path):
    journal = journal_path(root)
    if journal.is_symlink():
        raise ValueError("迁移日志不能是符号链接")
    if not journal.exists():
        return {"ok": True, "recovered": False}
    value = json.loads(journal.read_text(encoding="utf-8"))
    if value["target"] != str(root) or value["config"] != str(config_path):
        raise ValueError("中断日志的目标与本次命令不同；请用原 --data-dir 和 --config 恢复")
    # 日志中的临时路径必须是本次目标的同级保留名称，不能用日志删除任意路径。
    paths = {}
    for key in ("stage", "old", *(["work"] if value.get("work") else [])):
        path = Path(value[key])
        if (
            path.parent != root.parent
            or not path.name.startswith(f".{root.name}.migration-")
            or path.is_symlink()
        ):
            raise ValueError("中断日志包含不安全路径")
        paths[key] = path
    config_old = config_path.parent / value["config_old"]
    config_new = config_path.parent / value["config_new"]
    for path in (config_old, config_new):
        if (
            path.parent != config_path.parent
            or not path.name.startswith(f".{config_path.name}.migration-")
            or path.is_symlink()
        ):
            raise ValueError("中断日志包含不安全配置路径")
    if value["phase"] != "committed":
        if paths["old"].exists():
            if root.exists():
                shutil.rmtree(root)
            paths["old"].rename(root)
        elif not value["had_data"] and not paths["stage"].exists() and root.exists():
            shutil.rmtree(root)
        if value["write_config"]:
            if config_old.exists():
                config_old.replace(config_path)
            elif not value["had_config"] and not config_new.exists():
                config_path.unlink(missing_ok=True)
    for path in paths.values():
        # 提交/回滚已完成，剩余临时目录只是磁盘垃圾；清理竞态不能把迁移结果标成失败。
        remove_tree(path)
    config_old.unlink(missing_ok=True)
    config_new.unlink(missing_ok=True)
    journal.unlink()
    _fsync_directory(root.parent)
    return {
        "ok": True,
        "recovered": True,
        "result": "已完成清理" if value["phase"] == "committed" else "已回滚旧库",
    }


def _switch(stage, root, config_path, config_text, *, work=None):
    token = uuid4().hex
    old = root.parent / f".{root.name}.migration-old-{token}"
    config_old = config_path.parent / f".{config_path.name}.migration-old-{token}"
    config_new = config_path.parent / f".{config_path.name}.migration-new-{token}"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "target": str(root),
        "config": str(config_path),
        "stage": str(stage),
        "old": str(old),
        "work": str(work) if work else None,
        "config_old": config_old.name,
        "config_new": config_new.name,
        "had_data": root.exists(),
        "had_config": config_path.exists(),
        "write_config": config_text is not None,
        "phase": "prepared",
    }
    _sync_tree(stage)
    try:
        if config_text is not None:
            if config_path.exists():
                copy_file(config_path, config_old)
                with config_old.open("rb") as stream:
                    os.fsync(stream.fileno())
            with config_new.open("w", encoding="utf-8") as stream:
                stream.write(config_text)
                stream.flush()
                os.fsync(stream.fileno())
            _fsync_directory(config_path.parent)
        _write_journal(journal_path(root), value)
        if root.exists():
            root.rename(old)
        _fsync_directory(root.parent)
        stage.rename(root)
        _fsync_directory(root.parent)
        if config_text is not None:
            config_new.replace(config_path)
            _fsync_directory(config_path.parent)
        value["phase"] = "committed"
        _write_journal(journal_path(root), value)
    except BaseException:
        if journal_path(root).exists():
            _recover(root, config_path)
        else:
            config_old.unlink(missing_ok=True)
            config_new.unlink(missing_ok=True)
        raise
    _recover(root, config_path)


def import_backup(
    config,
    bundle,
    *,
    all_sources=False,
    sources=None,
    skip_existing=False,
    restore_config=False,
    dry_run=False,
    config_path=None,
    progress=None,
    verification="full",
):
    verification_mode(verification)

    require_sqlite(config, "业务备份导入（PostgreSQL 装载在阶段 5 交付）")
    bundle, root = bundle.resolve(), config.data_dir.resolve()
    config_path = (config_path or Path("config.local.yaml")).resolve()
    if bundle.is_relative_to(root) or root.is_relative_to(bundle):
        raise ValueError("备份目录与目标数据目录不能相互包含")
    if config_path.is_relative_to(root):
        raise ValueError("基础配置必须放在数据目录外")
    if progress:
        progress(
            "基础校验模式：信任备份内容；省去 SHA256 复算、逐向量复核和分镜探测。"
            if verification == "basic"
            else "正在校验备份格式、校验和及媒体依赖…"
        )
    manifest = inspect_bundle(bundle, verification=verification)
    selected = selection(manifest["sources"], all_sources, sources)
    if journal_path(root).exists():
        raise ValueError("发现中断迁移；请先运行 backup recover（使用相同的 --data-dir 和 --config）")
    with offline(root), temporary_directory(prefix=f".{root.name}.migration-work-", dir=root.parent) as tmp:
        work = Path(tmp)
        incoming, stage = work / "incoming", root.parent / f".{root.name}.migration-stage-{uuid4().hex}"
        incoming.mkdir()
        existing = source_list(root) if (root / "metadata.sqlite3").is_file() else []
        known = {s["id"] for s in existing}
        chosen = {s["id"] for s in manifest["sources"]} if selected is None else selected
        duplicates = chosen & known
        if duplicates and not skip_existing:
            raise ValueError(
                "素材已存在；使用 --skip-existing 保留目标及衍生数据：" + "、".join(sorted(duplicates))
            )
        if duplicates:
            selected = chosen - duplicates
        full_restore = not existing
        write_config = full_restore or restore_config
        maps, omitted = _filter_payload(bundle, incoming, selected, manifest)
        ids = {k: v for mapping in maps.values() for k, v in mapping.items()}
        relocate = _relocator(manifest["origin"]["data_dir"], root, manifest["path_map"], ids)
        rewrite_database(incoming / "metadata.sqlite3", relocate, secrets=True)
        for path in files_under(incoming):
            if finder_metadata(path):
                continue
            if path.suffix in {".json", ".yaml", ".yml"} and not immutable_evidence(
                path.relative_to(incoming).as_posix()
            ):
                _copy_payload_file(
                    path, path, transform=relocate, include_secrets=True, secrets=[], selected=selected
                )
        base = yaml.safe_load((bundle / "config/base.yaml").read_text(encoding="utf-8")) or {}
        base = walk(base, relocate)
        base["data_dir"] = str(root)
        Config.model_validate(base)
        base = _portable_base_config(base, root, config_path)
        config_text = yaml.safe_dump(base, allow_unicode=True) if write_config else None
        current_base = (
            yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
        )
        # 差异仅输出字段名和脱敏值，--json 不暴露现有密钥。
        config_diff = {"before": redact(current_base), "after": redact(base)} if write_config else {}
        report = {
            "ok": True,
            "dry_run": dry_run,
            "sources": source_list(incoming),
            **verification_report(verification),
            "skipped_sources": sorted(duplicates),
            "excluded_jobs": omitted,
            "config_action": "恢复备份配置" if write_config else "保留目标配置",
            "config_diff": config_diff,
            "id_maps": _combined_maps(manifest["id_maps"], maps, selected),
            "target": str(root),
            "required_environment": manifest["required_environment"],
            "missing_dependencies": manifest["missing_dependencies"],
        }
        report["dependencies"] = {
            "historical_indexes": [
                r["id"] for r in store_at(incoming).rows("SELECT id FROM index_versions ORDER BY id")
            ],
            "files": len(files_under(incoming)),
            "jobs": sorted(p.name for p in (incoming / "studio").glob("*") if p.is_dir()),
        }
        if progress:
            progress("配置处理：" + report["config_action"])
            if config_diff:
                progress("配置差异（已脱敏）：" + dumps(config_diff))
            progress("正在暂存并合并数据；通过 API 重建向量集合…")
        incoming_active = store_at(incoming).active_index()
        target_store = store_at(root) if (root / "metadata.sqlite3").is_file() else None
        prior_active = target_store.active_index() if target_store else None
        if prior_active and incoming_active:
            check_compatible(prior_active, incoming_active, target_store, store_at(incoming))
        try:
            stage.mkdir()
            prior_active = None
            if (root / "metadata.sqlite3").exists():
                # 目标也通过 backup API + Chroma API 重建，绝不拷贝内部 HNSW 文件。
                # 目标库可能是旧结构：暂存副本升级后再参与合并。
                snapshot(root / "metadata.sqlite3", stage / "metadata.sqlite3", allow_legacy=True)
                upgrade_database(stage / "metadata.sqlite3")
                prior_active = store_at(root).active_index()
                for path in files_under(root):
                    relative = path.relative_to(root).as_posix()
                    if (
                        relative.split("/")[0] in {"chroma", "locks"}
                        or path.name
                        in {
                            "metadata.sqlite3",
                            "metadata.sqlite3-wal",
                            "metadata.sqlite3-shm",
                        }
                        or path.suffix == ".lock"
                    ):
                        continue
                    destination = stage / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    copy_file(path, destination)
                target_vectors = work / "target-vectors"
                target_vectors.mkdir()
                collections = vector_worker(
                    "dump",
                    stage,
                    target_vectors,
                    origin=str(root),
                    original_indexes={},
                    original_representations={},
                    full=True,
                    verification=verification,
                )
                vector_worker(
                    "load",
                    stage,
                    target_vectors,
                    collections=collections,
                    index_map={c["index_id"]: c["index_id"] for c in collections},
                    rep_map={},
                )
                report["integer_id_maps"] = merge_database(stage, incoming, restore_config=write_config)
                for path in files_under(incoming):
                    if finder_metadata(path):
                        continue
                    relative = path.relative_to(incoming).as_posix()
                    if relative.startswith("metadata.sqlite3"):
                        continue
                    if path.suffix in {".json", ".yaml", ".yml"} and not immutable_evidence(relative):
                        value = (
                            json.loads(path.read_text(encoding="utf-8"))
                            if path.suffix == ".json"
                            else yaml.safe_load(path.read_text(encoding="utf-8"))
                        )
                        remapped = remap_local_refs(value, report["integer_id_maps"])
                        if remapped != value:
                            path.write_text(
                                dumps(remapped)
                                if path.suffix == ".json"
                                else yaml.safe_dump(remapped, allow_unicode=True),
                                encoding="utf-8",
                            )
                    destination = safe_path(stage, relative)
                    if destination.exists():
                        if digest(path) != digest(destination):
                            raise ValueError(f"文件内容冲突：{relative}")
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        copy_file(path, destination)
            else:
                if root.exists() and any(p for p in files_under(root) if p.suffix != ".lock"):
                    raise ValueError("目标不是空数据目录且没有有效数据库")
                shutil.copytree(incoming, stage, dirs_exist_ok=True)
            index_map = (
                maps["indexes"]
                if selected is not None
                else {c["index_id"]: c["index_id"] for c in manifest["collections"]}
            )
            vector_worker(
                "load",
                stage,
                bundle,
                collections=manifest["collections"],
                index_map=index_map,
                rep_map=maps["representations"],
            )
            if prior_active and incoming_active:
                report["active_index"] = union_index(stage, [prior_active, incoming_active])
            elif not prior_active and incoming_active:
                if incoming_active.get("search_release_id"):
                    store_at(stage).activate_release(incoming_active["search_release_id"])
                else:
                    store_at(stage).activate(incoming_active["id"])
                report["active_index"] = incoming_active["id"]
            else:
                report["active_index"] = prior_active["id"] if prior_active else None
            interrupt_tasks(stage / "metadata.sqlite3")
            for path in (stage / "studio").glob("*/job.json"):
                value = json.loads(path.read_text(encoding="utf-8"))

                def interrupted(item):
                    if isinstance(item, dict):
                        return {
                            key: (
                                "interrupted"
                                if key == "status" and v in ("queued", "running")
                                else interrupted(v)
                            )
                            for key, v in item.items()
                        }
                    if isinstance(item, list):
                        return [interrupted(v) for v in item]
                    return item

                path.write_text(dumps(interrupted(value)), encoding="utf-8")
            validate_database(stage, basic=verification == "basic")
            validate_media(stage)
            if progress:
                progress(
                    "正在基础核对数据库依赖、文件和向量数量（不探测媒体）…"
                    if verification == "basic"
                    else "正在核对外键、历史成员、表示、向量数量与媒体引用…"
                )
            vector_worker("verify", stage, stage, verification=verification)
            report["searchable_sources"] = (
                sorted({s["source_id"] for s in store_at(stage).snapshots(report["active_index"])})
                if report["active_index"]
                else []
            )
            if not dry_run:
                if progress:
                    progress("校验通过，正在持久化并切换数据和配置…")
                _switch(stage, root, config_path, config_text, work=work)
            return report
        finally:
            if stage.exists() and not journal_path(root).exists():
                remove_tree(stage)
