# 附带工具

`tools/` 下是与主管线配套的独立小工具，要求**自包含**（不依赖仓库外路径）。

## m4s-converter：哔哩哔哩缓存转 MP4

位置 `tools/m4s-converter/`。将哔哩哔哩客户端缓存目录下 DASH 分离的 m4s 音视频流**无损合并**为 MP4（MP4Box 封装，不转码），按番剧/分P 自动命名。

- 上游仓库：<https://github.com/mzky/m4s-converter>（MIT）
- 内含官方发布二进制 v1.5.7（darwin/arm64，2026-03-03）+ 便捷包装 `convert.sh`
- 唯一外部依赖：系统级 MP4Box（GPAC），`brew install gpac`

### 快速使用

```bash
# 默认缓存目录 ~/Movies/bilibili（或设 BILIBILI_CACHE 环境变量）
~/workspace/mindex/tools/m4s-converter/convert.sh

# 指定缓存目录
convert.sh /path/to/bilibili缓存目录

# 额外参数透传给原工具（如 -o 覆盖同名输出）
convert.sh /path/to/bilibili缓存目录 -o
```

### convert.sh 做了什么

- 定位 MP4Box（`which MP4Box`），缺失时明确报错退出。
- 第一个非 `-` 开头的参数视为缓存目录，其余透传给二进制。
- 预先喂入三个回车，跳过启动免责声明与结束确认（非交互、可脚本化）。
- 默认附加 `-a` 关闭弹幕下载（离线可用；需要弹幕 ass 时直接运行二进制且不带 `-a`，需联网）。
- 工作目录切到缓存目录，日志固定写 `<缓存目录>/m4s.log`，不污染当前目录。

### 工作原理

1. 递归扫描 `.m4s` 文件，读同目录隐藏文件 `.playurl` 区分视频/音频流。
2. 剥离 m4s 文件头的 9 个 `'0'` 填充字节，生成中间文件 `*-video.mp4` / `*-audio.mp3`（留在缓存子目录，可安全删除）。
3. 解析 `videoInfo.json` 取标题，调 MP4Box 无损封装。

输出布局（**原始 m4s 缓存不被修改**）：

```
<缓存目录>/output/<番剧名>-<UP主>/<分P标题>.mp4
```

同名输出按 mp4 元数据 + 输入内容哈希去重，重复运行安全。

### 行为细节

- 运行时请求 GitHub API 检查新版本（3 秒超时，失败仅告警）。
- 结束时用 `open` 打开输出目录（macOS）。
- **退出码几乎恒为 0**：转换是否成功以 `m4s.log` 中「已合成视频文件 / 未合成任何文件」为准。

### 原生二进制参数

直接运行 `./m4s-converter`（首次需手动回车同意免责声明）：

| 参数 | 说明 |
|---|---|
| `-c, --cachepath` | 自定义缓存目录（默认 `~/Videos/bilibili`） |
| `-a, --assoff` | 关闭自动弹幕（默认开启） |
| `-o, --overlay` | 覆盖同名输出（默认跳过并提示） |
| `-u, --summarize` | 未合并的 MP3/视频汇总到输出目录 |
| `-g, --gpacpath` | 自定义 MP4Box 路径 |
| `-v` / `-h` | 版本 / 帮助 |

### 与 mindex 集成

输出是标准 MP4，可直接作为管线输入：

```bash
uv run mindex prepare '<缓存目录>/output/<番剧>/<分P>.mp4'
```

### 法律声明

仅允许转换本人通过哔哩哔哩官方客户端合法缓存的内容，结果严格限于个人备份，禁止传播与商用（详见上游[免责声明](https://github.com/mzky/m4s-converter/blob/main/免责声明.md)）。

## tools/ 维护约定

- 新工具必须自包含，不依赖仓库外路径。
- 若新增 Python 脚本，统一使用 mindex 项目的 `.venv/bin/python`（uv 管理），不单独创建环境。
