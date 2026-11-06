# 数据备份与迁移

`mindex backup` 用于完整备份、选择素材迁移、空库恢复及已有库合并。迁移直接保存和恢复既有向量，不调用嵌入、转写、人物识别或分析模型。

本页主要说明本地素材库的 `backup` 工作流。local↔PG/S3 的同版本迁移使用
`mindex migration preflight/export/load/verify/activate`，见[集群切换边界](scaling.md#集群-runbook正式版随双机验收记录补全)。
该入口拒绝覆盖非空/异值目标；迁移首份目标对象写入前持久绑定源清单摘要与库 UUID，
按九个阶段账本续跑（先核对已有目标内容再补账，异值拒绝覆盖）；缺少必要媒体或向量的
元数据 bundle 不能激活。单机故障矩阵已覆盖各阶段中断与恢复；代表性规模的灾备
RPO/RTO 实测保持独立验收，不据工程样本声明可正式迁移大库（演练见[迁移与恢复](scaling-migration.md)）。
`migration export` 裁剪元数据库中已登记的结构化秘密字段；未知秘密载体保留用户原文、
列出位置并阻止正式激活。bundle 覆盖 Studio 目录历史、外部调用二进制回执及正式上传资产，
统一媒体闭包经权威资源登记核对；这些通过项不替代其他业务的媒体闭包核查。

## 旧共享命名空间升级

共享资源现按库 UUID 隔离：Chroma 为 `lib-<UUID无连字符>-idx-<索引ID>`，
S3 为 `<prefix>/libraries/<UUID无连字符>/`。本地文件及本地 Chroma 集合名称不变。
程序不会因新命名空间为空而自动读取旧资源，以免把其他库的同名索引或媒体当成本库数据。

旧 PG 部署使用未隔离的 `idx-<索引ID>` 和 `<prefix>/<key>` 时，先停止所有写入节点，
使用原数据库与原资源配置执行显式导出，再使用新的空数据库和节点目录恢复：

```bash
mindex --config old-cluster.yaml migration export --target /backup/namespace-upgrade \
  --legacy-namespace --with-media --with-vectors
mindex --config new-cluster.yaml migration preflight --source /backup/namespace-upgrade
mindex --config new-cluster.yaml migration load --source /backup/namespace-upgrade --with-vectors
mindex --config new-cluster.yaml migration verify --source /backup/namespace-upgrade
mindex --config new-cluster.yaml migration activate --source /backup/namespace-upgrade
```

`--legacy-namespace` 只影响导出读取，不改变原数据库或移动/删除旧对象；仅在确认旧共享资源
属于该库时使用。装载从第一份对象开始使用 bundle 的源库 UUID，元数据库提交后身份保持一致。
集群回到本地时同时恢复对象与逻辑文件视图。原库保持停写，切换应用连接前应完成自己的
媒体/向量闭包对账；当前仍有未交付的闭包与续跑项目，见[验证记录](scaling-enhancement-verification.md)。

## 操作前

停止 Web、CLI 处理管线、创作任务以及全部后台写入进程，再开始导出或导入。迁移会依次取得管线独占锁、素材生命周期锁、原片登记锁、索引锁和创作锁，锁被占用立即失败。锁不能替代停机：设置修改等短事务没有持有整个服务生命周期的锁。

导出目录必须不存在，也不能位于数据目录内。导入需要足够空间同时存放暂存结果、原库和备份；合并会重建目标库副本。备份目录和恢复目录不能互相包含。数据目录、备份文件中的符号链接均被拒绝；请先把需要携带的文件整理成普通文件。

## 导出与检查

```bash
uv run mindex backup list
uv run mindex backup export /backup/mindex-20261020 --all --dry-run
uv run mindex backup export /backup/mindex-20261020 --all
uv run mindex backup inspect /backup/mindex-20261020 --json

# 重复 --source 选择多个素材
uv run mindex backup export /backup/selected --source ID1 --source ID2

# 显式包含设置页保存在 SQLite 中的密钥
uv run mindex backup export /backup/private --all --include-secrets
```

不传 `--all` 或 `--source` 时，交互终端可以按素材名称/ID 搜索，输入逗号分隔的序号切换多选，输入 `/完成` 确定。脚本环境必须显式指定素材范围。`--all` 与 `--source` 互斥。

默认移除已存密钥，也会重写历史 SQLite 快照、任务配置和已知凭据字段，执行 VACUUM 清除数据库旧页中的密钥。`--include-secrets` 的备份包含明文凭据，请按含密钥文件保管。两种模式都不读取环境变量中的密钥；报告列出配置中使用的环境变量名。

`--dry-run` 会实际构建并检查临时快照，显示所选素材、向量、依赖、排除项和版本映射，完成后删除临时结果，不创建目标备份或切换目标库。它需要与实际操作相近的时间和暂存空间。`--json` 的结果写到标准输出，中文进度写到标准错误。

## 基础校验模式

导入、导出默认使用 `--verification full`。明确了解备份来源、信任其完整性的用户，可以主动选择 `--verification basic` 缩短时间：

```bash
mindex backup export /backup/selected --source ID --verification basic
mindex --config config.local.yaml backup import /backup/selected --all --skip-existing --verification basic
```

| 检查 | full（默认） | basic |
| --- | --- | --- |
| 格式、路径边界、符号链接、文件存在及大小 | 保留 | 保留 |
| SQLite | 完整性及依赖检查 | quick_check、外键及必要依赖检查 |
| 导出 SHA256 清单 | 生成并复算 | 生成一次，省去复算；仍可随后用 `backup inspect` 完整检查 |
| 导入全文件 SHA256 复算 | 保留 | 省略 |
| 向量读取、装载时缺失及重复 ID 检查 | 保留 | 保留 |
| 导入最终逐向量数值、文档、归属复核 | 保留 | 仅核对各集合数量 |
| 导入逐历史索引分镜媒体探测 | 保留 | 省略；媒体路径及文件存在性仍检查 |
| 重复素材、共享对象冲突、WeMM 空间兼容性 | 保留 | 保留 |

基础模式无法发现某些大小未变的文件损坏、向量内容或语义错误。它不会去掉历史索引或素材数据，不调用模型，也不会绕过离线锁、暂存和原子切换。模型兼容性证据仍校验，不伪造 pin/smoke 状态。输出及 JSON 会注明本次 `verification` 和 `skipped_checks`；导出清单另记录 `export_verification`，导入不会自动继承基础模式。

两种模式均可配合 `--dry-run`。基础模式仍需复制文件、重写数据库、通过 Chroma API 读取/建立集合并持久化；大型备份依然可能耗时。该参数只影响新启动的命令，无法改变已经运行的导入。

## 媒体组

组与成员关系通过正式迁移纳入业务库。全量备份保留空组；部分备份只保留涉及导出成员的组及对应关系。音乐库作为全局库携带曲目及文件，其非空音乐组随相应曲目导出。删除素材或曲目时，外键自动清理成员关系。

恢复保留稳定组 ID；合并遇到同类型、同名称且说明、成员均一致的组，沿用目标组 ID 并映射成员与任务组引用。说明或成员不同时添加确定性的导入后缀；同 ID 内容冲突按既有合并规则明确拒绝。部分备份未携带的组引用会保持失效状态，后续召回需重新选择，不扩大范围。

## 空库恢复

默认从目标配置的 `data_dir` 恢复。全局 `--config` 继续放在命令组之前；`--data-dir` 可以放在全局位置，也可以放在 `backup import` 后，后者优先。指定的数据目录按当前工作目录解析。

```bash
uv run mindex --config /new/config.local.yaml backup import /backup/mindex-20261020 \
  --data-dir /new/data --all --dry-run
uv run mindex --config /new/config.local.yaml backup import /backup/mindex-20261020 \
  --data-dir /new/data --all
```

`backup` 允许目标配置文件尚不存在。空库自动恢复基础 YAML、SQLite 运行时覆盖层、模型档案、音色、历史索引及活动检索组合。基础配置在数据目录外保存，项目内的 `data_dir`、字体和部署清单路径相对配置文件保存，外置数据目录保留绝对路径；受管任务路径会重定位。基础 YAML 和覆盖层分别保存，备份命令不会预先把覆盖层混进 YAML。

完整恢复保留版本 ID。选择导出或选择导入会重新生成索引成员清单、索引 ID 和人物发布组合，并输出旧新映射；任务引用同步更新。没有索引的素材仍然没有索引，导入不会自动推理。

## 已有库合并

```bash
uv run mindex --config target.yaml backup import /backup/selected --all --dry-run
uv run mindex --config target.yaml backup import /backup/selected --all --skip-existing

# 显式替换目标系统配置；建议先查看脱敏差异
uv run mindex --config target.yaml backup import /backup/selected --all --restore-config --dry-run
uv run mindex --config target.yaml backup import /backup/selected --all --restore-config
```

默认保留目标的基础 YAML、运行时覆盖和活动模型档案。`--restore-config` 恢复备份配置并在切换前输出脱敏差异。模型档案和音色会导入为可选条目，本地整数 ID 与任务凭据引用重新映射；同名但不同配置的条目会加上确定性的“导入”后缀。

重复素材默认使导入失败。`--skip-existing` 保留目标素材及其全部衍生数据，裁掉备份中这些素材的成员、向量、人物证据和相关任务，无法覆盖替换。共享人物实体、历史对象或同路径文件只有内容一致才复用，内容冲突则整次导入失败。

两端都有活动索引时，WeMM 嵌入空间、索引格式、预处理和模式必须兼容；不校验分析、ASR 或其他模型的兼容性，原有模型来源信息随历史快照保留；系统使用既有向量创建联合索引并激活。人物组合只合并两端已经发布的证据，不发布尚未发布的人工修改。没有活动索引的一端仅贡献数据及历史版本。报告中的 `searchable_sources` 是最终实际可检索的素材范围。

创作任务只在全部素材依赖都可确定、且都被选中时携带。素材范围不明或依赖不完整的任务会出现在排除报告中。历史索引、媒体引用、任务检查点以及任务 `inputs/` 下的查询媒体、画面来源文件与参考文稿一并保存；迁移时仍为 `queued` / `running` 的任务改为 `interrupted`，之后可以手动重试。

## 目录格式与校验

```text
manifest.json                 # 格式版本、来源、素材、依赖、排除项、映射和 SHA256
config/base.yaml              # 基础 YAML（不混入数据库覆盖层）
data/metadata.sqlite3         # SQLite backup API 一致快照
data/sources/…                # 原片及业务文件，保留原有相对布局
data/config-dependencies/…    # 字体、部署清单等
vectors/INDEX_ID/000000.json  # API 分批导出的显式向量、文档、元数据
```

全量备份保留历史文件和普通缓存；模型权重、可下载模型缓存、锁文件、未完成临时文件和 Chroma 内部索引文件列入排除清单。智能助手的本机运行状态（`agent.sqlite3` 会话与密钥保险库、`agent-attachments/` 附件暂存）也不进入备份。部分备份只携带选定素材的依赖、关联作品的演员表、所选素材当前及已发布历史证据依赖的人物和参考图、音色、用户文案模板（`script_templates`）、选题挖掘数据（`topic_*` 表，整体保留，不按素材裁剪）及系统配置；未知归属的历史缓存会排除。

导入先核对格式版本、文件大小、SHA256、路径边界、素材/向量清单和媒体依赖，再在目标目录同一文件系统的暂存区合并。旧备份的 SQLite 快照可能是较早的结构（含没有 `user_version` 的库），暂存副本先按版本化迁移升级到当前结构再合并。向量经 Chroma API 重建，并在另一个进程检查外键、历史成员、表示、向量及媒体引用。所有数据库连接和向量子进程退出后，才持久化并切换数据与配置。任何预检或暂存校验失败都不会发布暂存结果。

macOS Finder 自动生成的 `.DS_Store`，以及复制到外置盘时产生、格式合法且有对应主文件的 AppleDouble 文件（如 `._000001.json`），会被忽略，导出也会排除此类元数据。其他未登记文件仍使校验失败，错误最多列出 10 个路径并报告剩余数量；符号链接仍被拒绝。

`inspect` 会读取并核验整个备份，包含大文件的 SHA256，耗时取决于备份大小。SHA256 用于发现损坏，不是来源认证；请使用可信来源的备份。

## 切换中断后的恢复

切换前会在数据目录的父目录写入 `.<数据目录名>.migration.json`，保留旧目录及旧配置。遇到未完成日志，新的导入会拒绝继续。停止写入任务后，使用原来的配置路径和恢复路径执行：

```bash
uv run mindex --config /new/config.local.yaml --data-dir /new/data backup recover --json
```

尚未提交的切换回滚到旧数据和旧配置；已经提交但未完成清理的操作仅清理旧副本。不要在恢复前手动删除操作日志或旧目录。正常成功会清理日志、旧库和暂存副本。

迁移结果提交之后的临时目录清理带有重试：Finder、Spotlight 等进程在删除期间重建目录项（macOS 常见竞态）只会触发重试，重试后仍失败则发出 `RuntimeWarning` 提示手动清理，绝不把已经成功的导入或恢复标成失败。

## 全新 clone 恢复流程

1. clone 项目并进入项目目录，安装 Python 3.11–3.13、uv、ffmpeg/ffprobe 等系统依赖。
2. 执行 `uv sync`；如需人物处理，执行 `uv sync --extra person`。
3. 不启动 Web 或后台任务，运行 `uv run mindex backup inspect /backup/mindex --json`。
4. 执行 `uv run mindex backup import /backup/mindex --all --dry-run`，确认报告后去掉 `--dry-run`。默认生成 `data/` 和 `config.local.yaml`。
5. 按报告补充模型权重、本地模型目录和密钥；检查恢复的服务端点，异机部署时按实际环境调整。历史部署验证记录保留，但新部署仍需按原有工程门槛重新验证，不会因恢复自动获得验证通过状态。
6. 运行 `uv run mindex check`（存在活动索引时），启动相应模型服务后执行 `uv run mindex status --health` 验证查询模型空间，最后执行 `uv run mindex serve` 并检查检索与媒体播放。

操作系统依赖、模型权重、GPU 环境和模型服务进程不属于备份。仅恢复数据与既有向量不需要启动模型服务；真实检索时仍需提供匹配索引空间的查询嵌入服务。

## 模型服务与兼容性证据

已有 v2 索引依赖的 `capabilities/v2/<空间 ID>/` 基线、六个固定验证样本、部署验证报告及 SHA256 随备份携带；这些是模型验证依赖，不会作为新素材登记。部分备份也保留这些依赖。缺失或损坏的基线、样本会使迁移失败，不能通过重写验证状态继续使用旧索引。

恢复会重定位配置中指向部署清单的路径，但保留清单及验证证据内部的服务器路径和原始字节。同一模型服务仍须通过部署身份及兼容性检查；更换服务构建、参数或权重不会自动获得通过状态。旧版备份若未包含基线，需要从原库恢复完整验证目录，或按正常流程验证新部署。

`run-wemm.sh` 使用模型目录的物理路径。多个 clone 的 `models` 若软链接到同一目录，传给服务的模型路径会保持一致；可用 `MINDEX_WEMM_MODEL_DIR` 指定共享目录。`MINDEX_LLAMA_SERVER` 单独指定 llama-server 可执行文件。端口、模型别名、上下文等参数仍须匹配固定部署；仅“设置页连接成功”不足以证明旧索引可检索。

## 临时兼容 v2 迁移遗留表示

默认严格核对表示与成员清单。个别旧版 `format=1` 索引在历史切片边界修复后，表示 ID 无法从当前时间边界重新推导；可对单个已确认的临时素材显式使用 `--allow-legacy-representations`。该选项只影响本次导出，不能用于全库或多素材导出，不改变正常导入合并的检查规则。备份记录允许兼容的素材范围，保留既有向量数值并建立显式表示映射；向量缺失、归属不一致、文件校验失败及新版显式清单不一致仍会报错。

```bash
mindex backup export ~/backup/movie --source SOURCE_ID --allow-legacy-representations
```

未完成的历史索引保留已写入的向量、成员完成状态和任务状态，不自动补算或激活。
