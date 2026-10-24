# 中文素材检索评测

真实 trailer 的 20 条待复核中文查询在 `trailer.queries.draft.jsonl`。本轮分阶段实验使用 `mindex e2e freeze/phase`，详见 [操作手册](../docs/E2E_RUNBOOK.md)。草稿只能运行 preview，不能产生质量分数。评测现包含 Hit@1/5、MRR@10、nDCG@10，以及去对白、去风格消融和候选集身份检查；无答案查询单独诊断。

```bash
uv run python eval/generate_queries.py
```

生成 `queries.jsonl`：20 条开发文字、40 条测试文字、12 条视频模板。示例文字是标注起点，必须结合实际素材替换为**库中存在的事件**；没有相关素材的查询应单独评估无答案行为，不混入 Hit@5 目标。视频模板须填入实际查询片段。

每条查询的 `relevant` 必须人工填写，支持片段 ID 或原视频时间区间：

```json
{"id":"text-01-01","split":"dev","text":"一个人推开门，停顿后走进房间","relevant":[{"source_id":"素材SHA256","start":120.2,"end":124.8,"grade":3,"min_overlap":0.5}]}
```

或 `{"segment_id":"片段ID","grade":3}`。grade 为 1–3：部分相关、主要相关、完全相关。区间命中要求交集至少覆盖查询结果和标注区间中较短者的 `min_overlap` 比例（默认 0.5）。同一相关区间的重复窗口在 nDCG 中最多奖励一次；该指标按相关组评估，与逐条候选标注的标准数据集分数不可直接横比。

先汇总画面、描述和融合检索的候选池，补全相关目标，避免未标注的正确结果被误判。对白原句应替换成素材中真实的说法，动作和镜头标签亦需人工确认。开发、测试素材尽量分离，并覆盖相似人物/场景的困难负例。

```bash
uv run mindex evaluate eval/queries.jsonl --split dev --output data/eval-dev.json
uv run mindex evaluate eval/queries.jsonl --split test --output data/eval-test.json
uv run mindex evaluate eval/queries.jsonl --split video-test --output data/eval-video.json
```

未标注模板会被拒绝，不能产生虚构质量分数。报告比较纯视频、纯描述和融合，包含逐查询结果与耗时。INT8/NF4 需各自建立索引、使用相同测试集，记录实际模型空间 ID 与显存峰值。当前不自动搜索融合权重；只在开发集修改配置，然后冻结测试。

## 拆分镜全链路评测

[`split_eval.py`](split_eval.py) 用 [`split_cases.jsonl`](split_cases.jsonl) 中的用例评测创作工作台的拆分镜：先拆分，可选附带素材画面词表，也可选做召回检查。脚本调用真实拆分镜服务，模型取 `studio.generator` 配置；如果设置页激活了"文本拆分镜"档案，则以该档案为准。

用例文件每行一个 JSON 对象，字段如下：

- `id`：用例标识。
- `text`：输入文案。
- `expected`：期望的分镜文本列表，按顺序排列。
- `note`：说明，可选。

示例：

```json
{"id":"sg-welcome-abstract","text":"Welcome to Singapore — the Lion City, where nature, culture, and modern life come together in the most beautiful ways.","expected":["Welcome to Singapore — the Lion City,","where nature, culture, and modern life come together in the most beautiful ways."],"note":"抽象列举保持整句"}
```

```bash
uv run python eval/split_eval.py
uv run python eval/split_eval.py --materials 20261023-174141-5f6104 --recall
uv run python eval/split_eval.py --cases my_cases.jsonl --output /tmp/split.json --config config.local.yaml
```

- `--materials JOB_ID`：生成该创作任务的素材摘要，并把摘要里的画面词表作为 `vocabulary` 传给拆分镜。任务不存在时，脚本输出中文错误，以退出码 2 结束，不做任何拆分。摘要为空时，脚本输出为空原因（无生效索引、无已分析镜头或预算为 0），然后不带词表继续拆分。
- `--recall`：用每镜的召回词按工作台召回设置执行召回，列出前 3 条结果的素材名、起止时间与画面描述；没有结果的镜头标注"该镜无召回结果"。同时指定 `--materials` 时，召回范围限定为该任务的素材范围。

运行结束后，脚本输出三项指标：

- 切分吻合度：吻合的用例数占总用例数的百分比。实际分镜与期望分镜在去掉每镜首尾空白后逐镜完全一致，才算吻合。
- 过短分镜数：少于 3 个英文词、并且少于 5 个汉字的分镜总数，单独成镜的整句不计入。
- 平均分镜时长：按英文每秒 2.5 词、中文每秒 4 字估算，单位为秒，保留 1 位小数。

调用失败或未通过硬校验的用例会标记为失败，并记录中文原因；这类用例计为不吻合，评测继续。用例文件无法读取或格式错误时，脚本以退出码 2 结束。

报告默认写入 `data/eval-split-YYYYMMDD-HHMMSS.json`（本地时间），内容包含三项指标、每条用例的期望切分与实际切分（含 visual 与 query），以及召回结果。脚本对 `data/` 只读，唯一的写入是这份报告；它不读写任务 workspace，也不加锁，因此不会改动任何任务的方案与分镜。
