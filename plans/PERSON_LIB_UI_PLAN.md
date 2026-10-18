# 演员表导入：多图全流程改造

## Context

当前演员表导入按「一次展示一张原图」设计：图片切换靠 `import-image` 下拉，条目列在右侧单列，每条目带「已确认照片框仅一个人物」复选框（前后端双重门禁）。用户实际要传整部戏的 cast 截图（一次多张，或分多次各一张），期望：自动拆分覆盖每一张图、条目统一列在图片下方（每行两个）、去掉单人确认复选框、保存/确认始终作用于全部图片、重复条目静默跳过并汇总提示、页面默认列出所有历史 cast 图片。

关键现状（探索结论）：
- 草稿模型已支持多图：`person_imports.data` 的 `images[]` + 条目 `image_id`（persons.py `create_import`/`get_import`）；已有多图草稿实例。
- `POST /imports/{id}/parse` 把全部图塞进**一次** LLM 调用（person_api.py:50-107 `parse_cast`）：原子失败、模型常只返回首图条目——这是「只拆当前那张」观感的根源。
- `single_person` 门禁共 4 处：前端复选框 persons.js:200、拖框重置 :206、客户端校验 :197、后端 confirm 校验 persons.py:415-416；LLM 被强制写 false（person_api.py:63,102）。
- 已有可复用的去重：`add_reference`（persons.py:315-327）按 `(actor_id, role_id, digest)` 内容寻址复用参考图；同名演员/同作品同名角色在 confirm 时自动复用实体。
- 历史图片无需新存储：每张图已有 URL 路由 `GET /imports/{id}/images/{image_id}`。

已与用户确认的决策：历史图片全部列出（含已确认）；自动拆分一次点按逐张执行、显示每张进度/失败、可单张重试；重复条目静默跳过 + 汇总提示。

## 改动

### 1. 后端 `src/mindex/person_api.py`

- **parse 逐张化**：`parse_cast(config, image, draft_meta)` 改为单图函数（提示词去掉 single_person 相关句）；`POST /imports/{id}/parse` 接受可选 body `{image_ids: [...]}`，默认 = 该草稿全部图。服务端逐张调用、逐张聚合：目标图的**现有条目被替换**（重新拆分覆盖该图条目），单张失败只记 warning 不影响其他图。每图结果写入草稿新字段 `parse_state: {image_id: {"status": "parsed"|"failed", "error"?: str}}`。
- **追加图片**：`POST /imports/{id}/images`（multipart `files`，1–12 张，仅 draft 状态）。复用 `save_upload` + `Persons.image(raw,"imports")` 内容寻址；digest 已在草稿中的图跳过并记 warning（「第 N 张已存在，忽略」），返回更新后的草稿。
- **历史列表**：`GET /imports` → `[{id, status, work_id, entry_count, images:[{id,url,width,height}]}]`，按 rowid 倒序（表无时间列）。URL 复用现有图片路由。
- upload_import / parse 保留原有错误兜底（LLM 挂了不阻塞手工流程）。

### 2. 后端 `src/mindex/persons.py`

- `confirm_import`：
  - 删除 :415-416 的 single_person 门禁（字段保留在 `CastEntry` 中不启用，避免旧草稿 StrictModel 校验失败——零兼容风险）。
  - **批内去重**：确认前对 `actor_id is None and role_id is None` 的条目按 `(actor_name.strip(), role_name.strip())` 分组，保留首个有 bbox 的（稳定排序），其余计入 `skipped_duplicates`，不创建实体/参考图。
  - **重复导入识别**：`add_reference` 前查 `(actor_id, role_id, digest)` 是否已存在（复用 :315-319 的查询），已存在则计入 `ignored_existing`（参考图 id 复用、不新增行——现行为本就如此，只加计数）。
  - 返回体扩为 `{confirmed, skipped_duplicates: [{actor_name, role_name}], ignored_existing: N}`。
- 新增 `append_images(identifier, raws)` 供上面的追加端点调用（digest 去重逻辑在此）。

### 3. 前端 `src/mindex/static/persons.html`

`#cast` 面板重构为：
```
上传表单（多选）+ 恢复草稿 ID（保留）
<div id="import-gallery">：全部历史 cast 图缩略图（状态角标 草稿/已导入），点击加载
<div id="import-tools" hidden>：
  作品选择 / 新建作品名
  [自动拆分演员表] [手动增加条目] [保存草稿] [确认导入人物库]
  #import-warnings
  <div id="import-originals">：每图一个块（标题行：原图 N · W×H · 拆分状态 · [重新拆分]；下方 canvas）
  <div id="import-entries">：条目卡网格
```
删除 `import-image` 下拉和 `.import-layout` 两栏结构。

### 4. 前端 `src/mindex/static/persons.js`

- `displayDraft`：渲染所有原图（每图 canvas，`loadImage`→`loadImages` 多图 Image 缓存）；条目区移到图下方；`legacy` 剥离列表加 `single_person`；`status==='confirmed'` 时禁用 parse/add/save/confirm（只读查看）。
- `renderEntries`：删复选框；每条目卡加小型「所属原图」select（`newEntry` 默认第一张）；「选择照片框」按钮改为滚动到该条目所属 canvas 并高亮。
- `draw`/指针事件：按 image_id 找对应 canvas（`data-image` 属性），其余逻辑不变（拖框时同步 `entry.image_id` 为所在 canvas 的图）。
- **自动拆分**：客户端循环逐图调 `POST parse {image_ids:[id]}`（天然逐张进度）；主按钮只拆 `parse_state` 缺失或 failed 的图；每图「重新拆分」按钮强制重拆单张。每张完成后更新该图状态徽标。
- **上传**：`state.draft` 存在且为 draft → `POST /imports/{id}/images` 追加；否则 `POST /imports` 新建。提示语说明去向。
- **确认**：读响应的 `skipped_duplicates`/`ignored_existing`，notice 汇总「导入 N 条 · 跳过 M 条重复（演员/角色）· K 条已存在忽略」。
- **历史列表**：`refresh()` 时并行 `GET /imports`，渲染缩略图网格；点击 `displayDraft`。

### 5. `src/mindex/static/persons.css`

- `#import-entries{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}`，<800px 单列；`#import-originals` 块纵向堆叠，canvas `max-width:100%`；`#import-gallery` 缩略图 flex 网格（约 96px 高，object-fit:cover，状态角标）；删除 `.import-layout`。
- persons.html 引用版本号 persons.js/css 升 `?v=20261017-multicast`。

### 6. 测试与文档

- `tests/test_persons.py`：
  - 原单人门禁测试（:239-249）改为「合照 bbox 无确认也能导入」。
  - 新增：追加图片端点（含同 digest 跳过警告）、`GET /imports` 列表、confirm 批内去重（同名两条 → 一条实体+参考图，skipped_duplicates 返回）、重复导入 ignored_existing 计数（确认两次同 bbox 数据）。
  - parse 逐张：monkeypatch `person_api.parse_cast`，验证两图各自调用、单图失败只影响该图。
- `docs/persons.md`：导入流程描述同步（多图、逐张拆分、去重规则、历史列表）。

## 不改

- 参考图/实体的同名复用与合并语义、事务结构、`add_reference` 内容寻址去重本身。
- `CastEntry.single_person` 字段（保留为死字段换兼容）；LLM 提示词其余约束（原图坐标、忽略小头像等）。

## 验证

1. `uv run pytest tests/test_persons.py -q`；`node tests/persons_ui.cjs`（DOM 假体补 `import-originals` 等新节点后应仍过）。
2. **重启 `uv run mindex serve`**（后端改动不热载），Chrome MCP 实测：
   - 上传 2 张 cast 图（可用 `data/persons/imports/*.png` 现成文件）→ 两图并排出现、生成一个草稿；
   - 再传 1 张 → 追加进同一草稿；重复传同内容 → 提示已存在；
   - 点「自动拆分」→ 逐张进度（LLM 4040 未起时每张显示 failed + 重新拆分按钮，手工流程不受阻）；
   - 手动拖框/改条目（无复选框）→ 条目区在图片下方、每行两个；
   - 保存草稿 → 刷新页面 → 历史缩略图默认列出、点击恢复；
   - 造两条同名未关联条目确认 → notice 汇总跳过数，人物库无重复实体/参考图。
