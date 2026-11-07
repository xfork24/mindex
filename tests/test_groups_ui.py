"""隔离真实库中的组管理、详情归属、召回与音乐筛选浏览器验收。"""

import os
import subprocess
import wave

import pytest

from api import create_app
from conftest import FixtureEmbedder, FixtureWriter
from processing.indexing import IndexBuilder
from processing.pipeline import Pipeline
from shared.media import cut
from shared.media_groups import Groups
from shared.storage import Store
from studio import music_library, studio
from studio import workspace as ws
from test_agent_panel_ui import ROOT, playwright_available, serve


@pytest.mark.slow
@pytest.mark.skipif(not playwright_available(), reason="需要 Node 与 Playwright")
def test_group_management_and_filters_in_browser(config, video_file, tmp_path):
    pipeline = Pipeline(config)
    source_id = pipeline.prepare(video_file)
    second_video = tmp_path / "第二份素材.mp4"
    cut(video_file, 1, 3, second_video)
    second_id = pipeline.prepare(second_video)
    embedder = FixtureEmbedder(config)
    IndexBuilder(config, embedder).build(mode="visual", kinds=("shot",), activate=True)
    store = Store(config.data_dir)
    groups = Groups(store)
    music_group = groups.save("music", "音乐原文 Music")["id"]
    source_group = groups.save("source", "素材原文 Source")["id"]
    groups.change_members(source_group, [source_id])
    empty_group = None
    for kind in ("source", "music"):
        extra = groups.save(kind, "长组名称用于验证自然换行 " + "LongGroupNameWithoutSpaces" * 5)
        if kind == "source":
            empty_group = extra["id"]
    second_group = groups.save("source", "附加素材组")["id"]
    groups.change_members(second_group, [second_id])
    audio = tmp_path / "music.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\x00\x00" * 32000)
    track, _ = music_library.import_track(
        config,
        store,
        audio,
        ".wav",
        music_library.TrackMeta(title="音乐曲目 Music", source="测试", license="CC0", attribution="作者"),
        now=studio.now(),
    )
    job = studio.create_job(config, "颜色", title="组验收任务", options={"source_group_ids": [source_group]})
    w = ws.load(config, job["id"])
    p = ws.plan(w, w["active_plan"])
    ws.add_script(config, job["id"], w, p, "红色画面。", "manual")
    ws.set_board(
        config,
        job["id"],
        w,
        p,
        [studio.Shot(id="shot-group", text="红色画面。", query="红色", estimated_seconds=1)],
    )
    p["board_script"] = p["script"]
    ws.persist(config, job["id"], w)
    app = create_app(config, embedder, writer=FixtureWriter(config))
    server, thread, url = serve(app)
    try:
        result = subprocess.run(
            ["node", "tests/groups_ui.cjs"],
            cwd=ROOT,
            env={
                **os.environ,
                "MINDEX_GROUPS_URL": url,
                "MINDEX_GROUPS_SOURCE": source_id,
                "MINDEX_GROUPS_SECOND_SOURCE": second_id,
                "MINDEX_GROUPS_SECOND_GROUP": second_group,
                "MINDEX_GROUPS_EMPTY_GROUP": empty_group,
                "MINDEX_GROUPS_MUSIC": track["id"],
                "MINDEX_GROUPS_MUSIC_GROUP": music_group,
                "MINDEX_GROUPS_SOURCE_GROUP": source_group,
                "MINDEX_GROUPS_JOB": job["id"],
                "MINDEX_GROUPS_OUTPUT": str(tmp_path),
            },
            capture_output=True,
            text=True,
            timeout=150,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.should_exit = True
        thread.join(timeout=10)
