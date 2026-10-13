# 检索机制详解

检索由 `search.py` 的 `SearchEngine` 实现。在线查询**只调用嵌入服务**（WeMM 文字/视频向量），没有 Qwen 实时描述、没有在线大模型重排。**分数不是匹配概率**——它只是排名依据。

## 文字查询：五路召回 + 加权 RRF

`POST /api/search/text`（JSON：`text`、`granularity`、`limit`、`filters`）或 `mindex search`。流程：

```
查询文字 ──WeMM──→ 一个查询向量
                     ├── 视频表示向量召回（画面）
                     ├── 动作场景描述向量召回（description）
                     ├── 情绪镜头描述向量召回（style）
                     ├── 对白向量召回（dialogue）
                     └── jieba BM25 词法召回（lexical）
                              ↓ 每路取前 100 个候选（search.candidates）
                     加权 RRF 融合（k=60，同分并列排名）
                              ↓
                     同源时间 IoU > 0.5 去重，保留高排名
                              ↓
                     截取 limit 条（默认 20，上限 50）
```

要点：

- **候选集先过滤**：只看活动索引中该 `granularity` 的片段；hidden（含父级继承）在召回前排除；时长/来源/标签筛选也在此之前。
- **多窗口片段在同一路只计一次**：长镜头/场景有多个嵌入窗口时，向量召回扩展候选直到凑够不同片段，同一路内每片段保留最佳命中。
- **RRF 排名融合**：片段得分 = Σ 路权重 /(60 + 该片段在该路的排名)。默认权重 video/description/style = 1，dialogue/lexical = 0.5（`search.weights`）。相同距离的候选取并列排名，避免无区分度支路引入任意排序。权重由开发集确定后冻结，测试集不调参。
- **响应包含命中明细**：每个结果的 `matches` 列出命中支路、排名与余弦相似度；`score_type: weighted_rrf`；`eligible_segments` 是过滤后的候选池大小。
- **描述三视图**：enriched 分析把结构化 Annotation 拼成三个文本视图——description（自然描述+动作/场景/人物/对象）、style（情绪/氛围/景别/角度/镜头运动）、dialogue（对白原文），分别嵌入。

### BM25 细节

jieba `cut_for_search` 分词（过滤无中文/字母数字的 token），BM25 打分（k1=1.5、b=0.75 变体），文档 = 各片段三个视图的拼接（`lexical_exclude` 可排除部分字段，评测消融用）。

## 视频查询

`POST /api/search/video`（multipart：`file`、`granularity`、`limit`、JSON 字符串 `filters`）。限制**最长 30 秒、100MB**。

- 上传文件落临时目录，按与建库相同的 4s/3s 参数拆窗口，逐窗嵌入（单片 ≤4 秒时不裁剪直接嵌入）。
- **只检索视频画面表示**，不做在线情绪/剧情推断。
- 多窗口**等权融合**（每窗权重 1/窗口数，窗口数量不影响分数量级），再走同一套 IoU 去重。
- 查询视频**不入库**，临时文件成功或失败后自动清理。

## 粒度与筛选

| 参数 | 取值 | 说明 |
|---|---|---|
| `granularity` | `moment`（默认）/ `shot` / `scene` | 精细片段 / 完整镜头 / 语义场景 |
| `limit` | 1–50，默认 20 | |
| `filters.source_id` | str | 只在某素材内检索 |
| `filters.min_duration` / `max_duration` | float 秒 | 闭区间 |
| `filters.scene` / `emotion` / `shot_size` | str | 标签**不区分大小写的子串匹配**，多字段取交集；visual 模式下分析字段为空，标签筛选自然无结果 |

文字长度 1–4000 字符，空白查询被拒绝（422）。

## 就绪与身份校验

查询前依次校验：已有活动索引（否则明确报"尚未发布索引"）、索引状态 ready、嵌入服务空间身份与索引一致（否则 503）、视频预处理配置一致。服务不可用时页面/API 显示明确未就绪状态，不用模拟向量冒充结果。

## 结果与播放

每个结果带缩略图/媒体/上下文 URL、来源、原片时间、描述、标签、命中支路与分数。媒体支持 HTTP Range；`/api/context/{id}` 按需转码片段前后各 3 秒为浏览器可播放 MP4 并缓存。

## API 示例

```bash
# 文字查询
curl -s http://127.0.0.1:8000/api/search/text -H 'Content-Type: application/json' -d '{
  "text": "一个人在雨夜街道上奔跑",
  "granularity": "moment",
  "limit": 10,
  "filters": {"min_duration": 2, "max_duration": 6, "emotion": "紧张"}
}'

# 视频查询
curl -s http://127.0.0.1:8000/api/search/video -F file=@query.mp4 -F granularity=moment

# 状态（含服务健康与空间身份比对）
curl -s 'http://127.0.0.1:8000/api/status?health=true'
```

## 评测消融

`mindex evaluate` 在同一候选集上比较 profile：`video_only`、`description_only`、`fusion`（配置权重）、`no_dialogue`、`no_style`（同时从 BM25 文档移除该字段，避免词法泄漏）。视频查询只进 `video_only`。指标 Hit@1/5、MRR@10、按相关组计的 nDCG@10，详见 [e2e-framework.md](e2e-framework.md)。
