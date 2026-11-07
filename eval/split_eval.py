"""拆分镜全链路评测：用例集 → 拆分镜（可带素材画面词表）→ 可选召回，输出三项指标与逐用例报告。

运行：

    uv run python eval/split_eval.py [--cases eval/split_cases.jsonl] [--materials JOB_ID] [--recall]
[--output PATH]

只读访问 data/：读取配置、生效索引与任务 job.json，不读写任务 workspace，不加锁；
唯一写入的是评测报告（默认 data/eval-split-YYYYMMDD-HHMMSS.json）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from models.storyboard import hard_check, is_short, measure, whole_sentence

DEFAULT_CASES = Path(__file__).with_name("split_cases.jsonl")
RECALL_TOP = 3
NO_RECALL = "该镜无召回结果"
WORDS_PER_SECOND = 2.5  # 英文每秒词数
HAN_PER_SECOND = 4  # 中文每秒字数


# ---- 纯函数：指标（需求 11.2、11.11），供单元测试与属性测试直接调用 ----


def case_matches(expected: Sequence[str], actual: Sequence[str] | None) -> bool:
    """逐镜比较：去掉每镜首尾空白后完全相等且镜数相同才算吻合；失败用例（actual 为 None）恒不吻合。"""
    if actual is None:
        return False
    return len(expected) == len(actual) and all(
        e.strip() == a.strip() for e, a in zip(expected, actual, strict=True)
    )


def actual_texts(result: dict) -> list[str] | None:
    """用例结果中的实际分镜文本；失败用例返回 None。"""
    if result.get("status") != "ok" or result.get("actual") is None:
        return None
    return [shot["text"] for shot in result["actual"]]


def match_rate(results: Sequence[dict]) -> float:
    """吻合用例数 ÷ 总用例数 × 100；空集返回 0.0。失败用例计为不吻合。"""
    if not results:
        return 0.0
    matched = sum(1 for r in results if case_matches(r["expected"], actual_texts(r)))
    return matched / len(results) * 100


def short_count(results: Sequence[dict]) -> int:
    """过短分镜总数：少于 3 个英文词且少于 5 个汉字，且不是整句单独成镜。失败用例不计。"""
    total = 0
    for result in results:
        texts = actual_texts(result)
        if texts is None:
            continue
        total += sum(1 for i, text in enumerate(texts) if is_short(text) and not whole_sentence(texts, i))
    return total


def shot_seconds(text: str) -> float:
    """按英文每秒 2.5 词、中文每秒 4 字估算单镜时长（秒）。"""
    words, han = measure(text)
    return words / WORDS_PER_SECOND + han / HAN_PER_SECOND


def average_seconds(results: Sequence[dict]) -> float:
    """所有成功用例中各分镜估算时长的平均值，保留 1 位小数；没有分镜时为 0.0。"""
    durations = [shot_seconds(text) for r in results for text in (actual_texts(r) or [])]
    if not durations:
        return 0.0
    return round(sum(durations) / len(durations), 1)


def metrics(results: Sequence[dict]) -> dict:
    return {
        "match_rate": round(match_rate(results), 1),
        "short_shots": short_count(results),
        "average_seconds": average_seconds(results),
    }


# ---- 用例与执行 ----


def load_cases(path: Path) -> list[dict]:
    cases = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"用例文件第 {number} 行不是有效 JSON：{exc}") from exc
        if not isinstance(case.get("text"), str) or not isinstance(case.get("expected"), list):
            raise ValueError(f"用例文件第 {number} 行缺少 text 或 expected")
        cases.append(case)
    return cases


def recall_rows(searcher, query: str, granularity: str, filters) -> list[dict]:
    """与工作台召回阶段同一路径：TextSearch + 任务范围过滤，取前 3 条。"""
    from shared.types import TextSearch

    if not query.strip():
        return []
    result = searcher.text(TextSearch(text=query, granularity=granularity, limit=RECALL_TOP, filters=filters))
    rows = []
    for item in result.get("results", [])[:RECALL_TOP]:
        annotation = item.get("annotation") or {}
        rows.append(
            {
                "source_name": item.get("source_name"),
                "start": item.get("start"),
                "end": item.get("end"),
                "description": annotation.get("description") or item.get("description") or "",
            }
        )
    return rows


def evaluate_case(
    case: dict,
    storyboarder,
    *,
    max_shots: int,
    vocabulary: list[str] | None,
    search: Callable[[str], list[dict]] | None,
) -> dict:
    """评测单条用例；调用失败或未通过硬校验时标记为 failed 并记录中文原因（需求 11.11）。"""
    result = {
        "id": case.get("id"),
        "status": "ok",
        "error": None,
        "matched": False,
        "expected": list(case["expected"]),
        "actual": None,
    }
    try:
        drafts = storyboarder.split(case["text"], vocabulary=vocabulary)
        error = hard_check(case["text"], drafts, max_shots)
    except Exception as exc:  # noqa: BLE001 - 单条用例失败不应中断整轮评测
        drafts, error = None, f"拆分镜调用失败：{exc}"
    else:
        if error:
            error = f"未通过硬校验：{error}"
    if error:
        result.update(status="failed", error=error)
        return result
    shots = [{"text": d.text, "visual": d.visual, "query": d.query} for d in drafts]
    if search is not None:
        for shot in shots:
            try:
                rows = search(shot["query"])
            except Exception as exc:  # noqa: BLE001 - 召回失败只记在该镜上
                shot["recall"], shot["recall_error"] = [], f"召回失败：{exc}"
                continue
            shot["recall"] = rows
            if not rows:
                shot["recall_note"] = NO_RECALL
    result["actual"] = shots
    result["matched"] = case_matches(result["expected"], [s["text"] for s in shots])
    return result


def run(
    config,
    cases: list[dict],
    *,
    job_id: str | None = None,
    recall: bool = False,
    storyboarder=None,
    searcher=None,
    store=None,
    log: Callable[[str], None] = print,
) -> dict:
    """执行评测并返回报告字典（不写文件）。任务不存在时抛 EntityNotFound，且不做任何拆分。

    ``storyboarder``、``searcher``（SearchEngine 兼容，提供 ``text(TextSearch)``）、``store`` 可注入测试替身。
    """
    from models.adapters import make_storyboarder
    from shared.types import Filters
    from studio import studio
    from studio.material_digest import build_digest, merged_vocabulary
    from studio.studio import SPLIT_VERSION

    vocabulary = None
    scope = None
    materials = None
    if job_id is not None:
        state = studio.load_job(config, job_id)  # 只读 job.json；不存在时抛 EntityNotFound
        scope = studio.material_scope(state)
        if store is None:
            from shared.storage import Store

            store = Store(config.data_dir)
        digest = build_digest(config, store, state)
        materials = {"job_id": job_id, "stats": digest.stats(), "empty_reason": digest.empty_reason}
        if digest.empty:
            log(f"素材摘要为空：{digest.empty_reason}，本次拆分不使用素材画面词表")
        else:
            vocabulary = merged_vocabulary(digest)
            log(
                f"素材摘要：{digest.source_count} 个素材、{digest.segment_count} 个镜头，画面词 "
                f"{len(vocabulary or [])} 个"
            )

    search = None
    if recall:
        if searcher is None:
            from search.search import SearchEngine
            from shared.storage import Store

            active = (store or Store(config.data_dir)).active_index()
            if active is None:
                raise studio.StudioError("尚未发布索引，无法执行召回检查")
            searcher = SearchEngine(config, index_id=active["id"], release_id=active.get("search_release_id"))
        filters = studio.scoped_filters(Filters(), scope)
        granularity = config.studio.recall.granularity

        def search(query: str) -> list[dict]:
            return recall_rows(searcher, query, granularity, filters)

    storyboarder = storyboarder or make_storyboarder(config)
    results = []
    for number, case in enumerate(cases, 1):
        result = evaluate_case(
            case, storyboarder, max_shots=config.studio.max_shots, vocabulary=vocabulary, search=search
        )
        state_text = (
            ("吻合") if result["matched"] else ("失败：" + result["error"] if result["error"] else "不吻合")
        )
        log(f"[{number}/{len(cases)}] {result['id']}：{state_text}")
        results.append(result)

    return {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": config.studio.generator.model,
        "split_version": SPLIT_VERSION,
        "materials": materials,
        "recall": recall,
        "metrics": metrics(results),
        "cases": results,
    }


def report_path(data_dir: Path, now: datetime | None = None) -> Path:
    """默认报告路径 data/eval-split-YYYYMMDD-HHMMSS.json（本地时间）。"""
    return Path(data_dir) / f"eval-split-{(now or datetime.now()).strftime('%Y%m%d-%H%M%S')}.json"


def effective_config(config_path: str | None):
    """读取配置并只读合并设置页的激活档案（不执行任何迁移写入）。"""
    from settings.runtime_settings import apply_overrides
    from shared.config import load_config
    from shared.storage import Store

    config = load_config(config_path)
    if config.runtime_overrides and (config.data_dir / "metadata.sqlite3").is_file():
        config = apply_overrides(config, Store(config.data_dir))
    return config


def main(argv: Sequence[str] | None = None, *, config=None, storyboarder=None, searcher=None) -> int:
    from admin.curation import EntityNotFound

    parser = argparse.ArgumentParser(description="拆分镜全链路评测")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="用例文件（JSONL）")
    parser.add_argument("--materials", metavar="JOB_ID", help="用该创作任务的素材摘要画面词表拆分")
    parser.add_argument("--recall", action="store_true", help="对每镜召回词执行召回并列出前 3 条结果")
    parser.add_argument("--output", type=Path, help="报告路径，默认 data/eval-split-<时间>.json")
    parser.add_argument("--config", help="配置文件路径，默认 config.local.yaml")
    args = parser.parse_args(argv)

    config = config or effective_config(args.config)
    try:
        cases = load_cases(args.cases)
    except (OSError, ValueError) as exc:
        print(f"读取用例失败：{exc}", file=sys.stderr)
        return 2
    try:
        report = run(
            config,
            cases,
            job_id=args.materials,
            recall=args.recall,
            storyboarder=storyboarder,
            searcher=searcher,
        )
    except EntityNotFound:
        print(f"找不到创作任务：{args.materials}", file=sys.stderr)
        return 2
    output = args.output or report_path(config.data_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    m = report["metrics"]
    print(
        f"切分吻合度 {m['match_rate']}%，过短分镜 {m['short_shots']} 个，平均分镜时长 {m['average_seconds']} "
        f"秒"
    )
    print(f"报告已写入 {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
