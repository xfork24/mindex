#!/usr/bin/env python3
"""由 messages.json 生成浏览器使用的 messages.js。

messages.json 是界面文案的唯一源；messages.js 只是给页面 <script> 加载的镜像：
    window.MindexMessages = <messages.json 内容>;

用法：
    python scripts/build-messages.py           # 重新生成 messages.js
    python scripts/build-messages.py --check   # 仅检查是否一致，不一致时退出码 1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SHARED = Path(__file__).resolve().parents[1] / "frontend" / "src" / "shared"
SOURCE = SHARED / "messages.json"
TARGET = SHARED / "messages.js"
PREFIX = "window.MindexMessages = "


def render() -> str:
    text = SOURCE.read_text(encoding="utf-8")
    json.loads(text)  # 非法 JSON 时直接报错，不生成坏文件
    return f"{PREFIX}{text.rstrip()};\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="由 messages.json 生成 messages.js")
    parser.add_argument("--check", action="store_true", help="只检查 messages.js 是否与 messages.json 一致")
    args = parser.parse_args()
    expected = render()
    current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else None
    if args.check:
        if current != expected:
            print("messages.js 与 messages.json 不一致，请运行 python scripts/build-messages.py", file=sys.stderr)
            return 1
        print("messages.js 与 messages.json 一致")
        return 0
    if current == expected:
        print("messages.js 已是最新")
        return 0
    TARGET.write_text(expected, encoding="utf-8")
    print(f"已生成 {TARGET.relative_to(SHARED.parents[2])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
