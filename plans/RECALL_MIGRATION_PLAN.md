# 创作页分镜四类召回 + 镜头详情人物处理信息

> 历史记录：本计划已实施（验收见 [implementation-review](../docs/history/implementation-review.md)），内容可能与当前代码不符，以 [docs/studio.md](../docs/studio.md) 为准。

日期：2026-10-17。状态：已批准，待实施。

## Context

检索页（`/`）已有四类召回：人物（person_ids/mode/relation，含变体联合）、素材筛选（Filters）、图片找视频、视频找相似。创作页（`/studio`）分镜召回目前只调 `SearchEngine.text(text, granularity, limit)` 三个参数，四类能力全部缺失。目标：每个分镜可独立选择召回方式，复用检索页全部召回能力（仅新版版本工作台，旧版五步流程不改）。

附加需求：素材管理页镜头详情弹窗（`GET /api/admin/segments/{id}/details`）增加"是否经过人物处理"信息。

已确认范围：**仅新版工作台**（studio_workspace 主路径）；**UI 素材筛选只暴露素材下拉**（服务端传完整 Filters 模型）；人物仅在文字模式下可用（与检索页一致）。

## A. 数据模型（studio/studio.py）

新增 `ShotRecall(StrictModel)`（紧邻 `Shot`，studio/studio.py:68）：

```python
mode: Literal["text","image","video"] = "text"
person_ids: list[str] ≤30 / person_mode: auto|onscreen|mentioned / person_relation: all|any
filters: Filters（from shared/types.py；UI 只填 source_id）
media_id: str | None  # 查询文件 sha256（内容寻址）
media_kind: image|video | None
# 校验：text 不许带 media；image/video 必须带对应类型 media 且不许带人物设置
```

- `Shot.recall: ShotRecall | None = None`（向后兼容：旧 content 无此键）
- `ShotBody`（studio/studio_api.py:40-44）同步加 `recall` 字段（StrictModel extra=forbid，不同步则前端 422）
- `patch_plan` 内校验：person_ids 逐个 `Persons(store).entity()` 存在且非 work；image/video 时 `inputs/<media_id>.*` 文件必须存在
- merge 语义（studio.js）：合并保留上一镜 recall；上一镜为默认文字且无 recall 时继承下一镜设置

## B. 查询文件管理（image/video）

- 存储：`data/studio/<job_id>/inputs/<sha256><suffix>`，`.partial` 后 `replace` 原子写（复用 studio.save_bytes 模式）；内容寻址去重；rglob 自动纳入产物列表与 ZIP
- 端点：`POST /api/studio/jobs/{job_id}/plans/{plan_id}/query-media`（multipart file + kind），`mutation_lock()` 下写入。校验与检索页一致：图片 ≤20MB + `images.image_png` 可解码校验（422）；视频 ≤100MB + `media.probe` ≤30s（422）；同步工作放 `run_in_threadpool`
- 返回 `{media_id, kind, bytes, name, preview_url}`；preview 复用现有产物路由 `/jobs/{id}/artifacts/inputs/<file>`（支持 Range，零新代码）
- 公共上传 helper：新建 `backend/src/shared/uploads.py` 抽取 `api.py:29-41 save_upload`（避免 api↔studio_api 循环导入），api.py 改导入，行为不变
- 执行前兜底：按 digest 找文件并复验 sha256（复用 studio_artifacts.digest），失败抛 `StudioError("查询文件缺失或已损坏，请重新上传")`

## C. 执行与指纹（studio/studio_workspace.py）

1. **`settings()` recall 分支**（:499-508）增加 `"person_release"`：`Persons(Store(...)).published(active["id"])[0]["id"] if active else None`——修复既有缺口（text 查询隐式受人物发布影响但签名未覆盖）。代价：部署后既有 recall 版本一次性重建（纯检索开销），docs/studio.md 注明
2. **`specification()` recall payload**（:559-560）：`payload = {"query": ...}`；新 helper `recall_signature(content.get("recall"))` 规范化后非默认时加 `payload["recall"]`。规范化：person_ids 排序；filters 只留非默认键；media 引用 `{id, kind}`。**全默认判定必须严谨**（text + 无 person_ids + auto + all + filters 全空 → None，payload 与旧版逐字节一致，旧版本保持可复用）；注意 `person_mode != "auto"` 即使无 ids 也非默认
3. **`execute()` recall 分支**（:1159-1185）按 mode 分派 `engine.image/video/text`（text 传 person_ids/mode/relation/filters）；临时文件直接用 job 内 `inputs/` 原文件（SearchEngine 内部自做归一化/拆窗 TemporaryDirectory，与检索页一致）；`data` 增加 `mode`/`recall`/`meta`（recognized_people、person_coverage_missing、score_type 等）；`conflicts()` 文案改"查询、召回方式或召回设置已变化"
4. 不改动：preflight / needs_recall / match（按签名复用）/ column_nodes / select / render / compose

## D. UI（studio.js / studio.css / studio.html）

- 懒加载 `state.persons`（GET /api/persons，过滤 kind!=='work'）与 `state.sources`（GET /api/status），selectJob 时失效重取
- `renderShotEditor`（:650-718）加「召回设置」区块：召回方式 select（文字/图片找视频/视频找相似，非 text 隐藏人物控件，参照 app.js switchMode:31-43）；人物多选 + mode + relation（选项格式同 app.js:179）；素材下拉（全部素材/指定）；image/video 的 file input + 上传按钮（FormData → query-media）+ preview_url 预览；提示文案同 index.html 惯例（"最大 20MB"/"最长 30 秒 · 最大 100MB"）；recall 进入现有 JSON.stringify dirty 判定（构造时固定键序）
- `renderStageDetails`（:785-821）：显示方式/人物/素材摘要；person_coverage_missing 警告；image/video 附查询文件预览
- `renderCandidatePicker`（:1239-1277）：候选卡片加 person_evidence 展示（actor_name(/role_name) chip + 证据截图 img 懒加载 ≤3 条，点击开 `/persons?evidence=<id>`，参照 app.js:97-133）
- `cellSummary` 加方式前缀；studio.html 升级 `?v=` 版本串；studio.css 加 `.recall-editor`/`.evidence-chip`；不引入前端框架，复用 `el()/field()/button()`

## E. 需求 B：admin/segment_details.py 人物小节

新增 `person_section(store, segment)`（纯读 SQL）：

- 覆盖求交：person_coverage 区间排序合并 → 与镜头 [start,end] 求交 → covered/uncovered 子区间（容差 1e-6）
- 状态：processed / partial / not_processed + 中文 message
- 证据统计：person_evidence 按时间相交过滤，accepted/candidate/rejected 计数 + 人物名（person_entities 实时解析，用 persons.KIND_LABELS）
- 响应顶层加 `"person"` 键；admin.js showAnalysis 追加第四个 analysis-section「人物处理」（区间列表 + 证据计数）；admin.html 无需改

## F. 实施顺序与文件

| 步骤 | 文件 | 验证 |
|---|---|---|
| 1 | studio/studio.py（ShotRecall + Shot.recall） | `pytest tests/test_studio_api.py -q` |
| 2 | shared/uploads.py（新）+ api.py（改导入） | `pytest tests/test_pipeline.py -q` |
| 3 | studio/studio_workspace.py（settings/specification/execute/conflicts） | 新测试（先写） |
| 4 | studio/studio_api.py（ShotBody.recall + query-media + PATCH 校验） | 新测试 |
| 5 | tests/test_studio_recall.py（新）：向后兼容（无 recall = 旧签名复用）、四类模式、指纹失效矩阵（换人/换素材/换图→rebuild，改回→reuse，person_ids 重排不失效）、上传端点（去重/422/413/引用缺失）、仅人物名不调 embedder | `pytest tests/test_studio_recall.py -q` |
| 6 | studio.js / studio.css / studio.html（D 节） | `node --check` + serve_fixture 浏览器走查 |
| 7 | admin/segment_details.py + admin.js + tests/test_segment_details.py（三态覆盖/子区间数值/证据计数/无索引 200） | `pytest tests/test_segment_details.py -q` |
| 8 | docs/studio.md、docs/admin.md、CLAUDE.md | `uv run pytest -q` 全量 + `uv run ruff check backend/src tests` |

测试基建：`tests/test_studio_workspace.py` 的 prepared fixture（FixtureEmbedder 已实现 image/video，颜色向量）+ `tests/test_persons.py` 的 publish_content/catalogue SQL 模式。

## 验证（端到端）

1. 全量回归 + ruff + node --check
2. serve_fixture 浏览器走查：分镜编辑器四模式切换、人物多选/素材下拉/文件上传预览、候选卡片人物证据 chip、详情弹窗"人物处理"三态
3. 指纹行为：改配置 → preflight 显示需重建；改回 → 复用旧版本

## 风险

- person_release 入 settings 导致既有任务召回一次性重建（已接受，文档注明）
- StrictModel extra=forbid：ShotBody 与 Shot 必须严格同步
- JS dirty 判定依赖 recall 键序稳定（构造时固定）
- merge 规则是前端逻辑，浏览器走查必须覆盖
