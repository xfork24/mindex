"""分镜检测：本地 PySceneDetect 与远程 TransNetV2。"""

from __future__ import annotations

import logging
import os
from itertools import pairwise
from pathlib import Path

from models import adapters
from models.adapters import ModelError
from shared.config import Config
from shared.media import frame_timestamps, probe

log = logging.getLogger(__name__)


class AdaptiveShotDetector:
    def __init__(self, config: Config):
        self.config = config

    def detect(self, path: Path) -> list[tuple[float, float]]:
        from scenedetect import SceneManager, open_video
        from scenedetect.detectors import AdaptiveDetector

        log.warning("使用本地 AdaptiveDetector；尚未使用 TransNetV2。分镜质量需人工抽检。")
        video = open_video(str(path))
        manager = SceneManager()
        manager.add_detector(
            AdaptiveDetector(
                adaptive_threshold=self.config.detector.adaptive_threshold,
                min_scene_len=1,
                min_content_val=self.config.detector.min_content_val,
            )
        )
        manager.detect_scenes(video, show_progress=False, frame_skip=0)
        scenes = manager.get_scene_list(start_in_scene=True)
        timestamps = frame_timestamps(path)
        duration = probe(path)["duration"]
        bounds = [0.0]
        for _, end in scenes[:-1]:
            frame = end.get_frames()
            if frame < len(timestamps):
                bounds.append(timestamps[frame])
        bounds.append(duration)
        bounds = sorted(set(bounds))
        return list(pairwise(bounds))


class HTTPShotDetector:
    def __init__(self, config: Config):
        self.config = config

    def detect(self, path: Path) -> list[tuple[float, float]]:
        key = os.environ.get(self.config.detector.api_key_env, "")
        with path.open("rb") as handle:
            payload = adapters.request(
                "POST",
                self.config.detector.base_url.rstrip("/") + "/detect/video",
                timeout=self.config.detector.timeout,
                headers={"Authorization": f"Bearer {key}"} if key else {},
                files={"file": (path.name, handle, "video/mp4")},
                data={"threshold": str(self.config.detector.threshold)},
            )
        timestamps = frame_timestamps(path)
        if payload.get("model") != "TransNetV2" or payload.get("frame_count") != len(timestamps):
            raise ModelError("分镜服务的模型或解码帧数不匹配原视频")
        cuts = payload.get("cut_frames", [])
        if any(type(v) is not int or v < 1 or v >= len(timestamps) for v in cuts):
            raise ModelError("分镜服务返回无效帧索引")
        # Detection service returns one midpoint per transition interval.
        merged = []
        for value in sorted(set(cuts)):
            if not merged or value - merged[-1] > 2:
                merged.append(value)
        bounds = [0.0, *[timestamps[v] for v in merged], probe(path)["duration"]]
        return list(pairwise(bounds))
