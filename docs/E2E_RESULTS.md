# 真实模型端到端测试结果

> 历史记录，内容可能与当前代码不符，以 [e2e-framework.md](e2e-framework.md) 与当次实验目录为准。

更新日期：2026-10-13。本轮实验位于 `data/e2e/trailer-20261013/`，阶段状态以其中的 `run.json` 为准。本报告区分已观测结果与待执行项目。

## 数据与标注

输入为 117.083 秒预告片 `data/verification/film-toolchain/trailer.mp4`，素材 SHA256：`24699348d2317442e77bb6e7dde4149713bf5796547c38254ebb144851d0f26b`。固定候选为 61 个精细片段；镜头、片段与原片哈希已保存。

用户提供 `~/Downloads/queries.reviewed.jsonl`，确认人 `xfork24`，共 20 条中文查询，分为 8 dev / 12 test。冻结文件：`data/e2e/trailer-20261013/frozen-queries.reviewed.jsonl`；SHA256：`e3bf72a3d3199f99682b5fb5532c1e4c89217c1843bbe8a12cbd2e042ea1fb3f`。不使用 test 调参。

当前活动索引仍为完整电影的画面索引 `8ebf8b8455e2e94d277cf8fa`，包含 2,336 个精细片段。trailer 的测试构建默认不发布，网页测试通过 `serve --port 8001 --index-id INDEX_ID` 指定版本。

## 已完成的真实链路

| 项目 | 观测结果 | 证据 |
|---|---|---|
| WeMM 能力门槛 | 六个真实片段产生 4096 维有效单位向量；重复稳定且随内容变化，13.87 秒 | `smoke.json` |
| 画面索引 | 61 个向量，100.14 秒；重跑不重复推理 | `visual-latest.json`、`resume-check.json` |
| 视频查询 | 12 条原片、自裁、跨切点和转码查询均可召回；首位符合派生时间区间 | `video-engineering-check.json`，非独立语义质量验收 |
| ASR 原始文本 | 4 个 30 秒音频块（末块不足 30 秒），7.50 秒；原始响应保留 | `asr-latest.json` |
| CPU 强制对齐 | 固定模型 commit，37.28 秒；230 个有效区间，均在原片范围内 | `asr-alignment-check.json`；方言准确度仍需听辨 |
| Qwen 视觉冒烟 | 3 个实际片段通过图片输入和结构化输出检查，234.87 秒 | `analyze-smoke-latest.json`、`analyzer-smoke-review.json` |
| 整部电影已有索引 | 2,336 个 4096 维有效单位向量；Chroma/SQLite/媒体一致 | `data/e2e/whole-film-index-check.json` |

WeMM 与 ASR 分别使用用户提供的 Q8_0 GGUF；Qwen 来自用户的 kvmem 部署。三个服务的权重/mmproj 哈希及运行身份固定在 `data/deployments/`。Qwen 服务未暴露量化及真实 build 提交，记录为 `not_exposed`，不从启动脚本名推断量化。

## 已确认中文标注上的画面基线

画面索引：`948e6b40442b0230cbc529f4`。全部查询文字与先前真实预览相同，用户修订了两条查询的目标。本次直接按确认目标对保存的真实排名重新计分，无新推理请求。已校验候选、索引、查询和整理状态，保存排名仅包含 video 路由。

| 分组 | 查询数 | Hit@1 | Hit@5 | MRR@10 | nDCG@10 |
|---|---:|---:|---:|---:|---:|
| dev | 8 | 100.0% | 100.0% | 1.000 | 0.960 |
| test | 12 | 66.7% | 91.7% | 0.778 | 0.813 |

报告：`data/e2e/trailer-20261013/evaluation-baseline-replayed-reviewed.json`，含逐查询结果、原预览及确认标注哈希。历史 20 条请求 P50 为 482.5ms、P95 为 923.45ms；这些是先前已加载模型的客户端请求耗时，不是模型装载时间。

该结果仅是单支预告片上的冒烟，未满足正式验收要求的多个来源与至少 40 条 test，不据此宣称正式质量达标。

## 尚待完成

1. Qwen 全部镜头/精细片段分析与连续场景分组。批处理当前运行中，逐片保存检查点；此时保持 `:4040` 可用。
2. Qwen 完成后，由用户切回同一部署的 WeMM。构建独立 enriched 索引并检查，不改变完整电影活动索引。
3. 对相同 61 候选和已冻结中文集运行 video_only、video_asr、description_only、fusion、no_dialogue、no_style；分别保存 dev/test 与索引版本，不凭预期宣称融合有提升。Qwen 的收益归因和规模处理成本按 [QWEN_BENEFIT_EVALUATION.md](QWEN_BENEFIT_EVALUATION.md) 执行。
4. 在指定测试索引的浏览器完成中文搜索、播放、上下文及后台定位验证。此前真实画面版本已走通中文召回、片段播放和视频上传；新版本的完整链路仍需验证。
5. 人工听辨 ASR/对齐、独立视频相关性标注及部署机 GPU 峰值记录。当前未据此生成质量通过或显存验收结论。
6. 图片找视频：补齐原生图片嵌入、上传与预览、筛选及播放链路；待切回 WeMM 后进行真实图片调用。增加图片查询无需重建现有文字/视频向量。

工程检查：43 个自动化测试通过（47.86 秒），Ruff 检查及 sdist/wheel 构建通过。测试适配器结果只证明工程行为，真实质量分数来自上述实际调用和用户确认标注。
