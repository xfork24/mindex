# 人物库、出镜检索与镜头详情

实现范围与验收要求见 [PERSON_RECO_PLAN.md](../plans/PERSON_RECO_PLAN.md)。人物库入口 `/persons`，素材管理镜头详情入口 `/admin`。人物模型不参与既有 WeMM 三渠道索引构建。

## 先登记，再处理和发布

1. 演员表导入是人物的主要创建入口（页面第一位）：确认时同名演员与同作品同名角色自动复用，不存在的自动创建，无需先手动登记。作品在“演员表导入”中创建（同页“或新建作品名”），在“人物与参考图”中只作为筛选条件出现，也可手动登记演员（全局）和角色（归属一部或多部作品，跨电影角色共用同一身份）。演员与角色是不同身份，姓名歧义提示会标注身份类型。角色可有多个演员，演员可在多个作品出演多个角色。人物库身份与参考图均为全局管理，不绑定具体素材；素材仅通过“所属作品”参与角色关系。手动新建允许重名身份；演员表仅在已有唯一匹配时复用，多名同名演员须明确选择。检索遇到歧义必须选择具体 ID。
2. 在“处理与发布”中核对素材所属作品。每个素材默认有自己的作品，可关联到同一个已有作品。变更作品会清除草稿中的旧出镜配对与覆盖，发布版本仍可回退，重新处理后再发布。
3. “人物与参考图”默认只列出演员：点击演员卡片展开该演员的全部角色，再点收起。作品、演员、角色三个筛选对演员与角色同时生效；启用角色或作品筛选后，没有任何匹配角色的演员整张隐藏。演员卡片与角色卡片各自独立管理参考图：角色的照片配给角色本身（不与演员照片混用），可分别追加与移除；点击照片下的“修改配对”同时编辑姓名、别名及配对关系，角色卡片编辑作品与演员关系。未关联演员的角色也会单独列出，可补充出演演员。角色参考图同样参与人脸匹配，每张照片只归属照片中的一名演员；角色有多名出演演员时必须明确选择，匹配仍检查不同演员之间的分数差。角色关系变更后，不再属于该角色的演员照片会在任务报告中提示重新配对。“从原片选取参考”可同时指定演员与角色，定位时间后预览该帧并在截图上拖拽框选人物；截图始终配给所选角色，不框选则保存整幅画面。移除参考后重新处理已有区间，重新发布；视频特征不会重算。
4. 演员表可上传多张图，自动拆分只产生草稿。选择条目并在原图拖框（坐标为原图像素），也可用 x1/y1/x2/y2 精确修改。条目只含演员名与角色名：同一人物的不同时期（如老年叶淑柔）直接写成完整角色名，不使用阶段字段。同名演员与同作品同名角色自动复用并合并演员表；跨电影同一角色可在条目中显式关联。导入作品可下拉选择，也可直接填写新作品名创建。检查配对预览，删除重复头像和代表作干扰，确认照片只含一人。未确认不会写入人物库。LLM 不可用时直接手动增加条目；角色名缺失允许只建演员。
5. 先用约三分钟区间处理。任务请求立即返回 ID，按镜头保存检查点。任务失败或服务重启后可重试；追加照片仅重新提取新照片并重新配对已有视频特征。改名后重新发布即可，不需要重跑推理。
6. 核对候选或纠错：审核列表按镜头分组，显示完整证据和状态计数。状态按分镜归类而非逐帧：任一帧被自动接受或人工确认，整个分镜即为“已接受”；只有完全没有已确认帧的分镜才仍列在“待确认”；整镜证据均被驳回时为“已排除”。“已接受”不代表整个镜头已完成人工审核，卡内仍显示剩余候选。播放完整镜头后勾选演员及可选角色，再保存；也可排除此镜头的全部人物证据。勾选结果是人工确认的整个镜头出镜区间，其余旧证据驳回留档，该分镜随即不再出现在待确认列表。重新处理或更换模型空间不会覆盖此人工结论；此操作不会修改已发布版本。
7. 点击“发布当前人物证据”，生成不可变人物快照及绑定内容索引的检索发布记录，并在单个事务内切换。人物服务离线不影响已发布证据查询。回退可以选择发布组合；既有内容索引回退恢复该索引最近的对应组合。

角色检索支持模糊识别：输入“叶淑柔”会同时召回名为“老年叶淑柔”“少年叶淑柔”等以该名字结尾的角色（联合命中，任一变体出镜即算），不会因变体同名报歧义；输入完整名“老年叶淑柔”则只召回该角色（最长名优先）。演员名与角色名同名、或无关身份同名的歧义仍必须显式选择。

## 独立模型服务

应用侧安装解码与 ByteTrack：

```sh
uv sync --extra person --extra dev
./scripts/setup-person-service.sh
```

模型环境为 `.venv-person`。SDK 固定到 InsightFace commit `81929474ec02e54e3655f6841bced800009c592d`，使用 [`PersonAnalysis` 官方接口](https://github.com/deepinsight/insightface/blob/81929474ec02e54e3655f6841bced800009c592d/python-package/docs/person_analysis.md)。模型包须自行安装完整的 `cheetah_l` V2 到 `~/.insightface/models/cheetah_l/` 或配置的 `person.model_root/models/cheetah_l/`。服务检查四类任务、所有模型文件 SHA-256 与 manifest 摘要；不自动下载或修复模型。没有模型包时启动明确失败。

```sh
./scripts/run-person-service.sh
curl http://127.0.0.1:4050/health
```

把 health 的完整 `space` 保存为 YAML `person.expected_space` 或人物模型档案的 `expected_space`，固定权重摘要、预处理、SDK/ONNX Runtime、执行 provider 与尺寸。设置页可建立人物模型档案并测试连接。没有设置 expected_space 时，任务仍会固定启动时的空间，响应或完成时空间变化会拒绝发布；不同空间的参考和视频特征绝不混用。

默认 CPU provider。GPU 环境将 `onnxruntime` 换成对应 CUDA 的 `onnxruntime-gpu`，并设置 `MINDEX_PERSON_PROVIDERS=CUDAExecutionProvider,CPUExecutionProvider`。CPU/GPU 的 space 不同，需要重新提取。服务启动时持有与现有模型服务相同的 `MINDEX_GPU_LOCK`（默认 `~/.cache/mindex/gpu.lock`）；锁已占用会启动失败，不会停止其他模型。单 GPU 由操作者按阶段启动服务。已有通过 llama-server 启动的外部服务不一定持有此锁，仍需操作者安排显存。

`MINDEX_MODEL_API_KEY` 是服务端 Bearer 密钥。应用通过人物配置的 `api_key_env` 或模型档案保存的密钥发送；任务快照和 API 输出不会序列化明文密钥。

应用在本地按原片 PTS 解码采样，每秒最多 5 帧，极短镜头保留至少一个实际可解码帧。仅把采样 JPEG 按每批最多 16 张发给 `/extract/frames`，避免每个镜头重复上传整片。人脸/人体特征由服务提取；应用使用 ByteTrack 在镜头内关联。镜头边界、检测缺失、交叉框、关联不可靠或冲突人脸都会撤销身份继承。未匹配或不确定的检测保留为候选。证据覆盖只计采样帧后的有限时段，不把某一帧的人物扩散到整个镜头。

人脸匹配比较多张合格参考图的最佳分数，并检查不同演员之间的分数差。分数不是正确率。参考图校验只认清晰人脸主体：画面边缘的无人脸人体检出（路人、肩膀）不影响，群照与主体背对镜头时拒绝。质量门槛分两条，均可在设置页「人物识别」档案中调整：`min_reference_quality`（默认 0，全部参考图参与匹配，抬高后低于门槛的参考图会在任务报告 `reference_errors` 中显式列出）与 `min_face_quality`（默认 0.6，视频帧低于此值不做自动确认）。角色有歧义时仅登记演员身份，角色需要人工确认。纯外观的跨镜头候选，只使用同作品、同特征空间、人工确认且关联角色的样本；候选不会进入严格检索，也不会扩展样本。人体外观匹配始终只生成候选，需人工核对。

## 检索与接口

`POST /api/search/text` 新增：

```json
{"text":"阿青哭泣","person_ids":[],"person_mode":"auto","person_relation":"all"}
```

`auto` 只解析人物库姓名和别名；角色名支持后缀模糊识别（输入“叶淑柔”联合召回“老年叶淑柔”等变体，变体之间任一出镜即算，不影响多人物 `all` 的同时出镜判定）。命中时先以已发布出镜证据筛选，再用“哭泣”执行现有渠道召回，渠道权重不变。仅人物名时按出镜覆盖和证据质量排序，不调用 WeMM。`onscreen` 要求人物；`mentioned` 只检索对白并按登记名称/别名限制字面提及。`all` 要求多个人物（或变体组）出镜时间有交集，`any` 接受任一人物。覆盖缺失会返回提示，不降级为对白提及。搜索结果可查看截图、定位出镜时间和进入人物库纠错。

主要 API：

- `GET /api/persons`：实体、参考、任务、作品关系、覆盖与发布记录。
- `POST /api/persons/entities`、`PUT /api/persons/entities/{id}`：登记与修改。
- `POST /api/persons/references`（multipart，演员照片给 `actor_id`，角色照片给 `role_id` 及照片中的 `actor_id`；角色只有一名演员时可省略 `actor_id`）、`PUT/DELETE /api/persons/references/{id}`（PUT 的 JSON 使用相同配对字段）、`POST /api/persons/references/from-source`。
- `POST /api/persons/imports`（multipart 多文件）、`POST /imports/{id}/parse`、`PUT /imports/{id}`（保存草稿）、`POST /imports/{id}/confirm`（人工确认）。
- `POST /api/persons/sources/{id}/process`：`{"start":0,"end":180,"publish":false}`，返回 202 和任务 ID。
- `GET /api/persons/tasks/{id}`、`POST /tasks/{id}/retry`。
- `GET /api/persons/evidence?source_id=...&status=candidate`：分页读取原始证据。
- `GET /api/persons/review/shots`、`GET /review/shots/{shot_id}`：按镜头读取审核证据；`POST /review/shot`：`{"source_id":"...","shot_id":"...","selections":[{"actor_id":"...","role_id":null}]}`。空 selections 排除该镜头全部人物证据。
- `DELETE /api/persons/tasks/{id}`：删除失败或中断的任务记录，保留镜头缓存与出镜证据。
- `POST /api/persons/publish`：`{}` 绑定活动内容索引；`POST /api/persons/releases/{id}/activate` 恢复组合。

上述缩写路径均继续以 `/api/persons` 为前缀。

CLI：

```sh
mindex persons list
mindex persons save actor.json
mindex persons reference ACTOR_ID photo1.png photo2.png
mindex persons process SOURCE_ID --start 0 --end 180
mindex persons status TASK_ID
mindex persons process SOURCE_ID --retry TASK_ID
mindex persons review review.json
mindex persons publish
mindex persons activate RELEASE_ID
```

CLI review 使用与分镜审核接口相同的 JSON（source_id、shot_id、selections）。CLI process 启动独立工作进程并立即返回持久化任务 ID；日志位于 `data/persons/task-logs/`。数据库按镜头存储检测、特征、配对缓存；失败任务不会自动重发，显式重试继续检查点。删除素材清除该素材人物轨迹、缓存、截图和历史发布中的相关证据，保留全局演员和用户参考图。

## 素材管理镜头详情

镜头操作顺序为“详情、备注、收藏、隐藏”。`GET /api/admin/segments/{id}/details` 返回独立 `asr`、`subtitles`、`llm`：ASR 使用已有转写/词级结果；字幕读取各原片字幕轨道并按镜头时间过滤；LLM 读取该镜头已保存的最新分析。没有结果分别显示“ASR 未处理”“无字幕轨道”“LLM 未处理”。位图、无本镜头对白、提取失败和等待对齐分别显示真实状态。读取详情不运行 ASR/LLM，不依赖在线模型或索引发布。文本字幕缓存于 `data/subtitles/{source_id}/`。

## 验收界限

自动化与测试素材只能验证工程行为，不能证明真实人物识别准确率。恢复工作时，现有素材库已包含 12 张参考图、已完成的原片 600–1200 秒人物任务及 1 条人工审核记录；详见验证记录中的现有任务核验。应继续人工核对正脸转背影、交叉、遮挡、换装与同演员多角色，确认参考图质量与召回效果后才扩展整片。

## 旧数据兼容

人物身份仅有演员、作品与角色，不再提供角色阶段、年龄、造型或服装设置。旧阶段身份首次加载时保留 ID、姓名和别名，转为普通角色并继承原角色的作品及演员关系；旧参考图保留图片与演员配对，旧审核结论保留。历史发布快照保持原文件内容不变，在读取时兼容为普通角色。更改配对只重新匹配已有特征，不重新提取原片或画面嵌入。
