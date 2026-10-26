# Design Document

M3 后期与第三方工具

## Overview

| 期 | 内容 | 关键改动点 |
|---|---|---|
| 一 | 后期管线、CLI、转场、字幕样式、文字层、品牌、HTML 叠加 | `workspace/post.py`、`workspace/ass.py`、render/compose 处理器、`cli_studio.py` |
| 二 | 多 final 阶段、导出、平台预设、封面、元数据、发布包 | `stages.py`、`preflight.py`、`plans.py`、新 final 处理器 |
| 三 | 曲库、节拍对齐、第三方工具 | `studio/music_library.py`、`studio/beats.py`、`studio/tools/`、invoke 处理器 |

设计约束：Signature_Compat；版本不可变；ffmpeg 经 `shared/media.py`；测试用确定性替身；UI 文案经 `messages.json`。

## 1. 后期管线（Post_Pipeline）

`media_nodes.compose` 的逐镜音轨对齐与 boundaries 计算保持不变，其后各步移入 `workspace/post.py`：

```text
compose(config, job_id, w, p, directory)
  shots   = collect_shots(...)            # 现有：clips / tracks / boundaries
  video   = join(clips, transitions)      # 硬切 concat -c copy；有 next 时 xfade 链
  audio   = concat(tracks) → mix_music
  video, audio, boundaries = bookend(...) # 片头片尾（偏移 boundaries）
  subtitles = build_srt / build_ass(...)  # 有样式、高亮或 ASS 图层时生成 ASS
  final   = finish(video, audio, ...)     # 水印 + 烧录 + 封装
```

`post.compose_payload(config, w, p)` 返回需要并入 compose 签名的键（仅在参与时出现）：`transitions`、`subtitle_style`、`overlays`、`brand`、`post_version`。

## 2. 转场

`studio.ShotTransition`：

```python
class TransitionEdge(StrictModel):
    kind: Literal["fade", "white", "zoom", "slide"]
    duration: float = Field(default=0.5, ge=0.1, le=2)

class OverlapTransition(StrictModel):
    kind: Literal["fade", "dissolve", "wipeleft", "wiperight", "slideleft", "slideright", "circleopen", "fadeblack"]
    duration: float = Field(default=0.5, ge=0.1, le=2)

class ShotTransition(StrictModel):
    enter: TransitionEdge | None = None
    leave: TransitionEdge | None = None
    next: OverlapTransition | None = None
```

- 生效规则与签名在 `workspace/transitions.py`。镜内转场在 picture 回调之后对画面追加一遍滤镜（`studio_render.apply_edges`：fade/white 用 `fade`，zoom 用 2 倍超采样 `zoompan` 加淡入淡出，slide 在黑底上 `overlay` 位移），帧数不变。render payload 仅在有生效转场时加 `transition` 键与 `TRANSITION_VERSION`。
- 重叠转场：render 读本镜 `next.duration`，帧数 `frames + h`（h = round(d·fps)）输出 `video.mp4`，`data.handle_frames = h`；`frames` 仍是配音对齐帧数。素材类从源继续取（`cut_shot` 的 trim 窗口延长，不足 tpad clone），图片与动画直接多渲染。
- compose：以重叠转场相连的一组镜头用一次 filter_complex xfade 链合成一段（`offset = 组内前面各镜 frames 之和 / fps`，过渡从切点开始），其余镜头仍 `-c copy`，各段再 concat 复制拼接；输出累计帧数仍为 Σframes。

## 3. 字幕与 ASS 文字层

`workspace/ass.py`：

- `SubtitleStyle`（position、font_size、primary、outline_color、outline、shadow、box、highlight）。内置样式 `classic`（等价现状）、`outline`、`box`、`highlight`。
- `build_ass(events, boundaries, style, overlays, canvas)`：PlayResX/Y 取渲染尺寸，字号按短边缩放；高亮词用 `{\c&H..&}` 包裹（转义 `{}`、`\`）；图层各自一个 Style（Title、LowerThird、Card），入场用 `\fad` 与 `\move`。
- 烧录：`subtitles=final.ass:fontsdir=…`（ASS 自带样式，不再 force_style）。`burn=false` 时 ASS 只含图层，字幕仍以 mov_text 软轨封装，视频需重编码。

分镜字段：`highlights: list[str] | None`（≤10，每个 ≤20 字），`overlays: list[Overlay] | None`（≤4）。

```python
class Overlay(StrictModel):
    engine: Literal["ass", "html"] = "ass"
    kind: Literal["title", "lower_third", "data_card", "custom"]
    title: str | None; subtitle: str | None
    items: list[CardItem] | None          # data_card
    brief: str | None                     # html custom
    start: float | None; end: float | None # 相对镜头，None = 整镜
    effect: Literal["none", "fade", "slide"] = "fade"
```

ASS 图层进 compose 签名；HTML 图层进 overlay 与 render 签名。

## 4. 品牌模板

- 存储仿文案模板：`studio/brands/*.yaml` 内置；`brand_templates` 表（metadata v4：id、name、body JSON、content_hash、version、created_at、updated_at）。
- `BrandTemplate`：`subtitle: SubtitleStyle`、`overlay_style`（字体、主色、强调色、背景）、`intro/outro: BrandClip | None`（asset sha、文件名）、`watermark: Watermark | None`（asset、position、opacity、scale、margin、include_bookends）。
- 品牌素材 `data_dir/brand/<sha256>.<ext>`，上传接口 `POST /api/studio/brand-assets`。
- 方案 `brand_id: str | None`（None = 无品牌，旧方案）、`brand_overrides: dict | None`（subtitle、watermark、intro、outro 任一）。解析函数 `resolve_brand(store, p)` 深合并，结果哈希进签名。
- API：`/api/studio/brands` 列表/获取/复制/PUT/DELETE（DELETE 扫描 workspace.json 引用）。

## 5. 片头片尾与水印

- 片头片尾规范化：`fit_filters` + fps + yuv420p + 48k 立体声（无音轨补静音），缓存于任务 `cache/bookend/<sha>-<w>x<h>-<fps>-<fit>.mp4`（可删可重建，不入版本）。
- 拼接后 boundaries 整体加 intro 时长（帧对齐）；BGM 只铺正文（在偏移之前混音，再与片头片尾音轨拼接）。
- 水印：`overlay` 位置表达式（四角 + margin），`format=rgba,colorchannelmixer=aa=opacity`，scale 按短边比例；`include_bookends=false` 时用 `enable='between(t,a,b)'`。

## 6. HTML 叠加

- 逐镜 `overlay` 阶段（插在 tts 之后、render 之前；仅当分镜含 html 图层）。数据产物 `overlay.html`；内置模板在 `studio/overlays/*.html` 以 JSON 注入数据；custom 由 `animator` 生成，提示词追加“背景必须透明”。
- `animation_capture.capture(..., transparent=True)`：`page.screenshot(omit_background=True)`，输出 `qtrle`（argb）MOV；render 用 `[0][1]overlay=0:0:format=auto` 合成，时间窗由图层起止决定。
- 钉住：`origin=manual` 时 `pinned()` 为真，同动画。

## 7. 多 final 阶段与变体

- `StageHandler.variants(config, w, p) -> list[str | None]`，缺省 `[None]`。节点键 `stage` 或 `stage:variant`，`shot_id` 字段复用为 variant（final 节点原本为 None）。
- `selected(p, sid)`：compose → `p["final"]`；其他 → `p.get("finals", {}).get(key)`。
- `plans.choose`：final 阶段统一写入；`restore` 仍只接受 compose。
- 预检 final 段：按注册顺序、变体展开，`draft` 写入占位 `@key`；下游 `specification` 通过 `final_input(p, key)` 取上游。
- 加载时 `p.setdefault("finals", {})`；`studio_models.VERSION_STAGES`、agent Literal 改为注册表生成。

## 8. 导出、平台与发布

- `export:<canvas>`：derive 用 compose 额外产出的 `clean.mp4`（无字幕烧录、无水印）与 boundaries；按新画幅 `fit_filters` → 重建 ASS（新画幅）→ 水印 → 封装。compose 仅在存在 derive 导出时产出 clean（签名加 `clean: true`）。
- rerender：子方案 `variant_of=<主方案 id>`、`options` 覆盖 canvas/fit；主方案 set_board 时同步 board 指针与 content。export 节点引用子方案的 `final`。
- 平台预设 `studio/platforms/*.yaml`，`PlatformPreset` 模型。
- `cover:<platform>`：来源 frame（时间点）、aigc（prompt，image_generator）、upload（inputs sha）；Pillow 排版；候选帧 API 用 opencv 拉普拉斯方差。
- `metadata:<platform>`：writer LLM，JSON 输出，校验修正；`origin=manual` 钉住。
- `package:<platform>`：zip 与清单 `manifest.json`（文件 sha256）。

## 9. 曲库与节拍

- `music_tracks` 表（v5）：id、sha256、suffix、title、artist、source、license、attribution、expires_at、duration、bpm、beats JSON、energy、brightness、moods JSON、tags JSON、analysis_status、created_at。文件 `data_dir/music/<sha>.<ext>`。
- 分析 `studio/beats.py`：ffmpeg 解码 22050Hz 单声道 f32 → numpy STFT（2048/512）→ 半波整流谱通量 → 自相关在 60–200 BPM 求峰 → 动态规划节拍跟踪（Ellis 2007）。纯 numpy。
- `ShotMusic.kind="library"` + `track_id`；music 阶段复制文件（签名含曲目 sha），`cue_identity` 对新字段 None 省略。
- 节拍对齐：compose 在 boundaries 计算后，对每个切点在 `[cut, cut+tolerance]` 内找最近节拍，延迟量 δ（帧对齐）≤ 本镜 handle；画面多取 δ 帧 handle，配音段补 δ 静音。render 在 `beat_align` 开启时 handle 至少为 tolerance。
- 实现说明：曲目分析在 API 线程池中同步完成（几分钟的曲目约 1 秒），不经 `BackgroundTaskRunner`；情绪标签与推荐的文案模型不可用时退回确定性推断（节奏特征 / 关键词）。只对同一背景音乐段落内的切点对齐；有重叠转场（next）或出场转场（leave）的切点不推迟（leave 镜不留余量）。节拍取自段落音乐文件的分析，段落循环时按曲长周期延伸。xfade 时长取转场设置而非余量；未被 xfade 消耗的余量在拼接时裁掉（只重编码这几镜）。

## 10. 第三方工具

```text
studio/tools/
  registry.py   ToolSource 模型、tool_sources 表 CRUD、白名单检查
  http.py       HttpAdapter：manifest（submit/poll/result 路径、JSONPath 取产物）
  mcp.py        McpAdapter：stdio_client / streamablehttp_client + ClientSession
  artifacts.py  下载（主机白名单、max_bytes、流式）、类型校验（ffprobe / Pillow）
```

- `invoke` 阶段（画面阶段，加入 `PICTURE_STAGES`）：payload = {tool_id, tool_hash, arguments, uploads: [sha]}；run 调适配器 → 产物写入版本 `artifact.<ext>`。
- 视频产物：select 走 `_file_alignment`（`{version, file: "artifact"}`）；图片产物：render 走 `still_shot`。
- `tool_sources` 表（v6）。管理 agent 工具 `risk=danger`；`mcp-stdio` 登记不经 MCP 暴露，其余经 MCP 暴露。
- 实现说明（密钥）：助手的 `secret://` 引用是会话内临时引用（有 TTL），不能落库；因此工具源把密钥值存在只写的 `secret` 列（JSON，接口只返回名称与是否已填写，不含密钥的备份导出清空），连接设置的请求头 / 环境变量用 `secret:名称` 引用。助手登记时 `secrets` 只接受 `secret://` 引用，由回环客户端在发送前解引用。上传素材以 `uploads: [{name, mime, sha256, base64}]` 随参数发送（HTTP 请求体与 MCP 参数相同）。

## 11. 入口（Web、CLI、agent、MCP）

所有能力先实现 REST，其余三个入口都是 REST 的调用方：页面经 `shared/api.js`，CLI 与 agent 经 `agent/loopback.MindexClient`（CLI 在进程内调用，`--url` 时改走 HTTP），MCP 由 `mcp_server` 从 agent 注册表自动导出。

| 能力 | Web 页面 | CLI `mindex studio …` | agent 工具（MCP 同名导出） |
|---|---|---|---|
| 基础 | 创作页（既有） | `jobs` `plans` `preflight` `build` `status` `download` | 既有（`list_jobs`、`build_plan` 等） |
| 转场、字幕样式、高亮、ASS 文字层 | 分镜详情“后期”面板；方案参数 | `shot set --transition-* --highlight --overlay` | `edit_shots` 扩展字段 |
| HTML 叠加层 | 分镜详情叠加层源码编辑 | `overlay show/edit` | `get_overlay_source`、`edit_overlay_source` |
| 品牌模板 | 设置页“品牌模板”；方案选择 | `brand list/show/copy/update/delete/upload` | `list_brands`、`get_brand`、`copy_brand`、`update_brand`、`delete_brand` |
| 多画幅导出 | 创作页“导出”面板 | `export` | `set_exports`、`build_exports` |
| 平台预设 | 发布面板 | `platforms` | `list_platforms` |
| 封面 | 发布面板封面编辑 | `cover candidates/set` | `cover_candidates`、`set_cover` |
| 发布元数据 | 发布面板元数据编辑 | `metadata show/edit` | `get_publish_metadata`、`edit_publish_metadata` |
| 发布包 | 发布面板下载 | `publish` | `build_publish_package` |
| 曲库 | 素材管理页“音乐库”标签（`/admin/music`）；音乐提示点选曲 | `music import/list/analyze/recommend` | `list_music_tracks`、`analyze_music_track`、`recommend_music` |
| 第三方工具 | 设置页“第三方工具”配置组（`/settings#tools`）；分镜来源选择 | `tools list/test/add` | `list_tool_sources`、`test_tool_source`、`save_tool_source`（danger；mcp-stdio 登记不导出） |

`tests/test_m3_entrypoints.py` 以此表为数据，断言 REST 路由、typer 命令、`build_registry()` 工具名、MCP 导出列表与页面 HTML 中的元素 id 都存在。

## 12. Web 与桌面兼容

- 两端共用前端与后端，业务只在 REST；页面不使用 `window.MindexDesktop` 做业务分支。
- 包数据：`mindex.studio` package-data 增加 `brands/*.yaml`、`platforms/*.yaml`、`overlays/*.html`；`check-desktop-sidecar.py` 自动核对。
- 新模块用显式版本常量（`POST_VERSION`、`TRANSITION_VERSION` 等），不对 `__file__` 取哈希，因此无需加入 `SOURCE_MODULES`。
- 外部进程：ffmpeg 经 `shared/media.command`；MCP stdio 使用登记的绝对路径与环境变量，不依赖 GUI PATH。
- 字体：封面排版复用 `studio.subtitle.font_file`/`font_family` 的解析（桌面随包 CJK 字体），缺失时报错提示配置。
- 下载：zip、封面、导出成片接口返回 `Content-Disposition: attachment`，桌面由 `will-download` 处理。
- 可选组件：HTML 叠加层录制在桌面依赖可选 Chromium 组件，预检给出安装提示。
- 冒烟：每期在 `check-desktop-sidecar.py` 增加一步（第一期：带转场、ASS 文字层与水印的渲染；第二期：发布包；第三期：曲目分析与 stub HTTP 工具）。

## Testing Strategy

- 签名快照：`test_studio_signature_snapshot.py` 保持通过（Signature_Compat）。
- 媒体测试用小尺寸（320×180、几秒）合成素材，断言帧数、时长、像素。
- LLM、生图、MCP、HTTP 均用确定性替身或进程内 stub 服务。
- 节拍分析用合成节拍器音频。
