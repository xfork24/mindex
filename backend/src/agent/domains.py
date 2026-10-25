"""智能助手工具领域：页面领域映射、领域说明与会话已加载领域的最近使用（LRU）维护。

领域名元组在 ``mindex.shared.agent_domains``（配置校验也要用）；
``loaded`` 按最近使用排序，末尾为最近使用；meta 领域始终下发，不计入其中。
"""

from __future__ import annotations

from collections.abc import Iterable

from mindex.shared.agent_domains import LOADABLE_DOMAINS

# 页面 → 默认加载的领域。
PAGE_DOMAIN: dict[str, str] = {
    "studio": "studio",
    "settings": "settings",
    "search": "search",
    "admin": "library",
    "persons": "persons",
    "topics": "topics",
}

# 系统提示词中的领域目录：每个可加载领域一句说明。
DOMAIN_INFO: dict[str, str] = {
    "studio": "创作任务、方案、文案与文案模板、参考文稿、画幅、分镜与画面来源（含 AI 生成图片、AI 生成视频与 AI 动画）、"
               "背景音乐与歌词生成、构建与版本",
    "studio_detail": "创作诊断、版本依赖、召回词建议、人工选片、整列重做、AI 动画源码读取与修改、背景音乐歌词生成",
    "library": "素材与片段浏览、整理信息、素材报告、处理任务、上传素材与字幕",
    "search": "文字/图片/视频检索、找相似、区间抽帧、系统状态",
    "index": "内容索引版本列表、一致性检查、激活与回退",
    "persons": "演员/角色/参考图、识别演员、出镜审核、人物发布、检索组合、演员表导入",
    "settings": "模型档案（含图片/视频/音乐/动画生成）、业务设置、音色库与音色克隆、本机模型服务启停",
    "topics": "选题挖掘：领域档案、数据源（RSS、热榜、Brave 搜索、日历）、挖掘任务、选题卡片打分与素材覆盖、"
              "大纲、从选题建创作任务、任务档案同步、素材采集清单、每日定时",
}


def touch(loaded: list[str], domains: Iterable[str], limit: int = 3) -> list[str]:
    """把 domains 依次移到最近端（列表末尾），返回新列表，不修改入参。

    meta 与未知领域忽略（``loaded`` 里残留的也一并剔除）；结果无重复；
    超过 ``limit`` 时从头部淘汰最久未使用的领域。
    """
    known = set(LOADABLE_DOMAINS)
    result: list[str] = []
    for domain in [*loaded, *domains]:
        if domain not in known:
            continue
        if domain in result:
            result.remove(domain)
        result.append(domain)
    return result[-limit:] if limit > 0 else []
