#!/usr/bin/env bash
# m4s-converter 便捷包装：将哔哩哔哩客户端缓存的 m4s 音视频流无损合并为 mp4
#
# 用法:
#   ./convert.sh                     # 使用默认缓存目录 (BILIBILI_CACHE 或 ~/Movies/bilibili)
#   ./convert.sh /path/to/cache      # 指定缓存目录
#   ./convert.sh /path/to/cache -o   # 透传原生参数 (-o 覆盖同名输出等)
#
# 说明:
#   - 自动喂入回车跳过免责声明与退出确认，可在脚本中非交互调用
#   - 默认附加 -a 关闭弹幕下载（离线友好）；需要弹幕 ass 请直接运行本目录的 m4s-converter 二进制
#   - 日志固定写入 <缓存目录>/m4s.log，不污染当前工作目录
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$SCRIPT_DIR/m4s-converter"

if [[ ! -x "$BIN" ]]; then
  echo "错误: 找不到可执行文件 $BIN" >&2
  exit 1
fi

DEFAULT_CACHE="${BILIBILI_CACHE:-$HOME/Movies/bilibili}"

# 第一个非 - 开头的参数视为缓存目录，其余透传给二进制
if [[ $# -gt 0 && "$1" != -* ]]; then
  CACHE_PATH="$1"
  shift
else
  CACHE_PATH="$DEFAULT_CACHE"
fi

if [[ ! -d "$CACHE_PATH" ]]; then
  echo "错误: 缓存目录不存在: $CACHE_PATH" >&2
  exit 1
fi

if ! command -v MP4Box >/dev/null 2>&1; then
  echo "错误: 未找到 MP4Box，请先安装 GPAC: brew install gpac" >&2
  exit 1
fi

echo "缓存目录: $CACHE_PATH"
echo "输出目录: $CACHE_PATH/output"
# 在缓存目录内运行，使 m4s.log 落在缓存目录；喂入回车跳过两处交互确认
cd "$CACHE_PATH"
set +e
printf '\n\n\n' | "$BIN" -c "$CACHE_PATH" -a "$@"
status=$?
set -e

if [[ $status -ne 0 ]]; then
  echo "转换异常 (退出码 $status)，详见日志: $CACHE_PATH/m4s.log" >&2
  exit $status
fi
echo "完成。输出位于: $CACHE_PATH/output，日志: $CACHE_PATH/m4s.log"
