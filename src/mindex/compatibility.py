"""六条文本、六段视频的固定基线与按部署绑定的工程兼容性门槛。"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np

from .adapters import ModelError
from .config import Config, fingerprint
from .deployment import verified_file
from .media import content_hash

THRESHOLD = 0.999
CHECKS = {
    "finite",
    "dimensions",
    "unit_norm",
    "video_content_changes_vector",
    "text_content_changes_vector",
    "video_repeat_stable",
    "text_repeat_stable",
    "space_stable",
}


def location(config: Config, space: dict) -> Path:
    return config.data_dir / "capabilities" / "v2" / fingerprint(space)


def validation_path(config: Config, space: dict, deployment_id: str) -> Path:
    return location(config, space) / "deployments" / (deployment_id + ".json")


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise TypeError("需要对象")
        return value
    except (OSError, ValueError, TypeError) as exc:
        raise ModelError(f"兼容性文件缺失或损坏 {path}: {exc}") from exc


def _vectors(raw, dimensions: int) -> np.ndarray:
    try:
        array = np.asarray(raw, dtype=np.float64)
        if array.shape != (6, dimensions) or not np.isfinite(array).all():
            raise ValueError("维度或数值无效")
        norms = np.linalg.norm(array, axis=1, keepdims=True)
        if not np.isfinite(norms).all() or np.any(norms < 1e-8):
            raise ValueError("零向量或无效范数")
        return array / norms
    except (ValueError, TypeError) as exc:
        raise ModelError("基线/验证原始向量无效") from exc


def _check_samples(config: Config, samples: list):
    if not isinstance(samples, list) or len(samples) != 6:
        raise ModelError("基线必须包含六个固定文本/视频输入")
    try:
        for sample in samples:
            if not isinstance(sample["text"], str) or not sample["text"]:
                raise ValueError("缺少固定文本")
            if hashlib.sha256(sample["text"].encode()).hexdigest() != sample["text_sha256"]:
                raise ValueError("文本哈希不同")
            path = (config.data_dir / sample["path"]).resolve()
            if not path.is_relative_to(config.data_dir.resolve()):
                raise ValueError("视频样本路径越界")
            verified_file(path, sample["sha256"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelError("基线输入缺失或损坏") from exc


def load_baseline(config: Config, space: dict, *, allow_missing=False) -> dict | None:
    directory = location(config, space)
    path = directory / "baseline.json"
    marker = directory / "baseline.sha256"
    if not path.exists() and allow_missing and not marker.exists():
        prior = [_read(p) for p in (directory / "deployments").glob("*.json")]
        if not any(p.get("baseline_sha256") or p.get("passed") is True for p in prior):
            return None
    try:
        expected = marker.read_text().strip()
        verified_file(path, expected)
        baseline = _read(path)
        if (
            baseline["schema"] != 2
            or baseline["space"] != space
            or baseline["space_id"] != fingerprint(space)
            or baseline["deployment_id"] != fingerprint(baseline["deployment"])
            or baseline["preprocessing"] != space["video"]
        ):
            raise ValueError("空间/部署/预处理不匹配")
        _check_samples(config, baseline["samples"])
        if baseline["inputs_id"] != fingerprint(baseline["samples"]):
            raise ValueError("输入摘要无效")
        for kind in ("text", "video"):
            _vectors(baseline["raw_" + kind + "_vectors"], space["dimensions"])
        return baseline
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ModelError(f"基线缺失或损坏：{path}；禁止自动覆盖，请恢复基线或升级数值实现版本") from exc


def save_validation(config: Config, space: dict, deployment_id: str, report: dict):
    from .e2e import save

    path = validation_path(config, space, deployment_id)
    save(path, report)
    marker = path.with_suffix(".sha256")
    temporary = marker.with_suffix(".partial")
    temporary.write_text(content_hash(path) + "\n")
    temporary.replace(marker)


def revoke(config: Config, space: dict, report: dict):
    save_validation(
        config,
        space,
        report["deployment_id"],
        {
            "schema": 2,
            "passed": False,
            "status": "checking",
            "space_id": fingerprint(space),
            "deployment_id": report["deployment_id"],
            "deployment": report["deployment"],
        },
    )


def complete_validation(
    config: Config,
    space: dict,
    deployment: dict,
    samples: list,
    report: dict,
    raw_text: list,
    raw_video: list,
    prepared_hashes: list,
    baseline: dict | None,
) -> dict:
    from .e2e import save

    directory = location(config, space)
    text = _vectors(raw_text, space["dimensions"])
    video = _vectors(raw_video, space["dimensions"])
    checks_passed = set(report["checks"]) == CHECKS and all(v is True for v in report["checks"].values())
    if baseline is None and checks_passed:
        fixed_samples = []
        for number, (sample, prepared) in enumerate(zip(samples, prepared_hashes), 1):
            source = config.data_dir / sample["path"]
            verified_file(source, sample["sha256"])
            target = directory / "samples" / f"{number:02}.mp4"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            verified_file(target, sample["sha256"])
            fixed_samples.append(
                {
                    "id": sample["id"],
                    "path": str(target.relative_to(config.data_dir)),
                    "sha256": sample["sha256"],
                    "text": sample["text"],
                    "text_sha256": hashlib.sha256(sample["text"].encode()).hexdigest(),
                    "prepared_sha256": prepared,
                }
            )
        baseline = {
            "schema": 2,
            "space": space,
            "space_id": fingerprint(space),
            "deployment_id": deployment["deployment_id"],
            "deployment": deployment["deployment"],
            "preprocessing": space["video"],
            "samples": fixed_samples,
            "inputs_id": fingerprint(fixed_samples),
            "raw_text_vectors": raw_text,
            "raw_video_vectors": raw_video,
            "text_vectors": report["text_vectors"],
            "video_vectors": report["video_vectors"],
        }
        save(directory / "baseline.json", baseline)
        marker = directory / "baseline.sha256"
        temporary = marker.with_name(marker.name + ".partial")
        temporary.write_text(content_hash(directory / "baseline.json") + "\n")
        temporary.replace(marker)
    if baseline is None:
        return {**report, "passed": False, "compatibility": {"status": "structural-smoke-failed"}}
    baseline = load_baseline(config, space)
    similarity = {
        "text": np.sum(text * _vectors(baseline["raw_text_vectors"], space["dimensions"]), axis=1).tolist(),
        "video": np.sum(
            video * _vectors(baseline["raw_video_vectors"], space["dimensions"]), axis=1
        ).tolist(),
    }
    inputs_match = prepared_hashes == [s["prepared_sha256"] for s in baseline["samples"]]
    compatible = inputs_match and all(v >= THRESHOLD for values in similarity.values() for v in values)
    return {
        **report,
        "schema": 2,
        "passed": checks_passed and compatible,
        "deployment_id": deployment["deployment_id"],
        "deployment": deployment["deployment"],
        "baseline_sha256": content_hash(directory / "baseline.json"),
        "inputs_id": baseline["inputs_id"],
        "raw_text_vectors": raw_text,
        "raw_video_vectors": raw_video,
        "prepared_sha256": prepared_hashes,
        "compatibility": {"threshold": THRESHOLD, "cosine": similarity, "inputs_match": inputs_match},
    }


def require_gate(config: Config, space: dict, report: dict):
    """检索与构建均核验基线完整性、当前部署与全部十二条逐项向量对照。"""
    try:
        baseline = load_baseline(config, space)
        gate_path = validation_path(config, space, report["deployment_id"])
        verified_file(gate_path, gate_path.with_suffix(".sha256").read_text().strip())
        gate = _read(gate_path)
        if (
            gate["schema"] != 2
            or gate["passed"] is not True
            or gate["space"] != space
            or gate["space_id"] != fingerprint(space)
            or gate["deployment_id"] != report["deployment_id"]
            or gate["deployment"] != report["deployment"]
            or gate["baseline_sha256"] != content_hash(location(config, space) / "baseline.json")
            or gate["inputs_id"] != baseline["inputs_id"]
            or gate["prepared_sha256"] != [s["prepared_sha256"] for s in baseline["samples"]]
            or gate["compatibility"]["threshold"] != THRESHOLD
            or set(gate["checks"]) != CHECKS
            or not all(v is True for v in gate["checks"].values())
        ):
            raise ValueError("当前部署未通过或记录不匹配")
        for kind in ("text", "video"):
            actual = _vectors(gate["raw_" + kind + "_vectors"], space["dimensions"])
            original = _vectors(baseline["raw_" + kind + "_vectors"], space["dimensions"])
            if not np.all(np.sum(actual * original, axis=1) >= THRESHOLD):
                raise ValueError("与基线的余弦相似度低于 0.999")
    except (ModelError, KeyError, TypeError, ValueError, OSError) as exc:
        raise ModelError(
            f"当前部署尚未通过六片段兼容性门槛：{exc}；请执行 e2e phase smoke。"
            "失败时须升级 numerical_implementation、建立新基线并重建，禁止混用旧索引"
        ) from exc
