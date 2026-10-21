# persons/frames 存储改造（已实现，2026-10-20）

## 评估与修正

原方案以压缩展示帧、删除已确认镜头冗余帧为方向，成立。向量提取使用原采样输入，
其后复算只需缓存向量，不需要原分辨率图片。无需重新索引现有素材。

原方案存在以下需要修正的问题：

1. 标题的 12.29GiB → 约 0.2GiB 无可靠保证：未确认镜头仍保留全部证据图，实际收益必须预览和实测。
2. 匹配缓存键原本包含整个 `config.person.model_dump()`。新增压缩配置必须排除在缓存键之外，
   否则升级即改变既有匹配键。现已显式排除两个展示参数，保持与旧版参数字典相同。
3. 历史快照不可变，特征/匹配缓存复用也可能带回旧图引用。API 必须支持当前证据 → 历史快照
   → 镜头缩略图回退，并使用正确 MIME 类型。缓存复算写回时把缺失图片引用替换为镜头缩略图，
  防止重新写入死路径。无需重新发布即可继续查看。
4. clean 扫描发生在持锁前，不能直接执行旧候选集。人物规则在独占锁下重新计算保留集后再删。
   审核原来不持管线锁，仍可与迁移竞争；现已让审核读取/写回整段持 pipeline 共享锁，迁移/清理独占期间拒绝审核。
5. 全局内容寻址文件可被多种状态共用：转换后的路径应更新所有引用该图片的当前证据行，
   不限于 accepted；删除集合使用全源保留集，避免共享文件误删。
6. 保留帧缺失或解码失败时，该源必须停止删除并报告 error；不能以无图为由继续删除剩余证据。

## 实现

- `PersonConfig`: frames_max_edge=640（0=不缩）、frames_jpeg_quality=85。
- `Persons.image`: 支持缩放/JPEG，默认 references/imports 行为仍为原尺寸 PNG。
- 新视频处理在特征提取之后保存低分辨率图；模型输入及缓存身份不变。
- `retained_frame_paths`: 按 `(source_id, shot_id)` 分组，有任意 accepted 即确认；
  每个 `(actor_id, role_id)` 保留 accepted 有图行中最高真实分的一帧。
  manual+shot_review 使用 original_score，track 仅同身份兜底；无分最后，ID 稳定破同分。
  未确认镜头全部有图证据保留；多身份/多状态共享文件以集合并集保护。
- `persons migrate-frames [--apply] [--source ID]`: 默认预览，均持 pipeline 独占锁和 index-write 锁；
  apply 写审计，单源单事务，先写新图、再改 evidence/shot 特征缓存路径、提交后删旧图。
  不触碰向量、缓存 ID、coverage、person_versions、search_releases 或内容索引。
  已合规 JPEG 不再次编码，可幂等重跑；错误按源隔离，CLI 存在错误时退出 1。
- 审核卡片优先 accepted/真实分排序；shot_review 优先选择真实存在的演员图，否则镜头缩略图。
- API 证据图支持当前证据、历史快照及镜头缩略图回退，JPEG 返回 image/jpeg。
- clean 新增 A 级 person_frames，取消 frames 的 C 级整树保护；.DS_Store 归原有规则。
- previews 的 deleted_bytes 为旧文件删除量，非净空间节约（未扣新文件写入）；以迁移后 du 为准。

## 验证与使用

测试覆盖格式/缩放、真实分与 track 兜底、待审图保留、共享引用、预览不写、向量与快照不变、
迁移幂等/中断残留、缺帧阻止删除、历史 API 回退及 MIME、持锁后重新判定清理引用。

```bash
uv run pytest tests/test_persons.py tests/test_clean.py -q
uv run pytest -q
uv run ruff check backend/src tests
uv run mindex persons migrate-frames
uv run mindex persons migrate-frames --apply
```

删除原尺寸图片不可逆；历史图可能回退镜头缩略图。旧库实际迁移是显式 apply 操作，
无需重建任何既有索引。clips 存储不在本次范围。

### 本次验证结果

- 人物与 clean 回归 110 项通过，另补的审核互斥用例通过；新增原尺寸 JPEG 与卡片排序断言通过。
- `ruff check backend/src tests`、`git diff --check` 通过。
- 全量回归 644 passed / 3 failed；三个失败均已在独立的原始 HEAD 副本复现：
  `test_admin::test_live_curation_filters_text_video_without_mutating_index`（display_name）、
  `test_film::test_visual_index_does_not_require_asr_or_analysis`（向量数 10 vs 5）、
  `test_recall_e2e::test_subtitle_person_task_publish_strict_mentions_and_studio_media`（mentioned 空结果）。
- 真实库 frames 约 12G；预览被正在执行的任务持有的 pipeline/index 锁阻止，未执行真实库 apply。


### 真实库迁移执行结果（2026-10-20）

用户授权后已执行 `persons migrate-frames --apply`，无错误。Web 服务已保留原启动参数和环境重启，加载新版图片回退及审核锁逻辑。

- frames 磁盘分配占用：12,912,780 KiB → 69,236 KiB（约 12.3 GiB → 67.6 MiB，减少 99.46%）。
- 保留 2,074 张 JPEG，全部长边 ≤640；旧 PNG 已清空。JPEG 内容总计 66,516,922 bytes。
- 删除旧文件 10,381 个、13,201,208,844 bytes；改写 6,895 条证据、1,152 条 shot 特征缓存中的图片路径。
- 迁移前后规范化校验摘要一致：7,780 条特征、20,405 条证据除 image_path 外完全一致；
  1,231 条 coverage、1 个人物发布快照、3 个搜索发布、25 个索引版本、47,372 条索引成员及活动索引/发布指向均未变化。
- 重跑预览：2,074 unchanged，0 reencoded / deleted / rewritten，无错误。
- 真实 HTTP 抽查：已接受/候选压缩图、3 张已删除原图的回退、3 个发布快照证据图均返回可解码 JPEG（200）；审核列表 API 正常。
  当前审核列表中没有 candidate 分镜，候选证据图片已独立抽查。
- 全程不调用 WEMM 或人物模型服务，无需重新索引或重新发布。

执行报告保存在 `/tmp/mindex-frames-migration.json`，校验报告保存在 `/tmp/mindex-frame-audit-after.json`、
`/tmp/mindex-frame-http-check.json`，幂等预览为 `/tmp/mindex-frames-after-preview.json`。
