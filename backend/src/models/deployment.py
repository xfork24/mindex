"""文件内容身份、向量契约与部署清单；HTTP 观测不能证明服务器加载的 mmproj。"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

from models.adapters import ModelError
from shared.config import Config, fingerprint

ADAPTER_VERSION = "llama-native-wemm-2"
PREFIX = "<|im_start|>user\n"
SUFFIX = "<|im_end|><embedding>"
METADATA_FIELDS = ("vocab_type", "n_vocab", "n_ctx_train", "n_embd", "n_params", "size", "ftype")


def sha256_valid(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def file_state(path: Path) -> tuple:
    stat = path.stat()
    if not path.is_file():
        raise ModelError(f"不是普通文件：{path}")
    return (str(path.resolve()), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@lru_cache(maxsize=128)
def _file_hash(state: tuple) -> str:
    path = Path(state[0])
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    if file_state(path) != state:
        raise ModelError(f"计算 SHA256 期间文件发生变化：{path}")
    return digest.hexdigest()


def verified_file(path: Path, expected: str | None = None) -> dict:
    """每个进程首次读取内容；后续以路径、文件标识、大小和纳秒时间缓存。"""
    try:
        path = path.expanduser().absolute()
        state = file_state(path)
        digest = _file_hash(state)
        if file_state(path) != state:
            raise ModelError(f"核验期间文件发生变化：{path}")
        if expected is not None and digest != expected:
            raise ModelError(f"本地文件 SHA256 已变化：{path}；请建立新空间并重建索引")
        return {
            "path": str(path),
            "sha256": digest,
            "resolved_path": state[0],
            "device": state[1],
            "inode": state[2],
            "size": state[3],
            "mtime_ns": state[4],
            "ctime_ns": state[5],
        }
    except OSError as exc:
        raise ModelError(f"无法核验本地文件 {path}: {exc}{moved_hint(path)}") from exc


def moved_hint(path: Path) -> str:
    """清单记录的权重文件不存在时，在其所在 models 目录的子目录里找同名文件，提示是否被移动过。"""
    if path.exists():
        return ""
    root = next((parent for parent in path.parents if parent.name == "models"), None)
    if root is None or not root.is_dir():
        return ""
    found = sorted(p for p in root.rglob(path.name) if p.is_file())[:3]
    if not found:
        return ""
    return (
        f"。在 {', '.join(str(p.parent) for p in found)} 找到同名文件，文件可能被移动过："
        "本地 schema 2 清单允许按服务报告的新模型路径重新核验 SHA256；"
        "请确认服务的 model_path 和投影文件位置（见 docs/index-versions.md）"
    )


def content_identity(weights: str, mmproj: str) -> dict:
    if not sha256_valid(weights) or not sha256_valid(mmproj):
        raise ModelError("weights/mmproj 必须为 64 位小写 SHA256")
    digests = {"weights_sha256": weights, "mmproj_sha256": mmproj}
    return {**digests, "content_id": fingerprint(digests)}


def space_contract(config: Config, content: dict) -> dict:
    """只有显式语义版本和实际文件摘要进入 v2 空间；修改语义时必须升级版本。"""
    video = config.video
    return {
        "schema": 2,
        "content": content,
        "dimensions": config.embedding.dimensions,
        "pooling": {"version": 1, "method": "last-embedding-token"},
        "normalization": {"version": 1, "method": "l2-float32", "server_embd_normalize": 2},
        "template": {"version": 1, "prefix": PREFIX, "suffix": SUFFIX},
        "video": {
            "version": 1,
            "fps": video.fps,
            "max_frames": video.max_frames,
            "max_pixels": video.max_pixels,
            "short_shot_seconds": video.short_shot_seconds,
            "media": "input_video/h264-crf18-noaudio-whole-clip-v1",
        },
        "image": {"version": 1, "media": "image_url/rgb-png", "max_pixels": video.max_pixels},
        "implementation": ADAPTER_VERSION,
        "numerical_implementation": config.embedding.numerical_implementation,
    }


def normalized_metadata(meta: dict) -> dict:
    if not isinstance(meta, dict):
        raise ModelError("模型元数据必须为对象")
    for field in METADATA_FIELDS[:-1]:
        value = meta.get(field)
        minimum = 0 if field == "vocab_type" else 1
        if type(value) is not int or value < minimum:
            raise ModelError(f"模型元数据缺失或类型无效：{field}")
    if not isinstance(meta.get("ftype"), str) or not meta["ftype"].strip():
        raise ModelError("模型元数据缺失或类型无效：ftype")
    return {field: meta[field] for field in METADATA_FIELDS}


def validate_role(service, role: str, report: dict, quantization: str):
    meta, caps = report["metadata"], report["capabilities"]
    if meta["ftype"] != quantization:
        raise ModelError("部署清单量化与 GGUF 元数据不一致")
    if role in {"embedding", "analyzer"} and caps.get("vision") is not True:
        raise ModelError("llama.cpp 未启用视觉投影，无法处理视频/图片")
    if role == "embedding":
        if caps.get("video") is not True:
            raise ModelError("llama.cpp 未声明支持视频")
        if service.quantization != quantization:
            raise ModelError("本地 embedding.quantization 与部署清单不同")
        if meta["n_embd"] != service.dimensions:
            raise ModelError("GGUF n_embd 与目标嵌入维度不符；禁止截断或补齐")
    if role == "asr" and caps.get("audio") is not True:
        raise ModelError("ASR 服务未启用音频投影")


def check_local_files(pinned: dict, report: dict, *, allow_relocation: bool = False) -> dict:
    local = pinned["trust"]["source"] == "local-sha256"
    if not local:
        return {}
    cwd = Path(pinned["server_cwd"])
    model_path = report["observable"].get("model_path")
    if not isinstance(model_path, str) or not model_path.strip():
        raise ModelError("/props 缺少有效 model_path，无法核验本地 weights")
    loaded = Path(model_path)
    resolved = (cwd / loaded).resolve()
    paths = {name: Path(record["path"]) for name, record in pinned["files"].items()}
    if allow_relocation:
        # 核验服务当前报告的文件，不能仅核验仍留在旧路径的同名权重。
        paths["weights"] = cwd / loaded
        projection = report["props"].get("mmproj_path")
        if projection is not None:
            if not isinstance(projection, str) or not projection.strip():
                raise ModelError("/props 缺少有效 mmproj_path，无法核验本地投影")
            paths["mmproj"] = cwd / projection
        elif not paths["mmproj"].exists():
            # 标准 llama-server 通常不报告投影路径；支持模型和同名投影一起迁移。
            # 这里只定位候选文件，下面仍须核对清单中的内容摘要。
            paths["mmproj"] = resolved.parent / paths["mmproj"].name
    files = {
        name: verified_file(path, pinned["file_identity"][name + "_sha256"]) for name, path in paths.items()
    }
    if resolved != Path(files["weights"]["resolved_path"]):
        raise ModelError("/props.model_path 解析结果与本地 weights 文件不同")
    return files


def create_manifest(
    config: Config,
    role: str,
    report: dict,
    quantization: str,
    *,
    weights_file: Path | None = None,
    mmproj_file: Path | None = None,
    weights_sha256: str | None = None,
    mmproj_sha256: str | None = None,
    server_cwd: Path | None = None,
    launch_parameters: dict | None = None,
) -> dict:
    service = getattr(config, role)
    local = weights_file is not None or mmproj_file is not None
    remote = weights_sha256 is not None or mmproj_sha256 is not None
    if local == remote:
        raise ModelError("文件方式与远程 SHA 声明互斥，必须选择一种")
    if remote and (weights_sha256 is None or mmproj_sha256 is None):
        raise ModelError("远程声明需要 weights 和 mmproj 两项 SHA256")
    files = {}
    if local:
        if weights_file is None or mmproj_file is None or server_cwd is None:
            raise ModelError("本地清单需要 --weights-file、--mmproj-file 和 --server-cwd")
        if not server_cwd.expanduser().is_dir():
            raise ModelError("--server-cwd 必须是服务器实际工作目录")
        files = {"weights": verified_file(weights_file), "mmproj": verified_file(mmproj_file)}
        weights_sha256, mmproj_sha256 = files["weights"]["sha256"], files["mmproj"]["sha256"]
    content = content_identity(weights_sha256, mmproj_sha256)
    if quantization in {"int8", "nf4", "unknown"} or not quantization.strip():
        raise ModelError("请填写实际 GGUF 量化，例如 Q8_0")
    validate_role(service, role, report, quantization)
    value = {
        "schema": 2,
        "role": role,
        "model": service.model,
        "weights_sha256": weights_sha256,
        "mmproj_sha256": mmproj_sha256,
        "file_identity": content,
        "quantization": quantization,
        "pooling": "last" if role == "embedding" else None,
        "video_fps": float(config.video.fps),
        "space_contract": space_contract(config, content) if role == "embedding" else None,
        "files": files,
        "server_cwd": str(server_cwd.expanduser().resolve()) if local else None,
        "deployment": {**report["deployment"], "launch_parameters": launch_parameters or {}},
        "deployment_id": fingerprint({**report["deployment"], "launch_parameters": launch_parameters or {}}),
        "report": report,
        "trust": {
            "source": "local-sha256" if local else "operator-attested-sha256",
            "launch_parameters": launch_parameters or {},
            "attestation": "部署者确认实际加载的 weights/mmproj、pooling、video-fps 与启动参数。"
            "本地 SHA256 证明本地文件内容；标准 HTTP API 无法证明实际加载的投影或所有启动参数。",
        },
    }
    check_local_files(value, report)
    return value


def read_manifest(path: Path, service, role: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        schema = value["schema"]
        if type(schema) is not int or schema not in (1, 2) or value["role"] != role:
            raise ValueError("schema/role 不匹配")
        if schema == 1 and value["model"] != service.model:
            raise ValueError("model 不匹配")
        for name in ("weights_sha256", "mmproj_sha256"):
            if not sha256_valid(value[name]):
                raise ValueError(f"{name} 必须为实际文件 SHA256")
        if (
            not isinstance(value["quantization"], str)
            or not value["quantization"].strip()
            or value["quantization"] in {"unknown", "int8", "nf4"}
        ):
            raise ValueError("填写实际 GGUF 量化类型，例如 Q8_0")
        if role == "embedding" and value["pooling"] != "last":
            raise ValueError("WeMM 必须使用 --pooling last")
        if schema == 1:
            if not value["runtime_id"]:
                raise ValueError("缺少运行身份")
        else:
            if value["file_identity"] != content_identity(value["weights_sha256"], value["mmproj_sha256"]):
                raise ValueError("文件内容身份不一致")
            if value["deployment_id"] != fingerprint(value["deployment"]):
                raise ValueError("部署观测摘要无效")
            if not isinstance(value["trust"]["launch_parameters"], dict):
                raise TypeError("启动参数声明必须为对象")
            source = value["trust"]["source"]
            if source not in {"local-sha256", "operator-attested-sha256"}:
                raise ValueError("未知信任来源")
            if source == "local-sha256":
                if (
                    set(value["files"]) != {"weights", "mmproj"}
                    or not Path(value["server_cwd"]).is_absolute()
                ):
                    raise ValueError("缺少本地文件/服务器工作目录")
                for record in value["files"].values():
                    if not Path(record["path"]).is_absolute():
                        raise ValueError("本地文件必须使用绝对路径")
            if role == "embedding" and value["space_contract"]["content"] != value["file_identity"]:
                raise ValueError("空间契约内容身份不同")
        return value
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ModelError(f"部署清单无效 {path}: {exc}") from exc
