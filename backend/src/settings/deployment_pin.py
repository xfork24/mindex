"""设置页 / 智能助手：为 llama_cpp 档案执行 e2e probe 与 e2e pin，并把部署清单关联到档案。

与 ``mindex e2e probe`` / ``mindex e2e pin`` 共用 ``inspect_service`` 与 ``create_manifest``，
信任模型不变：

- probe 只读 ``/health``、``/props``、``/v1/models``，不推理；
- pin 优先复用 ``data_dir/deployments/`` 里已与当前部署完全一致的清单（同一部署观测、本地文件 SHA256
  与空间契约），这样已通过的 smoke 能力门槛继续有效；否则生成新的 schema 2 清单（从不覆盖旧文件）；
- 本机（回环地址）服务可从监听进程的命令行与工作目录自动定位 GGUF/mmproj；远程服务需要显式给出
  本地文件或 SHA256 声明。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from models.adapters import ModelError
from models.deployment import create_manifest, read_manifest, space_contract
from models.llama_cpp import checked_runtime, inspect_service
from shared.config import Config, fingerprint
from shared.jsonio import save

PIN_ROLES = ("embedding", "asr", "analyzer")
LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
MODEL_FLAGS = ("-m", "--model")
MMPROJ_FLAGS = ("-mm", "--mmproj")


def service_of(config: Config, role: str):
    if role not in PIN_ROLES:
        raise ModelError(f"只有 {'/'.join(PIN_ROLES)} 档案需要部署清单")
    service = getattr(config, role)
    if service.backend != "llama_cpp":
        raise ModelError(f"该档案 backend 为 {service.backend}，只有 llama_cpp 后端需要 e2e probe/pin")
    return service


def manifest_ref(config: Config, path: Path) -> str:
    """数据目录内的清单以相对路径保存（随数据目录迁移）；其他位置保存绝对路径。"""
    path = path.resolve()
    try:
        return str(path.relative_to(Path(config.data_dir).resolve()))
    except ValueError:
        return str(path)


def _run(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _flag(argv: list[str], names: tuple[str, ...]) -> str | None:
    for index, item in enumerate(argv):
        for name in names:
            if item == name and index + 1 < len(argv):
                return argv[index + 1]
            if item.startswith(name + "="):
                return item.split("=", 1)[1]
    return None


def listening_process(port: int) -> dict | None:
    """本机监听该端口的进程：pid、命令行与工作目录（尽力而为，失败返回 None）。"""
    pids = [
        line.strip() for line in _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"]).splitlines()
    ]
    pid = next((p for p in pids if p.isdigit()), None)
    if pid is None:
        return None
    command = _run(["ps", "-ww", "-o", "command=", "-p", pid]).strip()
    proc_cwd = Path(f"/proc/{pid}/cwd")
    if proc_cwd.exists():
        cwd = os.readlink(proc_cwd)
    else:
        cwd = next(
            (
                line[1:]
                for line in _run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"]).splitlines()
                if line.startswith("n")
            ),
            None,
        )
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()
    return {"pid": int(pid), "argv": argv, "cwd": cwd}


def detect_files(config: Config, service, report: dict) -> dict:
    """推断 weights/mmproj/server_cwd：/props.model_path + 本机监听进程命令行 + local_services 定义。"""
    found: dict = {"weights_file": None, "mmproj_file": None, "server_cwd": None, "sources": []}
    parsed = urlparse(service.base_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    model_path = report["observable"].get("model_path")
    process = listening_process(port) if (parsed.hostname or "") in LOOPBACK else None
    argv, cwd = [], None
    if process:
        argv, cwd = process["argv"], process["cwd"]
        found["pid"] = process["pid"]
        found["sources"].append("process")
    else:
        for item in getattr(config.local_services, "services", []) or []:
            if item.port == port:
                argv, cwd = list(item.argv), item.cwd
                found["sources"].append(f"local_services:{item.name}")
                break
    if cwd is None and isinstance(model_path, str) and Path(model_path).is_absolute():
        cwd = str(Path(model_path).parent)  # 绝对 model_path 与工作目录无关，取所在目录即可核对
    if cwd:
        found["server_cwd"] = str(Path(cwd).expanduser().resolve())
    base = Path(found["server_cwd"] or ".")
    weights = model_path if isinstance(model_path, str) and model_path.strip() else _flag(argv, MODEL_FLAGS)
    mmproj = report["deployment"]["parameters"].get("mmproj_path") or _flag(argv, MMPROJ_FLAGS)
    if weights:
        found["weights_file"] = str((base / weights).resolve())
        found["sources"].append("props.model_path" if weights == model_path else "argv")
    if mmproj:
        found["mmproj_file"] = str((base / mmproj).resolve())
    return found


def summary(report: dict) -> dict:
    observable = report["observable"]
    return {
        "model_id": report.get("model_id"),
        "model_path": observable.get("model_path"),
        "build_info": observable.get("build_info"),
        "modalities": observable.get("modalities"),
        "metadata": report.get("metadata"),
        "capabilities": report.get("capabilities"),
        "deployment_id": report.get("deployment_id"),
        "diagnostics": report.get("diagnostics"),
    }


def matching_manifests(config: Config, role: str, report: dict, prefer: str | None = None) -> list[Path]:
    """数据目录 deployments/ 下与当前部署兼容（部署观测、本地文件、空间契约）的清单。"""
    service = getattr(config, role)
    folder = Path(config.data_dir) / "deployments"
    candidates = sorted(folder.glob("*.json")) if folder.is_dir() else []
    if prefer:
        preferred = Path(prefer)
        candidates.sort(key=lambda p: p.resolve() != preferred.resolve())
    matches = []
    for path in candidates:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("schema") != 2 or raw.get("role") != role:
                continue
            expected = raw.get("deployment", {}).get("observable", {})
            actual = report["deployment"]["observable"]
            if raw.get("trust", {}).get("source") == "local-sha256":
                expected = {k: v for k, v in expected.items() if k != "model_path"}
                actual = {k: v for k, v in actual.items() if k != "model_path"}
            if expected != actual:
                continue  # 先做廉价比较，避免对无关清单重新哈希大文件
            pinned = read_manifest(path, service, role)
            if role == "embedding" and service.quantization != pinned["quantization"]:
                continue
            checked_runtime(service, pinned)
            if role == "embedding" and pinned["space_contract"] != space_contract(
                config, pinned["file_identity"]
            ):
                continue
            matches.append(path)
        except (ModelError, OSError, ValueError, KeyError, TypeError):
            continue
    return matches


def probe(config: Config, role: str, *, current_manifest: str | None = None) -> dict:
    """e2e probe：只读探测，附带自动定位到的本地文件与可复用清单。"""
    service = service_of(config, role)
    try:
        report = inspect_service(service)
    except (ModelError, ValueError, KeyError, TypeError) as exc:
        return {
            "ready": False,
            "role": role,
            "base_url": service.base_url,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }
    detected = detect_files(config, service, report)
    reusable = matching_manifests(config, role, report, prefer=current_manifest)
    return {
        "ready": True,
        "role": role,
        "base_url": service.base_url,
        **summary(report),
        "detected": detected,
        "reusable_manifests": [manifest_ref(config, p) for p in reusable],
        "current_manifest": current_manifest,
        "current_manifest_valid": bool(current_manifest)
        and any(p.resolve() == Path(current_manifest).resolve() for p in reusable),
    }


def pin(
    config: Config,
    role: str,
    *,
    label: str,
    quantization: str | None = None,
    weights_file: str | None = None,
    mmproj_file: str | None = None,
    server_cwd: str | None = None,
    weights_sha256: str | None = None,
    mmproj_sha256: str | None = None,
    launch_parameters: dict | None = None,
    force_new: bool = False,
    current_manifest: str | None = None,
) -> dict:
    """e2e pin：复用一致的旧清单，或生成新的 schema 2 清单；返回清单路径与身份摘要。"""
    service = service_of(config, role)
    explicit_files = weights_file is not None or mmproj_file is not None
    remote = weights_sha256 is not None or mmproj_sha256 is not None
    if explicit_files and remote:
        raise ModelError("本地文件方式与远程 SHA256 声明互斥，只能选择一种")
    if remote and (weights_sha256 is None or mmproj_sha256 is None):
        raise ModelError("远程声明需要 weights_sha256 和 mmproj_sha256 两项")
    report = inspect_service(service)

    if not (force_new or explicit_files or remote or launch_parameters):
        reusable = matching_manifests(config, role, report, prefer=current_manifest)
        if role == "embedding" and len(reusable) > 1:
            # 多份一致清单时优先已通过 smoke 门槛的那份（门槛按 deployment_id 记录，含启动参数声明）。
            passed = [p for p in reusable if gate_status(_with_manifest(config, role, p))["passed"]]
            reusable = passed + [p for p in reusable if p not in passed]
        if reusable:
            path = reusable[0]
            pinned = read_manifest(path, service, role)
            return _result(config, path, pinned, created=False)

    detected = {}
    if not remote:
        detected = detect_files(config, service, report)
        weights_file = weights_file or detected["weights_file"]
        mmproj_file = mmproj_file or detected["mmproj_file"]
        server_cwd = server_cwd or detected["server_cwd"]
        missing = [
            name
            for name, value in (
                ("weights_file", weights_file),
                ("mmproj_file", mmproj_file),
                ("server_cwd", server_cwd),
            )
            if not value
        ]
        if missing:
            raise ModelError(
                f"无法自动定位 {', '.join(missing)}（服务不在本机或 /props 未报告）；"
                "请提供部署机上的 GGUF/mmproj 文件路径，或改用 weights_sha256/mmproj_sha256 声明"
            )
    if not quantization:
        quantization = service.quantization if role == "embedding" else report["metadata"].get("ftype")
    pinned = create_manifest(
        config,
        role,
        report,
        str(quantization or ""),
        weights_file=Path(weights_file).expanduser() if not remote else None,
        mmproj_file=Path(mmproj_file).expanduser() if not remote else None,
        server_cwd=Path(server_cwd).expanduser() if not remote else None,
        weights_sha256=weights_sha256,
        mmproj_sha256=mmproj_sha256,
        launch_parameters=launch_parameters or {},
    )
    folder = Path(config.data_dir) / "deployments"
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-")[:40] or role
    path = folder / f"{role}-{slug}-{pinned['deployment_id'][:12]}.json"
    if path.exists():  # 从不覆盖已有清单
        path = path.with_name(f"{path.stem}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    save(path, pinned)
    return {**_result(config, path, pinned, created=True), "detected": detected or None}


def _result(config: Config, path: Path, pinned: dict, *, created: bool) -> dict:
    return {
        "manifest": manifest_ref(config, path),
        "manifest_path": str(path.resolve()),
        "created": created,
        "content_id": pinned["file_identity"]["content_id"],
        "space_id": fingerprint(pinned["space_contract"]) if pinned.get("space_contract") else None,
        "deployment_id": pinned["deployment_id"],
        "trust": pinned["trust"]["source"],
        "weights_sha256": pinned["weights_sha256"],
        "mmproj_sha256": pinned["mmproj_sha256"],
        "quantization": pinned["quantization"],
    }


def _with_manifest(config: Config, role: str, path: Path) -> Config:
    section = getattr(config, role).model_copy(update={"deployment_manifest": str(path)})
    return config.model_copy(update={role: section})


def gate_status(config: Config) -> dict:
    """嵌入清单关联后，核对六片段 smoke 能力门槛（不推理）。"""
    from models.adapters import make_embedder

    embedder = make_embedder(config)
    try:
        if hasattr(embedder, "require_gate"):
            embedder.require_gate()
        return {"passed": True}
    except ModelError as exc:
        return {"passed": False, "detail": str(exc)[:300]}
