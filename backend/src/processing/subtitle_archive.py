"""Explicit preparation-time archive of embedded subtitle dependencies."""
from __future__ import annotations

import json

from filelock import FileLock

from mindex.shared.media import MediaError, atomic_media, command, content_hash


def archive_path(store, source_id):
    return store.root / 'subtitle-archives' / source_id / 'manifest.json'


def load_archive(store, source_id):
    path = archive_path(store, source_id)
    if not path.is_file():
        raise MediaError('Subtitle archive missing; run explicit preparation/migration')
    data = json.loads(path.read_text(encoding="utf-8"))
    for relative, digest in data['files'].items():
        if content_hash(store.resolve_media(relative)) != digest:
            raise MediaError('Subtitle archive integrity check failed')
    return data


def archive_subtitles(store, source):
    from mindex.processing.dialogue import extract_subtitles, subtitle_tracks

    manifest = archive_path(store, source['id'])
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(manifest) + '.lock'):
        if manifest.exists():
            return load_archive(store, source['id'])
        original = store.resolve_media(source['path'])
        tracks = subtitle_tracks(original)
        metadata = json.loads(command(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(original)]))
        data = {'version': 1, 'tracks': tracks, 'streams': metadata.get('streams', []),
                'utterances': {}, 'files': {}}
        for stream in data['streams']:
            if stream.get('codec_type') == 'attachment':
                target = manifest.parent / f"attachment-{stream['index']}.bin"
                command(['ffmpeg', '-v', 'error', '-y', f"-dump_attachment:{stream['index']}",
                         str(target), '-i', str(original), '-t', '0', '-f', 'null', '-'])
                data['files'][str(target.relative_to(store.root))] = content_hash(target)
        for track in tracks:
            extension = '.mp4' if track['codec'] == 'mov_text' else '.mkv'
            raw = manifest.parent / f"track-{track['index']}{extension}"
            atomic_media(['-copyts', '-i', str(original), '-map', f"0:{track['index']}",
                          *(['-map', '0:t?'] if extension == '.mkv' else []), '-c', 'copy'], raw)
            data['files'][str(raw.relative_to(store.root))] = content_hash(raw)
        for track in tracks:
            if track['text']:
                data['utterances'][str(track['index'])] = extract_subtitles(
                    original, track, json.loads(source['media'])['duration'])
        partial = manifest.with_suffix('.partial')
        partial.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        partial.replace(manifest)
        return data
