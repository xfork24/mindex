"""智能助手工具领域常量。

放在 shared 下，供 ``shared/config.py`` 的配置校验与 ``agent/registry.py`` 共用；
本模块不得导入 ``mindex.agent``，避免循环依赖。
"""

from __future__ import annotations

# 全部工具领域；meta 领域的工具每一步都会下发。
AGENT_DOMAINS: tuple[str, ...] = (
    "meta",
    "studio",
    "studio_detail",
    "library",
    "search",
    "index",
    "persons",
    "settings",
)

# 可按需加载的领域（不含 meta）。
LOADABLE_DOMAINS: tuple[str, ...] = AGENT_DOMAINS[1:]

# 页面 → 默认加载的领域。
PAGE_DOMAIN: dict[str, str] = {
    "studio": "studio",
    "settings": "settings",
    "search": "search",
    "admin": "library",
    "persons": "persons",
}

# 系统提示词中的领域目录：每个可加载领域一句说明。
DOMAIN_INFO: dict[str, str] = {
    "studio": "创作任务、方案、文案、分镜、构建与版本",
    "studio_detail": "创作诊断、版本依赖、召回词建议、人工选片、整列重做",
    "library": "素材与片段浏览、整理信息、素材报告、处理任务、上传素材与字幕",
    "search": "文字/图片/视频检索、找相似、区间抽帧、系统状态",
    "index": "内容索引版本列表、一致性检查、激活与回退",
    "persons": "演员/角色/参考图、识别演员、出镜审核、人物发布、检索组合、演员表导入",
    "settings": "模型档案、业务设置、音色库与音色克隆",
}
