# 中文素材检索评测

真实 trailer 的 20 条待复核中文查询在 `trailer.queries.draft.jsonl`。本轮分阶段实验使用 `mindex e2e freeze/phase`，详见 [操作手册](../E2E_RUNBOOK.md)。草稿只能运行 preview，不能产生质量分数。评测现包含 Hit@1/5、MRR@10、nDCG@10，以及去对白、去风格消融和候选集身份检查；无答案查询单独诊断。

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
