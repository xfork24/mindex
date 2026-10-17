# 素材后台指南

后台地址 <http://127.0.0.1:8000/admin>。它**直接读 SQLite、不依赖活动索引或 GPU 服务**——素材切片完成即可管理，未嵌入也能播放/下载/编辑。

## 页面功能

- 按素材**显示名称或原文件名**搜索；分页每页 48 条（最大 100）。
- 查看镜头与精细片段：点击镜头查看其子片段，片段可返回并定位父镜头；显示时间顺序位置。
- 播放、下载 MP4（`download=true` 时按显示名+时间命名）；缩略图。
- 编辑显示名称、备注、收藏、隐藏；按可见性（visible/hidden/all）与收藏筛选。
- 镜头与精细片段的操作依次为“详情、备注、收藏、隐藏”。详情独立显示 ASR、字幕轨道、LLM 和人物处理信息；人物小节列出已处理/未处理的原片区间、已接受/待确认/已驳回证据数及关联人物。覆盖分为未处理、部分处理、处理完成；有覆盖但未识别出人物仍属于处理完成。
- 详情只读取已有数据及可缓存的字幕文本，无需在线模型或已发布索引，不启动 ASR、LLM 或人物推理。

## 整理语义

- **改名**：显示名实时覆盖检索结果显示；**不修改**文件、稳定 ID 或已有嵌入（索引快照不含整理信息）。
- **隐藏**：立即退出检索召回——素材隐藏影响全部子片段，镜头隐藏影响其精细片段；**恢复上级时保留片段自身的隐藏状态**。选「已隐藏」或「全部」可找回内容。恢复不需要重建向量。
- **收藏**：仅筛选标记，**不影响相似度排名**。
- 首版不含网页上传、物理删除、时间边界编辑；素材导入仍走 CLI（`mindex prepare`）。
- 整理数据存 SQLite 独立表（`source_curation`/`segment_curation`），旧库启动自动补表，旧素材默认未隐藏未收藏。

质量评测期间不要修改整理状态——e2e 实验把 curation 哈希冻结进 `run.json`，变化会使后续 phase 拒绝执行。

## 管理 API

前缀 `/api/admin`，完整定义见 `/docs`。

| 接口 | 说明 |
|---|---|
| `GET /sources?q=&visibility=visible\|hidden\|all&favorite_only=false&page=1&page_size=48` | 名称搜索（原名+显示名，不区分大小写子串）、筛选、分页 |
| `GET /sources/{id}` | 媒体信息、镜头/片段计数、缺失文件数、最近 5 条处理记录 |
| `PATCH /sources/{id}` | 修改整理字段 |
| `GET /sources/{id}/segments?kind=shot\|moment&parent_id=&visibility=&favorite_only=&page=&page_size=` | 片段列表；`parent_id` 仅用于 shot→moment |
| `GET /segments/{id}` | 片段详情：归属、时间位置、父子、隐藏继承 |
| `GET /segments/{id}/details` | ASR、字幕轨道、LLM 与人物处理覆盖/证据统计，按当前片段时间范围裁切 |
| `PATCH /segments/{id}` | 修改整理字段 |
| `GET /segments/{id}/media[?download=true]` | 未嵌入片段的播放/下载，支持 Range/HEAD |
| `GET /segments/{id}/thumbnail` | 缩略图 |

PATCH 请求体（`extra="forbid"`，未知字段 422）：

```json
{"display_name": "新的显示名", "notes": "备注", "favorite": true, "hidden": false}
```

- `display_name` 最长 200 字符，**空字符串恢复默认名**（存 NULL）；`notes` 最长 4000。
- 至少提供一个字段；素材不存在返回 404；文件缺失的媒体返回 404 并提示用 `split` 修复。

示例：

```bash
curl -s 'http://127.0.0.1:8000/api/admin/sources?q=trailer&page_size=10'
curl -s -X PATCH http://127.0.0.1:8000/api/admin/sources/SOURCE_ID \
  -H 'Content-Type: application/json' -d '{"display_name": "预告片", "favorite": true}'
```

## 与检索索引的关系

检索 API（`/api/segments/{id}` 等）读的是**活动索引的冻结快照**；后台 API 读的是**当前素材库**。两者通过稳定片段 ID 关联；后台的改名/隐藏在检索结果上以叠加层生效（`Library.decorate`），不触碰向量。
