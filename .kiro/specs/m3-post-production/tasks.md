# Implementation Plan: M3 后期与第三方工具

## Overview

三期顺序执行，每期结束有 Checkpoint。标记 `*` 的子任务为测试任务。

全程约束：
- 每项能力的“入口”子任务必须同时完成四个入口：Web 页面（中英文案）、`mindex studio` CLI（含 `--json`）、agent 工具、MCP 导出（同一 ToolSpec），并登记到 `tests/test_m3_entrypoints.py` 的入口矩阵（需求 22）。
- Web 与桌面 App 共用实现：业务只在 REST；新增包数据写进 pyproject package-data；ffmpeg 经 `shared/media`；不对 `__file__` 取哈希；下载带 `Content-Disposition`（需求 21）。
- 文案只改 `frontend/src/shared/messages.json`，运行 `python scripts/build-messages.py`；改动页面资源运行 `python scripts/build-assets.py`。
- 新字段遵守 Signature_Compat，`test_studio_signature_snapshot.py` 必须通过。
- 不创建 git commit，除非用户要求。

## Tasks

### 第一期 成片质感

- [x] 1. 后期管线重构（行为不变）
  - [x] 1.1 新建 `workspace/post.py`，把 compose 的拼接、混音、字幕、封装步骤移入
  - [x] 1.2 `post.compose_payload` 钩子接入 `handlers/compose.py`（未参与时返回空）
  - [x]* 1.3 现有 studio 测试与签名快照通过
  - _Requirements: 1.1, 1.2, 1.3_

- [x] 2. `mindex studio` 基础 CLI 与入口矩阵
  - [x] 2.1 `cli_studio.py`：`jobs`、`plans`、`preflight`、`build --watch`、`status`、`download`，进程内客户端 / `--url`
  - [x] 2.2 409 中文提示与非零退出；`--json`
  - [x]* 2.3 `tests/test_cli_studio.py`
  - [x] 2.4 `tests/test_m3_entrypoints.py` 入口矩阵框架（REST、CLI、agent、MCP、页面元素）
  - [x] 2.5 `docs/cli-reference.md`
  - _Requirements: 2.1–2.4, 22.2, 22.4_

- [x] 3. 镜内转场
  - [x] 3.1 `ShotTransition` 模型与 Shot 字段（None 省略）
  - [x] 3.2 render 实现 in/out 滤镜；payload 条件加键
  - [x] 3.3 入口：分镜详情后期面板、`studio shot set --transition-in/out`、agent/MCP `edit_shots`
  - [x]* 3.4 序列化兼容、签名不变、帧数与黑场亮度测试
  - _Requirements: 3.1–3.3, 22.1–22.3_

- [x] 4. 尾部余量与重叠转场
  - [x] 4.1 render 产出 handle（素材延长 / 冻结、图片与动画多渲染）
  - [x] 4.2 compose xfade 链（分批），无 next 时走 concat
  - [x] 4.3 入口：后期面板、CLI `--transition-next`、agent/MCP `edit_shots`
  - [x]* 4.4 总帧数、采样、boundaries 不变；末镜 next 忽略
  - _Requirements: 4.1–4.4, 22.1–22.3_

- [x] 5. 字幕样式模板
  - [x] 5.1 `workspace/ass.py`：`SubtitleStyle`、内置样式、`build_ass`（高亮、转义、缩放）
  - [x] 5.2 方案 `subtitle_style`、分镜 `highlights`；mux 烧录 ASS
  - [x] 5.3 入口：方案参数与后期面板、CLI、agent/MCP（方案参数与 `edit_shots`）
  - [x]* 5.4 ASS 快照、烧录冒烟、签名兼容
  - _Requirements: 5.1–5.4, 22.1–22.3_

- [x] 6. ASS 文字层
  - [x] 6.1 `Overlay` 模型与 Shot 字段；ASS 图层生成（title、lower_third、data_card）
  - [x] 6.2 `burn=false` 只烧图层、字幕软轨
  - [x] 6.3 入口：后期面板图层编辑、CLI `--overlay`、agent/MCP `edit_shots`
  - [x]* 6.4 时间换算、软字幕模式图层、像素检测
  - _Requirements: 6.1–6.3, 22.1–22.3_

- [x] 7. 品牌模板
  - [x] 7.1 `BrandTemplate` 模型、内置 YAML（package-data）、`brand_templates` 表（metadata v3）
  - [x] 7.2 品牌素材存储与上传接口；`/brands` CRUD（引用时 409）
  - [x] 7.3 方案 `brand_id`、`brand_overrides`；样式解析接入字幕与图层
  - [x] 7.4 入口：设置页品牌面板与方案选择、CLI `studio brand`、agent/MCP 品牌工具
  - [x]* 7.5 迁移幂等、哈希范围、覆盖合并、删除保护
  - _Requirements: 7.1–7.5, 21.2, 22.1–22.3_

- [x] 8. 水印、片头、片尾
  - [x] 8.1 片头片尾规范化与缓存；boundaries 偏移；BGM 只铺正文
  - [x] 8.2 水印叠加（含 `burn=false`）；方案覆盖片尾
  - [x] 8.3 入口：品牌面板与方案覆盖、CLI、agent/MCP
  - [x]* 8.4 时长、字幕偏移、水印像素、签名兼容
  - _Requirements: 8.1–8.3, 22.1–22.3_

- [x] 9. HTML 透明叠加层
  - [x] 9.1 `overlay` 阶段：内置 HTML 模板（package-data）+ LLM 生成，钉住语义
  - [x] 9.2 `animation_capture` 透明录制；render overlay 合成；预检阻塞（含桌面可选 Chromium 提示）
  - [x] 9.3 入口：叠加层源码编辑、CLI `studio overlay`、agent/MCP
  - [x]* 9.4 录制替身验证透明区域、签名含 HTML 哈希
  - _Requirements: 9.1–9.3, 21.5, 22.1–22.3_

- [x] 10. Checkpoint：第一期验证
  - [x] 10.1 ruff、文档链接、messages、assets、node --check、pytest not slow、入口矩阵
  - [x] 10.2 桌面：package-data 核对；`check-desktop-sidecar.py` 增加第一期渲染冒烟（无法本机构建时记录未验证范围）
  - 结果（2026-10-26）：静态检查全部通过；`pytest -m "not slow"` 1449 passed，唯一失败 `test_person_frame_pruning` 因当前环境未装 person extra（缺 `av`），与本功能无关；新增 slow 用例全部通过；冻结后端重建后 `check-desktop-sidecar.py --isolated-path --ffmpeg-dir desktop/build/ffmpeg/bin --font …` 通过。`--animation`（需联网下载 Chromium）未运行，HTML 文字层的真实透明录制未在本机验证。
  - _Requirements: 20.2, 21.6_

### 第二期 平台交付

- [x] 11. 多 final 阶段与变体
  - [x] 11.1 `variants()` 钩子、`p["finals"]`、selected/choose/new_plan/load 规范化
  - [x] 11.2 预检展开与占位、阻塞传播、`signatures.conflicts` 递归
  - [x] 11.3 diagnostics、agent 工具（select_version 的阶段取值由注册表生成，成片变体用 variant）去除写死的 compose；前端的成片面板在任务 12–17 的交付面板里统一展示各成片阶段
  - [x]* 11.4 只有 compose 时行为不变；测试用 final 阶段链式复用
  - _Requirements: 10.1–10.4_

- [x] 12. 多画幅快速派生
  - [x] 12.1 方案 `exports[]`；compose 产出 clean；`export:<canvas>` derive
  - [x] 12.2 入口：导出面板、CLI `studio export`、agent/MCP `set_exports`、`build_exports`
  - [x]* 12.3 尺寸、时长、字幕缩放、签名条件
  - _Requirements: 11.1, 11.2, 22.1–22.3_

- [x] 13. 多画幅重新渲染
  - [x] 13.1 子方案创建与同步；export 引用子方案成片
  - [x] 13.2 构建全部画幅（串行、整体取消）；页面折叠显示；CLI 与 agent/MCP 同步支持
  - [x]* 13.3 复用配音、同步、删除保护
  - _Requirements: 11.3, 11.4, 22.1–22.3_

- [x] 14. 平台预设
  - [x] 14.1 `studio/platforms/*.yaml`（package-data）与模型（YAML 注释注明核对日期与来源；YouTube 数值有帮助中心依据，B 站与抖音的字数与标签上限来自投稿页与公开资料，未找到平台官方文档，发布前需人工复核）
  - [x] 14.2 入口：发布面板、CLI `studio platforms`、agent/MCP `list_platforms`
  - _Requirements: 12.1, 12.2, 22.1–22.3_

- [x] 15. 封面
  - [x] 15.1 候选帧 API；`cover:<platform>` 三种来源；Pillow 排版（字体解析与字幕一致）
  - [x] 15.2 入口：封面编辑、CLI `studio cover`、agent/MCP
  - [x]* 15.3 尺寸、CJK 换行、预检阻塞
  - _Requirements: 13.1–13.3, 21.3, 22.1–22.3_

- [x] 16. 发布元数据
  - [x] 16.1 `metadata:<platform>` LLM 生成、校验修正、钉住
  - [x] 16.2 入口：元数据编辑器、CLI `studio metadata`、agent/MCP
  - [x]* 16.3 章节规则、长度限制、钉住保留
  - _Requirements: 14.1–14.3, 22.1–22.3_

- [x] 17. 发布包
  - [x] 17.1 `package:<platform>` 目录 + zip + manifest；下载接口（Content-Disposition）；方案 `platforms[]`
  - [x] 17.2 入口：发布面板、CLI `studio publish`、agent/MCP `build_publish_package`
  - [x]* 17.3 清单与哈希、上游变化重建
  - _Requirements: 15.1, 15.2, 21.5, 22.1–22.3_

- [x] 18. Checkpoint：第二期验证
  - [x] 18.1 全部检查与入口矩阵
  - [x] 18.2 桌面：发布包冒烟与下载保存
  - 结果（2026-10-26）：静态检查通过；`pytest -m "not slow"` 除缺 `av` 的人物用例外全部通过；studio 相关 slow 用例 150 passed、2 skipped；签名快照通过（新增方案参数为 None 时不落键）；冻结后端重建后 `check-desktop-sidecar.py --isolated-path --ffmpeg-dir … --font …` 通过（含平台预设接口与品牌页）。桌面外壳内的下载保存流程未在本机手动验证。
  - _Requirements: 20.2, 21.6_

### 第三期 扩展能力

- [x] 19. 曲库存储与导入
  - [x] 19.1 `music_tracks` 表、文件存储、API（引用时 409）
  - [x] 19.2 入口：设置页音乐库、CLI `studio music`、agent/MCP
  - [x]* 19.3 去重、授权必填、迁移幂等
  - _Requirements: 16.1, 16.5, 22.1–22.3_

- [x] 20. 曲目分析
  - [x] 20.1 `studio/beats.py`（纯 numpy）；后台任务；LLM 情绪标签
  - [x]* 20.2 合成节拍器 BPM 与节拍误差
  - _Requirements: 16.2, 21.4_

- [x] 21. 曲库选曲与推荐
  - [x] 21.1 `ShotMusic.kind=library`；music 阶段复制；授权过期阻塞
  - [x] 21.2 推荐接口；署名传入元数据；入口：音乐提示点选曲、CLI、agent/MCP `recommend_music`
  - [x]* 21.3 cue_identity 兼容、排序、过期阻塞
  - _Requirements: 16.3, 16.4, 22.1–22.3_

- [x] 22. 节拍对齐
  - [x] 22.1 方案 `beat_align`；render handle ≥ 容差；compose 切点延迟
  - [x] 22.2 入口：方案参数、CLI、agent/MCP
  - [x]* 22.3 切点距离 ≤ 1 帧、配音完整、关闭时签名不变
  - _Requirements: 17.1, 17.2, 22.1–22.3_

- [x] 23. 第三方工具登记与 HTTP 适配器
  - [x] 23.1 `tool_sources` 表、白名单、密钥引用；HTTP manifest 适配器；产物下载与校验
  - [x] 23.2 入口：设置页、CLI `studio tools`、agent/MCP（danger；mcp-stdio 登记不经 MCP）
  - [x]* 23.3 stub 服务：提交/轮询/下载、超时、超限、主机拒绝
  - _Requirements: 18.1–18.4, 22.1–22.3_

- [x] 24. MCP 客户端适配器
  - [x] 24.1 stdio（绝对路径与环境变量）与 streamable-http；list_tools 发现；call_tool 产物解析；惰性导入
  - [x]* 24.2 仓库内测试 MCP 服务器
  - _Requirements: 19.4, 21.6_

- [x] 25. 第三方工具分镜来源
  - [x] 25.1 `ShotSource.kind=tool`；`invoke` 阶段；视频与图片产物路径
  - [x] 25.2 入口：分镜来源选择与参数表单、CLI、agent/MCP `edit_shots`
  - [x]* 25.3 两种产物完整构建、上传授权阻塞、篡改校验
  - _Requirements: 19.1–19.3, 22.1–22.3_

- [x] 26. 文档与最终验证
  - [x] 26.1 `docs/studio.md`、`docs/cli-reference.md`、`docs/agent.md`、`docs/desktop.md`、`plans/ROADMAP.md`
  - [x] 26.2 完整验证与桌面第三期冒烟
  - 结果（2026-10-26）：静态检查（ruff、文档链接、messages/资源版本、全部前端脚本 `node --check`）通过；`pytest -m "not slow"` 1557 passed（不含缺 `av` 的人物用例）；studio 相关 slow 用例 42 passed；签名快照通过；无头 Chromium 打开 `/studio/music`、`/studio/tools`、`/studio/brands` 与创作页（工具来源编辑器、曲库选曲）无脚本错误。冻结后端重建后 `check-desktop-sidecar.py --isolated-path --ffmpeg-dir … --font …` 通过（新增曲目节奏分析、HTTP 工具替身与随包 mcp 客户端，`/studio/music`、`/studio/tools` 页面与接口）；冻结 CLI `studio tools add/test` 以 mcp-stdio 连接仓库内测试 MCP 服务器成功。未验证：真实第三方服务与公网 MCP 服务器、Electron 外壳内的手动操作。
  - _Requirements: 20.1, 20.2, 21.6, 22.5_
