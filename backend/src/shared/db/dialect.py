"""受控方言翻译：SQLite 语句 → PostgreSQL 可执行形态（plans §5）。

不做通用 SQL 转译。这里只覆盖三类**可机械验证**的改写，其余语义相关语句由调用点
用 ``DualSQL`` 显式双方言登记：

1. 占位符 ``?`` → ``%s``：跳过字符串字面量（含 ``''`` 转义）、双引号标识符、
   ``--`` 行注释与 ``/* */`` 块注释；
2. 裸标识符 ``end`` → ``"end"``：PG 保留字（SQLite 允许），业务表 segments/
   media_references/person_evidence 的 ``end`` 列；
3. ``INSERT OR REPLACE/IGNORE``：按 ``TABLE_SPECS`` 登记的冲突键改写为
   ``ON CONFLICT ... DO UPDATE/DO NOTHING``。

REPLACE 的忠实语义是「删旧行再插入」：未列出的列重置为默认值、rowid 移到末尾、
NOT NULL 无默认列照常报错——改写通过 ``SET 列=excluded.列 / 列=DEFAULT /
rowid=DEFAULT`` 精确复现（plans §2 的冲突键与重置语义）。``INSERT OR IGNORE``
在 PG 下只忽略唯一冲突（SQLite 还会跳过 NOT NULL/CHECK 违例行），差异登记在
TABLE_SPECS 注释。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cache

__all__ = ["TABLE_SPECS", "TableSpec", "UnsupportedSql", "rewrite_for_postgres"]

_PLACEHOLDER = re.compile(r"\?")


class UnsupportedSql(ValueError):
    """语句无法被受控翻译器安全改写；调用点必须改用 DualSQL 显式登记。"""


@dataclass(frozen=True)
class TableSpec:
    """REPLACE/lastrowid 改写所需的表元数据（与 schema_pg.py 的 DDL 同步维护）。"""

    columns: tuple[str, ...]  # 与 SQLite 逻辑列序一致（含后加列）
    conflict: tuple[str, ...]  # REPLACE 的主冲突键（SQLite REPLACE 实际替换时命中的约束）
    returning: str | None = None  # lastrowid 对应的整型主键（无则不追加 RETURNING）
    has_rowid: bool = True  # PG DDL 是否有独立 rowid 列（AUTOINCREMENT 表的 rowid 即 id）
    notes: str = ""
    extra_unique: tuple[tuple[str, ...], ...] = field(default=())  # 其他唯一约束：冲突时 PG 会报错而非替换


TABLE_SPECS: dict[str, TableSpec] = {
    "settings": TableSpec(
        columns=("key", "value"), conflict=("key",),
        notes="纯键值表；REPLACE 全列列出，无重置语义"),
    "transcripts": TableSpec(columns=("source_id", "profile", "data"), conflict=("source_id", "profile")),
    "annotations": TableSpec(columns=("segment_id", "profile", "data"), conflict=("segment_id", "profile")),
    "scene_plans": TableSpec(columns=("source_id", "profile", "data"), conflict=("source_id", "profile")),
    "media_references": TableSpec(
        columns=("path", "source_id", "start", "end", "version", "refs"), conflict=("path",)),
    "representations": TableSpec(
        columns=("id", "index_id", "segment_id", "view", "document", "cache_key"), conflict=("id",),
        notes="REPLACE 未列 cache_key 时重置为 NULL（与 SQLite 删插一致，缓存键随之失效）"),
    "channel_data": TableSpec(
        columns=("source_id", "channel", "profile", "status", "data", "updated_at"),
        conflict=("source_id", "channel"),
        notes="updated_at 有默认值：REPLACE 未列出时重置为当前时间（SQLite 同义）"),
    "person_evidence": TableSpec(
        columns=("id", "source_id", "shot_id", "actor_id", "role_id", "stage_id",
                 "start", "end", "status", "data"),
        conflict=("id",)),
    "person_coverage": TableSpec(
        columns=("source_id", "cache_id", "data"), conflict=("cache_id",)),
    "person_features": TableSpec(columns=("id", "source_id", "space", "kind", "data"), conflict=("id",)),
    "person_entities": TableSpec(columns=("id", "kind", "data"), conflict=("id",)),
    "search_releases": TableSpec(
        columns=("id", "content_id", "person_id", "created_at"), conflict=("id",),
        notes="业务路径只用 INSERT OR IGNORE；REPLACE 语义按删插登记（测试/维护用）"),
    "model_profiles": TableSpec(
        columns=("id", "role", "name", "provider", "config", "api_key", "created_at", "updated_at"),
        conflict=("id",), returning="id", has_rowid=False,
        extra_unique=(("role", "name"),),
        notes="UNIQUE(role,name)：次要约束冲突在 PG 下报 IntegrityViolation（SQLite 会替换），调用点已按冲突转用户错误"),
    "voices": TableSpec(
        columns=("id", "name", "kind", "provider", "voice_id", "sample_path", "status", "notes",
                 "created_at"),
        conflict=("id",), returning="id", has_rowid=False, extra_unique=(("name",),)),
    "runs": TableSpec(
        columns=("id", "stage", "source_id", "status", "message", "started_at", "finished_at"),
        conflict=("id",), returning="id", has_rowid=False),
}

# ---------------------------- 词法扫描 ----------------------------


def _segments(sql: str):
    """把 SQL 切成 (is_code, text) 片段：字符串/标识符/注释原样保留、顺序不变。"""
    out, buffer, i, n = [], [], 0, len(sql)

    def flush():
        if buffer:
            out.append((True, "".join(buffer)))
            buffer.clear()

    while i < n:
        char = sql[i]
        if char == "'":  # 字符串字面量（'' 转义）
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            flush()
            out.append((False, sql[i : j + 1]))
            i = j + 1
        elif char == '"':  # 双引号标识符
            j = sql.find('"', i + 1)
            j = n - 1 if j == -1 else j
            flush()
            out.append((False, sql[i : j + 1]))
            i = j + 1
        elif sql.startswith("--", i):  # 行注释
            j = sql.find("\n", i)
            j = n if j == -1 else j
            flush()
            out.append((False, sql[i:j]))
            i = j
        elif sql.startswith("/*", i):  # 块注释
            j = sql.find("*/", i + 2)
            j = n - 2 if j == -1 else j
            flush()
            out.append((False, sql[i : j + 2]))
            i = j + 2
        else:
            buffer.append(char)
            i += 1
    flush()
    return out


def _map_code(sql: str, transform) -> str:
    return "".join(text if not is_code else transform(text) for is_code, text in _segments(sql))


def convert_placeholders(sql: str) -> str:
    """``?`` → ``%s``（仅代码段；字符串/注释/标识符不动）。"""
    return _map_code(sql, lambda code: _PLACEHOLDER.sub("%s", code))


_RESERVED_TOKENS = re.compile(r"\b(end|trigger)\b", re.IGNORECASE)
_WORD = re.compile(r"\b\w+\b")


def quote_reserved(sql: str) -> str:
    """裸保留字标识符 ``end``/``trigger`` → 加引号形式（PG 保留字；字符串/注释不动）。

    ``CASE … END`` 的终结符不引用：按代码段的 CASE 深度区分（``end`` 在 CASE 内
    是终结符，深度 0 时才是 segments/media_references 等表的列名引用）。
    """
    depth = 0

    def convert_code(code: str) -> str:
        nonlocal depth

        def replace(match: re.Match) -> str:
            nonlocal depth
            word = match.group(0)
            lowered = word.lower()
            if lowered == "case":
                depth += 1
                return word
            if lowered == "end":
                if depth > 0:
                    depth -= 1
                    return word  # CASE 终结符
                return '"end"'
            if lowered == "trigger":
                return '"trigger"'
            return word

        return _WORD.sub(replace, code)

    return _map_code(sql, convert_code)


# json_extract 的**文本等值**形状（值是字符串：source_id/status/work/scope 等）：
# `json_extract(expr,'$.key') = ? / '字面量'` → `(expr::jsonb->>'key') = ...`。
# 数值比较、SELECT 列、嵌套路径、json_each/json_object 等形状不匹配 → 保持原文，
# 在 PG 上以「函数不存在」响亮失败（persons 证据路径的阶段 2/3 交付，不静默改写）。
_JSON_EXTRACT_EQ = re.compile(
    r"json_extract\(\s*(?P<expr>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*,"
    r"\s*'\$\.(?P<key>[A-Za-z_]\w*)'\s*\)(?P<cmp>\s*=\s*(?:%s|'[^']*'))",
    re.IGNORECASE,
)
# json_type(expr,'$.key') → jsonb_typeof：返回 'object'/'integer'/… 与 SQLite 同为文本。
_JSON_TYPE = re.compile(
    r"json_type\(\s*(?P<expr>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*,"
    r"\s*'\$\.(?P<key>[A-Za-z_]\w*)'\s*\)",
    re.IGNORECASE,
)


def convert_json_functions(sql: str) -> str:
    def extract_replacement(match: re.Match) -> str:
        expr, key = match.group("expr"), match.group("key")
        return f"({expr}::jsonb->>'{key}'){match.group('cmp')}"

    def type_replacement(match: re.Match) -> str:
        return f"jsonb_typeof({match.group('expr')}::jsonb->'{match.group('key')}')"

    return _JSON_TYPE.sub(type_replacement, _JSON_EXTRACT_EQ.sub(extract_replacement, sql))


_CURRENT_TIMESTAMP = re.compile(r"\bCURRENT_TIMESTAMP\b")
_PG_NOW_UTC = "(to_char(now() AT TIME ZONE 'utc','YYYY-MM-DD HH24:MI:SS'))"


def convert_current_timestamp(sql: str) -> str:
    """CURRENT_TIMESTAMP → 与 SQLite 同形的 UTC 文本（PG 原生带微秒与时区，格式不同）。"""
    return _map_code(sql, lambda code: _CURRENT_TIMESTAMP.sub(_PG_NOW_UTC, code))


# ---------------------------- INSERT 改写 ----------------------------

_INSERT_RE = re.compile(
    r"^\s*INSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO\s+(?P<table>[A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?:\((?P<cols>[^)]*)\))?\s*(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_PLAIN_INSERT_RE = re.compile(
    r"^\s*INSERT\s+INTO\s+(?P<table>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<rest>VALUES.*)$",
    re.IGNORECASE | re.DOTALL,
)


def _parse_baseline_tables() -> dict[str, tuple[str, ...]]:
    """从 PG 基线 DDL 解析全表列序（单一事实来源；供无列名 INSERT 补列）。"""
    from mindex.shared.db.schema_pg import BASELINE_DDL

    tables: dict[str, tuple[str, ...]] = {}
    for match in re.finditer(
            r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\);", BASELINE_DDL, re.DOTALL):
        name, body = match.group(1).lower(), match.group(2)
        columns, depth, current = [], 0, ""
        for char in body:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            if char == "," and depth == 0:
                columns.append(current)
                current = ""
            else:
                current += char
        if current.strip():
            columns.append(current)
        names = []
        for column in columns:
            entry = column.strip()
            head = entry.split()[0].strip('"').lower() if entry else ""
            # 约束子句（PRIMARY KEY(a,b)/UNIQUE(...)/CHECK(...)/FOREIGN KEY…）不是列。
            first_word = head.split("(")[0]
            if head and first_word not in {"primary", "unique", "check", "foreign", "constraint",
                                           "references"}:
                names.append(head)
        tables[name] = tuple(names)
    return tables


@cache
def _baseline_columns() -> dict[str, tuple[str, ...]]:
    return _parse_baseline_tables()


def rewrite_for_postgres(sql: str) -> str:
    """受控改写入口：占位符、JSON 函数、时间戳、保留字与登记过的 INSERT 改写。"""
    if match := _INSERT_RE.match(sql):
        return _rewrite_insert(match, sql)
    text = convert_json_functions(convert_placeholders(sql))
    text = quote_reserved(convert_current_timestamp(text))
    return _expand_positional_insert(text)


def _expand_positional_insert(sql: str) -> str:
    """``INSERT INTO t VALUES(…)``（无列名）：SQLite 隐式 rowid 不计数，PG 的显式
    rowid 列会与之差一。按基线列序补出列清单（不含 rowid），两侧行为一致。"""
    match = _PLAIN_INSERT_RE.match(sql)
    if not match:
        return sql
    table = match.group("table").lower()
    columns = _baseline_columns().get(table)
    if not columns:
        return sql
    listed = [column for column in columns if column != "rowid"]
    names = ",".join(_quote_column(column) for column in listed)
    return f"INSERT INTO {table} ({names}) {match.group('rest')}"


def _rewrite_insert(match: re.Match, original: str) -> str:
    verb, table = match.group(1).upper(), match.group("table").lower()
    rest = convert_json_functions(convert_placeholders(quote_reserved(match.group("rest")))).rstrip().rstrip(";")
    raw_columns = match.group("cols")
    if raw_columns:
        columns_sql = f"({raw_columns.strip()})"
    else:  # 无列名：按登记列序补出（SQLite 语义：除隐式 rowid 外按表序取值）
        spec = TABLE_SPECS.get(table)
        columns = spec.columns if spec else _baseline_columns().get(table, ())
        listed = [column for column in columns if column != "rowid"]
        columns_sql = f"({','.join(_quote_column(column) for column in listed)})"
    if verb == "IGNORE":  # 无冲突目标的 DO NOTHING 不需要表元数据
        return f"INSERT INTO {table}{columns_sql} {rest} ON CONFLICT DO NOTHING"
    spec = TABLE_SPECS.get(table)
    if spec is None:
        raise UnsupportedSql(f"表 {table} 未在 TABLE_SPECS 登记，不能自动改写：{original[:80]}…")
    listed = _columns(raw_columns, spec)
    sets = [f"{_quote_column(c)}=excluded.{_quote_column(c)}" for c in listed]
    unlisted = [column for column in spec.columns if column not in listed]
    if spec.has_rowid:
        unlisted.append("rowid")
    sets.extend(f"{_quote_column(column)}=DEFAULT" for column in unlisted)
    target = ", ".join(_quote_column(column) for column in spec.conflict)
    return (f"INSERT INTO {table}{columns_sql} {rest} "
            f"ON CONFLICT ({target}) DO UPDATE SET {', '.join(sets)}")


def _columns(raw: str | None, spec: TableSpec) -> tuple[str, ...]:
    if raw is None:  # 无列名 INSERT：按表的全列序解释
        return spec.columns
    return tuple(column.strip().strip('"') for column in raw.split(",") if column.strip())


def _quote_column(name: str) -> str:
    return f'"{name.lower()}"' if _RESERVED_TOKENS.fullmatch(name) else name
