#!/usr/bin/env python3
"""检查仓库内 Markdown 的相对链接、锚点和图片是否有效。

用法：
    python scripts/check-doc-links.py

只检查相对链接（含 HTML 的 href/src）；http(s)/mailto 链接不联网检查。
代码块与行内代码内的内容被忽略。存在失效链接时退出码为 1。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", ".venv-detector", ".venv-person", "node_modules", "models", ".pytest_cache"}
LINK = re.compile(
    r'(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)|!\[[^\]]*\]\(([^)\s]+)\)|(?:href|src)="([^"]+)"'
)


def markdown_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not (set(path.relative_to(ROOT).parts) & SKIP_DIRS)
        and not any(part.endswith(".egg-info") for part in path.parts)
    )


def slug(heading: str) -> str:
    text = re.sub(r"<[^>]+>|[`*]", "", heading.strip()).lower()
    return re.sub(r"[^\w\- 　]", "", text).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    found, in_code = set(), False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and (match := re.match(r"#{1,6}\s+(.*)", line)):
            found.add(slug(match.group(1)))
    return found


def check(path: Path) -> list[str]:
    body = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
    body = re.sub(r"`[^`\n]*`", "``", body)  # 行内代码是示例而非链接（如 `<video src="url#t=s,e">`）
    problems = []
    for match in LINK.finditer(body):
        target = match.group(1) or match.group(2) or match.group(3)
        if re.match(r"(https?:|mailto:)", target):
            continue
        name, _, fragment = target.partition("#")
        destination = (path.parent / unquote(name)).resolve() if name else path
        label = path.relative_to(ROOT)
        if not destination.exists():
            problems.append(f"{label}: 找不到 {target}")
        elif fragment and destination.suffix == ".md":
            known = anchors(destination)
            if fragment not in known and unquote(fragment) not in known:
                problems.append(f"{label}: 锚点不存在 {target}")
    return problems


def main() -> int:
    files = markdown_files()
    problems = [problem for path in files for problem in check(path)]
    for problem in problems:
        print(problem)
    print(f"检查 {len(files)} 个 Markdown 文件，{len(problems)} 处失效链接")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
