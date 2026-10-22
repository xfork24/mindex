# 项目架构与规范审查（2026-10-22）

范围：`HEAD` 2227bff 的源码、测试、打包配置与全部 Markdown。方法：阅读结构与关键模块，用脚本核对 CLI 帮助、配置模型、API 路由、相对链接与文档路径，运行 `ruff` 与 `pytest`。本文是带日期的审查记录，不代表之后的代码状态。

## 结论

分层目标清晰：SQLite 作事实来源，向量带模型空间身份，索引与切分布局不可变，检索失败时明确报告未就绪。CLI 帮助、配置模型与文档在命令和选项层面基本一致（逐条核对未发现不存在的命令或选项）。

主要问题集中在四处：Web/API 无认证；模块依赖成环，`shared/` 和 `models/` 反向依赖上层；若干超大文件；测试未全绿且没有 CI。文档的漂移已在本次修正，见末尾清单。

## 规模

| 项目 | 数据 |
|---|---|
| 后端 | 26,164 行 Python，84 个文件，10 个功能包 |
| 前端 | 原生 JS/CSS/HTML，`messages.js` 7,449 行，`studio.js` 2,559 行 |
| 测试 | 953 项（60 个 `test_*.py`），另有 Node/Playwright 的 `.cjs` 回归脚本 |
| 文档 | `docs/` 44 个 Markdown，其中 19 个是带日期的验证记录；`plans/` 21 个 |

## 发现

### 高

**1. Web 与 API 没有认证，设置页可改服务地址和密钥。**
`api.py` 的 `create_app` 没有认证中间件，仅 `models/model_server.py` 与 `persons/person_server.py` 有 `Depends(authorize)`。`mindex serve` 默认监听 `127.0.0.1`，这个默认值是对的；但 `--host 0.0.0.0` 会向网络开放删除素材、改模型 `base_url`、读写设置的全部接口。模型密钥明文存在 SQLite（接口只返回掩码，文档已说明）。
建议：为非回环地址增加访问令牌（如环境变量 `MINDEX_WEB_TOKEN`），或在 `--host` 非回环时拒绝启动，除非显式传入确认参数。本次已在 README 增加安全提示，未改代码。

**2. 完整测试不是全绿，且没有 CI。**
本次完整运行 `uv run pytest -q`：953 项中 **943 通过、10 失败**（耗时 22 分钟）。
- 7 项与 `docs/history/source-layout-verification.md` 记录的既有失败一致，该文档称它们在重构前就能复现。本次在 `HEAD` 上单独复现了 `test_admin.py::test_live_curation_filters_text_video_without_mutating_index`：首条搜索结果的 `display_name` 为 `None`，与断言不符。
- 另有 3 项**未记录**的失败，单独重跑仍然失败，不是并发波动：`test_backup.py::test_person_union_uses_published_evidence_only`、`test_studio_recall.py::test_person_recall_filters_by_published_evidence`、`test_studio_recall.py::test_workspace_recall_pins_active_person_release_and_reuses_after_rollback`。后两项抛出 `ValueError: 出镜审核必须指定角色`。该校验由提交 fd4ea33（"scope person appearances and review to work roles"）加入，测试没有同步。备份那一项得到空集合，原因未查。

仓库没有 `.github/` 或其他 CI 配置，`ruff` 之前也不通过（见末尾）。失败长期留存，会让新失败难以分辨。
建议：逐项判断是测试夹具漂移还是产品缺陷，修好或标记 `xfail` 并写明原因；加最小 CI（`ruff` + 不依赖媒体的测试子集）。

### 中

**4. 包依赖成环，分层名不副实。**
按 `import mindex.*` 统计包级依赖：`shared` 依赖 `admin`、`diagnostics`、`models`、`persons`、`studio`；`models` 依赖 `processing`、`studio`；`persons` 与 `admin`、`settings` 互相依赖。多数反向依赖是函数内延迟导入，运行时可用，但静态上成环。模块级的反向依赖有两处：
- `shared/work_cache.py` 导入 `admin.curation`、`admin.source_work`。
- `models/adapters.py` 导入 `studio.studio_artifacts.record`（记录模型调用）。
延迟导入还有 `shared/storage.py` → `persons.persons.init_person_schema`、`shared/media.py` → `studio.studio_artifacts`、`shared/compatibility.py` → `diagnostics.e2e`。
建议：把 `work_cache.py` 移到 `admin/`；把 `record` 这类横切记录能力下沉到 `shared/` 或改为回调注入；`Store` 的人物表初始化改为注册式。这样 `shared`、`models` 才能被独立服务环境（模型服务、检测服务）安全地只装一部分依赖。

**5. 超大文件。**
`persons/persons.py` 1,684 行，`studio/studio_workspace.py` 1,660，`cli.py` 1,360，`studio/studio_api.py` 1,173，`maintenance/backup.py` 1,130，`models/adapters.py` 1,060（7 个 Protocol 与全部后端实现同在一个文件）。前端 `studio.js` 2,559 行，`tests/test_persons.py` 约 155 KB。
建议：先拆 `adapters.py`（按 backend 分模块，工厂保留在原处）和 `cli.py`（按命令组拆分，`indexes`、`e2e`、`persons`、`clips`、`backup` 已是子命令组，天然可拆）。其余随功能修改时再拆。

**6. `messages.js` 是 `messages.json` 的手工镜像。**
两份各 1,868 键的内容靠 `tests/test_i18n.py` 保持一致。镜像文件有 7,449 行，每次文案变更都要同步两处。
建议：由脚本生成 `messages.js`，或让前端直接加载 JSON。

**7. 仓库卫生。**
- 约 10 MB 的二进制 `tools/m4s-converter/m4s-converter` 被追踪，且 `MANIFEST.in` 将其打入源码包。建议改为 Git LFS 或按需下载。
- `plans/` 内有重复或迭代版本：`PERSON_LIB_UI_PLAN.md` 与 `PERSON_LIB_UI_PLAN_v2.md`，`PERSON_REFACTOR_PLAN.md` 与 `PERSON_PROCESS_REFACTOR_PLAN.md`。建议归并或标注取代关系。
- 没有 `LICENSE` 文件，README 也未声明许可。是否开源由所有者决定，但缺省状态等于保留全部权利。
- `docs/` 44 个文件中 19 个是历史验证记录，与使用文档混放，检索时稀释主线。建议移入 `docs/history/`（需同步链接）。

### 低

**8. 打包与工具配置。**
- `pyproject.toml` 的 `description` 仍是早期定位（"Chinese text-to-video retrieval ..."），与现在的素材管理加创作工作台不符。
- `[tool.setuptools] packages` 手工列出全部子包，新增子包易漏。可改为 `find`。
- `[tool.ruff]` 没有显式 `select`，规则集依赖 ruff 版本默认值。
- `MANIFEST.in` 之前漏掉 `AGENT.md`（README 链接了它），已补。

**9. 模块命名重复包名。**
`persons/persons.py`、`search/search.py`、`admin/admin.py`、`studio/studio_*.py`。`AGENT.md` 将"各包保留原模块文件名"作为迁移约定，因此这是已知取舍，不建议单独处理。

**10. 异常处理。**
`except Exception` 共 32 处，未发现裸 `except:`、`shell=True`、`eval`、`pickle` 或 `yaml.load`；YAML 一律 `safe_load`，ffmpeg 调用使用参数列表。这部分没有问题，列出以便对照。

## 本次已修正

| 类别 | 内容 |
|---|---|
| 命令错误 | README 的 `mindex analyze SOURCE_ID` 改为 `mindex analyze --source SOURCE_ID`（`--source` 是选项，不是位置参数） |
| 脚本名 | `scripts/run-transnet-service.sh` 不存在，实为 `run-transnet.sh`；修正 `docs/DEPLOYMENT.md`、`CLAUDE.md`、`scripts/setup-transnet-service.sh` 的提示 |
| 脚本名 | `docs/macos-model-migration.md` 的 `scripts/run-local-wemm.sh` 改为 `run-wemm.sh`；`docs/REHASH_MIGRATION.md`（历史记录）加更名说明 |
| 本地文件 | `config.macos-v2.yaml` 不在仓库中，两份迁移文档补充说明；`REHASH_REFINEMENT_PLAN.md` 的位置由"根目录"改为 `plans/` |
| 失效链接 | 5 份验证记录中 27 处指向未入库 `artifacts/` 的链接改为行内路径，并在文首说明 |
| 配置文档 | `docs/config-reference.md` 补全 `person` 全部键与 `search.channel_weights`；`config.example.yaml` 补 `search.channel_weights`，更新过时的首行注释 |
| CLI 文档 | `docs/cli-reference.md` 补 `backup --verification` 与 `--allow-legacy-representations` |
| 架构文档 | `docs/architecture.md` 的数据流改为三渠道（并保留旧版）、补全此前缺失的全部后端模块、15 张 SQLite 表，以及 5 个数据目录和 `pipeline.lock` |
| 索引页 | `docs/README.md` 补 `persons.md`、`recall-enhancement.md` 与 19 份验证记录，修正 `search.md` 的描述 |
| Lint | `tests/test_persons.py` 的 4 处 ruff 错误（1 个导入排序，3 个未使用变量），改后 `ruff check backend/src tests` 通过 |
| 打包 | `MANIFEST.in` 加入 `AGENT.md` |
| README | 快速开始前移到功能实景之前；增加安全提示与常见问题；快速开始改用 `uv sync`；整理"源码与协作"段落 |

## 未处理

第 1–7 项与第 8 项的 `pyproject.toml` 改动需要产品或所有者决策，或改动面较大，本次只记录。10 项失败的测试没有改动。
