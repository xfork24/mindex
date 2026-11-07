"""智能助手工具集：按领域分模块，全部经回环客户端调用既有 REST 接口。"""

from __future__ import annotations

from agent.registry import ToolRegistry


def build_registry() -> ToolRegistry:
    # 延迟导入：该依赖的顶层导入链会回到本模块，保留运行时装配以消解循环。
    from agent.tools import (
        groups,
        index,
        library,
        library_tasks,
        meta,
        navigate,
        persons,
        persons_review,
        search,
        settings_tools,
        studio_animation,
        studio_brands,
        studio_build,
        studio_delivery,
        studio_detail,
        studio_music,
        studio_overlays,
        studio_read,
        studio_templates,
        studio_tools,
        studio_write,
        topics,
    )

    registry = ToolRegistry()
    for module in (
        groups,
        meta,
        navigate,
        studio_read,
        studio_templates,
        studio_brands,
        studio_delivery,
        studio_write,
        studio_build,
        studio_detail,
        studio_animation,
        studio_overlays,
        studio_music,
        studio_tools,
        library,
        library_tasks,
        search,
        index,
        persons,
        persons_review,
        settings_tools,
        topics,
    ):
        for spec in module.specs():
            registry.add(spec)
    return registry
