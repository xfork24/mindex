"""真实备份导出/导入链路保留组、空组、曲目文件和任务组引用。"""

import wave

import pytest

from shared.media_groups import Groups
from shared.storage import Store
from studio import music_library, studio
from test_backup import export, library, restore  # noqa: F401 — library 是隔离夹具


@pytest.mark.slow
@pytest.mark.parametrize("partial", [False, True])
def test_bundle_restores_groups_music_and_dynamic_job_scope(library, tmp_path, partial):  # noqa: F811
    config, sources, _, _ = library
    store = Store(config.data_dir)
    groups = Groups(store)
    source_group = groups.save("source", "范围组")["id"]
    groups.change_members(source_group, [sources[0]])
    empty_group = groups.save("source", "空组")["id"]
    omitted_group = groups.save("source", "其他素材组")["id"]
    groups.change_members(omitted_group, [sources[1]])
    music_group = groups.save("music", "曲目组")["id"]
    audio = tmp_path / "track.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\x00\x00" * 16000)
    track, _ = music_library.import_track(
        config,
        store,
        audio,
        ".wav",
        music_library.TrackMeta(title="曲目", source="测试", license="CC0", attribution="作者"),
        now=studio.now(),
    )
    groups.change_members(music_group, [track["id"]])
    job = studio.create_job(
        config, "组范围测试", title="备份测试", options={"source_group_ids": [source_group]}
    )
    bundle, target = tmp_path / "bundle", tmp_path / "target"
    export(library, bundle, **({"sources": [sources[0]]} if partial else {}))
    restored = restore(bundle, target)
    assert restored["ok"]
    result = Store(target)
    final_groups = Groups(result)
    assert final_groups.resolve("source", [source_group]) == {sources[0]}
    assert final_groups.resolve("music", [music_group]) == {track["id"]}
    assert (target / "music" / f"{track['id']}.wav").read_bytes() == audio.read_bytes()
    selected = {item["id"] for item in final_groups.list("source") if not item.get("system")}
    assert any(item.get("system") for item in final_groups.list("source"))
    if partial:
        assert selected == {source_group}
    else:
        assert {empty_group, omitted_group} <= selected
    state = studio.load_job(config.model_copy(update={"data_dir": target}), job["id"])
    assert state["source_group_ids"] == [source_group]
    final_groups.delete(music_group)
    assert (target / "music" / f"{track['id']}.wav").is_file()
