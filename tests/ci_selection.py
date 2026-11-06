"""CI 分组与确定性分片；不改变普通 pytest 的测试选择。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

EXTERNAL = {"requires_postgres", "requires_s3", "requires_chroma_server"}


def suite_for(nodeid, markers, fixtures):
    if EXTERNAL.intersection(markers):
        return None  # 由 scaling.yml 的真实后端专项执行。
    filename, name = nodeid.split("::", 1)
    if "backup" in Path(filename).stem or "backup" in name:
        return "backup"
    if "slow" in markers or "video_file" in fixtures:
        return "media"
    return "fast"


def split_nodes(nodeids, count, costs):
    """每个节点恰好进入一个分片；历史时间只决定负载，不决定是否执行。"""
    groups, loads = [[] for _ in range(count)], [0.0] * count

    def weight(nodeid):
        return max(0.05, costs.get(Path(nodeid.split("::", 1)[0]).name, 1.0))

    for nodeid in sorted(nodeids, key=lambda name: (-weight(name), name)):
        index = min(range(count), key=lambda i: (loads[i], len(groups[i]), i))
        groups[index].append(nodeid)
        loads[index] += weight(nodeid)
    return groups


def pytest_addoption(parser):
    group = parser.getgroup("mindex-ci", "CI 分组")
    group.addoption("--ci-suite", choices=["fast", "media", "backup"], help="选择一组离线测试")
    group.addoption("--ci-shard", default="1/1", help="确定性分片：序号/总数（从 1 开始）")
    group.addoption("--ci-plan", type=Path, help="在执行前保存分组清单")


def pytest_collection_modifyitems(config, items):
    suite = config.getoption("--ci-suite")
    if suite is None:
        if config.getoption("--ci-shard") != "1/1":
            raise pytest.UsageError("--ci-shard 必须与 --ci-suite 一起使用")
        return
    try:
        index, count = map(int, config.getoption("--ci-shard").split("/"))
        if not 1 <= index <= count <= 16:
            raise ValueError
    except ValueError:
        raise pytest.UsageError("--ci-shard 应为 序号/总数，且 1 <= 序号 <= 总数 <= 16") from None
    candidates = [item for item in items if suite_for(
        item.nodeid, {mark.name for mark in item.iter_markers()}, item.fixturenames) == suite]
    costs = json.loads(Path(__file__).with_name("ci-durations.json").read_text())
    selected = set(split_nodes([item.nodeid for item in candidates], count, costs)[index - 1])
    kept = [item for item in items if item.nodeid in selected]
    deselected = [item for item in items if item.nodeid not in selected]
    if not kept:
        raise pytest.UsageError(f"CI 分组 {suite} 的分片 {index}/{count} 没有测试，拒绝空跑")
    plan = config.getoption("--ci-plan")
    if plan:
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(json.dumps({"suite": suite, "shard": f"{index}/{count}",
                                   "selected": len(kept), "deselected": len(deselected),
                                   "nodeids": [item.nodeid for item in kept]},
                                  ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    items[:] = kept
    config.hook.pytest_deselected(items=deselected)

