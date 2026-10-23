"""一次性幂等迁移：把持久化存储里的绝对路径转成相对 data_dir.parent 的可移植路径。

覆盖范围：
  - 元数据库 dialogue_tasks / source_tasks / person_tasks / model_profiles 的 config 字段
  - studio 下各任务的 job.json（若含路径依赖字段）
  - 指定的 YAML 配置文件（默认 config.local.yaml）

规则（与 mindex.shared.config 保持一致）：
  - data_dir / deployment_manifest / font_file 这三个键参与相对化。
  - 位于 data_dir.parent 之内的绝对路径 → 相对路径（POSIX）。
  - data_dir 之外的路径（如 person.model_root、外部清单）→ 保持绝对。
  - 对已失效的历史绝对路径（例如改名前的旧项目根），若其包含
    config-dependencies/<dir>/<name> 片段且该文件存在于当前 data_dir 内，
    则先按片段重定位到当前 data_dir，再相对化，使结果指向真实存在的文件。
  - 已是相对路径的值原样保留，脚本可反复运行（幂等）。

绝不改写版本记录里的 model_identity / 结果溯源等不可变指纹字段——只处理
顶层 config 中真正被当作文件系统路径消费的字段。

用法：
    python scripts/migrate-portable-paths.py --config config.local.yaml [--apply]
默认仅预览（dry-run）；--apply 才落盘。落盘前自动备份数据库。
"""
from __future__ import annotations

import argparse
import datetime
import json
import shutil
import sqlite3
from pathlib import Path

import yaml

from mindex.shared.config import PORTABLE_PATH_KEYS, load_config

TABLES = ("dialogue_tasks", "source_tasks", "person_tasks", "model_profiles")
MARKER = "config-dependencies/"


def _relocate_stale(item: str, data_root: Path) -> str:
    """失效绝对路径按 config-dependencies 片段重定位到当前 data_dir（若命中存在的文件）。"""
    expanded = Path(item).expanduser()
    if not expanded.is_absolute() or expanded.is_file():
        return item
    posix = expanded.as_posix()
    if MARKER in posix:
        candidate = (data_root / posix[posix.index(MARKER):]).resolve()
        if candidate.is_file():
            return str(candidate)
    return item


def rewrite(value, data_dir: Path, changed: list, trail: str = ""):
    """递归就地相对化；返回新结构，并把每次改写记入 changed。"""
    data_root = data_dir.resolve()
    base = data_root.parent

    def visit(node, path):
        if isinstance(node, dict):
            result = {}
            for key, item in node.items():
                here = f"{path}/{key}"
                if key in PORTABLE_PATH_KEYS and isinstance(item, str) and item:
                    new = _relocate_stale(item, data_root)
                    p = Path(new).expanduser()
                    if p.is_absolute():
                        try:
                            inside = p.is_relative_to(base)
                        except AttributeError:  # pragma: no cover
                            inside = False
                        if inside:
                            new = p.relative_to(base).as_posix()
                    if new != item:
                        changed.append((here, item, new))
                    result[key] = new
                else:
                    result[key] = visit(item, here)
            return result
        if isinstance(node, list):
            return [visit(i, path) for i in node]
        return node

    return visit(value, trail)


def migrate_database(db_path: Path, data_dir: Path, apply: bool) -> list:
    report = []
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        for table in TABLES:
            try:
                rows = con.execute(f"SELECT id, config FROM {table}").fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                raw = row["config"]
                if not raw:
                    continue
                data = json.loads(raw)
                changed: list = []
                new = rewrite(data, data_dir, changed, trail=f"{table}#{row['id']}")
                if changed:
                    report.append({"table": table, "id": row["id"], "changes": changed})
                    if apply:
                        con.execute(
                            f"UPDATE {table} SET config=? WHERE id=?",
                            (json.dumps(new, ensure_ascii=False, sort_keys=True), row["id"]),
                        )
        if apply:
            con.commit()
    finally:
        con.close()
    return report


def migrate_studio(root: Path, data_dir: Path, apply: bool) -> list:
    report = []
    for job in sorted(root.glob("studio/*/job.json")):
        data = json.loads(job.read_text())
        changed: list = []
        new = rewrite(data, data_dir, changed, trail=str(job))
        if changed:
            report.append({"file": str(job), "changes": changed})
            if apply:
                tmp = job.with_suffix(".migrate-tmp")
                tmp.write_text(json.dumps(new, ensure_ascii=False, indent=2))
                tmp.replace(job)
    return report


def migrate_yaml(path: Path, data_dir: Path, apply: bool) -> list:
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    changed: list = []
    new = rewrite(data, data_dir, changed, trail=str(path))
    if not changed:
        return []
    if apply:
        tmp = path.with_suffix(path.suffix + ".migrate-tmp")
        tmp.write_text(yaml.safe_dump(new, allow_unicode=True, sort_keys=True))
        tmp.replace(path)
    return [{"file": str(path), "changes": changed}]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.local.yaml", help="YAML 配置路径")
    parser.add_argument("--apply", action="store_true", help="实际写入；缺省仅预览")
    args = parser.parse_args()

    config = load_config(args.config)
    data_dir = Path(config.data_dir)
    db_path = data_dir / "metadata.sqlite3"

    if args.apply and db_path.is_file():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = db_path.with_name(f"metadata.sqlite3.portable-bak-{stamp}")
        shutil.copy2(db_path, backup)
        print(f"已备份数据库 → {backup}")

    db_report = migrate_database(db_path, data_dir, args.apply) if db_path.is_file() else []
    studio_report = migrate_studio(data_dir, data_dir, args.apply)
    yaml_report = migrate_yaml(Path(args.config), data_dir, args.apply)

    total = sum(len(r["changes"]) for r in db_report + studio_report + yaml_report)
    print(f"{'已应用' if args.apply else '预览'}：数据库 {len(db_report)} 行、"
          f"studio {len(studio_report)} 文件、YAML {len(yaml_report)} 文件，共 {total} 处路径改写。")
    for section, items in (("数据库", db_report), ("studio", studio_report), ("YAML", yaml_report)):
        for item in items:
            label = item.get("file") or f"{item['table']}#{item['id']}"
            for where, old, new in item["changes"]:
                print(f"  [{section}] {label} @ {where}: {old}  →  {new}")
    if not args.apply and total:
        print("\n这是预览。确认无误后加 --apply 实际写入（将先备份数据库）。")


if __name__ == "__main__":
    main()
