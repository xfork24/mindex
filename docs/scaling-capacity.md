# 检索分页、查询预算与容量工装

集群检索在请求级 PG 快照内建立 eligible 关系，用 SQL 聚合计数、词法统计与人物排名。
ID 默认按 512 条分页，annotation 使用与参考路径相同的 Unicode casefold；人物证据只读取
当前页片段重叠区间。可表达的素材/粒度条件下推 Chroma，其余条件逐页过滤并合并全局排序。
集群缺失派生数据或分词器不匹配时要求回填，不自动装载全量快照。

运行参数位于 `search`：

```yaml
search:
  candidates: 100
  id_page_size: 512
  query_timeout_seconds: 5
  query_cache_bytes: 67108864
  query_cache_entries: 128
```

总期限覆盖连接池等待、SQL、向量/嵌入 HTTP 和水合。耗尽时返回 503、
`code=query_budget_exhausted`、`retryable=true` 和 `Retry-After: 1`，不会变成空结果。
`max_query_seconds` 仍是查询视频的时长上限，不是请求期限。
模型/检索参数的设置页热更新与部署字段重启边界见闭合计划第六批。

容量脚本为 `scripts/scaling-capacity.py`，两个固定 profile 保持原规模：

| profile | segments | 每版本 vectors | 版本数 | 维度 | 并发 |
|---|---:|---:|---:|---:|---:|
| 100k | 100000 | 1000000 | 3 | 4096 | 20 |
| 1m | 1000000 | 3000000 | 3 | 4096 | 20 |

预检只检查裸向量，仍需另留 HNSW、PG、WAL、备份和媒体空间：

```sh
.venv/bin/python scripts/scaling-capacity.py preflight --profile 100k --vector-storage /test-volume
.venv/bin/python scripts/scaling-capacity.py preflight --profile 1m --vector-storage /test-volume
```

使用独立空 cluster 配置，禁止指向业务库。工程模型配置必须明确为
`embedding.model: mindex-capacity-engineering-v1`、`quantization: float32`、`dimensions: 4096`，
`embedding.base_url` 指向下列工程服务。服务不读取真实模型，也不代表模型质量。

```sh
.venv/bin/python scripts/scaling-capacity.py seed --config /isolated/capacity.yaml --profile 100k --vector-storage /test-volume
.venv/bin/python scripts/scaling-capacity.py embedder --config /isolated/capacity.yaml --profile 100k --port 49110
# 使用独立节点目录启动两个 API，记录它们的真实 PID，然后执行：
.venv/bin/python scripts/scaling-capacity.py run --config /isolated/capacity.yaml --profile 100k \
  --api-url http://127.0.0.1:49201 http://127.0.0.1:49202 --api-pid 12345 12346 \
  --output artifacts/capacity-100k
```

两个 API 必须绑定同一库，使用互不共享的节点目录；固定每 API RSS≤2 GiB。
脚本核对实际三个向量集合与元数据条数、API PID，分别执行 1%/0.1% 素材范围。
原始请求逐项写入 JSONL，失败、503 和客户端超时也计入延迟分布及失败数；越界结果令专项失败。
保留冷请求、热 p50/p95/p99、每 API 峰值 RSS、池/队列/staging 指标、代码版本和源码摘要。
热 p95≤2s、p99≤5s、零错误和范围不越界为工程容量门槛。

`--smoke` 使用较小工程样本，仅验证工装；报告永远标明不代表容量验收。
规定规模无法容纳时保留环境不足记录，不能通过降低维度、样本或并发取得正式通过。
真实模型质量继续使用确认版本评测集与 `mindex e2e`，Hit@5≥85% 独立待验收；
工程近邻命中率不能替代这个结论。规定规模出现 Chroma 门槛失败时保留请求、指标和剖析，
再按原计划评估替代后端，不降低门槛。
