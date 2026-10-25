# CLI 完全参考

安装后有三个入口：`mindex`（本机工作台 CLI）、`mindex-model-server`（嵌入/ASR/分镜检测服务）和 `mindex-person-server`（人物服务）。模型服务运行在独立环境中。

## 在终端查看完整帮助

`mindex` 的根命令、命令组及每个叶子命令均支持 `-h` 和 `--help`。帮助包含用途、前置条件、默认行为、参数、输出、注意事项和示例，不加载配置、不访问素材库，也不要求启动模型。即使显式指定的配置不存在或 YAML 损坏，仍可查看子命令帮助。帮助保留 Rich 彩色边框与表格式选单，长中文参数在表格内自动折行，完整展示说明。

```bash
uv run mindex -h
uv run mindex clean --help
uv run mindex indexes -h
uv run mindex persons process -h
uv run mindex clips migrate --help
uv run mindex backup import --help
uv run mindex e2e phase -h
```

命令组不带子命令会显示选单（退出码 2）；显式请求帮助退出码为 0。CLI 中实际注册的命令及其帮助是参数清单的依据，本文补充使用流程。

| 命令组 | 子命令 |
| --- | --- |
| `indexes` | `list`、`activate` |
| `persons` | `list`、`save`、`reference`、`process`、`status`、`publish`、`review`、`activate`、`migrate-frames` |
| `clips` | `migrate`、`materialize`、`verify-archives`、`repair-boundaries`、`prune` |
| `backup` | `list`、`inspect`、`export`、`import`、`recover` |
| `e2e` | `init`、`probe`、`pin`、`phase`、`freeze`、`review`、`status` |
| `agent` | `tools`（另有顶层命令 `mcp`，见[智能助手与 MCP](#智能助手与-mcp)） |

## 全局规则

- `--config` / `-c` **必须放在子命令之前**：`uv run mindex --config profiles/int8.yaml index`。也可设环境变量 `MINDEX_CONFIG`（默认读 `config.local.yaml`）。
- 全局 `--data-dir DIR` 显式指定数据目录，按当前工作目录解析；`backup import --data-dir DIR` 也支持在子命令后指定恢复位置。
- `--verbose` 启用 DEBUG 日志，默认 INFO；`--no-runtime-overrides` 禁止合并设置页覆盖层，固定使用 YAML。
- 配置模型 strict（`extra="forbid"`），未知键直接校验失败。
- `data_dir` 已有元数据库时，CLI 命令会合并设置页（`/settings`）保存的覆盖层——激活的模型档案与检索调参对 CLI 同样生效，与 Web 行为一致；`mindex e2e`、`mindex clips`、`mindex backup` 命令组例外，使用 YAML 原值。备份会分别携带基础 YAML 与数据库覆盖层。
- `data_dir` 相对路径按**配置文件所在目录**解析；`deployment_manifest` 同理。
- 离线任务使用管线与素材/工作类型锁；全库维护需要独占锁。同素材同类任务或独占维护冲突时会拒绝并行，不能假设所有任务都允许并发。

## 数据备份与迁移

操作前停止 Web 和全部后台写入任务。导入导出均不调用模型，不重新计算向量。详见 [备份与全新 clone 恢复流程](backup.md)。

```bash
uv run mindex backup list --json
uv run mindex backup export /backup/mindex --all
uv run mindex backup export /backup/selected --source ID1 --source ID2 --dry-run
uv run mindex backup export /backup/private --all --include-secrets
uv run mindex backup inspect /backup/mindex --json
uv run mindex backup import /backup/mindex --all --data-dir /new/data --dry-run
uv run mindex backup import /backup/mindex --all --data-dir /new/data
uv run mindex backup import /backup/selected --source ID1 --skip-existing
uv run mindex backup import /backup/mindex --all --restore-config --dry-run
uv run mindex backup recover --data-dir /new/data --json
```

省略素材选择时，交互终端提供搜索及多选；非交互环境必须指定 `--all` 或重复使用 `--source`。导出目录必须不存在。`--verification full|basic`（导出与导入通用）选择校验强度，默认 `full`，`basic` 缩短时间但无法发现大小未变的文件损坏，见 [备份文档](backup.md)。`backup export --source ID --allow-legacy-representations` 仅用于单个已确认的旧版 format=1 临时素材，不能用于全库或多素材导出。默认移除已存密钥；`--include-secrets` 显式包含设置页密钥，始终不读取环境变量里的密钥。`inspect` 核对版本、文件大小、校验和、素材、向量及配置依赖。

空库自动恢复配置与活动检索组合，允许目标配置文件尚不存在。已有库默认保留配置，仅 `--restore-config` 恢复备份配置并输出脱敏差异。重复素材默认报错，`--skip-existing` 保留目标及其全部衍生数据。活动索引兼容时使用已有向量创建联合索引，不兼容或共享对象冲突则整个导入失败。`--dry-run` 构建并校验临时结果，返回依赖、版本映射及配置处理结果，不切换目标数据或配置；`--json` 结果写入标准输出，进度写入标准错误。

## 离线处理

### `mindex prepare VIDEOS...`

导入原视频、检测镜头并实际切片（= `detect` + `split`；默认 adaptive 为本地 CPU，配置 transnet 时需要对应检测服务）。可一次传多个文件。每个素材返回内容 SHA256 作为 SOURCE_ID。

```bash
uv run mindex prepare '/路径/电影.mkv' /路径/素材2.mp4
```

### `mindex detect VIDEO`

只扫描并保存分镜计划到 `plans/SOURCE_ID.json`，不切片、不调用嵌入模型。默认 adaptive 在本地检测，transnet 需对应检测服务。最后一行输出 SOURCE_ID。计划原子写入，后续编码中断不需要重扫。

### `mindex split SOURCE_ID`

按已保存计划切片；重跑可恢复（已存在的媒体跳过），也会修复个别缺失的切片/缩略图。

### `mindex inspect SOURCE_ID`

输出 JSON：计划镜头数、预计精细片段数、镜头时长 min/median/p95/max、短镜头数量、已落盘数量、缺失文件、最近处理记录。

### `mindex export SOURCE_ID [--output DIR]`

导出 `manifest.json`、`shots.csv`、`segments.csv`、`review.html`（浏览器直接打开的分页检查页，48 单元/页）。默认 `data/reports/SOURCE_ID/`。CSV 路径相对 `data_dir`；review.html 使用相对媒体路径，移动报告需保留相对布局。

### `mindex enrich-dialogue [SOURCE] [--subtitle-stream INDEX] [--no-publish] [--workers 2]`

字幕优先补充对白；省略 SOURCE 则批量处理。默认断点续跑并自动发布；`--no-publish` 仅生成对白数据。MiniMax 保存服务词级时间戳，不调用本地 ForcedAligner。默认不触发 LLM 分析。Web 与 CLI 共用生效配置和管线锁。详见 [三渠道改造说明](recall-enhancement.md)。

### `mindex transcribe [--source SOURCE_ID]`

批量转写（30 秒块检查点）。省略 `--source` 处理全部素材。无音轨素材记录 `no_audio`；`asr.enabled: false` 记录 `disabled`。完成后可停止 ASR 服务。

### `mindex align PYTHON [--source SOURCE_ID]`

用独立 CPU ForcedAligner 环境对齐已缓存的原始 ASR 文本（用于 `asr.backend: llama_cpp` / `openai` 等纯文本路径；MiniMax 自带时间戳不需要此步）。**`PYTHON` 是位置参数**，指向 `.venv-align/bin/python`：

```bash
uv run mindex align .venv-align/bin/python
```

要求配置中 `asr.aligner_revision` 已固定为实际 commit。

### `mindex analyze [--source SOURCE_ID]`

分析镜头和精细片段，组织连续场景；失败后可重跑，已完成片段的检查点复用。

### `mindex index [--activate] [--mode auto|visual|enriched] [--channel CHANNEL...] [--source SOURCE_ID] [--kind KIND...]`

建立或继续新索引，输出索引 ID。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--activate` | 关 | 全部检查通过后设为活动索引 |
| `--mode` | `auto` | `auto` 使用已完成渠道；`visual` 更新画面；`enriched` 保留旧版完整索引入口 |
| `--source` | 全部 | 更新该素材；新版保留活动索引中的其他素材和渠道 |
| `--channel` | 全部可用渠道 | 可重复指定 `visual` / `dialogue` / `analysis`；未选渠道保留 |
| `--kind` | auto/enriched→moment、shot、scene；visual→moment、shot | 可重复指定 `shot`/`moment`/`scene`；visual 不支持 scene |

### `mindex ingest VIDEOS... [--activate]`

执行默认阶段（prepare→enrich-dialogue→auto index），不触发 LLM 分析。对白处理面向库内素材，不仅限本次输入。**仅适用于服务可同时访问的环境**，单 GPU 互斥场景禁用。

## 检索与状态

### `mindex search TEXT [--granularity moment|shot|scene] [--limit 20] [--channel CHANNEL...]`

文字检索，输出 JSON 结果（limit 1–50）。机制见 [search.md](search.md)。

```bash
uv run mindex search '雨夜，一个人强忍眼泪，镜头慢慢推近' --granularity scene
```

### `mindex status [--health]`

索引、素材、处理记录与模型配置；`--health` 额外探测嵌入服务并校验空间身份是否匹配活动索引。

### `mindex serve [--host 127.0.0.1] [--port 8000] [--index-id INDEX_ID] [--allow-insecure]`

启动页面与检索 API。`/` 检索页、`/admin` 素材后台、`/persons` 人物库、`/studio` 创作页（含 `/studio/topics` 选题挖掘与 `/studio/templates` 文案模板）、`/settings` 设置页、`/docs` OpenAPI。`topics.scheduler.enabled` 为真时随服务运行每日选题挖掘，并在启动时补跑当天错过的定时（见[选题挖掘](topics.md#每日定时)）。

`--index-id` 将查询、片段媒体和上下文绑定到指定 ready 索引，用于独立版本验证；不会修改活动索引。

**访问令牌（可选）。** 设置环境变量 `MINDEX_WEB_TOKEN` 后，页面和 API 都需要令牌：浏览器访问 `/login` 提交令牌，服务器设置 HttpOnly、SameSite=Strict 的 Cookie（内容是令牌的 HMAC，不是令牌本身），`POST /logout` 清除登录（不接受 GET）；`/login` 的 `next` 只接受站内相对路径，其他值回到 `/`；脚本使用 `Authorization: Bearer <令牌>`。未登录的页面请求跳转到 `/login`，API 返回 401。`/assets/shared/` 下的公共样式、脚本和文案字典不需要令牌。未设置该变量时不启用认证，行为不变。令牌通过环境变量传入，不放进 YAML 或命令行参数。监听非回环地址（127.0.0.0/8、`::1`、`localhost` 以外）且未设置令牌时，`serve` 拒绝启动并以退出码 1 提示；确认风险后可传 `--allow-insecure` 强制启动，此时只输出警告。

## 索引版本管理

### `mindex check [--index-id ID]`

发布前一致性检查（向量与表示对账、片段完成、文件存在、表示齐全）。默认检查活动索引；发现问题列出并以退出码 1 结束。

### `mindex indexes list`

列出全部索引版本与当前活动索引。

### `mindex indexes activate INDEX_ID`

检查通过后在 SQLite 事务内发布。回退 = 先切回旧模型服务/配置，再激活旧索引。

## 磁盘清理

### `mindex clean [--apply]`

扫描可安全回收的磁盘数据；**默认预览**，加 `--apply` 才真正删除。分级判定、保留策略与 C 级待调整清单详见 [cleanup.md](cleanup.md)；过期阈值在配置 `clean` 节调整（[config-reference.md](config-reference.md)）。

- **A 级（每次都删）**：崩溃残留临时目录、`.DS_Store`、上传暂存残留、孤儿文件（切片/缩略图/转码缓存/人物图/声音样本/未注册原片），以及 `persons/frames` 冗余展示帧（已确认镜头每身份保留一帧，未确认镜头全部保留）；部分上传/原片残留受 `clean.temp_grace_hours` 宽限期保护
- **B 级（策略过期）**：非活动索引版本（保留活动 + 最近 `clean.index_keep_versions` 个非活动 ready + 全部非 ready）、旧指纹检查点、context/shot_media/studio/reports/字幕缓存、过期助手附件（`agent.attachment_ttl_hours`）、runs 旧记录
- **C 级（只报告不删）**：model-cache、一次性验证产物、数据库备份、VACUUM 建议——出现在输出 `notes` 中

apply 分两段持锁：先独占 `pipeline.lock` + `index-write.lock`（管线与索引类，与素材删除同序），释放后再取 `studio.lock`（创作类，拿不到则该三类标记 skipped 而非失败）。管线/索引锁被占时以中文提示退出码 1。大规模清理前建议停 serve。输出 JSON 的 `categories` 包含各类别 `error`/`skipped`，应逐项检查；单类别错误记录在报告中，不一定使整体退出码为 1。CLI 只提供 `--apply`，无按类别删除或命令行过期天数选项；保留策略在 YAML 的 `clean` 节配置。

```bash
uv run mindex clean                       # 预览各类别可回收项与字节数
uv run mindex clean --apply               # 实际删除
```

## 智能助手与 MCP

### `mindex agent tools [--domain DOMAIN...] [--names-only]`

输出智能助手工具注册表：名称、领域、风险级别（read/write/danger）、是否导出 MCP、描述与参数 JSON Schema。`--domain` 可重复，取值 `meta`、`studio`、`studio_detail`、`library`、`search`、`index`、`persons`、`settings`。只读，不访问素材库。

### `mindex mcp [--base-url URL] [--token-env MINDEX_WEB_TOKEN] [--policy read|write|all] [--domain DOMAIN...]`

以 stdio 传输运行 MCP 服务（服务名 `mindex`），供 deepseek-harness、Claude Code、Codex 等调用；标准输出只用于协议。省略 `--base-url` 时在本进程内装配应用，否则连接已运行的服务。写操作与高风险操作受 `agent.mcp.policy`（默认 `read`）限制。`--domain` 可重复，只导出这些领域（`report_capability_gap` 始终导出），覆盖 `agent.mcp.domains`；未知领域启动失败。需要 `uv sync --extra mcp`。配置示例见 [智能助手](agent.md#mcp-导出与外部-harness)。

## 选题挖掘

`mindex topics` 命令组与创作页「选题挖掘」、`/api/topics`、智能助手 `topics` 领域共用同一服务层。命令直接读写数据目录，不需要服务在运行，输出均为 JSON；失败时错误写到标准错误，退出码为 1。机制与示例见[选题挖掘](topics.md)。

| 命令 | 说明 |
|---|---|
| `profiles list/show/create/update/delete` | 领域档案；`create`/`update` 接受 `--name --domain --subtopic --audience --positioning --taboo --keyword --canvas --schedule/--no-schedule --schedule-time --schedule-count` 或 `--file`；`delete` 需要 `--yes` |
| `sources list/add/update/remove/test` | 数据源；`add PROFILE --kind rss\|hotlist\|brave\|calendar\|local --name … [--url …] [--config JSON] [--secret-env VAR]`；`remove` 需要 `--yes` |
| `sources import-calendar SOURCE [FILE] [--sample finance\|tech\|history] [--append]` | 为日历来源导入 ICS/CSV 或内置样例 |
| `fetch PROFILE [--source ID...]`、`items PROFILE [--limit N] [--source ID]` | 立即抓取并入库去重；列出候选条目 |
| `discover PROFILE [--skip-fetch] [--outline-count N]` | 在本进程前台运行一次挖掘，进度写到标准错误；已有挖掘任务在运行时退出码 1 |
| `runs list/show/cancel/retry` | 挖掘任务记录、取消（跨进程生效）、前台重试 |
| `list PROFILE [--status new\|adopted\|dismissed\|all]`、`show CARD`、`update CARD [--title] [--outline-file] [--template] [--status]` | 选题卡片 |
| `score CARD`、`coverage CARD`、`outline CARD` | 重新打分、素材覆盖报告、生成大纲 |
| `create-job CARD [--force]` | 从选题建创作任务 |
| `job-profile show/sync JOB` | 任务的账号档案快照与同步 |
| `gaps list/done/reopen/export` | 素材采集清单；`export PROFILE [--output FILE]` 输出 UTF-8 BOM 的 CSV |
| `schedule status` | 每日定时状态（定时只在 `mindex serve` 运行期间生效） |

## 评测与联调

### `mindex evaluate QUERIES [--split test] [--split dev|video] [--output FILE] [--index-id INDEX_ID]`

评测已冻结标注的查询集。未确认标注（`label_status != confirmed`）会被拒绝。split 对应查询文件中的 `split` 字段：`dev` 调参、`test` 报告、`video` 视频查询（分组名由查询文件决定）。

```bash
uv run mindex evaluate eval/queries.jsonl --split dev --output data/eval-dev.json
```

### `mindex model-check VIDEO [--text '描述'] [--repeats 3] [--output FILE]`

验证真实文字/视频嵌入：维度、范数、余弦、耗时重复稳定性，抓取服务 `/metrics` 显存（如有）。要求视频不超过 `video.short_shot_seconds`（默认 6 秒）。

```bash
uv run mindex model-check data/clips/ID/moment/SEG.mp4 --text '画面描述' --output data/model-check.json
```

## e2e 实验框架

详见 [e2e-framework.md](e2e-framework.md)。所有命令不会启动或切换远程模型。

| 命令 | 作用 |
|---|---|
| `mindex e2e init --source ID --run DIR` | 冻结已有分镜、生成标注草稿与 12 个查询视频；不调用模型 |
| `mindex e2e probe --output FILE [--role embedding\|asr\|analyzer]` | 只探测已启动服务，保存兼容性报告；无推理 |
| `mindex e2e pin --quantization Q --output FILE [--role ...]` | 生成 schema 2；本地文件参数与远程 SHA 声明互斥，不覆盖旧清单 |
| `mindex e2e phase PHASE --run DIR [--python PATH] [--queries FILE] [--split test] [--index-id ID] [--activate]` | 执行一个阶段；构建默认不发布，查询默认固定实验版本 |
| `mindex e2e freeze QUERIES --run DIR --confirmed-by '姓名'` | 人工确认后冻结标注并记录 SHA256 |
| `mindex e2e review QUERIES --run DIR` | 生成可播放全部候选、选择目标并导出确认标注的本地页面 |
| `mindex e2e status --run DIR` | 查看阶段状态、当前配置分析完成数量和活动索引，无模型请求 |

phase 取值：`smoke → visual → preview → asr → align → analyze(-smoke) → enriched → evaluate → video-evaluate → check`。其中 `align` 需 `--python`，`preview`/`evaluate`/`video-evaluate` 需 `--queries`；`video-evaluate` 的默认 `test` 分组会映射为 `video`。

## 人物库与人物发布

人物命令使用的身份、参考图和任务均保存在当前数据目录。人物处理服务需单独安装与启动，见 [人物库指南](persons.md)。

| 命令 | 参数与行为 |
| --- | --- |
| `persons list` | 输出实体与参考图 JSON，无模型请求 |
| `persons save FILE [--identifier ID]` | 从实体 JSON 新建；指定 ID 则修改 |
| `persons reference ACTOR IMAGES...` | 为演员 ID 添加一张或多张参考图，人物服务质检失败会拒绝 |
| `persons process SOURCE [--start SEC] [--end SEC] [--publish] [--retry TASK]` | 后台扫描，立即返回任务 ID 和日志路径；默认开始 0 秒、至片尾，不发布 |
| `persons status TASK` | 查询任务状态、结果、错误；TASK 不是素材 ID |
| `persons review FILE` | 写入镜头人工出镜确认，不自动发布 |
| `persons publish [--content INDEX_ID]` | 将已接受证据冻结为人物快照并发布检索组合，默认绑定活动内容索引 |
| `persons activate RELEASE` | 激活已有检索组合，同时切换内容索引与人物快照 |
| `persons migrate-frames [--source ID] [--apply]` | 迁移压缩展示帧、清理冗余，默认预览；无需重新索引 |

实体 JSON 至少包含 `kind`（`actor`/`work`/`role`）和 `name`，可包含 `aliases`、`work_ids`、`actor_ids`。角色需至少一个 `work_id`。例如：

```json
{"kind":"actor","name":"示例演员","aliases":[]}
```

镜头审核 JSON 示例：

```json
{"source_id":"SOURCE_ID","shot_id":"SHOT_ID","selections":[{"actor_id":"ACTOR_ID","role_id":null}]}
```

`process --retry TASK` 只接受同素材的 failed/interrupted/cancelled 任务。`activate` 的参数是检索组合 ID，不是单独的人物版本 ID；切换后仍需匹配的嵌入配置才能执行向量查询。

## 旧切片迁移与镜头维护

本命令组使用 YAML 原值，不合并设置页覆盖层。详见 [架构与媒体服务](architecture.md)。

| 命令 | 行为 |
| --- | --- |
| `clips migrate [--source ID] [--apply] [--verify]` | 默认盘点；apply 备份并登记引用，verify 需配合 apply 验证媒体；不删除旧实体 |
| `clips materialize --all` | 必须显式指定 all，直接从 shot 恢复旧路径实体，无预览开关 |
| `clips verify-archives [--source ID]` | 完整校验归档原片 SHA256，不等同于普通 check |
| `clips repair-boundaries [--source ID] [--audit-file FILE] [--apply]` | 默认生成切点审计报告；可读取已有 JSON，apply 才修复，旧媒体保留 |
| `clips prune MIGRATION_REPORT [--apply]` | 根据已验证的迁移报告预览旧实体删除；验收后再 apply，保留原片和 shot |

省略 `--source` 时面向全部素材。通用磁盘回收使用 `mindex clean`，而非 `clips prune`。

## mindex-model-server（GPU 侧）

在角色独立 venv 中运行本项目自定义模型服务（部署见 [DEPLOYMENT.md](DEPLOYMENT.md)）：

```bash
.venv-embedding/bin/mindex-model-server --role embedding --config config.local.yaml --host 0.0.0.0
```

| 参数 | 说明 |
|---|---|
| `--role` | `embedding`（默认端口 4010）/ `asr`（4020）/ `detector`（18203），必填 |
| `--config` | 配置文件路径 |
| `--host` / `--port` | 监听地址；端口缺省按角色 |

三个角色之间用 GPU 文件锁互斥（`MINDEX_GPU_LOCK` 可改路径，默认 `~/.cache/mindex/gpu.lock`），单 worker。鉴权用 `MINDEX_MODEL_API_KEY` 环境变量。

## schema 2 pin 与迁移

本地使用 `--weights-file FILE --mmproj-file FILE --server-cwd DIR`，流式计算 SHA256 并核对 `/props.model_path`；远程使用 `--weights-sha256 SHA --mmproj-sha256 SHA`，明确标记操作员声明。`--launch-parameters FILE` 记录部署者确认的启动参数 JSON，并参与部署摘要。HTTP API 不能证明实际加载的投影或全部数值配置。未经当前部署 smoke 基线对照不能建索引或检索；详见 [macOS 迁移](macos-model-migration.md)。

## 原片只登记与对白方式

`mindex register 视频...` 验证媒体、按内容去重并原子登记到当前素材库，不启动分镜检测或模型。返回 `source_id` 与 `duplicate`。

`mindex enrich-dialogue SOURCE --method auto|subtitle|asr [--subtitle-stream INDEX] [--no-publish]` 分别指定自动字幕优先、严格文本字幕轨道或直接 ASR。严格字幕需要选轨，失败不回退；默认 auto 兼容已有调用。处理成功而发布失败时返回独立 `publication` 错误，处理结果保留。
