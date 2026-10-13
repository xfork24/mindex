# 本地真实模型端到端验证操作手册

计划见 [E2E_TEST_PLAN.md](E2E_TEST_PLAN.md)。本手册实现串行阶段，不启动或卸载远程模型。CPU 分镜、SQLite、Chroma 和网页运行在本机。每次只在 GPU 机器启动一个模型。TTS 不参与本轮。

## 当前实验

- 原素材：`data/verification/film-toolchain/trailer.mp4`，117.083 秒。
- 素材 SHA256：`24699348d2317442e77bb6e7dde4149713bf5796547c38254ebb144851d0f26b`。
- 已有 61 个 shot、61 个 moment；实验候选固定为 61 个 moment。
- 实验目录：`data/e2e/trailer-20261013/`。`run.json` 是实际阶段状态，不能用本手册描述代替实际结果。
- `source.json`、`segments.json`、`config.initial.json` 固定输入；每个阶段重新检查原素材、切片哈希、切分配置及整理状态。
- 20 条中文草稿：`queries.draft.jsonl`，8 dev + 12 test。已根据画面填入暂定目标，全部需人工复核；动态动作不能只看缩略图，相关候选池需补全。对白查询等转写完成后补充，不以字幕推定真值。
- 2026-10-13 用户提交 `~/Downloads/queries.reviewed.jsonl`，复核人 `xfork24`。已冻结为 `frozen-queries.reviewed.jsonl`，SHA256 `e3bf72a3d3199f99682b5fb5532c1e4c89217c1843bbe8a12cbd2e042ea1fb3f`。后续文字评测使用此文件，不再使用草稿。
- 12 条视频查询和 5 条无答案诊断分别在 `video-queries.draft.jsonl`、`no-answer.draft.jsonl`。查询视频从不进入素材库。

现有 `config.local.yaml` 已选 `llama_cpp`。部署清单在 `data/deployments/`。可随时改 backend 回到 `mindex` 自定义服务，原有 `/embed/text`、`/embed/video`、`/transcribe` 协议继续支持。

## 1. WeMM：兼容性、能力门槛与画面索引

GPU 主机的已确认启动命令：

```bash
/home/hengliu/workspace/llama.cpp/build/bin/llama-server \
  -m ./WeMM-9B-Q8_0.gguf --mmproj ./mmproj-WeMM-9B-BF16.gguf \
  --host 0.0.0.0 --port 4010 -ngl 999 \
  --embeddings --pooling last --video-fps 4
```

首次实际探测发现未带 `--embeddings` 时返回 501，即使 `/health` 是 ok 也无法嵌入；因此健康检查不等于模型能力通过。

```bash
# 仅获取健康和运行信息，无推理。
uv run mindex e2e probe --output data/e2e/wemm-probe.json

# 用户提供部署机真实文件 SHA256 后，固定运行清单。已存在时不要覆盖。
uv run mindex e2e pin \
  --weights-sha256 4bb0e97b279404883207001fea15f6369d7c43930d1adc73a406ba1d9b66806c \
  --mmproj-sha256 62d8c77bbfd8c8cdf4a2f99b401dda5d978d1413d5bea29b15fc7c109a9c73b7 \
  --quantization Q8_0 --output data/deployments/wemm.json

# 新素材实验先 prepare，再 init。当前 trailer 实验已 init，不重复执行。
uv run mindex e2e init --source SOURCE_ID --run data/e2e/NEW_RUN

uv run mindex e2e phase smoke --run data/e2e/trailer-20261013
uv run mindex e2e phase visual --run data/e2e/trailer-20261013
uv run mindex e2e phase check --run data/e2e/trailer-20261013
uv run mindex search '骑自行车的人靠近手推车'
uv run mindex serve
```

`smoke.json` 包含六片段的文字/视频相似度矩阵、视频两两相似度、重复稳定性、维度、范数和耗时。常量视频向量、非有限数值、零向量、错误维度、身份变化会阻止建库；探测中断也不会留下通过的能力门槛。结构门槛通过不等于检索质量达标。

适配器直接发送 `/v1/embeddings` 的 `input_video`，不降级成描述嵌入或单帧平均。文本和视频使用官方单轮 user + `<embedding>` 模板，查询与文档无额外指令。视频全片均匀采样至最多 24 帧、4fps、单帧最多 262144 像素，H264 CRF18、不带声音，服务使用 `--video-fps 4`；超长输入必须先分窗。两端均固定预处理参数。

llama.cpp API 不独立证明文件哈希或启动池化参数：哈希和启动命令由部署者确认，客户端在每次请求前后核对 build、模型元数据、模板、模态等可观察指纹。该信任边界写入空间身份。更换 GGUF、mmproj、运行构建、模板、适配器代码或预处理会要求新的能力门槛和索引；不会混入旧向量。

`visual` 与 `enriched` 建立独立不可变版本并记录 index_id，默认不改变活动库。明确发布时加 `--activate`。重跑复用已完成向量；失败可重跑同一阶段。原始 trailer 默认不需要重新分镜。新增电影的活动索引已存在时，使用 `mindex serve --port 8001 --index-id TEST_INDEX_ID` 验证测试版本。

## 2. ASR：原始文本与 CPU 对齐

完成所有画面基线查询后，再停止 WeMM、启动 `:4020` Qwen3-ASR。ASR 需要其实际 GGUF/mmproj 哈希，重复 `e2e probe --role asr` / `e2e pin --role asr`，输出到 `data/deployments/asr.json`。ASR 部署应启用音频投影和模型对应的 chat template，原始响应须包含 `language …<asr_text>…`；异常响应不会当作对白。

```bash
uv run mindex e2e phase asr --run data/e2e/trailer-20261013
```

每 30 秒保存原始文本、语言、原始响应、部署身份和原视频起点，缓存位于 `data/transcripts/SOURCE_ID/PROFILE/raw/`。原始文本阶段完成后状态是 `pending_alignment`；`analyze` 会拒绝使用未对齐对白。转写中断后复用已完成块，不重转整片。

强制对齐用独立 CPU 环境，避免改动主程序 Transformers 或占用远程 16GB 显存：

```bash
uv venv .venv-align --python 3.11
uv pip install --python .venv-align/bin/python -r deploy/requirements-alignment.txt
uv run mindex e2e phase align --run data/e2e/trailer-20261013 \
  --python .venv-align/bin/python
```

本地配置已固定 `Qwen/Qwen3-ForcedAligner-0.6B` commit `c7cbfc2048c462b0d63a45797104fc9db3ad62b7`。更换 aligner 时在原始转写之前固定 revision；配置变化会使用独立缓存。worker 强制 CPU/float32，空文本不制造时间戳。对齐结果检查边界并映射回原片时间；每块检查点可续跑。未安装环境或未下载权重时会明确失败，不虚构结果。CPU 模型按块加载，以独立进程隔离内存，整部电影可按性能数据再优化为常驻 worker。

至少人工核对 10 处对白及时间偏差，记录听辨困难、方言、背景音乐和静音块。未完成这项检查时，不宣称对白质量通过。

## 3. Qwen 分析与完整索引

停止 ASR，启动 `:4040` Qwen3.8:27b，模型应支持图片输入。本地配置 `analyzer.backend: llama_cpp`，须与 WeMM/ASR 相同方式固定部署清单：先 `e2e probe --role analyzer`，再 `e2e pin --role analyzer`（提供 Qwen GGUF 与 mmproj 的实际 SHA256、量化类型），输出到 `data/deployments/analyzer.json`——`config.local.yaml` 已指向该文件，清单缺失时 `analyze` 阶段会明确拒绝执行。先用 `analyze-smoke` 三个片段核对 JSON 与画面依据，再批量分析。已完成分析检查点会复用。

```bash
uv run mindex e2e probe --role analyzer --output data/e2e/qwen-probe.json
uv run mindex e2e pin --role analyzer \
  --weights-sha256 <Qwen GGUF SHA256> --mmproj-sha256 <mmproj SHA256> \
  --quantization <实际量化> --output data/deployments/analyzer.json
uv run mindex e2e phase analyze-smoke --run data/e2e/trailer-20261013
uv run mindex e2e phase analyze --run data/e2e/trailer-20261013
```

描述包含动作、场景、情绪依据和镜头信息；连续场景必须完整覆盖、最长 45 秒。完成后停止 Qwen，重新启动同一份 WeMM；运行身份变了则先核对部署，不直接修改旧清单。

```bash
uv run mindex e2e phase enriched --run data/e2e/trailer-20261013
uv run mindex e2e phase check --run data/e2e/trailer-20261013
uv run mindex indexes list
# 回退到 visual 阶段保存的 index_id：
uv run mindex indexes activate VISUAL_INDEX_ID
```

实验 enriched 仍仅索引 moment，保证各路相同候选集；镜头和场景描述仍被分析和保存。Qwen 支持 `e2e probe/pin --role analyzer` 固定 GGUF/mmproj 与运行指纹。本次 kvmem 部署报告 `build_info=b0-unknown` 且未暴露量化，另存用户给出的 `kvmem-v0.16.0-rc3` 启动信息，量化明确记为 `not_exposed`。库的正式发布可使用原有 `index` 命令选 shot/moment/scene。

## 4. 冻结人工标注与评估

在每条标注复核并补全后，把 `label_status` 改为 `confirmed`，使用真实确认人。确认不会由程序自动完成。

当前已确认的中文集保留全部原查询文字，修订两条查询的相关目标。因此已对保存的真实画面召回排名做回顾计分，结果见 `evaluation-baseline-replayed-reviewed.json`：dev 8 条 Hit@1/5 均为 100%；test 12 条 Hit@1 66.7%、Hit@5 91.7%、MRR@10 0.778、nDCG@10 0.813。该报告明确记录历史推理和新标注的哈希，不代表重新调用模型，也不能替代完整语义索引对比。

也可运行 `mindex e2e review QUERY_FILE --run RUN_DIR` 生成 `label-review.html`，逐条观看、选择相关候选并导出 JSONL，再冻结导出的文件。评测/preview/check 可用 `--index-id` 指定版本；默认使用本实验的 enriched/visual 版本。

```bash
uv run mindex e2e freeze data/e2e/trailer-20261013/queries.draft.jsonl \
  --run data/e2e/trailer-20261013 --confirmed-by '实际复核人'
uv run mindex e2e phase evaluate --run data/e2e/trailer-20261013 \
  --queries data/e2e/trailer-20261013/frozen-queries.jsonl --split dev
uv run mindex e2e phase evaluate --run data/e2e/trailer-20261013 \
  --queries data/e2e/trailer-20261013/frozen-queries.jsonl --split test
# 视频集也要先 freeze，再指定 frozen-video-queries.jsonl：
uv run mindex e2e phase video-evaluate --run data/e2e/trailer-20261013 \
  --queries data/e2e/trailer-20261013/frozen-video-queries.jsonl
```

画面/完整索引分别保存结果，不覆盖对方：报告文件名包含 index_id、split 和阶段。报告包括 Hit@1/5、MRR@10、按相关组计的 nDCG@10、逐查询排名/分路贡献、模型空间、查询 SHA256、索引与整理状态指纹、首请求和后续 P50/P95。无答案诊断记录返回结果，不混入相关性分数，也不要求系统返回空集。

比较 video_only、video_asr、description_only、fusion、no_dialogue、no_style。video_asr 同时排除描述与风格的向量和 BM25 文本，用于区分 ASR 与 Qwen 的收益；去对白和去风格也从 BM25 文档中移除该字段。视频/图片查询只跑视频分路，避免重复报出相同的“融合效果”。收益归因、逐查询退步及规模处理成本见 [QWEN_BENEFIT_EVALUATION.md](QWEN_BENEFIT_EVALUATION.md)。

图片工程查询集保存在 `image-queries.draft.jsonl`，12 张来自原片候选的派生帧，覆盖 JPEG/PNG/WebP。切回 WeMM 后先运行 `e2e phase preview --queries data/e2e/trailer-20261013/image-queries.draft.jsonl --run data/e2e/trailer-20261013 --index-id VISUAL_INDEX_ID`。独立图片相关性尚未确认，不自动生成质量分数。图片人工集可使用 `image` 路径、媒体 `sha256` 和 `split: image`，经 freeze 后运行 `e2e phase evaluate --split image`。

只用 dev 调参。trailer 结果属于冒烟，不能证明 Hit@5 ≥85%。正式验收需要多个真实来源、长镜头、无字幕对白、困难负例，以及 20 dev +40 test 人工标注。

## 5. 浏览器、性能与异常验收

真实服务保持 WeMM，浏览器打开 `/`：输入中文、看排序、播放片段、打开上下文、跳转对应素材管理。`/admin` 验证改名、隐藏继承/恢复及收藏不影响排名。质量评测期间不能修改整理状态；另开功能测试后恢复并重新建立实验冻结点。

在部署机采集 `nvidia-smi` 的显存曲线、峰值/OOM/模型装载时间，保存在实验目录；客户端耗时已自动记录，但不能代表 GPU 峰值。记录已加载模型的首次推理与后续推理，模型装载时间单独报告。

异常测试在独立测试库运行：断网/超时、部分索引恢复、向量空间不匹配、非法上传、坏视频、空查询、父级隐藏与版本回退。生产库不通过破坏文件或篡改向量模拟故障。工程自动化检查用 `uv run pytest -q`；合成视频和模拟服务仅验证工具链，不算真实检索质量证据。

真实模型结果、人工标注、浏览器证据和 GPU 数据缺一时，保留待验收状态，不生成“全流程已通过”的结论。

参考：[llama.cpp server](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)、[WeMM 模板与实现](https://huggingface.co/tencent/WeMM-Embedding-9B/tree/main)、[Qwen3-ASR/ForcedAligner](https://github.com/QwenLM/Qwen3-ASR)。
