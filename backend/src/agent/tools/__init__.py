"""第一期工具集：全部经回环客户端调用既有 REST 接口。"""

from __future__ import annotations

from mindex.agent.registry import ToolRegistry


def build_registry() -> ToolRegistry:
    from mindex.agent.tools import (
        library,
        meta,
        navigate,
        settings_tools,
        studio_build,
        studio_read,
        studio_write,
    )

    registry = ToolRegistry()
    for module in (meta, navigate, studio_read, studio_write, studio_build, settings_tools, library):
        for spec in module.specs():
            registry.add(spec)
    return registry
