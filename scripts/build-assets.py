#!/usr/bin/env python3
"""把前端页面引用的 /assets/ 资源版本参数改写为文件内容哈希。

页面 HTML（frontend/src/*/*.html）中 src/href 指向 /assets/ 的地址统一写成 ``/assets/<路径>?v=<哈希>``，
哈希取被引用文件内容 SHA-256 的前 10 位；资源内容变化时地址随之变化，浏览器不会继续使用旧缓存。
JS 中已带 ``?v=`` 的 /assets/ 字符串（例如智能助手面板按需加载的 shot-player.js）按同一规则改写，
且先于 HTML 计算，使 HTML 中 JS 的哈希反映改写后的内容。

文案改动后先运行 build-messages.py 生成 messages.js，再运行本脚本。

用法：
    python scripts/build-assets.py           # 改写过期的版本参数
    python scripts/build-assets.py --check   # 仅检查，存在过期版本时列出并以退出码 1 结束
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "frontend" / "src"
HTML_URL = re.compile(r'(?P<attr>\b(?:src|href)=")/assets/(?P<path>[^"?#]+)(?:\?v=[^"#]*)?(?P<end>")')
JS_URL = re.compile(r"""(?P<attr>['"`])/assets/(?P<path>[^'"`?#\s]+)\?v=[^'"`#\s]*(?P<end>['"`])""")
DIGEST = 10


class Assets:
    """在内存中计算改写结果；哈希优先取尚未写盘的改写后内容。"""

    def __init__(self) -> None:
        self.scripts = sorted(WEB.glob("*/*.js"))
        self.pages = sorted(WEB.glob("*/*.html"))
        self.current = {p: p.read_text(encoding="utf-8") for p in (*self.scripts, *self.pages)}
        self.expected = dict(self.current)
        self.missing: list[str] = []

    def digest(self, path: Path) -> str:
        data = self.expected[path].encode("utf-8") if path in self.expected else path.read_bytes()
        return hashlib.sha256(data).hexdigest()[:DIGEST]

    def rewrite(self, page: Path, pattern: re.Pattern) -> str:
        def replace(match: re.Match) -> str:
            target = (WEB / match["path"]).resolve()
            if WEB.resolve() not in target.parents or not target.is_file():
                self.missing.append(f"{page.relative_to(ROOT)}：/assets/{match['path']} 不存在")
                return match[0]
            return f"{match['attr']}/assets/{match['path']}?v={self.digest(target)}{match['end']}"

        return pattern.sub(replace, self.expected[page])

    def build(self) -> dict[Path, str]:
        # JS 之间可能互相引用：逐轮改写直到不再变化。
        for _ in range(len(self.scripts) + 1):
            self.missing.clear()
            changed = False
            for path in self.scripts:
                text = self.rewrite(path, JS_URL)
                if text != self.expected[path]:
                    self.expected[path], changed = text, True
            if not changed:
                break
        else:
            self.missing.append("JS 资源之间存在循环引用，版本参数无法收敛")
        for path in self.pages:
            self.expected[path] = self.rewrite(path, HTML_URL)
        return {p: t for p, t in self.expected.items() if t != self.current[p]}


def main() -> int:
    parser = argparse.ArgumentParser(description="把页面中的 /assets/ 版本参数改写为内容哈希")
    parser.add_argument("--check", action="store_true", help="只检查版本参数是否与资源内容一致，不改写文件")
    args = parser.parse_args()
    assets = Assets()
    stale = assets.build()
    for line in assets.missing:
        print(line, file=sys.stderr)
    if assets.missing:
        return 1
    if args.check:
        if stale:
            for path in stale:
                print(f"版本参数过期：{path.relative_to(ROOT)}", file=sys.stderr)
            print("请运行 python scripts/build-assets.py", file=sys.stderr)
            return 1
        print("资源版本参数与内容一致")
        return 0
    for path, text in stale.items():
        path.write_text(text, encoding="utf-8")
        print(f"已更新 {path.relative_to(ROOT)}")
    if not stale:
        print("资源版本参数已是最新")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
