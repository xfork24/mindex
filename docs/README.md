# Mindex 文档中心

本目录是项目的系统性参考文档，讲清**机制与用法**；根目录的操作手册讲**按步骤执行**，两者互补、互相链接。

## 按需求导航

| 我想… | 先读 | 再读 |
|---|---|---|
| 了解整体架构与数据流 | [架构与模块详解](architecture.md) | [索引版本与空间身份](index-versions.md) |
| 跑通离线处理管线 | [离线管线详解](pipeline.md) | [电影处理指南](../FILM_WORKFLOW.md) |
| 查某个命令怎么用 | [CLI 完全参考](cli-reference.md) | [配置参考](config-reference.md) |
| 理解检索排名怎么算 | [检索机制详解](search.md) | — |
| 换模型 / 回退索引 | [索引版本与空间身份](index-versions.md) | [部署说明](../DEPLOYMENT.md) |
| 做真实模型端到端验证 | [e2e 实验框架](e2e-framework.md) | [端到端操作手册](../E2E_RUNBOOK.md)、[测试计划](../E2E_TEST_PLAN.md) |
| 管理素材（改名/收藏/隐藏） | [素材后台指南](admin.md) | — |
| 用哔哩哔哩缓存转 MP4 | [附带工具](tools.md) | [tools/m4s-converter/README.md](../tools/m4s-converter/README.md) |
| 标注评测查询 | [评测标注说明](../eval/README.md) | [e2e 实验框架](e2e-framework.md) |

## 文档一览

- [architecture.md](architecture.md) — 模块职责、离线/在线数据流、存储布局、SQLite 表、五条核心不变量
- [pipeline.md](pipeline.md) — detect/split/transcribe/align/analyze/index 各阶段机制、检查点与断点续跑、指纹键
- [cli-reference.md](cli-reference.md) — `mindex` 与 `mindex-model-server` 全部命令、参数、默认值、示例
- [config-reference.md](config-reference.md) — 全部配置键、类型、默认值、影响与常用组合
- [search.md](search.md) — 五路召回、加权 RRF、视频查询窗口化、筛选器、分数语义
- [index-versions.md](index-versions.md) — 索引版本指纹、发布检查、激活与回退、能力门槛
- [e2e-framework.md](e2e-framework.md) — probe/pin 信任模型、11 个 phase 状态机、标注冻结、评测 profile
- [admin.md](admin.md) — 素材后台功能与管理 API
- [tools.md](tools.md) — m4s-converter 等附带工具

## 根目录文档（任务导向）

| 文档 | 用途 |
|---|---|
| [README.md](../README.md) | 项目概览与快速上手 |
| [PLAN.md](../PLAN.md) | 设计决策、验收标准（40 条测试集 Hit@5 ≥85%） |
| [FILM_WORKFLOW.md](../FILM_WORKFLOW.md) | 整部电影「分镜 → 切片 → 画面索引」最短流程 |
| [DEPLOYMENT.md](../DEPLOYMENT.md) | GPU 服务部署（自定义 mindex 服务 / llama.cpp 路径）、HTTP 协议 |
| [E2E_RUNBOOK.md](../E2E_RUNBOOK.md) | 真实模型端到端验证操作手册（当前实验：trailer-20261013） |
| [E2E_TEST_PLAN.md](../E2E_TEST_PLAN.md) | 端到端测试设计与通过条件 |
| [eval/README.md](../eval/README.md) | 评测查询标注说明与模板生成器 |
| [VERIFICATION.md](../VERIFICATION.md) | 实现验证记录（带日期的历史档案） |
