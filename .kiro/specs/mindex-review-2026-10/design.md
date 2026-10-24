# Design Document

Mindex 全面审查（2026-10）、架构改造与创作能力扩展

## Overview

工作分三个阶段：

| 阶段 | 内容 | 约束 |
|---|---|---|
| A | 本 spec、报告 1–3、路线图 | 只新增文件，不改已跟踪文件 |
| Gate | 等待 agent-capability-expansion 完成 → 统一检查 → 单独提交它的改动 | 见需求 2 |
| B | 全部代码改动与文档修正 | 改动保持未提交 |

阶段 B 的顺序：先修复与基础设施（安全、IO、日志、lint、边界、runner、迁移、锁），再做创作重构（executor、拆包、阶段注册表、拆前端），最后做新能力（模板、参考文稿、画幅、五种来源、两个角色）和文档。

设计约束沿用 AGENT.md / CLAUDE.md：中文消息；UI 文案唯一源 `messages.json`；配置 strict；SQLite 是事实来源；禁止跨嵌入空间比较向量；镜头媒体一律经 `ShotMedia`；版本不可变；测试用确定性 Fixture 适配器。

## Gate 检测

`scripts/` 不新增常驻进程。执行者在自己的回合内轮询：

1. 解析 `agent-capability-expansion/tasks.md`，不存在 `[-]`/`[~]` 任务，也不存在非可选的 `[ ]` 任务；
2. 或自开始轮询起累计等待满 2 小时。

不做统一检查（用户确认）。满足后 `git add` 当时的全部改动路径（逐个列出，`.kiro/` 被 gitignore，`docs/reviews/2026-10/` 与 `plans/ROADMAP.md` 排除在外），提交说明总结该 spec 的能力。

## 架构改造

### 后台任务

```text
shared/background_tasks.py
  BackgroundTaskRunner(store, table, *, label)
    lock(row) / validate(...) / execute(row, config, save, progress)   ← 子类实现
    get / start / retry / cancel / launch / run                        ← 基类
    public(row)                                                        ← 统一行格式（字段不变）
```

`SourceTasks`、`DialogueTasks`、`PersonTasks` 继承基类；`run` 的终态写入、`TaskCancelled`、`log.exception`、锁释放、线程表清理集中在基类。

### 迁移

```text
shared/migrations.py
  migrate(conn, steps: list[Callable])   # 读 user_version，逐步执行并在同一事务内递增
```

metadata 库 v1 = 当前完整 schema（含幂等检测：列已存在则跳过）；agent 库同理。`Store` 初始化只调用 `migrate`。

### 锁

```text
shared/locks.py
  ORDER = ("pipeline", "index-write", "library", "register", "studio")
  named(root, name, *, exclusive) -> Lock
  acquire_ordered(root, names, ...) -> ExitStack    # 名称必须按 ORDER 递增
```

内部统一用 fcntl 实现（Windows 分支留给 M4，接口不变）；`filelock` 保留给按任务粒度的工作锁，由 `locks.work(root, source_id, work)` 包装。

### 创作执行与拆包

```text
studio/executor.py        StudioExecutor：guard、文件锁、当前线程、取消事件、哨兵 API、worker 包装
studio/workspace/
  __init__.py             显式导出公共入口（ws.load/persist/plan/version/preflight/run_build ...）
  persistence.py          root/path/load/persist、legacy 迁移
  versions.py             add_version/choose/restore/valid_files/file_info
  plans.py                PlanOptions、new_plan/remove_plan、script/board 编辑、快照还原
  signatures.py           settings/specification/signature_matches/conflicts/match
  stages.py               StageHandler 协议与注册表
  handlers/               script.py split.py recall.py tts.py select.py render.py compose.py
                          generate.py animate.py
  preflight.py            preflight/column_nodes/node_dependencies
  builder.py              run_build/_run_build/取消/recover
  details.py              detail/retrieval_dependencies/source_shots
```

`StageHandler`：

```python
class StageHandler(Protocol):
    name: str
    scope: Literal["plan", "shot", "final"]
    def settings(self, ctx, plan) -> dict: ...
    def specification(self, ctx, plan, sid) -> Spec: ...      # 与现有 payload 逐字节一致
    def dependencies(self, plan, sid) -> list[str]: ...
    def preflight(self, ctx, plan, sid, node) -> None: ...     # 可把 node 置为 blocked/skip
    def run(self, ctx, plan, sid, directory) -> tuple[dict, dict]: ...
    def conflict_labels(self, ctx, old, new) -> list[str]: ...
```

每个分镜的阶段序列由来源决定：

| 来源 | 阶段 |
|---|---|
| recall | recall → select → render |
| clip | select → render |
| image | render |
| aigc | generate → select → render |
| animation | animate → render |

`tts` 对所有来源都存在。签名快照测试固定历史 fixture 的 `signature`，确保 recall 来源逐字节不变。

### 前端

`studio.js` 拆为 `studio-state.js`、`studio-api.js`（薄封装 `shared/api.js`）、`studio-board.js`、`studio-sources.js`、`studio-recall.js`、`studio-build.js`、`studio-plan.js`、`studio-templates.js`、`studio-main.js`，按此顺序以经典脚本加载，共享状态挂在 `window.MindexStudio`。`scripts/build-assets.py` 计算每个资源的内容哈希，改写 HTML 中的 `?v=`，`--check` 只校验。

## 数据模型

```python
class PlanOptions:                       # 新增字段
    template_id: str = "base"
    references: list[ReferenceRef] = []  # {id: sha256, name, chars}
    canvas: Literal["16:9", "9:16", "1:1"] = "16:9"
    fit: Literal["pad", "crop"] = "pad"

class ShotSource(discriminated union on kind):
    RecallSource(kind="recall")
    ClipSource(kind="clip", media_id, start: float | None)
    ImageSource(kind="image", media_id, motion: Literal[...], strength: float)
    AigcSource(kind="aigc", prompt | None, negative | None, image_id | None, seed | None)
    AnimationSource(kind="animation", brief | None)

class TimelineVideo:                     # 判别联合
    LibraryVideo(kind="library", source_id, segment_id, segment_ids, start, end, ...)  # 缺省，兼容旧数据
    FileVideo(kind="file", ref: {"version"|"input": id}, start, end, alignment)
```

`Shot.source` 为 None 时等价 recall，dump 时省略，保证旧 content 版本号不变。

`canvas` 映射来自配置 `studio.render.canvases`（默认 16:9→1280×720、9:16→720×1280、1:1→1080×1080）；未选 16:9 时才把 canvas 写进 render/compose 签名，保证旧方案签名不变。

## 文案模板

```yaml
# backend/src/studio/templates/base.yaml
id: base
name: 通用解说
category: general
kind: outline          # outline | rewrite
version: 1
instructions: |
  ...
constraints:
  max_chars: null      # 缺省用 studio.max_input_chars
```

- `studio/templates.py`：加载、校验（strict）、`content_hash`；用户模板表 `script_templates(id, name, category, kind, base_id, body JSON, created_at, updated_at)`。
- API：`GET/POST /api/studio/templates`、`GET/PUT/DELETE /api/studio/templates/{id}`、`POST /api/studio/templates/{id}/copy`。
- script 签名参数：`{"writer": ..., "prompt_version": 3, "template": {"id", "hash"}, "references": [sha...]}`。旧版本参数里没有 template，因此对老任务会显示"提示词或模板已变化"，但 preflight 已有文案时直接复用、不重写（现有规则保留）。
- `Writer.write(outline, *, template, materials=None, references=())`。

## 参考文稿

`POST /api/studio/jobs/{job}/plans/{pid}/references`（multipart 文件或 JSON 文本）→ 写 `inputs/<sha>.txt`（UTF-8 规范化）→ 加入 `options.references`；`DELETE .../references/{id}` 只移除登记。总字数上限 `studio.max_reference_chars`（默认 30000）。

## 视频生成

```python
class VideoGenerator(Protocol):
    def submit(self, request: VideoRequest) -> str: ...
    def poll(self, task_id: str) -> VideoStatus: ...           # queued/running/succeeded/failed + url|error
    def fetch(self, status: VideoStatus, target: Path) -> None: ...
    def cancel(self, task_id: str) -> None: ...                 # 可选，不支持则 no-op
    def health(self) -> dict: ...                               # 测试连接：鉴权/可达性，不生成
```

- 配置 `studio.video_generator`：`backend`、`base_url`、`model`、`api_key_env`、`timeout`、`poll_interval`、`max_wait`、`durations`（允许时长）、`comfyui.workflow`（JSON）、`comfyui.mapping`。
- MiniMax 与通义万相的请求字段以官方文档为准，代码注释记录文档出处与核对日期；测试用 HTTP 桩。
- ComfyUI：`POST /prompt` → `GET /history/{id}` → `GET /view?filename=...`；映射项为 `"节点ID.inputs.字段"`。
- `generate` 阶段产物：`versions/<vid>/generated.mp4` + `request.json`（无密钥）。

## AI 动画

- `models/animator.py`：`Animator.animate(brief, *, seconds, width, height, fps, previous=None) -> str`；`ANIMATOR_PROMPT_VERSION = 1`；`validate_animation(html)` 做静态校验。
- `studio/animation_capture.py`：

```text
capture(html, seconds, width, height, fps, target)
  playwright chromium headless, 临时 user-data-dir, offline route（只放行 data:/blob:）
  add_init_script(VIRTUAL_CLOCK)       # window.__mindexAdvance(ms) 推进 rAF/now/Date/timers/document.timeline
  page.set_content(html, wait_until="load")
  for i in range(frames): advance(1000/fps); screenshot(png) → ffmpeg stdin (image2pipe)
  总时长上限 studio.animator.max_capture_seconds，单帧超时 5s
```

- `animate` 阶段产物：`versions/<vid>/animation.html`；人工保存走 `PATCH .../shots/{sid}/animation` 生成 `origin=manual` 版本。
- 预览接口 `GET .../versions/{vid}/animation.html` 响应头：`Content-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; media-src data: blob:`，页面以 `sandbox="allow-scripts"` iframe 加载。

## 设置角色

`ROLES` 增加 `video_generator`、`animator`；`PROFILE_FIELDS` 增加对应白名单字段；`merge_profile` 写入 `studio.video_generator` / `studio.animator`；`_probe` 增加两个分支（视频生成调 `health`，动画生成发一次最小 chat）。

## Error Handling

全部用户可见错误为中文；新增阶段失败遵循现有规则：保留部分产物与诊断、记录 failed 版本、不选中失败版本。外部生成服务的错误映射为 `ModelError`（503）或阶段失败。

## Testing Strategy

- 签名快照：用固定 fixture 工作台记录拆分前全部签名，重构后逐项比较。
- 产物一致：固定 fixture 构建 `timeline.json` 拆分前后比较（去掉时间戳字段）。
- 桩服务：ComfyUI、MiniMax、万相用本地 HTTP 桩；动画录制在未安装 Playwright 时跳过并断言中文指引。
- 性质测试（hypothesis）：原子写并发、锁顺序、来源判别联合往返、动画静态校验。
- 不能运行的验证（浏览器 UI 回归、真实厂商、真实 ComfyUI、真实模型质量）在交付说明中列出。
