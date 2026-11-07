"""候选条目聚类：文本向量的贪心余弦聚类；嵌入不可用时退化为按规范化标题分组。

纯函数为主，便于测试：``greedy`` 只接收向量，``by_title`` 只接收标题。
"""

from __future__ import annotations

from contextlib import nullcontext

import numpy as np

from topics.items import normalize_title

EMBED_BATCH = 32


def item_text(item: dict) -> str:
    return (item["title"] + "。" + (item.get("summary") or "")[:200]).strip()


def embed(embedder, texts: list[str]) -> np.ndarray:
    """整批嵌入共用一次 recall_scope（同检索语义）：只做一次前置身份探测，逐条推理不再前后复探。"""
    scope = getattr(embedder, "recall_scope", None)
    with scope() if scope else nullcontext():
        vectors = []
        for start in range(0, len(texts), EMBED_BATCH):
            vectors.extend(embedder.text(texts[start : start + EMBED_BATCH], role="document"))
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or len(matrix) != len(texts):
        raise ValueError("嵌入服务返回的向量数量不符")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return matrix / norms


def greedy(vectors: np.ndarray, threshold: float) -> list[list[int]]:
    """按输入顺序逐个归入第一个质心余弦相似度不低于阈值的簇，否则新建簇；返回下标列表。"""
    clusters: list[list[int]] = []
    centroids: list[np.ndarray] = []
    for index, vector in enumerate(vectors):
        best, score = -1, -1.0
        for position, centroid in enumerate(centroids):
            value = float(np.dot(vector, centroid))
            if value > score:
                best, score = position, value
        if best >= 0 and score >= threshold:
            clusters[best].append(index)
            members = vectors[clusters[best]]
            centroid = members.mean(axis=0)
            norm = np.linalg.norm(centroid)
            centroids[best] = centroid / norm if norm else centroid
        else:
            clusters.append([index])
            centroids.append(vector)
    return clusters


def by_title(titles: list[str]) -> list[list[int]]:
    groups: dict[str, list[int]] = {}
    for index, title in enumerate(titles):
        groups.setdefault(normalize_title(title)[:24] or str(index), []).append(index)
    return list(groups.values())


def rank(clusters: list[list[int]], items: list[dict], limit: int) -> list[list[int]]:
    """簇按（条目数、最大热度）降序取前 ``limit`` 个；簇内保持输入顺序。"""

    def key(cluster):
        hot = max((items[i].get("hot") or 0) for i in cluster)
        return (-len(cluster), -hot, cluster[0])

    return sorted(clusters, key=key)[:limit]


def cosine_max(vector: np.ndarray, matrix: np.ndarray | None) -> float:
    if matrix is None or not len(matrix):
        return 0.0
    return float(np.max(matrix @ vector))
