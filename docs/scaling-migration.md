# 分阶段迁移与恢复

执行计划见 [剩余能力闭合计划](../plans/horizontal-scaling-completion-plan.md)。只支持同一程序/schema 版本迁移；先停写导出，不支持混合协议滚动运行。

```bash
mindex --config source.yaml migration export --target /isolated/bundle --with-media --with-vectors
mindex --config target.yaml migration preflight --source /isolated/bundle
mindex --config target.yaml migration load --source /isolated/bundle --with-vectors
mindex --config target.yaml migration verify --source /isolated/bundle
mindex --config target.yaml migration activate --source /isolated/bundle
```

首次写目标对象之前，持久保存 migration_id、完整清单摘要、源数据库摘要与库 UUID。阶段为预检、身份、媒体、向量、元数据、恢复任务、验证、激活；对象、集合和向量批次另有 pending/verified 账本。进程中断后重复同一 load 命令；写入成功但未落账时先读取并核对字节/向量，再补记。不同清单、异值对象、额外向量或已有业务内容均拒绝覆盖。禁止手动删除账本绕过检查。

迁移中的目标允许诊断读取，业务写入和领取任务拒绝；ready 返回未激活状态，live 仍表示进程存活。旧 running attempt 不当作成功：可重试任务重新排队或中断，未知 GPU/外部请求保留占用并要求核对。源表清单和恢复后运行状态分开对账。激活只有在媒体/向量/元数据验证和秘密检查完成后原子开放；已激活目标即使产生新写入，也不能重放旧 bundle。反向迁移先重新 export，以新清单为准。

已登记配置字段中的明文秘密在暂存副本裁剪并清理旧页；环境变量/密钥引用保留。普通用户内容不重写；未知结构化秘密载体列出位置并阻止正式激活。请在原数据或隔离副本中明确处理，再重新导出，不要改写 bundle 清单绕过摘要。未完成的 S3 原生 multipart 不能跨后端搬运：先续传完成或显式取消。

持久卷灾备入口：

```bash
.venv/bin/python scripts/verify-scaling-recovery.py --output artifacts/scaling-recovery-unique
```

入口创建独立 PG 持久卷、basebackup、WAL archive 和对象保护目录；在 basebackup 后追加写入，切断源 PG/MinIO，用另一持久卷及独立 MinIO 恢复到保护 LSN，逐表/逐对象复算摘要。只清理本轮服务和卷，保护副本、日志与报告留在 output。默认工程样本不能证明代表性规模 RPO≤24h、RTO≤8h；报告保留实际字节数和恢复时间，正式门槛维持独立待实测。组件验收使用的 tmpfs 不用于灾备结论。
