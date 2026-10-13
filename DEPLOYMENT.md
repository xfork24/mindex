# GPU 模型部署与联调

应用本机不加载大模型。GPU 机器建议 Linux/WSL2、RTX 5060 Ti 16GB、32GB 内存，并有足够的模型缓存和素材临时空间。三个角色使用独立 Python 环境，避免 ASR、TensorFlow 和 WeMM 的依赖冲突。

用户提供的地址：WeMM `192.168.1.86:4010`，ASR `:4020`，TTS `:4030`，Qwen `:4040/v1`。截至 2026-10-13，四个模型均已部署但未启动，按需单独启动；保存地址不会触发启动或模型调用。TransNetV2 可选服务使用 `:18203`。TTS 地址已作为预留配置保存，不参与视频切片或检索。实际接口仍须符合下文协议。

## 当前部署路径：已有 llama.cpp / GGUF 服务

用户的四个服务地址保持不变，当前 `config.local.yaml` 的嵌入、ASR 与分析（Qwen）三个后端均选择 `llama_cpp`，各自需要 `e2e probe` + `e2e pin` 生成的部署清单（`data/deployments/wemm.json`、`asr.json`、`analyzer.json`；清单缺失或哈希不符时对应阶段拒绝执行）。WeMM 已确认使用 Q8_0 GGUF、BF16 mmproj，需 `--embeddings --pooling last --video-fps 4`。详细启动、哈希固定、原生视频接口验证和单 GPU 切换流程见 [E2E_RUNBOOK.md](E2E_RUNBOOK.md)。下文的自定义 `mindex` 模型服务仍作为可切换的替代后端，按本文档各节部署。

## WeMM：文字与视频嵌入

把本项目复制到 GPU 机器。在项目目录中执行：

```bash
uv venv --python 3.11 .venv-embedding
uv pip install --python .venv-embedding/bin/python -e .
```

按 [PyTorch 官方安装选择器](https://pytorch.org/get-started/locally/) 安装支持当前 GPU/驱动的 CUDA 版 `torch` 和 `torchvision`，目标解释器为 `.venv-embedding/bin/python`。不要使用 CPU wheel。安装后验证：

```bash
.venv-embedding/bin/python -c 'import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available()); print(torch.cuda.get_device_name(0), torch.cuda.get_device_capability(0)); print(torch.ones(2, device="cuda") + 1)'
uv pip install --python .venv-embedding/bin/python -r deploy/embedding.txt
cp config.example.yaml config.local.yaml
.venv-embedding/bin/mindex-model-server --role embedding --config config.local.yaml --host 0.0.0.0
```

首次启动下载官方权重。默认 INT8、batch=1，无 CPU/disk offload；不能容纳时停止服务，将 `embedding.quantization` 在服务器与客户端都改为 `nf4` 再启动，并重新建索引。不能仅根据权重体积宣布 16GB 已满足，需要实测短视频输入的峰值。

采用[官方 Transformers 嵌入调用](https://huggingface.co/tencent/WeMM-Embedding-9B)，使用 `model.embedding`，不调用聊天模型生成文本来冒充 embedding。默认 4fps、最多 24 帧、单帧 `max_pixels=262144`；输出完整 4096 维归一化浮点向量。长视频由客户端拆成不超过 6 秒的窗口。`nf4` 是 BitsAndBytes 4bit 权重量化，向量本身仍是浮点。

`revision: main` 会在服务启动时解析为实际 Hugging Face snapshot commit，并返回身份指纹。正式验收后建议把服务配置锁定到该 commit。自建本地权重路径亦可作为 `embedding.model`，其权重及 JSON 配置内容会被哈希；服务端与客户端模型名须相同。

其他模型通过相同 HTTP 协议接入。当前内置 `WeMMEngine` 只保证遵循 WeMM 的文档调用形式，INT8/NF4 在目标 GPU 上的兼容性仍需联调确认。

## ASR：中文对白及时间对齐

停止 Qwen/WeMM。使用独立环境：

```bash
uv venv --python 3.11 .venv-asr
uv pip install --python .venv-asr/bin/python -e .
# 在该环境安装适配 GPU 的 CUDA 版 torch / torchvision。
uv pip install --python .venv-asr/bin/python -r deploy/asr.txt
.venv-asr/bin/mindex-model-server --role asr --config config.local.yaml --host 0.0.0.0
```

使用[官方 Qwen3-ASR Python 包](https://github.com/QwenLM/Qwen3-ASR)，加载 `Qwen/Qwen3-ASR-1.7B` 及专用 `Qwen/Qwen3-ForcedAligner-0.6B`，对齐输出单词/字符级时间戳。二者作为一个 ASR 阶段执行，不与主要分析或嵌入模型并驻。客户端将音频拆成 30 秒块后上传，每块断点保存。

客户端执行 `uv run mindex transcribe`；结束后停止 ASR，启动已有 Qwen，再执行 `uv run mindex analyze`。ASR 是离线索引用途，网页不提供音频入口。

ASR 验收后应固定 `asr.revision` 为实际权重 commit。现有 OpenAI 兼容 Qwen 服务无法保证暴露权重 revision，因此 `analyzer.revision` 由部署者维护；更换其权重或量化时必须更新该值，再运行分析和建索引，避免复用旧分析检查点。

## TransNetV2：可选的神经网络分镜服务

本地默认 AdaptiveDetector 已可使用；需要 TransNetV2 时：

```bash
uv venv --python 3.11 .venv-detector
uv pip install --python .venv-detector/bin/python -e .
uv pip install --python .venv-detector/bin/python -r deploy/detector.txt
git clone https://github.com/soCzech/TransNetV2.git models/TransNetV2
```

按[上游说明](https://github.com/soCzech/TransNetV2) 下载完整 `inference/transnetv2-weights` SavedModel 文件；Git LFS pointer 不能作为权重。设置：

```bash
export MINDEX_TRANSNET_DIR="$PWD/models/TransNetV2/inference"
.venv-detector/bin/mindex-model-server --role detector --config config.local.yaml --host 0.0.0.0
```

客户端改 `detector.backend: transnet`，再执行 `prepare`。服务逐帧低分辨率解码，以 100 帧上下文窗口、50 帧输出步长调用上游 `predict_raw`，避免一次加载整部视频。连续高分转场取中点，客户端合并相距不超过 2 帧的重复边界，并映射到原视频帧时间戳。默认阈值 0.5，需用真实素材抽检。

## 单 GPU 调度与访问配置

所有本项目模型服务使用同一个进程间 GPU 文件锁，默认 `~/.cache/mindex/gpu.lock`，同时启动第二个角色会失败。自定义 `MINDEX_GPU_LOCK` 时，各角色必须使用同一路径；服务运行单 worker。

**现有 Qwen 服务不受该锁管理。** 请手动停止它并确认显存释放后，再运行 ASR、TransNetV2 或 WeMM；切回 Qwen 前先停止其他角色。项目不会擅自停止已有服务。

可设置 `MINDEX_MODEL_API_KEY` 保护三个服务，客户端通过配置的 `api_key_env` 从同名环境变量读取。Qwen 可单独使用 `MINDEX_QWEN_API_KEY`。局域网服务监听 `0.0.0.0`，本地网页默认仅监听 `127.0.0.1`；该 Demo 不提供多用户认证。

## 服务协议

### 嵌入服务

`GET /health`：

```json
{
  "ready": true,
  "role": "embedding",
  "space": {
    "model": "tencent/WeMM-Embedding-9B",
    "revision": "实际权重commit",
    "quantization": "int8",
    "dimensions": 4096,
    "normalized": true,
    "fps": 4,
    "max_frames": 24,
    "max_pixels": 262144,
    "implementation": "稳定的实现和依赖指纹"
  }
}
```

`POST /embed/text` 接受 `{"texts":["查询文字"],"role":"query"}`；文档用 `role: document`。`POST /embed/video` 与 `POST /embed/image` 使用 multipart `file`，直接上传文件，不传本机绝对路径。图片使用 WeMM 原生图片输入，输出与文字/视频同一空间的向量。

响应为 `{"vectors":[[...]],"space_id":"..."}`；`space_id` 必须等于 `mindex.config.fingerprint(space)`。客户端校验数量、维度、有限值、非零范数以及服务身份，不接受仅返回任意同维向量的服务。同一个服务身份必须采用固定的模板与预处理。

### ASR 与分镜服务

- `POST /transcribe`：multipart `file`；响应 `model`、`revision`、`utterances: [{start,end,text}]`，时间相对于上传块，单位秒。
- `POST /detect/video`：multipart `file`、`threshold`；响应 `model: TransNetV2`、`frame_count`、`cut_frames`，帧号从 0 开始，切点是新镜头的开始位置。
- Qwen 使用已有 `/v1/chat/completions`，帧通过 `image_url` 数据 URL 上传；不假设服务支持原生视频容器或音频。

## 部署验收

1. 运行健康检查，确认返回的模型、量化、维度与预处理完全匹配客户端。
2. 分别请求中文文字和实际短视频嵌入，确认 4096 维、单位范数、相同 `space_id`。
   可直接运行 `uv run mindex model-check /路径/短片段.mp4 --text '画面描述' --output data/model-check.json`。内置服务的 `/metrics` 返回 PyTorch 分配器峰值；它不包含其他进程及驱动占用，仍需核对 `nvidia-smi`。
3. 用最长默认输入窗口测量 `nvidia-smi` 显存和服务延迟，记录模型加载时间与常驻推理时间。
4. 执行小批真实视频的完整离线流程，在页面搜索并播放命中片段。
5. 在同一固定测试集比较 INT8 与 NF4；量化切换必建新索引。没有真实结果前不宣布达到检索质量目标。

