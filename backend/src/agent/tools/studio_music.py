"""Studio 曲库工具：列出、读取、导入、修改、分析曲目与按段落文案推荐（``/api/studio/music-library``）。

- 曲目 ID 即音频内容 sha256；导入时授权来源、许可、署名必填，重复内容返回已有曲目。
- 选用曲目：edit_shots 的 music 设为 ``{kind: "library", track_id}``，从该分镜起铺这首曲目，之后的分镜顺延。
- 推荐会调用文案模型推断情绪与节奏（不可用时按关键词推断）；授权过期的曲目不参与推荐，选用后预检阻塞。
- 曲库是全局数据：改动事件为 ``{domain: "studio", music_library: true}``。删除只在曲库页或 CLI 中进行。
"""

from __future__ import annotations

from pydantic import Field, model_validator

from mindex.agent.registry import Preview, ToolContext, ToolError, ToolOutput, ToolSpec
from mindex.agent.tools.common import check, load_view
from mindex.agent.tools.studio_detail import shot_ref
from mindex.agent.tools.studio_write import JobId, PlanId
from mindex.shared.config import StrictModel

TrackId = Field(pattern=r"^[0-9a-f]{64}$", description="曲目 ID（list_music_tracks 或 recommend_music 给出的完整 64 位 ID）")
FOCUS = {"page": "music"}  # 素材管理 · 音乐库（/admin/music）
MUTATION = {"domain": "studio", "music_library": True}
MOOD_LABELS = {"calm": "平静", "warm": "温暖", "happy": "欢快", "energetic": "动感", "epic": "恢宏", "tense": "紧张",
               "sad": "伤感", "mysterious": "神秘", "inspiring": "励志", "tech": "科技", "romantic": "浪漫",
               "playful": "俏皮"}
MOOD_KEYS = tuple(MOOD_LABELS)


def moods_text(moods) -> str:
    return "、".join(MOOD_LABELS.get(key, key) for key in moods or []) or "无"


def license_text(item: dict) -> str:
    expiry = ("已过期（" + item["expires_at"] + "）") if item.get("expired") else (
        f"有效至 {item['expires_at']}" if item.get("expires_at") else "长期")
    return f"来源 {item['source']}，许可 {item['license']}，{expiry}"


def track_row(item: dict) -> str:
    bpm = f"{item['bpm']:g} BPM" if item.get("bpm") else {"pending": "待分析", "failed": "分析失败"}.get(
        item.get("analysis_status"), "—")
    return (f"{item['id']} | {item['title']}{(' — ' + item['artist']) if item.get('artist') else ''} | "
            f"{item['duration']:g}s | {bpm} | {moods_text(item.get('moods'))} | {'已过期' if item.get('expired') else '可用'}")


def track_text(item: dict) -> str:
    lines = [f"曲目 {item['id']}「{item['title']}」{('，作者 ' + item['artist']) if item.get('artist') else ''}",
             f"授权：{license_text(item)}", f"署名：{item['attribution']}",
             f"时长 {item['duration']:g} 秒；标签：{'、'.join(item.get('tags') or []) or '无'}；情绪：{moods_text(item.get('moods'))}"]
    if item.get("groups"):
        lines.append("所属组：" + "、".join(f"{group['name']} ({group['id']})" for group in item["groups"]))
    if item.get("analysis_status") == "ready":
        lines.append(f"节奏分析：{item['bpm']:g} BPM，{item.get('beat_count')} 个节拍，能量 {item.get('energy')}，"
                     f"亮度 {item.get('brightness')}" + ("（分析算法已更新，建议 analyze_music_track）" if item.get("stale") else ""))
    else:
        lines.append(f"节奏分析：{'失败：' + (item.get('analysis_error') or '') if item.get('analysis_status') == 'failed' else '尚未分析'}")
    return "\n".join(lines)


def compact(item: dict) -> dict:
    return {key: item.get(key) for key in ("id", "title", "artist", "duration", "bpm", "moods", "tags", "source",
                                           "license", "attribution", "expires_at", "expired", "analysis_status")}


class ListMusicTracks(StrictModel):
    music_group_ids: list[str] = Field(default_factory=list, description="多组成员取并集，再与其他限制取交集；空组无候选")
    mood: str | None = Field(default=None, description=f"按情绪键筛选：{', '.join(MOOD_KEYS)}")
    query: str | None = Field(default=None, max_length=100, description="按曲名、作者、来源、标签搜索")


async def list_music_tracks(args: ListMusicTracks, ctx: ToolContext) -> ToolOutput:
    params = {key: value for key, value in (("mood", args.mood), ("q", args.query)) if value}
    if args.music_group_ids:
        params["music_group_ids"] = args.music_group_ids
    items = check(await ctx.client.get("/api/studio/music-library", **params), "列出曲库")["items"]
    rows = [compact(item) for item in items]
    text = "\n".join(track_row(item) for item in items) or "（曲库为空：可用 import_music_track 导入）"
    return ToolOutput({"items": rows}, text=f"共 {len(rows)} 首曲目（ID | 曲名 | 时长 | BPM | 情绪 | 授权）：\n{text}\n"
                                            "用 edit_shots 的 music={kind: library, track_id} 选用。", focus={**FOCUS, **({"music_group_ids": args.music_group_ids} if args.music_group_ids else {})})


class GetMusicTrack(StrictModel):
    track_id: str = TrackId


async def get_music_track(args: GetMusicTrack, ctx: ToolContext) -> ToolOutput:
    item = check(await ctx.client.get(f"/api/studio/music-library/{args.track_id}"), "读取曲目")
    item.pop("beats", None)
    return ToolOutput(item, text=track_text(item), focus=FOCUS)


class ImportMusicTrack(StrictModel):
    attachment: str = Field(pattern=r"^attachment://", description="音频附件引用（MP3/WAV/FLAC/OGG/M4A/AAC，≤20 分钟）")
    source: str = Field(min_length=1, max_length=200, description="授权来源（必填），例如曲库网站或作者")
    license: str = Field(min_length=1, max_length=200, description="许可（必填），例如 CC BY 4.0")
    attribution: str = Field(min_length=1, max_length=300, description="署名（必填），发布元数据会带上")
    title: str | None = Field(default=None, max_length=120, description="曲名，缺省为文件名")
    artist: str | None = Field(default=None, max_length=120)
    expires_at: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="授权有效期 YYYY-MM-DD，缺省长期")
    tags: list[str] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def filled(self):
        if not (self.source.strip() and self.license.strip() and self.attribution.strip()):
            raise ValueError("授权来源、许可与署名不能为空")
        return self


async def preview_import_music_track(args: ImportMusicTrack, ctx: ToolContext) -> Preview:
    return Preview("导入曲目到曲库并分析节奏（按内容去重，不改动任何方案）", [
        {"label": "附件", "after": args.attachment},
        {"label": "授权", "after": f"来源 {args.source}，许可 {args.license}，" + (f"有效至 {args.expires_at}" if args.expires_at else "长期")},
        {"label": "署名", "after": args.attribution}],
        warnings=["请确认授权信息真实有效：署名会写进发布元数据"], focus=FOCUS)


async def import_music_track(args: ImportMusicTrack, ctx: ToolContext) -> ToolOutput:
    data = {"source": args.source, "license": args.license, "attribution": args.attribution, "title": args.title,
            "artist": args.artist, "expires_at": args.expires_at, "tags": ",".join(args.tags or []) or None}
    item = check(await ctx.client.upload("/api/studio/music-library", files={"file": args.attachment}, data=data),
                 "导入曲目")
    item.pop("beats", None)
    head = "曲库中已有相同音频（未改动原记录）：" if item.get("duplicate") else "已导入曲目："
    return ToolOutput(item, text=head + "\n" + track_text(item), mutation=MUTATION, focus=FOCUS)


class UpdateMusicTrack(StrictModel):
    track_id: str = TrackId
    title: str | None = Field(default=None, min_length=1, max_length=120)
    artist: str | None = Field(default=None, max_length=120)
    source: str | None = Field(default=None, min_length=1, max_length=200)
    license: str | None = Field(default=None, min_length=1, max_length=200)
    attribution: str | None = Field(default=None, min_length=1, max_length=300)
    expires_at: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="授权有效期；显式 null 改为长期")
    tags: list[str] | None = Field(default=None, max_length=20, description="标签（整体替换）")
    moods: list[str] | None = Field(default=None, max_length=3, description=f"情绪键（整体替换，≤3）：{', '.join(MOOD_KEYS)}")

    def changes(self) -> dict:
        given = self.model_fields_set - {"track_id"}
        return {key: getattr(self, key) for key in given if getattr(self, key) is not None or key == "expires_at"}

    @model_validator(mode="after")
    def something(self):
        if not self.changes():
            raise ValueError("至少修改一项")
        unknown = [key for key in self.moods or [] if key not in MOOD_LABELS]
        if unknown:
            raise ValueError(f"情绪键只能取 {', '.join(MOOD_KEYS)}")
        return self


async def preview_update_music_track(args: UpdateMusicTrack, ctx: ToolContext) -> Preview:
    item = check(await ctx.client.get(f"/api/studio/music-library/{args.track_id}"), "读取曲目")
    rows = [{"label": key, "before": str(item.get(key)), "after": str(value)} for key, value in args.changes().items()
            if item.get(key) != value]
    if not rows:
        raise ToolError("给出的字段与曲目当前内容相同", code="no_changes")
    warnings = ["改署名不会改动已构建的背景音乐版本；重新构建后发布元数据才使用新署名"] if "attribution" in args.changes() else []
    return Preview(f"修改曲目「{item['title']}」", rows, warnings=warnings, focus=FOCUS)


async def update_music_track(args: UpdateMusicTrack, ctx: ToolContext) -> ToolOutput:
    item = check(await ctx.client.put(f"/api/studio/music-library/{args.track_id}", args.changes()), "修改曲目")
    item.pop("beats", None)
    return ToolOutput(item, text="已修改：\n" + track_text(item), mutation=MUTATION, focus=FOCUS)


class AnalyzeMusicTrack(StrictModel):
    track_id: str = TrackId
    retag: bool = Field(default=True, description="重新打情绪标签；false 保留现有（含人工修改的）标签")


async def preview_analyze_music_track(args: AnalyzeMusicTrack, ctx: ToolContext) -> Preview:
    item = check(await ctx.client.get(f"/api/studio/music-library/{args.track_id}"), "读取曲目")
    return Preview(f"重新分析曲目「{item['title']}」的节奏" + ("并重新打情绪标签" if args.retag else ""), [
        {"label": "当前", "before": f"{item.get('bpm') or '—'} BPM，情绪 {moods_text(item.get('moods'))}", "after": "重新分析"}],
        warnings=["情绪标签会调用文案模型（可能产生费用）"] if args.retag else [], focus=FOCUS)


async def analyze_music_track(args: AnalyzeMusicTrack, ctx: ToolContext) -> ToolOutput:
    item = check(await ctx.client.post(f"/api/studio/music-library/{args.track_id}/analyze",
                                       {"ai": True, "retag": args.retag}), "分析曲目")
    item.pop("beats", None)
    return ToolOutput(item, text="分析完成：\n" + track_text(item), mutation=MUTATION, focus=FOCUS)


class RecommendMusic(StrictModel):
    music_group_ids: list[str] = Field(default_factory=list, description="音乐组取并集；推荐仅在组成员内评分")
    job_id: str | None = JobId
    plan_id: str | None = PlanId
    shot: str | None = Field(default=None, description="背景音乐段落起始分镜：完整 ID、短 ID 或 #序号")
    text: str | None = Field(default=None, max_length=8000, description="段落文案；不给时用 job_id/shot_id 指定的分镜段落")
    seconds: float | None = Field(default=None, gt=0, le=3600, description="段落时长（秒）；按分镜推荐时缺省为预估时长")
    limit: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def located(self):
        if not (self.text and self.text.strip()) and not self.shot:
            raise ValueError("需要 text，或 shot（背景音乐段落起始分镜；job_id 省略时用当前页面的任务）")
        return self


async def recommend_music(args: RecommendMusic, ctx: ToolContext) -> ToolOutput:
    body = {"limit": args.limit, "seconds": args.seconds, "ai": True, "music_group_ids": args.music_group_ids}
    if args.text and args.text.strip():
        body["text"] = args.text
    else:
        view = await load_view(ctx, args.job_id, args.plan_id)
        body.update(job_id=view.job["id"], plan_id=view.plan_id, shot_id=shot_ref(view, args.shot))
    result = check(await ctx.client.post("/api/studio/music-library/recommend",
                                         {key: value for key, value in body.items() if value is not None}), "推荐曲目")
    rows = [{**compact(item), "score": item["score"], "reasons": item["reasons"]} for item in result["items"]]
    bpm = "–".join(map(str, result["bpm_range"])) if result["bpm_range"] else "不限"
    lines = [f"{index}. {track_row(item)} | 得分 {item['score']:g}：{'；'.join(item['reasons']) or '—'}"
             for index, item in enumerate(result["items"], 1)]
    text = (f"推断情绪：{moods_text(result['moods'])}（{ {'model': '文案模型', 'keywords': '关键词', 'none': '未能推断'}[result['source']] }）；"
            f"节奏 {bpm} BPM。\n" + ("\n".join(lines) or "没有可用曲目（曲库为空或授权都已过期）")
            + "\n选定后用 edit_shots 把该分镜的 music 设为 {kind: library, track_id}。")
    return ToolOutput({**{k: result[k] for k in ("moods", "bpm_range", "source", "seconds")}, "items": rows},
                      text=text, focus={**FOCUS, **({"music_group_ids": args.music_group_ids} if args.music_group_ids else {})})


def specs() -> list[ToolSpec]:
    return [
        ToolSpec("list_music_tracks", "studio_post",
                 "列出曲库曲目：ID、曲名、时长、BPM、情绪标签、授权是否过期。可按情绪键或关键词筛选。",
                 ListMusicTracks, "read", list_music_tracks),
        ToolSpec("get_music_track", "studio_post", "读取单首曲目：授权来源、许可、署名、有效期、节奏分析与情绪标签。",
                 GetMusicTrack, "read", get_music_track),
        ToolSpec("import_music_track", "studio_post",
                 "把音频附件导入曲库（授权来源、许可、署名必填），自动分析 BPM/节拍并打情绪标签；相同音频去重。",
                 ImportMusicTrack, "write", import_music_track, preview_import_music_track),
        ToolSpec("update_music_track", "studio_post", "修改曲目的授权信息、标签或情绪标签（只传要改的字段）。",
                 UpdateMusicTrack, "write", update_music_track, preview_update_music_track),
        ToolSpec("analyze_music_track", "studio_post", "重新分析曲目的 BPM、节拍、能量与亮度，可选重新打情绪标签。",
                 AnalyzeMusicTrack, "write", analyze_music_track, preview_analyze_music_track),
        ToolSpec("recommend_music", "studio_post",
                 "按段落文案（或 shot 指定的背景音乐段落）推荐曲库曲目：推断情绪与节奏区间，按情绪、BPM、"
                 "时长排序并给出理由；授权过期的曲目不参与。",
                 RecommendMusic, "read", recommend_music),
    ]
