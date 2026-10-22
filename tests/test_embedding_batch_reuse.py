from conftest import FixtureEmbedder

from mindex.processing.indexing import IndexBuilder, VectorIndex, check_index
from mindex.processing.pipeline import Pipeline


def test_new_snapshot_copies_visual_embeddings_in_batches(config, video_file, monkeypatch):
    from test_channel_recall import add_dialogue

    pipeline = Pipeline(config)
    sid = pipeline.prepare(video_file)
    builder = IndexBuilder(config, FixtureEmbedder(config))
    old = builder.build(source_id=sid, channels=['visual'], activate=True)
    add_dialogue(config, sid)
    batches = []
    original = VectorIndex.upsert_many

    def copy(self, entries):
        batches.append(len(entries))
        return original(self, entries)

    def forbidden(*args, **kwargs):
        raise AssertionError('reusable visual must skip individual reads and model inference')

    monkeypatch.setattr(VectorIndex, 'upsert_many', copy)
    read = VectorIndex.vector

    def individual_read(self, identifier):
        assert self.collection.name != 'idx-' + old
        return read(self, identifier)

    monkeypatch.setattr(VectorIndex, 'vector', individual_read)
    monkeypatch.setattr(builder.embedder, 'video', forbidden)
    new = builder.build(source_id=sid, channels=['dialogue'], activate=True)
    assert new != old
    assert batches and max(batches) > 1
    assert not check_index(pipeline.store, new)
