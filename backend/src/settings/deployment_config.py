"""部署配置文件的读取、字段边界、校验与跨进程修订保存。凭据仅采用环境变量引用。"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from fastapi import HTTPException
from filelock import FileLock

from shared.config import Config

SECTIONS = ("server", "database", "vector", "storage", "queue", "locks", "gpu", "uploads")
IDENTITY = {"server.library_id"}
MIGRATION = {
    "database.backend",
    "database.postgres.dsn_env",
    "storage.backend",
    "storage.s3.endpoint_url",
    "storage.s3.bucket",
    "storage.s3.prefix",
    "vector.backend",
    "vector.chroma_server.url",
}


def leaves(value, prefix=""):
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict) and item:
            yield from leaves(item, path)
        else:
            yield path, item


def selected(config):
    return {
        **{name: getattr(config, name).model_dump(mode="json") for name in SECTIONS},
        "agent": {"gateway_url": config.agent.gateway_url},
    }


def merge(base, patch):
    result = dict(base)
    for key, value in patch.items():
        result[key] = merge(result.get(key, {}), value) if isinstance(value, dict) else value
    return result


def source_file(config):
    return getattr(config, "_source_path", None)


def read(config):
    path = source_file(config)
    if path is None or not path.is_file() or path.is_symlink():
        return None, None, "部署未提供可写配置文件"
    content = path.read_bytes()
    raw = yaml.safe_load(content) or {}
    if not isinstance(raw, dict):
        raise HTTPException(422, "部署配置必须是 YAML 对象")
    writable = (
        bool(path.stat().st_mode & 0o222) and os.access(path.parent, os.W_OK) and os.access(path, os.W_OK)
    )
    return raw, hashlib.sha256(content).hexdigest(), "" if writable else "部署配置文件只读"


def view(config, store):
    raw, revision, reason = read(config)
    saved = Config.model_validate(raw) if raw is not None else config
    values = selected(saved)
    # 当前真实身份固定；空配置值不允许经设置页切换到另一库。
    for field, value in list(leaves(values)):
        if (field.endswith("_url") or field.endswith(".url")) and value:
            parsed = urlsplit(value)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                section = values
                *parts, key = field.split(".")
                for part in parts:
                    section = section[part]
                section[key] = (
                    f"{parsed.scheme}://{parsed.hostname}"
                    + (f":{parsed.port}" if parsed.port else "")
                    + parsed.path
                )
    values["server"]["library_id"] = store.library_id
    fields = {
        path: {
            "editable": not reason and path not in IDENTITY,
            "restart_required": True,
            "migration_required": path in MIGRATION,
        }
        for path, _ in leaves(values)
    }
    running = selected(config)
    running["server"]["library_id"] = store.library_id
    return {
        "revision": revision,
        "writable": not reason,
        "read_only_reason": reason,
        "config": values,
        "fields": fields,
        "restart_required": values != running,
        "scope": "当前节点配置文件；模型和检索运行参数经原设置接口热更新",
    }


def validate(config, store, patch):
    if not isinstance(patch, dict) or set(patch) - set(SECTIONS) - {"agent"}:
        raise HTTPException(422, "部署配置含不可编辑分区")
    if "agent" in patch and (not isinstance(patch["agent"], dict) or set(patch["agent"]) - {"gateway_url"}):
        raise HTTPException(422, "部署设置仅支持 agent.gateway_url；模型参数经原设置接口保存")
    for path, value in leaves(patch):
        if path in IDENTITY and value != store.library_id:
            raise HTTPException(409, "切换素材库必须经过迁移、验证和激活流程")
        if path.endswith("_env") and (not isinstance(value, str) or not value.isidentifier()):
            raise HTTPException(422, f"{path} 必须是有效的环境变量名称")
        if (path.endswith("_url") or path.endswith(".url")) and value:
            parsed = urlsplit(value)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise HTTPException(422, f"{path} 不接受明文凭据、查询串或片段，请使用环境变量引用")
    raw, revision, reason = read(config)
    if reason:
        raise HTTPException(409, reason)
    candidate = merge(raw, patch)
    candidate.setdefault("server", {})["library_id"] = store.library_id
    try:
        parsed = Config.model_validate(candidate)
        # 延迟导入：保持异常捕获范围，依赖加载失败时仍走原有降级路径。
        from shared.backends.context import deployment_form

        deployment_form(parsed)
    except (ValueError, RuntimeError) as exc:
        # 不把 Pydantic 的 input_value 明文带回客户端。
        raise HTTPException(422, "部署组合或字段校验失败，请检查后端、连接池预算及资源组声明") from exc
    references = []
    if parsed.database.backend == "postgres":
        references.append(parsed.database.postgres.dsn_env)
    if parsed.storage.backend == "s3":
        references += [parsed.storage.s3.access_key_id_env, parsed.storage.s3.secret_access_key_env]
    for name in references:
        if not os.environ.get(name, "").strip():
            raise HTTPException(422, f"凭据引用环境变量 {name} 未设置；请先在部署环境中提供")
    before, after = dict(leaves(selected(Config.model_validate(raw)))), dict(leaves(selected(parsed)))
    changed = sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))
    return candidate, {
        "valid": True,
        "revision": revision,
        "changed_fields": changed,
        "restart_required": bool(changed),
        "migration_required": any(key in MIGRATION for key in changed),
    }


def save(config, store, patch, revision):
    path = source_file(config)
    if path is None:
        raise HTTPException(409, "部署未提供可写配置文件")
    with FileLock(str(path) + ".deployment.lock", timeout=5):
        candidate, result = validate(config, store, patch)
        if not revision or revision != result["revision"]:
            raise HTTPException(409, "部署配置修订冲突，请重新读取后合并；当前输入已保留")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".deployment-", suffix=".yaml", delete=False
            ) as target:
                temporary = Path(target.name)
                os.fchmod(target.fileno(), path.stat().st_mode & 0o777)
                target.write(yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False).encode())
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return {**result, **view(config, store), "saved": True}
