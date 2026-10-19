# persons/frames 确认帧改造（12.29GiB → 约 0.2GiB）

## Context

`data/persons/frames/`（单源 10,381 张、1920×1036 PNG、中位 1.2MB/张，共 12.29GiB）按 5fps 全程采样、原分辨率 PNG 全量落盘，JPEG→PNG 转码反而放大体积。调研证实：**帧的唯一不可替代用途是给人看**（审核卡片图、审核对话框图、检索 onscreen 证据图、发布快照图）；特征提取与所有复算（匹配重算、coverage、EvidenceReferences）只用向量，不需要帧文件。

用户决策（2026-10-19）：
- **确认帧以「分镜+人物」为单位**：每个 (shot, identity) 取其 accepted 证据中真实分最高的帧；不同身份可共用同帧（内容寻址天然去重）
- **分镜确认判定**：分镜内任一证据被接受（∃ accepted 行）即已确认分镜；已确认分镜只保留各身份确认帧，其余未确认帧删除；**未确认分镜保留全部证据帧**（现有 7,896 条 candidate 待审，审核最需要看图）
- 保留帧**降分辨率**（长边 ≤640）转 JPEG；新处理流程直接生成低分辨率帧
- 本次范围仅此一项；clips（moment/shot/scene）存储改造已另行决策暂缓，不在本计划内

## 一、现状评估（已核实，附 file:line）

| 事实 | 来源 |
|---|---|
| 帧生成：5fps 从原片抽帧，JPEG q90 内存字节 → PNG 原尺寸落盘，sha256 内容寻址；**frames 是 `Persons.image()` 唯一调用方**（其余 5 处走 references/imports） | person_processing.py:608-622、persons.py:374-387 |
| 帧字节入 person_features 前已 pop，缓存只存 image_path 引用与特征向量；缓存命中不重抽帧 | person_processing.py:620、584-666 |
| 伪分规则已存在且可复用：`original_score()` 用 `evidence_type=='manual' and confirmation=='shot_review'` 判定 synthetic 并取 original_score | persons.py:874-890；`evidence_score()` 871 |
| track 行 score=1.0 伪分 | person_processing.py:408 |
| 现状分布：accepted 5,556 行/4,476 张/5.18GB；candidate 7,896 行；rejected 6,953 行；25 个 (actor,role) 有 accepted；2,428 张被多状态共用；35 行无 image_path | 只读 SQL |
| 消费方：证据图 API（person_api.py:398-415）、审核卡片图（persons.py:1080-1082 取第一条有图行）、检索证据图（search.py:234）、shot_review 挑图（persons.py:802-803、825）、publish 快照（persons.py:1140-1143 不可变） | — |
| `media()` **硬编码 image/png 且以 HTTPException(404) 报错**（不是 FileNotFoundError）→ 回退链须自行 resolve | person_api.py:206-210 |
| 前端已有 thumbnail_url 回退先例 | persons.js:640 |
| clean 当前把 persons/frames 列为 C 级保护子树 | clean.py:33、37 |

## 二、实现设计

### 1. 配置（PersonConfig，config.py:46-61 区域）
```python
frames_max_edge: int = Field(default=640, ge=0)      # 确认帧长边上限，0=原尺寸
frames_jpeg_quality: int = Field(default=85, ge=1, le=100)
```
两键**不参与模型空间身份**，改值不触发特征重算（帧只给人看）。同步 config.example.yaml person 节 + docs/config-reference.md 新增 `## person` 节（该文件现无 person 节）。

### 2. `Persons.image()` 参数化（persons.py:374-387）
```python
def image(self, raw, namespace="references", *, max_edge=0, image_format="PNG", jpeg_quality=85) -> dict
```
沿用 40M 像素上限 → `ImageOps.contain` 缩放（max_edge>0 且超限才缩）→ 按 format 保存 → sha256 命名 `persons/<ns>/<digest>.<png|jpg>` → 返回缩放后 width/height。references/imports 五处调用零改动保持 PNG 原尺寸；person_processing.py:619-622 传 `max_edge=config.person.frames_max_edge, image_format="JPEG", jpeg_quality=...`。

### 3. 确认帧保留集（单一实现，persons.py 模块级函数）
```python
def retained_frame_paths(store, source_id=None) -> dict[str, set[str]]
    """已确认分镜：每个 (actor_id, role_id) 取 accepted 有图行中真实分最高的一帧；
    未确认分镜：保留该镜全部有 image_path 的证据帧。返回 {source_id: {相对路径}}。"""
```
排序键复用现有口径：synthetic（manual+shot_review）用 `original_score`，track 的 1.0 不参与排序仅在同身份无其它候选时兜底；无分时 `evidence_score()` 返回 None → 排在最后。按 (source_id, shot_id) 分组，组内 ∃ accepted 即已确认（与 `SHOT_STATES` persons.py:918-927 同口径）。identity 用行原值 (actor_id, role_id)，不做角色推断——保留策略宁多勿少。多状态共用帧（2,428 张）由集合求并集天然归并。

### 4. 迁移命令 `mindex persons migrate-frames [--apply] [--source ID]`（cli.py persons_cli 组）
```python
def migrate_frames(config, *, source_id=None, apply=False) -> dict   # persons.py
```
- **锁**：照 clean.py 的 `SharedFileLock(pipeline.lock, exclusive=True)` 非阻塞（失败 → 中文报错「有离线处理任务正在运行…」退出 1）+ `index_write_lock`（timeout=10，失败 → 「人物检索正在发布…」）。单源独立事务、独立容错；apply 段包 `store.run("persons-migrate-frames")` 审计
- **步骤**：①算保留集 → ②逐保留帧判定目标形态（已是 .jpg 且长边 ≤max_edge 则原样保留，否则解码→缩放→JPEG→新 digest 写新文件，**先写新后删旧**）→ ③单源单事务重写 `person_evidence.data.image_path` 与 `person_features`（kind='shot'）`data.frames[].image_path` → ④提交后删除该源目录下不在新保留集的文件 + 清空目录
- **不触碰**：person_versions / search_releases（不可变快照，悬挂路径由 API 回退兜底）、无 image_path 的行、仅被非保留理由引用的 candidate/rejected 行（留档）
- **幂等**：PIL 同版本重编码确定 → 中途崩溃重跑收敛（写新后崩溃=孤儿被步骤④删；提交后删旧前崩溃=旧 PNG 进删除集）。跨 PIL 版本可能多一轮重编码，无害且报告 `unchanged` 计数可见
- **报告**：`{applied, sources:[{source_id, kept_frames, reencoded, unchanged, deleted_files, deleted_bytes, rewritten_evidence, rewritten_features, error}], total{...}}`

### 5. 运行时配套（防迁移后悬挂路径）
- **证据图 API**（person_api.py:398-415）：回退链 `原帧 → person_versions 同 id 证据 → shot thumbnail → 404`；因 `media()` 抛 HTTPException，改为直接 `store.resolve_media()` 判存在；`media_type` 按后缀推断（.jpg → image/jpeg）。API 层返回 200 回退图，前端零改动
- **审核卡片图**（persons.py:1080-1082）：已确认分镜优先取确认帧的 evidence id（与保留集同排序口径），回退第一条有图行，全无 → None
- **shot_review 挑图**（persons.py:802-803、825）：改为「文件真实存在优先」——候选序列 `[该 actor 有图行按确认帧排序] + [其余有图行] + [shot.thumbnail]`，取首个 `is_file()` 的；全缺失用 thumbnail。保留原「该演员优先」语义，防止把死路径写进新证据行

### 6. clean 集成（clean.py）
- `PROTECTED_SUBTREES` 移除 `"persons/frames"`；`PROTECTED_NOTES` 删 persons/frames 条目
- 新增规则 `person_frames`（级别 **A**，结构性引用判定无 mtime 门槛，与 orphan_clips 同性质）：保留集 = `retained_frame_paths()`，盘上不在集内的文件即候选；execute 为 unlink + `_prune_empty_parents`；policy 文案注明「旧库先跑 migrate-frames 获得降分辨率收益」。与迁移共用同一函数 → 行为口径一致

### 7. 测试
`tests/test_persons.py`（复用 catalogue/photo/config fixture，PIL 造图）+ `tests/test_clean.py`（复用 make_config/invoke/write）：
1. `image` 缩放与格式：frames 传参得 .jpg 且 size=(640,360)、digest 可重复；默认调用仍 .png 原尺寸；max_edge=0 不缩
2. 保留集策略：排除 manual/track 伪分（用 original_score 0.5 输给 face 0.6）、track 仅兜底、未确认分镜全保留、无图行跳过、多身份共路径去重
3. 迁移幂等与路径重写：预览零改动；apply 后 .jpg 新文件 + evidence/features 路径重写 + 旧 PNG 删除；二次 apply 无重编码无删除；制造「DB 已指新、旧 PNG 残留」中断态 → 只删孤儿
4. API 回退链：帧缺失 → 200 返回缩略图（content-type 正确）；缩略图也缺 → 404；行缺失但 versions 快照有 → 回退成功
5. 卡片图优先确认帧（candidate 行 start 更早但 accepted 行在后时选后者）
6. shot_review 挑存在的文件，全缺失回退 thumbnail
7. clean person_frames 规则：保留帧留、其余删；persons/frames 内 .DS_Store 被 ds_store 覆盖
8. purge 仍整树删除 frames 子树（不回归）

### 8. 文档
docs/persons.md（确认帧机制 + 新命令）、docs/cleanup.md（A 级加 person_frames、C 级删 persons/frames、VACUUM 建议措辞）、docs/config-reference.md（person 节）、config.example.yaml、CLAUDE.md 存储布局补 `persons/`

### 9. 实现顺序（6 个可独立绿的提交）
1. 配置键 + `image()` 参数化 + person_processing 传参 + 测试 1（新处理立即产低清帧，旧库不受影响）
2. `retained_frame_paths` + 测试 2（纯函数）
3. `migrate_frames` + CLI + 锁 + 报告 + 测试 3
4. 运行时回退配套（media_type、回退链、卡片图、挑图）+ 测试 4/5/6
5. clean.py 集成 + 测试 7/8（**必须在 3 之后上线**）
6. 文档

### 风险
- 迁移幂等：写新→提交→删旧顺序 + 内容寻址确定性（已在上文 §4 说明）
- person_versions 不可变快照的证据图会落到 thumbnail 回退（预期行为，文档明示；重新 publish 后自愈）
- 多状态共用帧按并集保留，不会误删确认帧

## 三、验证

- `uv run pytest tests/test_persons.py tests/test_clean.py -q` → 全量 `uv run pytest -q` 回归；`uv run ruff check src tests`
- 真实库先 `mindex persons migrate-frames` **预览**核对数量级（accepted 5,556 行/4,476 帧 → 确认帧并集应远小于此），确认后再 `--apply`；apply 后 `du -sh data/persons/frames` 核对实际收益与报告一致
- 迁移后人工走查：审核页已确认分镜卡片图、待审分镜卡片图、检索 onscreen 证据图、历史已发布组合的证据图四处显示正常

## 四、待办（同步计划文件）

批准后把本计划同步到 `plans/PERSON_REFACTOR_PLAN.md`（当前为评估阶段草稿）。
