# m4s-converter — 哔哩哔哩缓存 m4s 转 mp4

将哔哩哔哩客户端缓存目录下 DASH 分离的 m4s 音视频流**无损合并**为 mp4（MP4Box 封装，不转码），并按番剧/分P标题自动命名。

- 上游仓库：<https://github.com/mzky/m4s-converter>（MIT 协议）
- 二进制版本：v1.5.7（`m4s-converter-darwin_arm64`，2026-03-03 发布）
- 二进制 SHA256：`2ec8cc37c848c86093fd587ae9099ac087d44bc3f52ed999c8565d41e35e982a`

本目录自包含：`m4s-converter`（官方发布二进制）+ `convert.sh`（便捷包装），可在任意工作目录下执行，不依赖本仓库之外的文件。

## 依赖

| 依赖 | 说明 |
|------|------|
| MP4Box (GPAC) | 唯一外部依赖，`brew install gpac` 安装；工具通过 `which MP4Box` 自动定位（本机已装 26.07） |

## 快速使用

```bash
# 转换默认缓存目录（~/Movies/bilibili，可用环境变量 BILIBILI_CACHE 覆盖）
./convert.sh

# 转换指定缓存目录
./convert.sh /Users/hengliu/Movies/bilibili

# 透传原生参数（例如 -o 覆盖同名输出文件）
./convert.sh /Users/hengliu/Movies/bilibili -o

# 保留中间文件（默认合成后自动清理 *-video.mp4 / *-audio.mp3）
./convert.sh --keep-intermediates /Users/hengliu/Movies/bilibili
```

`convert.sh` 已自动处理：

- 跳过启动时的免责声明确认和结束时的"按任意键退出"（非交互、可脚本化）
- 默认附加 `-a` 关闭弹幕下载（离线可用；见下文"弹幕字幕"）
- 工作目录切到缓存目录，日志固定写入 `<缓存目录>/m4s.log`
- 合成流程正常结束后自动清理中间文件（`*-video.mp4` / `*-audio.mp3`，成品保留在 `output/`；`--keep-intermediates` 可保留）
- 过滤二进制首次运行时的 `logPath m4s.log stat err` 启动提示（检查尚不存在的日志文件所致，无实际影响）
- `-h` / `--help` 显示包装层用法（含 `--keep-intermediates`）与原生参数

## 工作原理

1. 递归扫描缓存目录中的 `.m4s` 文件，读取同目录隐藏文件 `.playurl`（DASH 流信息）区分视频流与音频流
2. 剥离 m4s 文件头的 9 个 `'0'` 填充字节，生成中间文件 `*-video.mp4` / `*-audio.mp3`（`convert.sh` 在合成流程正常结束后自动删除它们；直接运行二进制则留在原缓存子目录，可安全删除）
3. 解析 `videoInfo.json` 获取标题，调用 MP4Box 将音视频无损封装为 mp4

输出布局（原始 m4s 缓存不被修改）：

```
<缓存目录>/output/<番剧名>-<UP主>/<分P标题>.mp4
```

同名输出文件按 mp4 元数据 + 输入内容哈希去重，重复运行安全（已转换的会自动跳过）。

## 原生二进制完整参数

如需更多控制，可直接运行二进制（注意首次运行需手动回车同意免责声明）：

```bash
./m4s-converter -c /Users/hengliu/Movies/bilibili    # -c 指定缓存目录（默认 ~/Videos/bilibili）
```

| 参数 | 说明 |
|------|------|
| `-h, --help` | 帮助 |
| `-v, --version` | 版本 |
| `-a, --assoff` | 关闭自动生成弹幕功能（默认开启） |
| `-o, --overlay` | 覆盖同名输出视频（默认保留并提示跳过） |
| `-u, --summarize` | 将未合并的 MP3/视频文件汇总到输出目录 |
| `-c, --cachepath` | 自定义缓存目录 |
| `-g, --gpacpath` | 自定义 MP4Box 路径（`select` 弹出选择对话框） |

## 弹幕字幕

工具默认会按 cid 从 `comment.bilibili.com` 下载弹幕 xml 并转换为 `.ass`，放在 mp4 同目录（需联网）。`convert.sh` 为保证离线可用默认关闭了此功能；如需弹幕，直接运行二进制且不带 `-a`。

## 行为细节

- 运行时会请求 GitHub API 检查新版本（3 秒超时，失败仅告警，不影响离线使用）
- 结束时会用 `open` 打开输出目录（macOS）
- 退出码几乎恒为 0，转换是否成功以日志 `m4s.log` 中的"已合成视频文件/未合成任何文件"为准

## 与 mindex 集成

输出的 mp4 为标准格式，可直接作为 mindex 索引管线的输入素材：

```bash
uv run mindex --config config.local.yaml prepare <output 下的 mp4>
```

## 故障排查

**MP4Box 报 `Library not loaded: /opt/homebrew/opt/ffmpeg/lib/libavdevice.63.dylib`**（m4s 剥头成功、合成失败）：gpac formula 硬链接普通 `ffmpeg` formula 的库；本机若只装了 `ffmpeg-full`（无普通 `ffmpeg`），`libgpac` 的绝对路径引用会落空。修复——把引用重指到 ffmpeg-full（soname 一致）并重签名：

```bash
DYLIB=/opt/homebrew/Cellar/gpac/*/lib/libgpac.*.dylib
for lib in libavdevice.63 libavfilter.12 libswscale.10 libavformat.63 libavcodec.63 libavutil.61; do
  install_name_tool -change "/opt/homebrew/opt/ffmpeg/lib/$lib.dylib" \
    "/opt/homebrew/opt/ffmpeg-full/lib/$lib.dylib" $DYLIB
done
codesign -f -s - $DYLIB && MP4Box -version
```

注意：`brew upgrade/reinstall gpac` 会还原为官方 bottle，需重跑上述补丁；或改为卸载 `ffmpeg-full`、安装普通 `brew install ffmpeg` 后 `brew reinstall gpac`（brew 原生方案，二者不可共存）。

## 维护约定

- 本目录工具必须自包含，不依赖仓库外路径
- 若后续在 `tools/` 下新增 Python 脚本，统一使用 mindex 项目的 `.venv/bin/python`（uv 管理），不单独创建环境

## 法律声明

仅允许转换本人通过哔哩哔哩官方客户端合法缓存的内容，转换结果严格限于个人备份，禁止传播与商用（详见上游[免责声明](https://github.com/mzky/m4s-converter/blob/main/免责声明.md)）。
