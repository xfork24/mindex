#!/usr/bin/env bash
# m4s-converter 便捷包装：将哔哩哔哩客户端缓存的 m4s 音视频流无损合并为 mp4
#
# 用法:
#   ./convert.sh                     # 使用默认缓存目录 (BILIBILI_CACHE 或 ~/Movies/bilibili)
#   ./convert.sh /path/to/cache      # 指定缓存目录
#   ./convert.sh /path/to/cache -o   # 透传原生参数 (-o 覆盖同名输出等)
#   ./convert.sh --keep-intermediates [其他参数]
#                                    # 保留中间文件（默认合成后自动清理）
#   ./convert.sh -h                  # 查看包装层与原生参数帮助
#
# 说明:
#   - 自动喂入回车跳过免责声明与退出确认，可在脚本中非交互调用
#   - 默认附加 -a 关闭弹幕下载（离线友好）；需要弹幕 ass 请直接运行本目录的 m4s-converter 二进制
#   - 日志固定写入 <缓存目录>/m4s.log，不污染当前工作目录
#   - 合成流程正常结束后默认清理中间文件（*-video.mp4 / *-audio.mp3），成品保留在 output/；
#     加 --keep-intermediates 可保留
#   - 过滤二进制首次运行时的 "logPath m4s.log stat err" 启动提示
#     （启动时检查尚不存在的日志文件所致，日志随后正常创建，无实际影响）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$SCRIPT_DIR/m4s-converter"
# 二进制启动时 stat 尚不存在的 m4s.log 会打印一行无害提示，统一过滤
BIN_NOISE="logPath m4s.log stat err"

if [[ ! -x "$BIN" ]]; then
  echo "错误: 找不到可执行文件 $BIN" >&2
  exit 1
fi

# 过滤启动提示后，把捕获的二进制 stderr 原样输出
show_stderr() {
  grep -v -F "$BIN_NOISE" "$1" >&2 || true
}

# -h/--help：打印包装层用法，并附原生参数（透传给二进制）
for arg in "$@"; do
  if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
    cat <<'USAGE'
用法: ./convert.sh [选项] [缓存目录] [原生参数...]

包装层选项:
    --keep-intermediates
                     保留中间文件（默认合成后自动清理
                     *-video.mp4 / *-audio.mp3，成品保留在 output/）
    -h, --help       显示本帮助

说明:
  - 默认缓存目录: BILIBILI_CACHE 环境变量或 ~/Movies/bilibili
  - 自动附加 -a 关闭弹幕下载（离线友好）
  - 除 --keep-intermediates / -h 外的参数原样透传给 m4s-converter

原生参数（透传）:
USAGE
    errfile="$(mktemp)"
    st=0
    "$BIN" -h </dev/null 2>"$errfile" || st=$?
    show_stderr "$errfile"
    rm -f "$errfile"
    exit $st
  fi
done

DEFAULT_CACHE="${BILIBILI_CACHE:-$HOME/Movies/bilibili}"

# 解析包装层自身选项，其余参数透传给二进制
KEEP_INTERMEDIATES=0
PASSTHROUGH=()
for arg in "$@"; do
  case "$arg" in
    --keep-intermediates) KEEP_INTERMEDIATES=1 ;;
    *) PASSTHROUGH+=("$arg") ;;
  esac
done

# 第一个非 - 开头的参数视为缓存目录，其余透传给二进制
if [[ ${#PASSTHROUGH[@]} -gt 0 && "${PASSTHROUGH[0]}" != -* ]]; then
  CACHE_PATH="${PASSTHROUGH[0]}"
  PASSTHROUGH=("${PASSTHROUGH[@]:1}")
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
errfile="$(mktemp)"
set +e
if [[ ${#PASSTHROUGH[@]} -gt 0 ]]; then
  printf '\n\n\n' | "$BIN" -c "$CACHE_PATH" -a "${PASSTHROUGH[@]}" 2>"$errfile"
else
  printf '\n\n\n' | "$BIN" -c "$CACHE_PATH" -a 2>"$errfile"
fi
status=$?
set -e
show_stderr "$errfile"
rm -f "$errfile"

if [[ $status -ne 0 ]]; then
  echo "转换异常 (退出码 $status)，详见日志: $CACHE_PATH/m4s.log" >&2
  exit $status
fi

# 默认清理已合成的中间文件（成品在 output/，中间文件可随时由 m4s 重新生成；
# output/ 子树排除在外，保留 -u 汇总进去的文件）
if [[ $KEEP_INTERMEDIATES -eq 1 ]]; then
  echo "保留中间文件（--keep-intermediates）"
else
  count=0
  while IFS= read -r -d '' file; do
    rm -f -- "$file"
    count=$((count + 1))
  done < <(find "$CACHE_PATH" ! -path "$CACHE_PATH/output/*" -type f \
    \( -name "*-video.mp4" -o -name "*-audio.mp3" \) -print0)
  if [[ $count -gt 0 ]]; then
    echo "已清理中间文件 $count 个（成品保留在 output/）"
  fi
fi

echo "完成。输出位于: $CACHE_PATH/output，日志: $CACHE_PATH/m4s.log"
