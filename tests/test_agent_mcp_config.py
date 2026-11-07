from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from shared.agent_domains import LOADABLE_DOMAINS
from shared.config import AgentMcpConfig, Config

ROOT = Path(__file__).resolve().parents[1]


def test_domains_default_none_exports_all():
    assert AgentMcpConfig().domains is None
    assert AgentMcpConfig(domains=None).domains is None


def test_domains_accepts_loadable_and_dedupes_in_order():
    cfg = AgentMcpConfig(domains=["search", "library", "search", "persons", "library"])
    assert cfg.domains == ["search", "library", "persons"]
    assert AgentMcpConfig(domains=list(LOADABLE_DOMAINS)).domains == list(LOADABLE_DOMAINS)


@pytest.mark.parametrize("domains", [["meta"], ["search", "unknown"], []])
def test_domains_rejects_meta_unknown_and_empty(domains):
    with pytest.raises(ValidationError, match="agent.mcp.domains"):
        AgentMcpConfig(domains=domains)


def test_domains_via_full_config_and_example_yaml():
    cfg = Config.model_validate({"agent": {"mcp": {"domains": ["index"]}}})
    assert cfg.agent.mcp.domains == ["index"]
    with pytest.raises(ValidationError, match="未知领域"):
        Config.model_validate({"agent": {"mcp": {"domains": ["bogus"]}}})
    data = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    assert "domains" in data["agent"]["mcp"]
    assert Config.model_validate(data).agent.mcp.domains is None
