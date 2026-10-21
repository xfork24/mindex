# 人物与参考图交互改造 + 全局可搜索多选框

## Context

四项需求：①「人物与参考图」演员卡展开改为**整行展开**（参考创作页分镜工作台的行内详情交互），删除顶部伸缩按钮；②演员卡显示中文名+外文名、第二行显示角色数；③UI 提供演员/角色删除（有参考图或已接受出镜证据时拒绝，用户已确认该策略）；④所有涉及作品/演员/角色的**多选**列表框支持实时搜索过滤，条目默认按字典序排列。

关键现状（探索结论）：
- 演员卡 `actorCard`（persons.js:81-93）：`button.person-toggle` 标题（`▸ 名字 · N 个角色`）+ 角色以 `div.role-grid`（112px 迷你格）嵌在**卡片内部**展开；点击整目录全量重渲染（`renderCatalogue`）。
- 创作页参考模式（studio.js:587-720）：`tr 配对行 + tr 详情行`，详情行单 `td colspan`，`.shot-detail-panel{position:sticky;left:0;width:var(--matrix-viewport-width)}`，仅展开时插入 DOM；CSS Grid 场景等价物是 `grid-column:1/-1` 的兄弟行。
- 实体删除后端**没有**（persons/person_api.py 路由表无 DELETE /entities）；唯一守卫先例：`validate_update`（persons/persons.py:220-241）拒绝删除仍有已接受出镜证据的作品关联；参考图删除是裸 `DELETE FROM person_references`。
- 多选框清单（全部 `name · shortid` 标签、rowid 序、无搜索）：index `actor-select/role-select`（app.js fillOptions:222）；persons `filter-work/actor/role`、`entity-work/entity-actors`（persons.js options():18）；studio 召回演员/角色（studio.js fillAllSelect:22）。各页面 JS 独立、无共享脚本；`全部选择` 逻辑已三处重复。
- 演员别名即外文名（导入时 foreign_name 存为 alias）。

## 改动

### 1. 整行展开（persons.js `renderCatalogue`/`actorCard` + persons.css）

- `renderCatalogue` 循环改为：`list.append(actorCard(actor)); if(state.expanded.has(actor.id)) list.append(roleRow(actor));`
- `roleRow(actor)`：`div.actor-detail-row`（`grid-column:1/-1`），内容 = 标题行（`{name} 的 {N} 个角色` + 「收起」按钮）+ `.role-grid-wide`（`repeat(auto-fill,minmax(190px,1fr))`，比现在 112px 宽）放 `roleCard(role)`。
- 演员卡可点击区域 = 整卡：卡片 `click` 监听里 `event.target.closest('button,input,select,textarea,label,a,form')` 命中则放行（按钮/上传/参考图操作不受影响，参考 studio.js:575 忽略交互子元素的写法）；点击翻转 `state.expanded` 后重渲染（沿用现有全量重渲染语义，卡片轻量无媒体重载问题；参考图 `img.loading=lazy` 已有）。
- 删除 `.person-toggle` 按钮与 persons.css:7 的样式；卡内 `.role-grid`（112px）随展开外移而废弃。`aria-expanded` 移到卡片 article 上。
- 同步 persons.html:7 的说明文案（点击演员卡片整行展开角色）。

### 2. 中文名+外文名、角色数第二行（persons.js `actorCard`）

- 标题：`<h3>{actor.name}</h3>`，有别名时同行追加 `<span class="foreign">{aliases.join(' / ')}</span>`（外文名/别名）。
- 第二行：`<p class="muted">{roles.length} 个角色</p>`（`roles=rolesOf(actor)`，过滤感知，与展开行内容一致）。

### 3. 演员删除（persons.js + persons/person_api.py + persons/persons.py）

- 后端 `Persons.delete_entity(identifier)` + `DELETE /api/persons/entities/{identifier}`：
  - 仅 actor/role（work 返回「作品暂不支持删除」）。
  - 守卫：`person_references` 中 `actor_id=?`（含角色配对引用，role 同理 `role_id=?`）存在 → `ValueError("先移除 N 张参考图")`；`person_evidence` 中 `status='accepted'` 且 `actor_id/role_id=?` 存在 → `ValueError("仍有已接受出镜证据；请先重新核对" )`（与 validate_update 同款措辞风格）。
  - actor 删除时联动清理配对：所有 `role.actor_ids` 含该演员的角色重存（去掉该 id，`INSERT OR REPLACE`），避免悬挂引用。
  - 裸 `DELETE FROM person_entities WHERE id=?`（同 references 删除先例）。
- 前端：演员卡与角色卡各加小号「删除」按钮（红字 quiet 风格，stopPropagation）；成功后 `refresh()` + notice；失败显示后端中文错误（run() 已有 notice）。
- 测试（tests/test_persons.py 新增）：空演员删除成功；有参考图 422；有 accepted 证据 422；带配对的演员删除后角色 `actor_ids` 清理；角色删除守卫两条。

### 4. 可搜索多选框（新共享脚本 frontend/src/shared/multiselect.js + 各页接入）

- 新文件 `multiselect.js`，index/persons/studio 三页 `<script defer>` 引入（放在各页主脚本之前；加 `?v=20261018-multisearch`）：
  - `MindexMultiSearch.sort(entries)`：`a.name.localeCompare(b.name,'zh') || a.id.localeCompare(b.id)`。
  - `MindexMultiSearch.attach(select)`：幂等（`select.dataset.searchable`）；在 select 前插入 `<input type="search" class="multi-search" placeholder="搜索…">`；`input` 事件实时 `option.hidden = query && o.value!==ALL_VALUE && !o.text.includes(query)`（`全部选择` 行恒可见）；查询值存 `select.dataset.query`，供重填后重放。
  - `MindexMultiSearch.refilter(select)`：按 `dataset.query` 对当前 options 重放过滤（fill 后调用）。
- 接入点（fill 后 sort + attach/refilter）：
  - app.js `fillOptions`（:222）：entries 排序后渲染，`attach` 一次；`fillRoleOptions` 重填后 `refilter`。
  - persons.js `options()`（:18）：multiple 分支排序（single select 不动，需求只覆盖多选）+ attach/refilter；`fillFilters` 重填后 refilter。
  - studio.js `fillAllSelect`（:22）：排序 + attach/refilter。
- 样式：style.css（全页共享）加 `.multi-search{width:100%;margin-bottom:6px;padding:6px 9px;border:1px solid var(--line);border-radius:6px;font-size:12px}`，`select[multiple]{min-width:0}`。
- 版本号：index/persons/studio 三页 js+css 统一升 `?v=20261018-multisearch`。

### 5. 出镜审核批量核对（追加需求）

- 每个分镜卡片加勾选框（`state.reviewPicked` 按 `source|shot` 记录）；审核工具行加「批量核对（N）」按钮，选中 ≥1 时可用。
- 复用 `#review-dialog`，批量模式下隐藏图片与视频（`hidden`），标题显示分镜数；`review-choices` 由全部所选分镜的作品角色并集构建（建议项＝任一分镜的候选，预勾选＝所有分镜均已接受的身份交集）。
- 保存/排除对每个所选分镜逐一 `POST /api/persons/review/shot`（同一份 selections），完成后清空选择、刷新列表并汇总提示「已批量核对 N 个分镜」。后端无改动。

### 6. 收尾

- **计划副本写入 `plans/PERSON_LIB_UI_PLAN_v2.md`**（批准后第一步）。
- `persons_ui.cjs` 假体补 `multiselect.js` 引入的 DOM 假设（若 persons.js 顶层调用 attach —— 只在 fill 时调用，假体不触发则无需改；跑一遍确认）。
- `docs/persons.md` 更新（整行展开、删除守卫、搜索多选、批量核对）。

## 不改

- 检索页/创作页的多选提交语义、`全部选择` 行为；单选框（capture/import 条目内 actor/role 下拉）不加搜索；published 快照与检索链路。

## 验证

1. `uv run pytest tests/test_persons.py -q`（新增删除用例全过）。
2. `node --check` 三个 js；`node tests/persons_ui.cjs`、`node tests/studio_window_ui.cjs`。
3. **临时端口实例**（如 8001 + /tmp 数据目录，不碰用户 8000/4050）浏览器实测：
   - 人物与参考图：点演员卡 → 整行展开宽角色格；再点收起；顶部无伸缩按钮；标题中文名+外文名、下行 N 个角色；
   - 删除：空演员删除成功；有参考图的删除被拒并提示；
   - 三页多选框：输入即过滤、清空恢复、`全部选择` 恒在、条目按字典序；重填（刷新/选演员过滤角色）后搜索框与过滤状态保持。
