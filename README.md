<div align="center">

![Mindex：管理海量视频素材，连接每一次创作](docs/assets/hero.svg)

<h1>Mindex</h1>

**海量素材，有序管理。创作灵感，即刻成片。**

集视频资产管理、多模态检索与 AI 视频创作于一体的工作台。<br>
从整片、镜头、片段到人物与画面，从素材入库到配音、剪辑与成片，让视频资产持续产生价值。

![Python 3.11–3.13](https://img.shields.io/badge/Python-3.11%E2%80%933.13-365c49?style=flat-square)
![FastAPI](https://img.shields.io/badge/API-FastAPI-365c49?style=flat-square)
![SQLite + Chroma](https://img.shields.io/badge/Storage-SQLite%20%2B%20Chroma-365c49?style=flat-square)

[功能概览](#功能概览) · [快速开始](#快速开始) · [功能实景](#功能实景) · [工作原理](#工作原理) · [文档](#文档)

</div>

---

Mindex 将海量视频的管理、理解、检索与再创作连接在同一个工作流中。批量导入素材，建立从原片到镜头、片段与帧的内容组织；通过画面、台词、人物与语义组合定位素材；再把文案转成分镜，完成配音、选片、音画对齐和视频合成。

无论是持续积累的视频资产库、影视与长视频整理，还是解说、主题混剪和内容再创作，都可以从同一套素材与人物资料出发。

素材、元数据、索引和创作产物保存在本地；模型通过独立 HTTP 服务接入，支持自建部署与已适配的线上服务。使用线上服务时，对应的文本、图像或音频会发送给所配置的服务。

## 功能概览

| | 能做什么 |
| :--- | :--- |
| **🎞️ 视频资产管理** | 批量导入与预处理，按原片、镜头、片段和帧浏览素材；搜索、备注、收藏、隐藏，按需播放与下载。 |
| **🔎 多模态与复合检索** | 文字、图片、视频查询，结合人物、台词、场景、情绪、景别与时长筛选，在素材库中定位可用画面。 |
| **👥 人物与角色管理** | 导入演员表，管理演员、角色、作品与参考图；处理人物识别，审核出镜证据并发布、回溯。 |
| **✍️ AI 视频创作** | 从文案自动生成分镜，逐镜召回素材与配音，选择候选、调整音画对齐，渲染带字幕的成片。 |
| **🧩 构建与产物管理** | 多方案创作、逐节点重做、历史版本复用、完整依赖树，以及分镜、音频、字幕、成片的统一留档。 |
| **⚙️ 模型与声音配置** | 六类模型角色独立配置，连接自建或线上服务；调整检索权重、管理音色与声音克隆。 |

## 快速开始

### 1. 启动本地工作台

需要 **Python 3.11–3.13、uv、FFmpeg 与 ffprobe**。本地应用负责媒体处理、数据库、API 和页面；模型服务另行配置。

```bash
git clone https://github.com/xfork24/mindex.git
cd mindex

uv sync                      # 开发或运行测试时改用 uv sync --extra dev
cp config.example.yaml config.local.yaml
uv run mindex serve
```

打开 **<http://127.0.0.1:8000>**。

| 页面 | 地址 | 用途 |
| :--- | :--- | :--- |
| 视频检索 | `/` | 文字、图片、视频查询 |
| 素材管理 | `/admin` | 上传、处理、整理素材 |
| 人物库 | `/persons` | 身份、参考图、出镜审核与发布 |
| 创作 | `/studio` | 文案、分镜、配音与成片 |
| 设置 | `/settings` | 模型档案、音色、检索参数 |
| API 文档 | `/docs` | FastAPI 交互式接口文档 |

> **安全提示：** Web 与 API 没有登录认证，设置页还可以修改模型服务地址和密钥。`serve` 默认只监听 `127.0.0.1`；使用 `--host 0.0.0.0` 会向整个网络开放全部读写功能，只应在可信网络中使用，或放在带认证的反向代理之后。
>
> `config.example.yaml` 中的局域网地址仅是部署示例。首次使用请修改 `config.local.yaml`，或在设置页配置并激活自己的模型服务。没有素材、索引或可用模型时，页面会显示真实的未就绪状态。

### 2. 导入素材，先跑通画面检索

最短路径只需要**嵌入服务**，无需先运行 ASR、LLM 分析或 TTS。默认主模型配置为 **WeMM-Embedding-9B / 4096 维**；部署方式见[模型部署指南](docs/DEPLOYMENT.md)。

```bash
# 本地 CPU：导入、检测镜头、生成片段。支持 FFmpeg 可解码的 MP4、MKV 等。
uv run mindex prepare '/绝对路径/素材.mp4'

# 记下上一条命令输出的 SOURCE_ID；确认嵌入服务就绪后执行。
uv run mindex index --mode visual --source SOURCE_ID --kind moment --kind shot --activate
uv run mindex check

# 用一句描述找片段。
uv run mindex search '一个人在雨夜街道上奔跑'
```

也可以全程使用素材管理页：**添加新素材 → 分镜处理 → 更新嵌入 / 检索 → 视频检索**。示例显式包含 `moment` 与 `shot`，方便同时用于精细片段检索和 Studio 默认的镜头召回。

默认分镜使用本地 AdaptiveDetector，也可配置 TransNetV2；两者均需抽检。默认保留不超过 6 秒的短镜头，长镜头按 4 秒窗口、3 秒步长生成精细片段。详细步骤见[电影处理指南](docs/FILM_WORKFLOW.md)。

### 3. 按需补全对白、分析和人物

画面、对白与分析是独立渠道，可以先用、再补全；补充对白不会自动触发 LLM 分析。

```bash
# 优先提取可用文本字幕，否则使用已配置的 ASR；此步暂不发布索引。
uv run mindex enrich-dialogue SOURCE_ID --no-publish

# 可选：配置并启动视觉理解服务后，生成动作、场景、情绪等分析。
uv run mindex analyze --source SOURCE_ID

# 嵌入服务就绪后，发布已完成的渠道，复用兼容的已有向量。
uv run mindex index --mode auto --source SOURCE_ID --activate
uv run mindex check
```

素材管理页还支持指定对白处理方式、选择字幕轨道和上传外部字幕。人物功能需要 `uv sync --extra person --extra dev`、独立人物服务和参考图；按**登记 / 导入 → 处理 → 审核 → 发布**的顺序操作，见[人物库指南](docs/persons.md)。

### 4. 用素材库创作新视频

在 `/studio` 输入文案并限定可用素材，创建任务后拆分镜，逐镜召回画面与生成旁白，再选片、预览并构建成片。可导出 **MP4、SRT、WebVTT、时间轴 JSON**，也可打包下载任务产物。

| 环节 | 所需能力 |
| :--- | :--- |
| 拆分镜 | 文本 LLM |
| 画面语义召回 | 活动内容索引 + 匹配的嵌入服务 |
| 配音 | TTS + 可用音色 |
| 选片、渲染、合成 | 本地 FFmpeg；烧录字幕另需 libass 与可用字体 |

召回与配音可以分阶段执行，适合单 GPU 轮换模型服务。Studio 从已有素材中选片与合成，选片的叙事效果仍需人工审阅。完整操作见[Studio 指南](docs/studio.md)。

### 常见问题

| 现象 | 原因与处理 |
| :--- | :--- |
| 页面或接口显示"未就绪" | 还没有素材、活动索引或可用的模型服务。按上文第 2 步导入并发布索引，再运行 `uv run mindex status --health` 查看缺少什么。 |
| 检索返回 503 | 当前嵌入配置与活动索引的模型空间不一致，系统拒绝跨空间比较向量。恢复原模型服务和配置，或新建索引并激活，见[索引版本](docs/index-versions.md)。 |
| 同时运行多个任务被拒绝 | 处理任务按素材与类型互斥，GPU 模型按阶段轮换。等待当前任务结束，或在设置页关闭 GPU 互斥后再并行。 |
| 烧录字幕失败 | FFmpeg 缺少 libass 或可用字体。安装后重试，或改用软字幕轨道。 |

## 功能实景

### 01 · 管理视频资产，从整片深入到每一个镜头

把不断增长的素材集中入库，按显示名称或原文件名查找，结合收藏与可见性筛选。原片、镜头、精细片段和帧各有对应的浏览与处理入口；可从镜头进入子片段，也可以回到原片位置，播放、下载和整理所需内容。

![视频资产管理：素材列表、镜头预览、片段入口与整理操作](docs/assets/media-library.png)

**处理按需推进。** 分镜、对白、视觉分析与人物识别分别执行，独立查看进度与结果；完成分镜即可管理镜头，再按需要更新检索。任务支持检查点复用与失败重试，处理完成和已进入检索的覆盖分别显示。

<details>
<summary>查看素材预处理与镜头详情</summary>

![素材预处理：分镜、对白、分析与人物处理状态](docs/assets/media-processing.png)

镜头详情汇集已有的对白、字幕、视觉分析与人物处理信息，查看内容不触发新一轮模型推理。

![镜头详情：对白、字幕、分析与人物信息](docs/assets/shot-details.png)

</details>

### 02 · 组合画面、人物与台词，精准定位素材

用描述寻找动作、场景、情绪和镜头，也可以上传参考图片或短视频查询相似画面。选择演员与角色后，还能区分**人物实际出镜**和**对白中被提及**，按多人同时出现或任一人物筛选，并限定素材范围、时长与检索粒度。

<table>
<tr><th>人物 + 台词复合检索</th><th>直接输入台词检索</th></tr>
<tr>
<td width="50%"><a href="docs/assets/person-dialogue-search.png"><img src="docs/assets/person-dialogue-search.png" alt="选择人物并结合台词召回片段"></a></td>
<td width="50%"><a href="docs/assets/dialogue-search.png"><img src="docs/assets/dialogue-search.png" alt="输入台词并浏览命中的视频片段"></a></td>
</tr>
</table>

结果保留原片位置、命中说明和人物证据，可直接播放或进入素材管理继续整理。详见[检索机制](docs/search.md)。

### 03 · 建立人物资产，让演员、角色与作品相互关联

演员与角色分别管理，维护作品关系、别名和多张参考图。可以从演员表图片批量导入，人工调整识别框与配对关系，也可以直接从视频原片截取并框选人物参考图。人物资料可复用于检索与 Studio 逐镜选片。

![人物资产管理：演员、角色、参考图与作品关系](docs/assets/people-library.png)

<details>
<summary>查看演员表导入、原片取参考图与出镜审核</summary>

**演员表导入。** 从图片拆出演员与角色条目，核对姓名、参考框和已有身份后确认入库。

![演员表导入与条目核对](docs/assets/cast-import.png)

**从原片补充参考。** 定位视频帧，框选人物，为角色追加参考图。

![从原片框选人物并添加角色参考图](docs/assets/reference-capture.png)

**出镜审核。** 按分镜浏览识别证据，对候选进行确认或纠错；已发布证据用于人物检索。

![逐镜审核人物识别与出镜证据](docs/assets/appearance-review.png)

**发布与回溯。** 人物证据与内容索引组成可恢复的发布组合，人物模型离线时仍能查询已发布证据。

![人物证据发布与历史版本回溯](docs/assets/person-releases.png)

</details>

### 04 · 文案驱动创作，把素材组织成新故事

输入文案并选定可用素材，AI 自动拆成分镜。每镜分别保存旁白、画面查询和预计时长；召回、配音、选片与渲染在同一张工作台中推进，支持人工修改、插入、合并和调整分镜顺序。

![AI 根据文案生成分镜，并在工作台中逐镜执行后续阶段](docs/assets/storyboard.png)

**逐镜寻找合适的画面。** 每个分镜可选择文字与人物、图片或视频召回，限定素材范围。浏览候选及其人物证据，预览画面并指定参考锚点，或交由自动选片综合匹配。

![逐镜浏览召回的视频候选与人物证据](docs/assets/shot-candidates.png)

**让画面与旁白对齐。** 以实际配音时长为依据，在原片上下文中拖动定长选片窗口；时间轴展示缩略帧、镜头边界与参考锚点，支持逐帧微调和音画试听。

![在视频时间轴拖动选片窗口，使画面与配音对齐](docs/assets/timeline-alignment.png)

**从分镜到完整作品。** 分镜独立渲染视频、配音和字幕轨道，再合成为成片。支持原声混音、字幕配置，以及 MP4、SRT、WebVTT 和时间轴 JSON 导出。

![最终视频预览与视频、字幕、时间轴下载](docs/assets/final-video.png)

<details>
<summary>查看逐镜查询设置、配音试听与分镜渲染预览</summary>

![分镜的旁白、画面查询、人物与素材范围设置](docs/assets/shot-query.png)

![生成的分镜配音与音频试听](docs/assets/narration.png)

![分镜渲染产物的视频与音轨预览](docs/assets/shot-preview.png)

</details>

### 05 · 保留每次创作的版本，改一镜也能继续构建

一个任务可以新建、复制和切换多个方案。召回、配音、选片、渲染分别记录版本，既能选用历史产物，也能只重做某个节点。构建前检查依赖与兼容性，复用可用结果；新构建失败时保留已有成片。

<table>
<tr><th>成片的完整依赖树</th><th>历史构建与产物</th></tr>
<tr>
<td width="50%"><a href="docs/assets/build-dependencies.png"><img src="docs/assets/build-dependencies.png" alt="成片依赖树，追溯各分镜及输入版本"></a></td>
<td width="50%"><a href="docs/assets/build-history.png"><img src="docs/assets/build-history.png" alt="浏览全部历史构建与对应产物"></a></td>
</tr>
</table>

<details>
<summary>查看当前构建的产物清单</summary>

统一浏览候选、音频、分镜视频、字幕、时间轴与诊断文件，也可导出任务 ZIP。

![当前构建的全部产物与调试记录](docs/assets/build-artifacts.png)

</details>

### 06 · 自由组合模型，配置检索与创作能力

多模态理解、文本拆分镜、嵌入、语音识别、语音合成和人物识别分别配置模型档案，可连接已适配的线上服务与自建服务。检索权重、默认音色等设置对后续请求或任务生效，支持音色管理与声音克隆。

<details>
<summary>查看模型服务、召回权重与音色克隆配置</summary>

![模型服务与处理、检索、语音合成设置](docs/assets/model-settings.png)

![调整各渠道的检索融合权重](docs/assets/retrieval-weights.png)

![添加克隆音色并上传参考样本](docs/assets/voice-cloning.png)

</details>

完整用法见[素材管理](docs/admin.md)、[人物库](docs/persons.md)、[Studio](docs/studio.md)和[设置](docs/settings.md)。

## 工作原理

```mermaid
flowchart LR
    A[视频素材] --> B[本地分镜与切片]
    B --> C[画面嵌入]
    B --> D[字幕 / ASR 对白]
    B --> E[LLM 视觉分析]
    C --> F[(版本化内容索引)]
    D --> F
    E --> F
    B --> P[人物处理与人工审核]
    P --> Q[发布人物证据]
    F --> G[文字 / 图片 / 视频检索]
    Q --> G
    G --> H[Studio 逐镜选片]
    I[创作文案] --> J[拆分镜]
    J --> G
    J --> K[TTS 配音]
    K --> H
    H --> L[FFmpeg 渲染与字幕合成]
    L --> M[成片与可追溯产物]
```

**三渠道召回。** 新版文字查询分别检索画面、对白和分析。对白与分析内部结合语义向量和 BM25，再按默认 1:1:1 的渠道权重做 RRF 融合，并去除同源重叠片段。图片与视频查询只走画面渠道；在线查询无需实时 LLM 描述或重排。嵌入服务不可用且有文本渠道时，可明确降级为 BM25。

**本地数据与独立模型。** FastAPI 提供 API，原生 HTML / CSS / JavaScript 提供界面；SQLite 保存业务数据，Chroma 保存向量，FFmpeg 处理媒体。自建模型可在本机或另一台机器运行，按阶段安排资源，不要求全部模型同时常驻。

**版本与证据可追溯。** 内容索引、人物发布和 Studio 执行产物保留版本。更新分析不会改写旧索引快照；构建新方案可复用输入与文件校验均匹配的成功产物。查询会检查嵌入空间身份，禁止跨模型空间比较向量。

<details>
<summary><strong>模型切换、配置与数据存储</strong></summary>

配置从 `config.local.yaml` 读取；可用 `MINDEX_CONFIG`，或在子命令前指定 `--config`。设置页激活的模型档案与业务参数会覆盖对应配置，Web 与普通 CLI 共用覆盖层。

```bash
uv run mindex --config profiles/custom.yaml status --health

# 更换嵌入模型或预处理后，新建索引；先检查，再激活。
uv run mindex index
uv run mindex indexes list
uv run mindex check --index-id NEW_INDEX_ID
uv run mindex indexes activate NEW_INDEX_ID

# 回退前先恢复旧模型服务 / 配置。
uv run mindex indexes activate OLD_INDEX_ID
```

切换嵌入档案不会自动转换旧向量；空间不匹配时，向量查询拒绝比较并提示修复。切分布局参数变更应使用新的 `data_dir`。详见[索引版本](docs/index-versions.md)和[配置参考](docs/config-reference.md)。

```text
data/
├── sources/              原素材
├── plans/                分镜计划
├── clips/                片段 / 媒体归档
├── thumbnails/           缩略图
├── transcripts/          转写与检查点
├── persons/              人物参考图、证据帧与任务记录
├── studio/               创作任务、方案、版本与成片
├── voices/               克隆音色样本
├── chroma/               向量数据
└── metadata.sqlite3      元数据与运行设置
```

隐藏在召回前过滤并向下继承，收藏不改变相关性排名。需要回收磁盘时先运行 `uv run mindex clean` 预览，确认后再加 `--apply`，见[清理指南](docs/cleanup.md)。

</details>

## API 与开发

除了网页，Mindex 也提供 CLI 与 HTTP API。服务启动后，可在 <http://127.0.0.1:8000/docs> 查看完整接口。

```bash
curl -X POST http://127.0.0.1:8000/api/search/text \
  -H 'Content-Type: application/json' \
  -d '{"text":"雨夜，一个人强忍眼泪","granularity":"moment","limit":10}'
```

| 接口 | 用途 |
| :--- | :--- |
| `POST /api/search/text` | 文字查询、渠道选择、人物与素材筛选 |
| `POST /api/search/image` | 图片找视频；默认最大 20MB，支持静态 JPEG / PNG / WebP |
| `POST /api/search/video` | 视频找相似；默认最长 30 秒、最大 100MB |
| `GET /api/admin/sources` | 浏览素材；不依赖活动索引 |
| `GET /api/persons` | 人物、参考图、覆盖与发布记录 |
| `GET /api/studio/jobs` | 创作任务；方案、构建与版本接口见 Studio 文档 |
| `GET /api/status?health=true` | 索引与模型健康状态 |

`granularity` 支持 `moment`（精细片段）、`shot`（镜头）、`scene`（已分析的连续场景）；所选粒度需要已建库。返回数量为 1–50。详细筛选与排名语义见[检索指南](docs/search.md)。

### 源码与协作

```text
backend/src/      Python 后端；安装后的导入命名空间为 mindex
frontend/src/     原生 HTML / CSS / JavaScript；shared/ 为公共资源
scripts/          模型服务安装、启动及维护脚本
deploy/           人物与本地分镜检测服务的独立环境依赖
tests/            Python 测试、Node 断言与浏览器回归
docs/             使用指南、架构与验证记录
```

前后端均按 `search`、`admin`、`persons`、`studio`、`settings` 功能组织。

- `uv run mindex serve` 同时提供页面和 API，无需构建前端。静态资源经 `/assets/<功能模块>/<文件名>` 提供，打包后随 `mindex.web` 安装。
- 从旧目录结构的检出更新后，执行 `uv sync --extra dev` 刷新可编辑安装。
- 完整目录、模块职责与打包映射见[架构文档](docs/architecture.md#模块职责)。

代理协作约定见 [AGENT.md](AGENT.md)，Claude Code 技术导航见 [CLAUDE.md](CLAUDE.md)。

<details>
<summary><strong>开发检查与真实模型评测</strong></summary>

```bash
uv sync --extra person --extra dev
uv run pytest -q
uv run ruff check backend/src tests
node --check frontend/src/search/app.js
```

UI 回归另需 Node.js、Playwright 及其浏览器；可用 `MINDEX_PLAYWRIGHT_MODULE` 指定模块路径。完整 `pytest` 目前并非全绿：其中 7 项既有失败记录在[目录重组验证记录](docs/history/source-layout-verification.md)，另有 3 项人物相关失败见[项目审查](docs/project-review-2026-10-22.md)；其他历史记录见[验证记录](docs/VERIFICATION.md)。自动化测试使用真实媒体处理、数据库与 HTTP 链路，模型部分使用明确标记的确定性测试适配器；它们验证工程行为，不证明真实模型的检索或人物识别准确率。

真实质量评测需要人工标注查询并固定模型、预处理与索引版本。流程见[端到端操作手册](docs/E2E_RUNBOOK.md)、[e2e 框架](docs/e2e-framework.md)及[评测标注说明](eval/README.md)。检索分数是排序依据，不是匹配概率。

</details>

## 文档

| 我想…… | 从这里开始 |
| :--- | :--- |
| 跑通一部电影的导入与画面索引 | [电影处理指南](docs/FILM_WORKFLOW.md) |
| 部署模型、接入自建服务 | [部署与服务协议](docs/DEPLOYMENT.md) |
| 了解处理阶段、恢复与增量建库 | [离线管线](docs/pipeline.md) · [三渠道独立处理](docs/recall-enhancement.md) |
| 管理素材、字幕与处理任务 | [素材后台](docs/admin.md) |
| 管理人物并按出镜查找片段 | [人物库与证据审核](docs/persons.md) |
| 从文案制作视频、调整方案 | [Studio 工作台](docs/studio.md) |
| 配置服务、音色和检索参数 | [设置页](docs/settings.md) · [配置参考](docs/config-reference.md) |
| 备份素材、迁移合并或在新机器恢复 | [数据备份与迁移](docs/backup.md) |
| 查看命令、模块和数据流 | [CLI 参考](docs/cli-reference.md) · [架构](docs/architecture.md) |
| 评测模型或切换索引版本 | [端到端验证](docs/E2E_RUNBOOK.md) · [索引版本与回退](docs/index-versions.md) |

更多机制说明、操作手册与历史验收记录见 **[文档中心](docs/README.md)**。

---

<div align="center">

**从海量素材，到每一次创作。**

[开始使用](#快速开始) · [阅读文档](docs/README.md) · [反馈问题](https://github.com/xfork24/mindex/issues)

</div>
