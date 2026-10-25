"""日历来源：导入的 ICS/CSV 事件（存 ``topic_calendar_events``）或 ICS 订阅地址；只取今天起 ``days`` 天内的事件。

- ICS：VEVENT 的 SUMMARY、DTSTART（日期、日期时间、UTC ``Z`` 或 ``TZID``；不带时区的按本地时区）、DESCRIPTION、
  ``RRULE:FREQ=YEARLY``（每年重复，适合纪念日与"历史上的今天"）；支持折行与转义。拒绝含 DOCTYPE 的内容。
- CSV：表头至少有 ``date``、``title``，可选 ``desc``、``kind``、``recurring``（yearly/true/1/是）。
  ``date`` 为 ``YYYY-MM-DD``，或 ``MM-DD``（视为每年重复、没有起始年份）。
- 每年重复且有起始年份的事件，标题附"N 周年"。
- 样例（``topics/samples/*.csv``）随包分发（package-data，桌面版由 PyInstaller 收集），可在页面一键载入。
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from pydantic import Field

from mindex.shared.config import StrictModel
from mindex.topics.common import new_id
from mindex.topics.sources.base import FetchContext, Item, SourceError, http_get, item, register

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"
SAMPLES = {"finance": "金融：消费节点与数据发布（示例日期）", "tech": "科技：节日与发布会（示例日期）",
           "history": "历史：重大事件纪念日"}
MAX_EVENTS = 5000
TRUE = {"yearly", "true", "1", "yes", "y", "是", "每年"}


class CalendarConfig(StrictModel):
    url: str | None = Field(default=None, pattern=r"^https?://", max_length=2000,
                            description="ICS 订阅地址；留空时使用导入的事件")
    days: int = Field(default=14, ge=1, le=60, description="取今天起多少天内的事件")


class Event(StrictModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")  # 没有起始年份的事件存 2000 年（闰年，容纳 02-29）
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=1000)
    kind: str = Field(default="", max_length=40)
    recurring: bool = False
    has_year: bool = True


# ---------------------------------------------------------------- 解析


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value).strip()


def _ics_date(value: str, params: str) -> date | None:
    value = value.strip()
    try:
        if re.fullmatch(r"\d{8}", value):
            return datetime.strptime(value, "%Y%m%d").date()
        found = re.fullmatch(r"(\d{8}T\d{6})(Z?)", value)
        if found:
            moment = datetime.strptime(found.group(1), "%Y%m%dT%H%M%S")
            if found.group(2):
                from datetime import UTC

                return moment.replace(tzinfo=UTC).astimezone().date()
            return moment.date()  # 本地时间或 TZID：按日期取
    except ValueError:
        return None
    return None


def parse_ics(text: str) -> list[Event]:
    if re.search(r"<!\s*DOCTYPE", text[:4096], re.IGNORECASE):
        raise SourceError("日历内容不是 ICS 格式")
    if "BEGIN:VCALENDAR" not in text.upper():
        raise SourceError("日历内容不是 ICS 格式（缺少 BEGIN:VCALENDAR）")
    events, current = [], None
    for line in _unfold(text):
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            current = {}
            continue
        if upper == "END:VEVENT":
            if current and current.get("title") and current.get("date"):
                events.append(Event(date=current["date"].isoformat(), title=current["title"][:200],
                                    summary=current.get("summary", "")[:1000], recurring=current.get("recurring", False)))
            current = None
            continue
        if current is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, _, params = head.partition(";")
        name = name.upper()
        if name == "SUMMARY":
            current["title"] = " ".join(_unescape(value).split())
        elif name == "DESCRIPTION":
            current["summary"] = _unescape(value)
        elif name == "DTSTART":
            current["date"] = _ics_date(value, params)
        elif name == "RRULE":
            current["recurring"] = "FREQ=YEARLY" in value.upper()
        if len(events) >= MAX_EVENTS:
            break
    return events


def parse_csv(text: str) -> tuple[list[Event], list[str]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    fields = {(name or "").strip().lower() for name in reader.fieldnames or []}
    if not {"date", "title"} <= fields:
        raise SourceError("CSV 需要表头 date 与 title（可选 desc、kind、recurring）")
    events, errors = [], []
    for number, row in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items() if k}
        title, raw = " ".join(row.get("title", "").split()), row.get("date", "")
        if not title and not raw:
            continue
        recurring = row.get("recurring", "").lower() in TRUE
        has_year = True
        if re.fullmatch(r"\d{1,2}-\d{1,2}", raw):
            month, day = (int(part) for part in raw.split("-"))
            raw, recurring, has_year = f"2000-{month:02d}-{day:02d}", True, False
        try:
            parsed = date.fromisoformat(raw)
        except ValueError:
            errors.append(f"第 {number} 行日期无效：{raw or '（空）'}")
            continue
        if not title:
            errors.append(f"第 {number} 行缺少标题")
            continue
        events.append(Event(date=parsed.isoformat(), title=title[:200], summary=row.get("desc", "")[:1000],
                            kind=row.get("kind", "")[:40], recurring=recurring, has_year=has_year))
        if len(events) >= MAX_EVENTS:
            break
    return events, errors


def parse_text(text: str, fmt: str | None = None) -> tuple[list[Event], list[str]]:
    fmt = fmt or ("ics" if "BEGIN:VCALENDAR" in text[:2000].upper() else "csv")
    if fmt == "ics":
        return parse_ics(text), []
    return parse_csv(text)


# ---------------------------------------------------------------- 事件 → 候选条目


def occurrences(event: Event, today: date, days: int) -> list[date]:
    start = date.fromisoformat(event.date)
    end = today + timedelta(days=days)
    if not event.recurring:
        return [start] if today <= start <= end else []
    found = []
    for year in (today.year, today.year + 1):
        try:
            moment = start.replace(year=year)
        except ValueError:  # 2 月 29 日：非闰年改到 2 月 28 日
            moment = start.replace(year=year, day=28)
        if today <= moment <= end and (not event.has_year or moment.year >= start.year):
            found.append(moment)
    return found


def to_items(events: list[Event], today: date, days: int, source_key: str) -> list[Item]:
    items = []
    for event in events:
        for moment in occurrences(event, today, days):
            years = moment.year - date.fromisoformat(event.date).year if event.recurring and event.has_year else 0
            title = f"{event.title}（{years} 周年）" if years > 0 else event.title
            when = datetime.combine(moment, datetime.min.time()).astimezone().isoformat(timespec="seconds")
            label = f"{event.kind}：" if event.kind else ""
            found = item(title, label + event.summary, event_at=when,
                         extra={"dedup": f"cal:{source_key}:{moment.isoformat()}:{event.title}"[:300]})
            if found:
                items.append(found)
    items.sort(key=lambda value: value.event_at)
    return items


# ---------------------------------------------------------------- 存储


def replace_events(store, source_id: str, events: list[Event], *, append: bool = False) -> int:
    with store.connect() as db:
        if not append:
            db.execute("DELETE FROM topic_calendar_events WHERE source_id=?", (source_id,))
        for event in events:
            db.execute("INSERT INTO topic_calendar_events(id,source_id,date,title,summary,kind,recurring,has_year) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       (new_id("ti"), source_id, event.date, event.title, event.summary, event.kind,
                        int(event.recurring), int(event.has_year)))
        return db.execute("SELECT COUNT(*) FROM topic_calendar_events WHERE source_id=?", (source_id,)).fetchone()[0]


def stored_events(store, source_id: str) -> list[Event]:
    rows = store.rows("SELECT * FROM topic_calendar_events WHERE source_id=? ORDER BY date, rowid", (source_id,))
    return [Event(date=r["date"], title=r["title"], summary=r["summary"], kind=r["kind"], recurring=bool(r["recurring"]),
                  has_year=bool(r["has_year"])) for r in rows]


def sample_list() -> list[dict]:
    result = []
    for name, label in SAMPLES.items():
        path = SAMPLES_DIR / f"{name}.csv"
        events, _ = parse_csv(path.read_text(encoding="utf-8"))
        result.append({"name": name, "label": label, "events": len(events)})
    return result


def sample_text(name: str) -> str:
    if name not in SAMPLES:
        raise ValueError(f"没有这个日历样例：{name}（可选 {'、'.join(SAMPLES)}）")
    return (SAMPLES_DIR / f"{name}.csv").read_text(encoding="utf-8")


class CalendarSource:
    kind: ClassVar[str] = "calendar"
    label: ClassVar[str] = "日历"
    config_model: ClassVar[type] = CalendarConfig
    third_party: ClassVar[bool] = True  # 订阅地址会被请求；只用导入事件时不发请求
    secret: ClassVar[str | None] = None

    def fetch(self, ctx: FetchContext, cfg: CalendarConfig) -> list[Item]:
        today = datetime.now().astimezone().date()
        source_id = ctx.source_id or ""
        if cfg.url:
            _, _, body = http_get(ctx.config, cfg.url, label="日历订阅")
            try:
                text = body.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise SourceError("日历订阅不是 UTF-8 文本") from exc
            events = parse_ics(text)
        else:
            events = stored_events(ctx.store, source_id)
            if not events:
                raise SourceError("还没有导入日历事件：上传 ICS/CSV 或载入样例")
        return to_items(events, today, cfg.days, source_id)[: ctx.config.topics.fetch.max_items_per_source]


register(CalendarSource())

