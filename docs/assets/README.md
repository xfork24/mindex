# README 截图来源

以下图片最初来自用户 2026-11-02 提供的 `~/Desktop/functions` 截图集（1.1–5.5，共 33 张，浅色主题）。2026-11-03 用 `scripts/capture-readme-screenshots.cjs` 对线上服务按**暗色主题**重新截取：界面语言仍为英文，每张图的页面与功能位置与原截图一一对应，数据取截取时服务现状。图片反映对应版本的界面；当前功能行为以实现和使用文档为准。

## 自动连播图（gallery-*.gif）

GitHub 渲染 README 时剥离 `<script>` 与 `style`，无法放置轮播控件；动画 WebP 多数场景只显示首帧，GIF 才是 README 内保证播放的动画格式。同一组截图因此合成为一张循环播放的 GIF；README 中**所有截图（单图与分组动图）都放进默认折叠的 `<details>`**，展开分组动图后自动连播，并在块内列出逐张原图链接。分组、帧顺序、每帧秒数与中英文标题在 `scripts/build-readme-gallery.py` 维护，源图与产物的 SHA-256 记录在 `gallery.json`。

```bash
python scripts/build-readme-gallery.py                # 源图变化后重新生成
python scripts/build-readme-gallery.py --check        # 仅检查产物是否过期（测试调用）
python scripts/build-readme-gallery.py --format webp  # 体积优先时可改用动画 WebP
```

产物为 1000px 宽、每帧 3 秒、无限循环；不同宽高比的截图按组内最宽帧的深色底画布（前端暗色主题 `--bg`）居中显示。

| 资源 | 原始文件 |
| --- | --- |
| `search-text-and-person.png` | 1.1-匹配文本和人物.png |
| `search-text-and-object.png` | 1.2-匹配文本和物体.png |
| `search-emotion-closeup.png` | 1.3-查找悲伤特写人物分镜.png |
| `search-similar-scenes.png` | 1.4-查找相似场景分镜.png |
| `search-by-image-or-video.png` | 1.5-以图片或视频查找分镜.png |
| `admin-preprocess.png` | 2.1-预处理视频素材.png |
| `admin-shots.png` | 2.2-管理素材分镜.png |
| `admin-shot-analysis.png` | 2.3-素材分镜分析结果.png |
| `admin-music.png` | 2.4-管理背景音乐.png |
| `admin-groups.png` | 2.5-管理素材分组.png |
| `persons-cast-import.png` | 2.6-自动导入人物cast表.png |
| `persons-actors-roles.png` | 2.7-管理演员和角色.png |
| `persons-appearance-detection.png` | 2.8-自动识别人物出镜.png |
| `studio-jobs.png` | 3.1-视频创作任务.png |
| `studio-storyboard.png` | 3.2-管理视频创作分镜.png |
| `studio-timeline-alignment.png` | 3.4-文案、音频与视频对齐.png |
| `studio-build-dependencies.png` | 3.5-最终成片版本依赖细节.png |
| `topics-mining.png` | 3.6-AI辅助选题.png |
| `topics-settings.png` | 3.7-选题设置.png |
| `studio-script-templates.png` | 3.8-文案模板管理.png |
| `studio-delivery-templates.png` | 3.9-交付模板管理.png |
| `studio-style-templates.png` | 3.10-风格模板设置.png |
| `agent-create-video-1.png` | 4.1-使用Agent自动从素材库制作视频1.png |
| `agent-create-video-2.png` | 4.2-使用Agent自动从素材库制作视频2.png |
| `agent-create-video-3.png` | 4.3-使用Agent自动从素材库制作视频3.png |
| `agent-create-video-4.png` | 4.4-使用Agent自动从素材库制作视频4.png |
| `agent-search-clips.png` | 4.5-使用Agent检索特定视频片段.png |
| `settings-models-harness.png` | 5.1-模型与harness设置.png |
| `settings-model-card.png` | 5.2-自定义模型卡片.png |
| `settings-model-config.png` | 5.3-自定义模型配置.png |
| `settings-weights-voices-services.png` | 5.4-召回、音色库、本地服务与工具管理.png |
| `settings-dark-theme-i18n.png` | 5.5-暗色风格和中英文切换.png |
