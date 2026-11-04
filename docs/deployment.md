# GPU 模型部署与联调

应用服务的 PG/S3/Chroma/worker 部署及当前限制见 [水平扩展部署指南](scaling.md)。
集群 API 不自动管理本机模型进程；节点数据目录是缓存和临时工作区。
本页说明独立模型服务，其真实并发容量与 GPU 资源组仍需单独实测。

应用本机不加载大模型。GPU 机器建议 Linux/WSL2、RTX 5060 Ti 16GB、32GB 内存，并有足够的模型缓存和素材临时空间。三个角色使用独立 Python 环境，避免 ASR、TensorFlow 和 WeMM 的依赖冲突。

示例地址（按需替换为实际部署）：WeMM `:4010`，ASR `:4020`，TTS `:4030`，Qwen `:4040/v1`，TransNetV2 可选服务 `:18203`。保存地址不会触发启动或模型调用；单 GPU 分阶段轮换时按需单独启动。TTS 地址可作为预留配置保存，不参与视频切片或检索。实际接口仍须符合下文协议。

## 启动脚本的后端选项

全部 `scripts/run-*.sh` 接受 `--backend metal|cuda|cpu`，也支持 `--backend=cuda` 和 `--help`。命令行优先于对应环境变量；不指定选项或新环境变量时，保持原默认设备选择。

```bash
bash scripts/run-wemm.sh --backend metal
bash scripts/run-qwen3-asr.sh --backend cuda
bash scripts/run-tts-voiceclone.sh --backend cpu
bash scripts/run-tts-voicedesign.sh --backend metal
bash scripts/run-music-gen.sh --backend cuda
bash scripts/run-transnet.sh --backend cpu
bash scripts/run-person-service.sh --backend cpu
```

| 脚本 | 后端环境变量 | 原默认行为 | 映射 |
| --- | --- | --- | --- |
| `run-wemm.sh` | `MINDEX_WEMM_BACKEND` | GPU 层数 99，服务自动选设备 | Metal → `MTL0`，CUDA → `CUDA0`；CPU 关闭模型层、投影、KV 和运算卸载 |
| `run-qwen3-asr.sh` | `MINDEX_ASR_BACKEND` | GPU 层数 99，服务自动选设备 | 同 WeMM |
| `run-tts-voiceclone.sh` | `MINDEX_TTS_BACKEND` | 服务自动选设备 | `MTL0` / `CUDA0` / `CPU` |
| `run-tts-voicedesign.sh` | `MINDEX_TTS_BACKEND` | 服务自动选设备 | 同 voiceclone |
| `run-music-gen.sh` | `MINDEX_MUSIC3_BACKEND` | Metal | audio.cpp 原生 `--backend` |
| `run-transnet.sh` | `MINDEX_TRANSNET_BACKEND` | `MINDEX_TRANSNET_DEVICE`，未设时 CPU | TensorFlow Metal 插件 / CUDA / 隐藏 GPU |
| `run-person-service.sh` | `MINDEX_PERSON_BACKEND` | `MINDEX_PERSON_PROVIDERS`，未设时 CPU | ONNX Runtime CUDA / CPU Provider；当前 SDK 不支持 Metal，选择时明确报错 |

所选后端需要匹配的硬件、二进制构建及独立 Python 环境。TransNet 的 Metal 需要与 TensorFlow 版本兼容的 [`tensorflow-metal`](https://developer.apple.com/metal/tensorflow-plugin/)，CUDA 需要支持 CUDA 的 TensorFlow 和可见 NVIDIA GPU。人物服务使用的固定版 InsightFace `PersonAnalysis` 仅允许 CPU/CUDA，`--backend metal` 会明确报错，即使 ONNX Runtime 本身提供 CoreML 也不会冒充支持；扩展它需要另行适配第三方 SDK。人物服务的 CUDA 需要提供 `CUDAExecutionProvider` 的 ONNX Runtime。显式选择 GPU 后端但依赖或设备不可用时启动报错；人物模型中 CUDA Provider 不支持的算子仍可由 CPU Provider 执行。

这些选项只选择推理设备，不安装依赖，也不改写模型清单。切换 WeMM/ASR 的实际推理后端或参数仍需按原流程重新 pin/验证；仅模型路径迁移的兼容规则不受影响。设置页管理服务时可在 YAML 的 `local_services.services[].argv` 中加入 `--backend` 及其值。

## 当前部署路径：已有 llama.cpp / GGUF 服务

用户的四个服务地址保持不变，当前 `config.local.yaml` 的嵌入、ASR 与分析（Qwen）三个后端均选择 `llama_cpp`，各自需要 `e2e probe` + `e2e pin` 生成的部署清单（`data/deployments/wemm.json`、`asr.json`、`analyzer.json`；清单缺失或哈希不符时对应阶段拒绝执行）。WeMM 已确认使用 Q8_0 GGUF、BF16 mmproj，需 `--embeddings --pooling last --video-fps 4`。详细启动、哈希固定、原生视频接口验证和单 GPU 切换流程见 [e2e-runbook.md](e2e-runbook.md)。下文的自定义 `mindex` 模型服务仍作为可切换的替代后端，按本文档各节部署。

## WeMM：文字与视频嵌入

把本项目复制到 GPU 机器。在项目目录中执行：

```bash
uv venv --python 3.11 .venv-embedding
uv pip install --python .venv-embedding/bin/python -e .
```

按 [PyTorch 官方安装选择器](https://pytorch.org/get-started/locally/) 安装支持当前 GPU/驱动的 CUDA 版 `torch` 和 `torchvision`，目标解释器为 `.venv-embedding/bin/python`。不要使用 CPU wheel。安装后验证：

```bash
.venv-embedding/bin/python -c 'import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available()); print(torch.cuda.get_device_name(0), torch.cuda.get_device_capability(0)); print(torch.ones(2, device="cuda") + 1)'
uv pip install --python .venv-embedding/bin/python 'transformers==5.2.0' 'accelerate>=1.1.0' 'bitsandbytes>=0.45' 'qwen-vl-utils[decord]==0.0.14' 'Pillow>=11'
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
uv pip install --python .venv-asr/bin/python qwen-asr   # qwen-asr 自行约束 Transformers 版本
.venv-asr/bin/mindex-model-server --role asr --config config.local.yaml --host 0.0.0.0
```

使用[官方 Qwen3-ASR Python 包](https://github.com/QwenLM/Qwen3-ASR)，加载 `Qwen/Qwen3-ASR-1.7B` 及专用 `Qwen/Qwen3-ForcedAligner-0.6B`，对齐输出单词/字符级时间戳。二者作为一个 ASR 阶段执行，不与主要分析或嵌入模型并驻。客户端将音频拆成 30 秒块后上传，每块断点保存。

客户端执行 `uv run mindex transcribe`；结束后停止 ASR，启动已有 Qwen，再执行 `uv run mindex analyze`。ASR 是离线索引用途，网页不提供音频入口。

ASR 验收后应固定 `asr.revision` 为实际权重 commit。现有 OpenAI 兼容 Qwen 服务无法保证暴露权重 revision，因此 `analyzer.revision` 由部署者维护；更换其权重或量化时必须更新该值，再运行分析和建索引，避免复用旧分析检查点。

## TransNetV2：可选的神经网络分镜服务

### 本地 macOS / Apple Silicon

已有脚本式服务使用相同启动方式。独立 `.venv-detector` 环境安装 TensorFlow 2.18.1，默认 CPU 模式，不持有 GPU 角色锁，可与现有 llama.cpp 服务共存。无需安装 CUDA 或 tensorflow-metal。

```bash
./scripts/setup-transnet-service.sh  # 首次安装，下载/校验上游代码及完整 SavedModel，并验证推理
./scripts/run-transnet.sh            # 前台运行，Ctrl+C 停止
curl http://127.0.0.1:18203/health
```

模型保存到 `models/TransNetV2/inference`，上游固定版本 `85cef72af9a916bdfd7cc94a670c9cdfbf12d1ed`；安装脚本从 Git LFS 媒体端下载真实权重，校验 SHA256 与大小，重复执行复用已校验权重。安装清单为 `models/TransNetV2/mindex-install.json`。依赖见 `deploy/detector-local.txt`，不会把 TensorFlow 装入应用、人物或 ASR 环境。

客户端 `config.local.yaml` 使用：

```yaml
detector:
  backend: transnet
  base_url: http://127.0.0.1:18203
```

Web 已运行时需在当前任务结束后重启，以加载 YAML。新素材登记时冻结分镜配置，已有素材及任务继续保留原切分方式；重试旧任务不会切换检测器。需要用 TransNetV2 重切已登记的 Adaptive 素材时，应在新的 `data_dir` 导入，以保留原布局和索引。

启动参数通过环境变量调整：`MINDEX_CONFIG`、`MINDEX_TRANSNET_HOST`（默认 127.0.0.1）、`MINDEX_TRANSNET_PORT`（默认 18203）、`MINDEX_TRANSNET_DIR`、`MINDEX_TRANSNET_DEVICE`（默认 cpu，可设 metal/cuda，auto 可使用环境中可见的 GPU）、`TF_NUM_INTRAOP_THREADS`（默认 4）、`TF_NUM_INTEROP_THREADS`（默认 2）。`--backend` 或 `MINDEX_TRANSNET_BACKEND` 优先于旧 `MINDEX_TRANSNET_DEVICE`。GPU/auto 模式仍遵守 GPU 角色锁。HTTP 认证沿用 `MINDEX_MODEL_API_KEY`。

本机冒烟（2026-10-18，Apple Silicon / TensorFlow 2.18.1 / FFmpeg 9.0.2）通过：真实上游权重对 6 秒三色测试视频返回两个转场，隔离 `prepare` 生成三个镜头与精细片段，接口与帧数边界测试 15 项通过。FFmpeg 9 使用 `-fps_mode passthrough` 保留每帧，不使用已删除的 `-vsync`。正式素材仍需抽检。

### Linux / CUDA

需要 GPU 部署时使用原有独立环境：

```bash
uv venv --python 3.11 .venv-detector
uv pip install --python .venv-detector/bin/python -e .
uv pip install --python .venv-detector/bin/python 'tensorflow[and-cuda]>=2.16'
git clone https://github.com/soCzech/TransNetV2.git models/TransNetV2
```

按[上游说明](https://github.com/soCzech/TransNetV2) 下载完整 `inference/transnetv2-weights` SavedModel 文件；Git LFS pointer 不能作为权重。设置：

```bash
export MINDEX_TRANSNET_DIR="$PWD/models/TransNetV2/inference"
.venv-detector/bin/mindex-model-server --role detector --config config.local.yaml --host 0.0.0.0
```

客户端改 `detector.backend: transnet`，再执行 `prepare`。服务逐帧低分辨率解码，以 100 帧上下文窗口、50 帧输出步长调用上游 `predict_raw`，避免一次加载整部视频。连续高分转场取中点，客户端合并相距不超过 2 帧的重复边界，并映射到原视频帧时间戳。默认阈值 0.5，需用真实素材抽检。

## ComfyUI：可选的 AIGC 视频生成服务

`aigc` 分镜（见 [AI 生成视频](studio.md#ai-生成视频)）可改用自建 ComfyUI 代替线上 MiniMax/通义万相；图片生成与音乐生成同样可用本地 ComfyUI 工作流（设置页已建好“本地 ComfyUI”卡，导入工作流与节点映射后激活）。ComfyUI 不随本项目安装，按其官方说明在 GPU 机器上单独部署并装好所需视频模型与自定义节点（例如 Wan、HunyuanVideo、LTX-Video 等工作流），启动时监听局域网地址：

```bash
python main.py --listen 0.0.0.0 --port 8188
```

Mindex 只使用 ComfyUI 内置的 HTTP 路由：`GET /system_stats`（设置页测试连接，只检查可达性与工作流，不提交任务）、`POST /upload/image`（首帧参考图）、`POST /prompt`（提交）、`GET /history/{prompt_id}` 与 `GET /queue`（轮询）、`GET /view`（下载输出）、`POST /queue` / `POST /interrupt`（取消）。

1. 在 ComfyUI 中调通一个输出视频的工作流（保存节点需输出 mp4/webm/gif/webp 等），用菜单「导出（API）」（Export (API)）保存 JSON；界面格式（含 `nodes`/`links`）会被拒绝。
2. 设置页“视频生成”角色新建档案，后端选 `comfyui`，base_url 填 `http://<GPU主机>:8188`，导入上一步的 JSON，并为请求字段填节点映射 `节点ID.inputs.输入名`：`prompt` 必填，`negative`、`seed`、`frames`（= 时长 × `fps`）、`width`、`height`、`image`（加载图片节点的 `image` 输入，有参考图的分镜必需）按工作流选填；未映射或未提供的字段保留工作流原值。保存时逐项校验节点与输入是否存在，错误为中文。
3. `durations` 填工作流实际能生成的时长（秒），`fps` 与工作流帧率一致；测试连接通过后激活档案。

ComfyUI 自身没有鉴权；如在前面加了反向代理鉴权，可用档案的 `api_key_env`（默认 `MINDEX_VIDEO_API_KEY`）以 `Authorization: Bearer` 头传递。ComfyUI 占用 GPU 时不受本项目 GPU 锁管理，与其他角色共用一张卡时需手动错开。

## AI 动画录制（可选）

AI 动画分镜（`animation` 来源）的渲染在本机（运行 `mindex serve` 的机器，CPU 即可）用无头 Chromium 逐帧录制动画源码，不需要 GPU 服务。录制依赖是可选 extra，不装时其余功能不受影响，只有 animation 分镜的渲染会失败并给出安装指引：

```bash
uv sync --extra dev --extra animation        # 安装固定版本的 playwright（Python）
uv run playwright install chromium           # 下载 Playwright 对应版本的 Chromium（约 150 MB，需联网）
# Linux 服务器缺系统库时：uv run playwright install --with-deps chromium（需要 root）
```

浏览器缓存在 `~/.cache/ms-playwright`（macOS 为 `~/Library/Caches/ms-playwright`），可用 `PLAYWRIGHT_BROWSERS_PATH` 改位置。录制时浏览器离线运行、拦截全部网络请求、使用录完即删的临时 profile；单个分镜的墙钟上限为 `studio.animator.max_capture_seconds`（默认 60 秒，画幅大或镜头长时调大）。动画源码本身由“动画生成”角色的 LLM（缺省跟随智能助手模型）编写，见 [设置页](settings.md)。

## 单 GPU 调度与访问配置

使用 GPU 的本项目模型服务共享进程间 GPU 文件锁，默认 `~/.cache/mindex/gpu.lock`，同时启动第二个角色会失败。CPU 模式 TransNetV2 不占用此锁。自定义 `MINDEX_GPU_LOCK` 时，各 GPU 角色必须使用同一路径；服务运行单 worker。

**现有 Qwen 服务不受该锁管理。** 请手动停止它并确认显存释放后，再运行 ASR、TransNetV2 或 WeMM；切回 Qwen 前先停止其他角色。项目不会擅自停止已有服务。

可设置 `MINDEX_MODEL_API_KEY` 保护三个服务，客户端通过配置的 `api_key_env` 从同名环境变量读取。Qwen 可单独使用 `MINDEX_QWEN_API_KEY`。局域网服务监听 `0.0.0.0`，本地网页默认仅监听 `127.0.0.1`；需要网络访问时用 `MINDEX_WEB_TOKEN` 启用单一访问令牌（未设置令牌时监听非回环地址会被 `serve` 拒绝，除非传 `--allow-insecure`），不提供多用户账户。

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

响应为 `{"vectors":[[...]],"space_id":"..."}`；`space_id` 必须等于 `mindex.shared.config.fingerprint(space)`。客户端校验数量、维度、有限值、非零范数以及服务身份，不接受仅返回任意同维向量的服务。同一个服务身份必须采用固定的模板与预处理。

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

