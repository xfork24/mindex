#!/usr/bin/env python3
"""智能助手真实模型评测：在隔离素材库上，让真实模型跑与 tests/test_agent_scenarios.py 相同的验收场景。

素材、嵌入、配音、拆分镜、文案创作均为测试专用的确定性适配器，只有智能助手模型是真实服务——评测的是模型
理解需求、调用工具、澄清与遵守确认流程的能力，不涉及检索或配音质量。每个场景使用全新的隔离数据目录。

用法（密钥从环境变量读取，不要写在命令行里）：
    MINDEX_AGENT_API_KEY=... uv run python scripts/agent_eval.py --backend openai \\
        --base-url https://api.deepseek.com/v1 --model deepseek-chat
    ANTHROPIC_API_KEY=... uv run python scripts/agent_eval.py --backend anthropic \\
        --base-url https://api.anthropic.com --model claude-sonnet-4-5 --api-key-env ANTHROPIC_API_KEY
    可用 --scenario merge_rebuild 只跑指定场景（可重复）；报告与事件日志写入 --output 目录。
    --list 列出全部场景及说明（无需模型与密钥）；模型不支持图片输入时用 --no-vision 跳过需要视觉的场景。
    场景含面板附件、索引回退、构建与后台任务取消、人物审核发布、访谈改写、竖屏方案、附件作画面等；
    全部在隔离数据目录内完成。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))


def make_video(path: Path) -> Path:
    from mindex.shared.media import command

    command(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "color=c=red:s=160x120:r=25:d=1",
             "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=25:d=1",
             "-f", "lavfi", "-i", "color=c=green:s=160x120:r=25:d=8",
             "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)])
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["openai", "anthropic"])
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--api-key-env", default="MINDEX_AGENT_API_KEY")
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--scenario", action="append", help="只运行指定场景，可重复")
    parser.add_argument("--no-vision", action="store_true", help="跳过需要模型支持图片输入的场景")
    parser.add_argument("--list", action="store_true", help="列出全部场景及说明后退出（不调用模型）")
    parser.add_argument("--timeout", type=float, default=300, help="单个场景超时秒数")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "agent-eval")
    args = parser.parse_args()

    from agent_scenarios import SCENARIOS, drive, final_text, prepare, tool_names

    if args.list:
        for scenario in SCENARIOS:
            needs = f" [需要：{'、'.join(scenario.needs)}]" if scenario.needs else ""
            print(f"{scenario.name}{needs}\n    请求：{scenario.prompt}" + (f"\n    说明：{scenario.note}" if scenario.note else ""))
        return 0
    missing = [flag for flag, value in (("--backend", args.backend), ("--base-url", args.base_url),
                                        ("--model", args.model)) if not value]
    if missing:
        parser.error("缺少参数：" + " ".join(missing))
    if not os.environ.get(args.api_key_env):
        print(f"环境变量 {args.api_key_env} 未设置模型密钥", file=sys.stderr)
        return 2

    from fastapi.testclient import TestClient

    from mindex.shared.config import Config

    chosen = [s for s in SCENARIOS if (not args.scenario or s.name in args.scenario)
              and not (args.no_vision and "vision" in s.needs)]
    if not chosen:
        print("没有匹配的场景：" + ", ".join(s.name for s in SCENARIOS), file=sys.stderr)
        return 2
    # 附件端点在启用令牌时要求凭证；评测以本机面板身份上传附件。
    for name in ("MINDEX_WEB_TOKEN", "MINDEX_MCP_TOKEN"):
        os.environ.pop(name, None)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    output = args.output / f"{stamp}-{args.backend}-{args.model.replace('/', '_')}"
    output.mkdir(parents=True, exist_ok=True)
    report = {"backend": args.backend, "model": args.model, "base_url": args.base_url, "started": stamp, "scenarios": []}
    with tempfile.TemporaryDirectory(prefix="mindex-agent-eval-") as directory:
        video = make_video(Path(directory) / "工程测试专用_颜色样本.mp4")
        for scenario in chosen:
            config = Config(data_dir=Path(directory) / scenario.name / "data")
            config.embedding.dimensions = 8
            config.agent.llm = config.agent.llm.model_copy(update={
                "backend": args.backend, "base_url": args.base_url, "model": args.model,
                "api_key_env": args.api_key_env, "max_tokens": args.max_tokens})
            started = time.monotonic()
            entry = {"name": scenario.name, "prompt": scenario.prompt, "note": scenario.note}
            events = []
            try:
                world = prepare(scenario, config, video)
                with TestClient(world.app()) as client:
                    events = drive(client, scenario, world, timeout=args.timeout)
                    scenario.check(world, client, events)
                entry["passed"] = True
            except Exception as exc:  # noqa: BLE001 — 评测要记录每个场景的失败原因
                entry.update(passed=False, error=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc()[-2000:])
            entry.update(seconds=round(time.monotonic() - started, 1), tools=tool_names(events),
                         reply=final_text(events)[-1500:],
                         usage=[e["data"].get("usage") for e in events if e["type"] == "assistant/message"])
            (output / f"{scenario.name}.events.json").write_text(json.dumps(events, ensure_ascii=False, indent=1))
            report["scenarios"].append(entry)
            print(f"{'✓' if entry['passed'] else '✗'} {scenario.name} ({entry['seconds']}s) "
                  f"tools={entry['tools']}" + ("" if entry["passed"] else f"\n    {entry['error']}"), flush=True)
    passed = sum(1 for s in report["scenarios"] if s["passed"])
    report["pass_rate"] = f"{passed}/{len(report['scenarios'])}"
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"通过 {report['pass_rate']}；报告：{output / 'report.json'}")
    return 0 if passed == len(report["scenarios"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
