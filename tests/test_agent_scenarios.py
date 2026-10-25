"""智能助手验收场景（确定性）：脚本化模型驱动真实应用，按最终状态判定。

同一组场景由 scripts/agent_eval.py 交给真实模型评测；这里只验证工程链路，不代表真实模型质量。
"""

from __future__ import annotations

import pytest
from agent_scenarios import SCENARIOS, drive, prepare
from fastapi.testclient import TestClient

from mindex.agent.llm.scripted import ScriptedLlm

pytestmark = pytest.mark.slow  # 重媒体用例：CI 快速回归跳过，每晚完整运行


def test_scenario_names_unique():
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_scenario(config, video_file, monkeypatch, scenario):
    # 附件端点在启用令牌时要求凭证；场景以本机面板身份上传。
    monkeypatch.delenv("MINDEX_WEB_TOKEN", raising=False)
    monkeypatch.delenv("MINDEX_MCP_TOKEN", raising=False)
    world = prepare(scenario, config, video_file)
    llm = ScriptedLlm(scenario.script(world))
    with TestClient(world.app(llm)) as client:
        events = drive(client, scenario, world)
        assert events[-1]["type"] == "turn/end" and events[-1]["data"]["reason"]["kind"] == "completed", events[-1]
        scenario.check(world, client, events)
    assert not llm.script, f"脚本未用完：{llm.script}"
