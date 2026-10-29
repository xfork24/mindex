#!/usr/bin/env bash
# run-*.sh 共用的后端选项；空默认值表示保持原启动器的自动设备选择。

mindex_parse_backend() {
  MINDEX_RUN_BACKEND="$1"
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --backend)
        if [[ $# -lt 2 || -z "$2" ]]; then
          echo "--backend 需要参数：metal、cuda 或 cpu" >&2
          exit 2
        fi
        MINDEX_RUN_BACKEND="$2"
        shift 2
        ;;
      --backend=*)
        MINDEX_RUN_BACKEND="${1#*=}"
        if [[ -z "$MINDEX_RUN_BACKEND" ]]; then
          echo "--backend 需要参数：metal、cuda 或 cpu" >&2
          exit 2
        fi
        shift
        ;;
      -h|--help)
        echo "用法：${0##*/} [--backend metal|cuda|cpu]"
        echo "默认后端：${MINDEX_RUN_BACKEND:-${MINDEX_RUN_DEFAULT_LABEL:-由服务自动选择}}；命令行优先于环境变量。"
        exit 0
        ;;
      *)
        echo "未知参数：$1；用法：${0##*/} [--backend metal|cuda|cpu]" >&2
        exit 2
        ;;
    esac
  done
  case "$MINDEX_RUN_BACKEND" in
    ''|metal|cuda|cpu) ;;
    *) echo "无效后端：${MINDEX_RUN_BACKEND}；可选 metal、cuda 或 cpu" >&2; exit 2 ;;
  esac
}

mindex_llama_backend_args() {
  case "$MINDEX_RUN_BACKEND" in
    '') MINDEX_BACKEND_ARGS=(-ngl 99) ;;
    metal) MINDEX_BACKEND_ARGS=(-ngl 99 --device MTL0) ;;
    cuda) MINDEX_BACKEND_ARGS=(-ngl 99 --device CUDA0) ;;
    cpu) MINDEX_BACKEND_ARGS=(-ngl 0 --device none --no-mmproj-offload --no-kv-offload --no-op-offload) ;;
  esac
}

mindex_tts_backend_args() {
  case "$MINDEX_RUN_BACKEND" in
    '') MINDEX_BACKEND_ARGS=() ;;
    metal) MINDEX_BACKEND_ARGS=(--device MTL0) ;;
    cuda) MINDEX_BACKEND_ARGS=(--device CUDA0) ;;
    cpu) MINDEX_BACKEND_ARGS=(--device CPU) ;;
  esac
}
