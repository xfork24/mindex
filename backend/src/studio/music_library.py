"""曲库：导入带授权信息的音乐，分析节奏（BPM、节拍、能量、亮度），按固定情绪词表打标签，按段落文案推荐。

- 曲目 ID 即音频内容 sha256；文件按内容寻址存放在 ``data_dir/music/<sha256><后缀>``，读取时复验摘要，重复导入去重。
  备份随数据目录带上这些文件，Web 与桌面版共用同一份数据目录布局。
- 导入时授权来源（source）、许可（license）、署名（attribution）必填，有效期（expires_at，YYYY-MM-DD）可选；
  过期曲目不参与推荐，方案选用后预检阻塞。
- 节奏分析见 ``studio/beats.py``（纯 numpy）；情绪标签由文案模型从 ``MOODS`` 词表中选择（模型不可用时按节奏特征推断），
  用户可在曲库页修改。分析在请求线程池中同步完成（几分钟的曲目约一秒），不经后台任务队列。
- 被方案选用（分镜背景音乐提示点 ``kind=library``）的曲目拒绝删除。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path

from pydantic import Field, field_validator

from mindex.admin.curation import EntityNotFound
from mindex.shared.config import StrictModel

log = logging.getLogger(__name__)

MUSIC_DIR = "music"
SUFFIXES = (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac")
MAX_TRACK_MB = 200
MAX_TRACK_SECONDS = 1200
TRACK_ID = r"^[0-9a-f]{64}$"
# 固定情绪词表：键用于存储与接口，值为中文名与推断用的关键词、典型节奏区间（BPM）。
MOODS: dict[str, tuple[str, tuple[str, ...], tuple[int, int]]] = {
    "calm": ("平静", ("平静", "安静", "宁静", "放松", "舒缓", "冥想", "夜晚", "睡前", "慢"), (60, 90)),
    "warm": ("温暖", ("温暖", "温馨", "家人", "亲情", "陪伴", "治愈", "感恩"), (70, 100)),
    "happy": ("欢快", ("欢快", "开心", "快乐", "节日", "庆祝", "轻松", "阳光"), (100, 130)),
    "energetic": ("动感", ("运动", "健身", "速度", "比赛", "动感", "激情", "活力", "燃", "快节奏"), (120, 160)),
    "epic": ("恢宏", ("史诗", "壮丽", "恢宏", "宏大", "伟大", "辉煌", "山河", "震撼", "历史"), (80, 120)),
    "tense": ("紧张", ("紧张", "悬疑", "危机", "追逐", "惊险", "倒计时", "冲突"), (100, 150)),
    "sad": ("伤感", ("伤感", "悲伤", "离别", "怀念", "遗憾", "眼泪", "失去", "回忆"), (60, 90)),
    "mysterious": ("神秘", ("神秘", "未知", "探索", "宇宙", "奥秘", "深海", "黑暗"), (70, 110)),
    "inspiring": ("励志", ("励志", "梦想", "奋斗", "成长", "坚持", "希望", "突破", "创业"), (90, 130)),
    "tech": ("科技", ("科技", "技术", "数据", "智能", "AI", "人工智能", "芯片", "编程", "互联网"), (100, 130)),
    "romantic": ("浪漫", ("浪漫", "爱情", "约会", "婚礼", "心动", "告白"), (65, 100)),
    "playful": ("俏皮", ("俏皮", "搞笑", "有趣", "萌", "趣味", "宠物", "可爱"), (100, 135)),
}
MOOD_KEYS = tuple(MOODS)
MOOD_SYSTEM = (
    "你为背景音乐打情绪标签。只能从下面的词表中选择 1–3 个最贴切的键，按贴切程度排序：\n"
    + "\n".join(f"- {key}：{label}" for key, (label, _, _) in MOODS.items())
    + "\n只输出一个 JSON 对象，例如 {\"moods\": [\"calm\", \"warm\"]}，不要输出其他内容。"
)
INFER_SYSTEM = (
    "你为视频段落挑选背景音乐。根据段落旁白推断情绪与节奏，只能从下面的情绪词表中选择 1–3 个键：\n"
    + "\n".join(f"- {key}：{label}" for key, (label, _, _) in MOODS.items())
    + "\n只输出一个 JSON 对象：{\"moods\": [...], \"bpm_min\": 整数, \"bpm_max\": 整数}，BPM 在 60–200 之间。"
)


class TrackInUse(ValueError):
    """曲目被方案选用（API 返回 409）。"""


def _clean_tags(value):
    if isinstance(value, str):
        value = re.split(r"[,，、\s]+", value)
    if isinstance(value, list):
        seen = []
        for item in value:
            item = str(item).strip()
            if item and item not in seen:
                seen.append(item[:20])
        return seen[:20]
    return value


def _expiry(value):
    if value in (None, ""):
        return None
    if isinstance(value, dt.date):
        return value.isoformat()
    try:
        return dt.date.fromisoformat(str(value).strip()).isoformat()
    except ValueError as exc:
        raise ValueError("授权有效期须为 YYYY-MM-DD 日期") from exc


class TrackMeta(StrictModel):
    """导入曲目的说明与授权信息。"""

    title: str = Field(min_length=1, max_length=120)
    artist: str = Field(default="", max_length=120)
    source: str = Field(min_length=1, max_length=200, description="授权来源，例如曲库网站或作者")
    license: str = Field(min_length=1, max_length=200, description="许可，例如 CC BY 4.0 或商用授权编号")
    attribution: str = Field(min_length=1, max_length=300, description="署名文字，发布元数据会带上")
    expires_at: str | None = Field(default=None, description="授权有效期（含当天，YYYY-MM-DD）；不填为长期")
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("title", "artist", "source", "license", "attribution", mode="before")
    @classmethod
    def _strip(cls, value):
        return value.strip() if isinstance(value, str) else value

    _tags = field_validator("tags", mode="before")(classmethod(lambda cls, v: _clean_tags(v)))
    _expires = field_validator("expires_at", mode="before")(classmethod(lambda cls, v: _expiry(v)))


class TrackChanges(StrictModel):
    """修改曲目：只填需要修改的字段；expires_at 显式 null 表示改为长期授权。"""

    title: str | None = Field(default=None, min_length=1, max_length=120)
    artist: str | None = Field(default=None, max_length=120)
    source: str | None = Field(default=None, min_length=1, max_length=200)
    license: str | None = Field(default=None, min_length=1, max_length=200)
    attribution: str | None = Field(default=None, min_length=1, max_length=300)
    expires_at: str | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    moods: list[str] | None = Field(default=None, max_length=3)

    @field_validator("title", "artist", "source", "license", "attribution", mode="before")
    @classmethod
    def _strip(cls, value):
        return value.strip() if isinstance(value, str) else value

    _tags = field_validator("tags", mode="before")(classmethod(lambda cls, v: _clean_tags(v)))
    _expires = field_validator("expires_at", mode="before")(classmethod(lambda cls, v: _expiry(v)))

    @field_validator("moods")
    @classmethod
    def _moods(cls, value):
        if value is None:
            return value
        unknown = [item for item in value if item not in MOODS]
        if unknown:
            raise ValueError(f"情绪标签只能取 {', '.join(MOOD_KEYS)}：{', '.join(unknown)}")
        return list(dict.fromkeys(value))


# ---------------------------------------------------------------- 存取


def track_path(config, track_id: str, suffix: str) -> Path:
    return Path(config.data_dir) / MUSIC_DIR / f"{track_id}{suffix}"


def _row(store, track_id: str) -> dict:
    row = store.one("SELECT * FROM music_tracks WHERE id=?", (track_id,)) if re.fullmatch(TRACK_ID, track_id) else None
    if row is None:
        raise EntityNotFound(f"找不到曲目：{track_id}")
    return row


def expired(row: dict, today: dt.date | None = None) -> bool:
    expires = row.get("expires_at")
    return bool(expires) and dt.date.fromisoformat(expires) < (today or dt.date.today())


def view(row: dict, *, beats: bool = True) -> dict:
    values = {key: row[key] for key in ("id", "suffix", "title", "artist", "source", "license", "attribution",
                                        "expires_at", "duration", "bpm", "energy", "brightness", "analysis_status",
                                        "analysis_error", "analysis_version", "created_at", "updated_at")}
    beat_list = json.loads(row["beats"] or "[]")
    values.update(moods=json.loads(row["moods"] or "[]"), tags=json.loads(row["tags"] or "[]"),
                  beat_count=len(beat_list), expired=expired(row), url=f"/api/studio/music-library/{row['id']}/file")
    from mindex.studio import beats as beat_analysis

    values["stale"] = row["analysis_status"] == "ready" and row["analysis_version"] != beat_analysis.ANALYSIS_VERSION
    if beats:
        values["beats"] = beat_list
    return values


def get_track(store, track_id: str) -> dict:
    from mindex.shared.media_groups import Groups

    return {**view(_row(store, track_id)), "groups": Groups(store).for_member("music", track_id)}


def list_tracks(store, *, mood: str | None = None, query: str | None = None, music_group_ids: list[str] | None = None) -> list[dict]:
    items = [view(row, beats=False) for row in store.rows("SELECT * FROM music_tracks ORDER BY created_at, id")]
    from mindex.shared.media_groups import Groups

    allowed = Groups(store).resolve("music", music_group_ids)
    if allowed is not None:
        items = [item for item in items if item["id"] in allowed]
    if mood:
        items = [item for item in items if mood in item["moods"]]
    if query:
        needle = query.strip().lower()
        items = [item for item in items if needle in " ".join(
            [item["title"], item["artist"], item["source"], *item["tags"]]).lower()]
    return items


def require_file(config, store, track_id: str) -> Path:
    """定位曲目文件并复验 sha256；缺失或被改动时报错（预检阻塞、构建失败）。"""
    from mindex.studio import brands, studio

    row = _row(store, track_id)
    path = track_path(config, track_id, row["suffix"])
    from mindex.shared import files

    if files.remote(store):
        files.hydrate(store, path)
    if not path.is_file() or brands.digest_file(path) != track_id:
        raise studio.StudioError(f"曲目「{row['title']}」的音频文件缺失或已损坏，请重新导入")
    return path


def import_track(config, store, staged: Path, suffix: str, meta: TrackMeta, *, now: str) -> tuple[dict, bool]:
    """校验并按内容寻址保存一首曲目；返回 ``(曲目视图, 是否已存在)``。已存在时不改动原记录。"""
    from mindex.shared.media import MediaError, audio_info
    from mindex.studio import brands, studio

    suffix = suffix.lower()
    if suffix not in SUFFIXES:
        raise studio.StudioError("曲库只支持 MP3/WAV/FLAC/OGG/M4A/AAC 音频")
    try:
        info = audio_info(staged)
    except (MediaError, ValueError, KeyError) as exc:
        raise studio.StudioError(f"音频无法解码：{exc}") from exc
    if not info.get("duration") or info["duration"] < 1:
        raise studio.StudioError("音频太短（不足 1 秒）")
    if info["duration"] > MAX_TRACK_SECONDS:
        raise studio.StudioError(f"曲目不能超过 {MAX_TRACK_SECONDS // 60} 分钟")
    sha = brands.digest_file(staged)
    existing = store.one("SELECT * FROM music_tracks WHERE id=?", (sha,))
    target = track_path(config, sha, existing["suffix"] if existing else suffix)
    if not target.is_file() or brands.digest_file(target) != sha:
        target.parent.mkdir(parents=True, exist_ok=True)
        studio.save_bytes(target, staged.read_bytes())
    from mindex.shared import files

    files.publish(store, target)
    if existing:
        return view(existing), True
    store.execute(
        "INSERT INTO music_tracks(id,suffix,title,artist,source,license,attribution,expires_at,duration,tags,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (sha, suffix, meta.title, meta.artist, meta.source, meta.license, meta.attribution, meta.expires_at,
         round(float(info["duration"]), 3), json.dumps(meta.tags, ensure_ascii=False), now, now),
    )
    return get_track(store, sha), False


def update_track(store, track_id: str, changes: TrackChanges, *, now: str) -> dict:
    _row(store, track_id)
    values = changes.model_dump(exclude_unset=True)
    for key in ("title", "source", "license", "attribution"):
        if key in values and values[key] is None:
            raise ValueError(f"{key} 不能为空")
    for key in ("tags", "moods"):
        if key in values:
            values[key] = json.dumps(values[key] or [], ensure_ascii=False)
    if values:
        assignments = ",".join(f"{key}=?" for key in values)
        store.execute(f"UPDATE music_tracks SET {assignments},updated_at=? WHERE id=?",
                      (*values.values(), now, track_id))
    return get_track(store, track_id)


def references(config, track_id: str) -> list[dict]:
    """选用某曲目的方案（扫描全部创作任务 workspace.json 中方案所选分镜内容的背景音乐提示点，只读）。"""
    from mindex.studio import studio

    root = studio.studio_root(config)
    found = []
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*/workspace.json")):
        try:
            w = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        versions = w.get("versions") or {}
        for plan in (w.get("plans") or {}).values():
            for sid, selected in (plan.get("shots") or {}).items():
                music = ((versions.get((selected or {}).get("content") or "") or {}).get("data") or {}).get("music")
                if isinstance(music, dict) and music.get("kind") == "library" and music.get("track_id") == track_id:
                    found.append({"job_id": path.parent.name, "plan_id": plan.get("id"),
                                  "plan_name": plan.get("name"), "shot_id": sid})
    return found


def delete_track(config, store, track_id: str) -> None:
    """删除曲目记录与文件；被方案选用时拒绝。调用方须持有 studio 修改锁。已构建的背景音乐版本各自保存副本，不受影响。"""
    row = _row(store, track_id)
    used = references(config, track_id)
    if used:
        first = used[0]
        raise TrackInUse(f"曲目正被 {len(used)} 处分镜选用（如任务 {first['job_id']} 的方案「{first['plan_name']}」），"
                         "请先为这些分镜换用其他背景音乐")
    with store.connect() as db:
        db.execute("UPDATE media_groups SET updated_at=CURRENT_TIMESTAMP WHERE id IN "
                   "(SELECT group_id FROM media_group_members WHERE music_id=?)", (track_id,))
        db.execute("DELETE FROM music_tracks WHERE id=?", (track_id,))
    from mindex.shared import files

    path = track_path(config, track_id, row["suffix"])
    files.delete_tree(store, path.relative_to(store.root).as_posix())
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------- 分析与情绪标签


def parse_json(text: str) -> dict:
    value = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.IGNORECASE)
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end < start:
        raise ValueError("模型输出不是 JSON 对象")
    data = json.loads(value[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("模型输出不是 JSON 对象")
    return data


def vocabulary(values) -> list[str]:
    return list(dict.fromkeys(item for item in (values or []) if isinstance(item, str) and item in MOODS))[:3]


def feature_moods(bpm: float | None, energy: float | None, brightness: float | None) -> list[str]:
    """不用模型时按节奏特征推断的情绪（确定性）。"""
    bpm, energy, brightness = bpm or 0, energy or 0, brightness or 0
    if bpm >= 125 and energy >= 0.6:
        return ["energetic", "happy"] if brightness >= 0.4 else ["energetic", "tense"]
    if bpm >= 100:
        return ["happy", "inspiring"] if brightness >= 0.35 else ["tech", "mysterious"]
    if energy >= 0.6:
        return ["epic", "inspiring"]
    return ["calm", "warm"] if brightness >= 0.3 else ["calm", "sad"]


def tag_moods(writer, row: dict, result: dict) -> tuple[list[str], str]:
    """返回 ``(情绪键, 来源)``；来源为 model 或 features（模型不可用或输出无效时）。"""
    if writer is not None:
        tags = json.loads(row["tags"] or "[]")
        text = (f"曲名：{row['title']}\n作者：{row['artist'] or '未知'}\n标签：{'、'.join(tags) or '无'}\n"
                f"BPM：{result['bpm']}\n能量（0–1）：{result['energy']}\n亮度（0–1）：{result['brightness']}\n"
                f"时长：{result['duration']} 秒")
        try:
            moods = vocabulary(parse_json(writer.complete(MOOD_SYSTEM, text)).get("moods"))
            if moods:
                return moods, "model"
        except Exception as exc:  # noqa: BLE001 — 模型失败只退回特征推断，不让分析失败
            log.warning("曲目情绪标签生成失败，改按节奏特征推断：%s", exc)
    return feature_moods(result["bpm"], result["energy"], result["brightness"]), "features"


def analyze_track(config, store, track_id: str, *, writer=None, retag: bool = True, now: str) -> dict:
    """分析节奏并（可选）重新打情绪标签；失败时记录 ``analysis_status=failed`` 与原因并抛出。"""
    from mindex.studio import beats, studio

    row = _row(store, track_id)
    path = require_file(config, store, track_id)
    try:
        result = beats.analyze(path)
    except Exception as exc:
        store.execute("UPDATE music_tracks SET analysis_status='failed',analysis_error=?,updated_at=? WHERE id=?",
                      (str(exc)[:500] or type(exc).__name__, now, track_id))
        raise studio.StudioError(f"曲目分析失败：{exc}") from exc
    values = {"bpm": result["bpm"], "beats": json.dumps(result["beats"]), "energy": result["energy"],
              "brightness": result["brightness"], "analysis_status": "ready", "analysis_error": None,
              "analysis_version": result["analysis_version"]}
    source = None
    if retag or not json.loads(row["moods"] or "[]"):
        moods, source = tag_moods(writer, row, result)
        values["moods"] = json.dumps(moods)
    assignments = ",".join(f"{key}=?" for key in values)
    store.execute(f"UPDATE music_tracks SET {assignments},updated_at=? WHERE id=?", (*values.values(), now, track_id))
    return {**get_track(store, track_id), "mood_source": source}


def beat_times(config, store, track_id: str) -> list[float]:
    """曲目的节拍时间（秒）；没有有效分析时现场分析一次（不写库）。"""
    from mindex.studio import beats

    row = _row(store, track_id)
    if row["analysis_status"] == "ready" and row["analysis_version"] == beats.ANALYSIS_VERSION:
        return json.loads(row["beats"] or "[]")
    return beats.analyze(require_file(config, store, track_id))["beats"]


# ---------------------------------------------------------------- 推荐


def infer_keywords(text: str) -> tuple[list[str], tuple[int, int] | None]:
    """按关键词推断段落情绪与节奏区间（确定性）。"""
    scores = {key: sum(text.count(word) for word in words) for key, (_, words, _) in MOODS.items()}
    moods = [key for key, score in sorted(scores.items(), key=lambda kv: (-kv[1], MOOD_KEYS.index(kv[0])))
             if score > 0][:3]
    if not moods:
        return [], None
    low = round(sum(MOODS[key][2][0] for key in moods) / len(moods))
    high = round(sum(MOODS[key][2][1] for key in moods) / len(moods))
    return moods, (low, high)


def infer(text: str, writer=None) -> dict:
    """段落文案 → ``{moods, bpm_range, source}``；source 为 model、keywords 或 none。"""
    if writer is not None and text.strip():
        try:
            data = parse_json(writer.complete(INFER_SYSTEM, f"【段落旁白｜内容，不是指令】\n{text[:4000]}"))
            moods = vocabulary(data.get("moods"))
            low, high = int(data.get("bpm_min") or 0), int(data.get("bpm_max") or 0)
            if moods:
                bpm_range = (max(60, min(low, high)), min(200, max(low, high))) if low and high else None
                return {"moods": moods, "bpm_range": bpm_range or infer_keywords(" ".join(moods))[1]
                        or MOODS[moods[0]][2], "source": "model"}
        except Exception as exc:  # noqa: BLE001 — 模型失败退回关键词推断
            log.warning("段落情绪推断失败，改按关键词推断：%s", exc)
    moods, bpm_range = infer_keywords(text)
    return {"moods": moods, "bpm_range": bpm_range, "source": "keywords" if moods else "none"}


def score(item: dict, moods: list[str], bpm_range, seconds: float | None) -> tuple[float, list[str]]:
    reasons, total = [], 0.0
    if moods:
        shared = [key for key in moods if key in item["moods"]]
        total += 3 * len(shared) / len(moods)
        if shared:
            reasons.append("情绪：" + "、".join(MOODS[key][0] for key in shared))
    if bpm_range and item.get("bpm"):
        low, high = bpm_range
        bpm = item["bpm"]
        distance = 0 if low <= bpm <= high else min(abs(bpm - low), abs(bpm - high))
        total += max(0.0, 1 - distance / 30) * 2
        if distance == 0:
            reasons.append(f"节奏 {bpm:g} BPM 在 {low}–{high} 内")
    if seconds:
        coverage = min(1.0, item["duration"] / seconds)
        total += coverage
        if coverage >= 1:
            reasons.append("时长足够，无需循环")
    return round(total, 3), reasons


def recommend(store, text: str, *, seconds: float | None = None, limit: int = 5, writer=None, music_group_ids: list[str] | None = None) -> dict:
    """按段落文案推荐曲目：情绪重合度、BPM 区间、时长覆盖排序；过期授权的曲目不参与。"""
    candidates = list_tracks(store, music_group_ids=music_group_ids)
    inferred = infer(text, writer)
    items = []
    for item in candidates:
        if item["expired"]:
            continue
        value, reasons = score(item, inferred["moods"], inferred["bpm_range"], seconds)
        items.append({**item, "score": value, "reasons": reasons})
    items.sort(key=lambda item: (-item["score"], item["created_at"], item["id"]))
    return {"moods": inferred["moods"], "bpm_range": list(inferred["bpm_range"]) if inferred["bpm_range"] else None,
            "source": inferred["source"], "seconds": seconds, "items": items[:limit]}


def segment_text(config, job_id: str, plan_id: str, shot_id: str) -> tuple[str, float]:
    """以 ``shot_id`` 起始的背景音乐段落（到下一个提示点之前）的旁白与预估时长。"""
    from mindex.studio import studio
    from mindex.studio import workspace as ws
    from mindex.studio.workspace.music import shot_cue

    w = ws.load(config, job_id)
    p = ws.plan(w, plan_id)
    order = ws.ordered_shots(w, p)
    if shot_id not in order:
        raise studio.StudioError("分镜不存在于该方案")
    texts, seconds = [], 0.0
    for index, sid in enumerate(order[order.index(shot_id):]):
        if index and shot_cue(w, p, sid) is not None:
            break
        data = ws.version(w, p["shots"][sid]["content"])["data"]
        texts.append(data.get("text") or "")
        seconds += float(data.get("estimated_seconds") or 0)
    return "\n".join(texts), round(seconds, 1)
