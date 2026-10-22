# 素材详情加载验证

2026-10-20，本地素材库只读验证；未重建索引、重跑处理任务或修改素材记录。

## 行为

- `GET /api/admin/sources/{id}?detail=summary` 仅返回基础信息、片段数量及 GPU 锁配置。默认完整详情接口保持兼容。
- 基础详情、镜头分页、完整状态并发加载，独立渲染。完整状态加载或失败时处理入口禁用，失败可单独重试。
- 切换素材取消旧请求并递增请求序号；镜头分页与状态轮询也检查序号。首屏和 URL 定位无需等待完整状态。
- 媒体时间线、文件信息、探测结果仅在一次详情请求内复用。沿用既有磁盘探测缓存，没有新增跨请求状态缓存。
- 完整状态使用批量分析记录和轻量渠道投影，不构建完整索引快照；发布内容读取限定当前素材。

## 本地测量

下表为直接调用接口所用 Library 方法的耗时，非实际浏览器绘制时间。首次指新 Python 进程第一次调用，磁盘探测缓存已存在；不代表清空操作系统缓存后的冷启动。

| 素材 | 片段数 | 基础详情 | 首屏 48 镜头 | 完整状态首次 | 完整状态重复 | 完整详情数据库读取次数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 给阿嬷的情书.mp4 | 4057 | 1–2ms | 3.9ms | 2.68s | 2.30s | 82 |
| Botanic Garden.MP4 | 50 | 2ms | 3.1ms | 0.30s | 0.30s | 87 |

长素材完整状态仍略高于热加载 2 秒目标。主要剩余开销为人物发布数据读取与解析、媒体依赖文件检查；没有以省略检查换取速度。前端真实浏览器首屏绘制与播放器交互尚未实测。

## 一致性与回归

- 用 HEAD 中优化前的 Library/source_work 实现读取同一长素材，与新实现逐字段比较完整详情：全部一致，包括缺失文件、对白覆盖、分析和发布状态。该对照运行基线 20.85s、新实现 3.14s（当时有并发测试，故不用于上表）。
- `tests/test_source_work.py tests/test_dialogue.py tests/test_shot_media.py tests/test_source_detail_loading.py`：41 项通过（新增媒体探测复用测试前）。
- 最终 `tests/test_source_detail_loading.py tests/test_shot_media.py tests/test_admin.py -k 'not live_curation_filters_text_video_without_mutating_index'`：23 项通过，1 项排除。
- `node tests/admin_detail_loading_ui.cjs`：通过。受控异步响应验证慢状态不阻塞详情和镜头请求、取消旧请求、旧响应不覆盖新选择、状态失败独立重试。属于加载逻辑测试，并非浏览器端到端测试。
- Ruff、JavaScript 语法检查、`git diff --check`：通过。
- `test_live_curation_filters_text_video_without_mutating_index` 的检索首项 display_name 断言失败。将 snapshots_for 替换回 HEAD 原实现后同样失败；未修改现有检索排序或用户已有向量索引改动。
