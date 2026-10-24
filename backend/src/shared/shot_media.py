"""Shot-only media access. Never resolves an archived source path."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from filelock import FileLock

from mindex.shared.media import MediaError, atomic_media, probe

VERSION = 'shot-media-v1'
EPSILON = 0.000001


@dataclass(frozen=True)
class ShotReference:
    shot_id: str
    path: str
    start: float
    end: float
    offset: float
    absolute_start: float

    def absolute(self, local: float) -> float:
        return self.absolute_start + local - self.start

    def logical(self, local: float) -> float:
        return self.offset + local - self.start


class ShotMedia:
    def __init__(self, store, *, cache_probes=True, reuse_checks=False):
        self.store = store
        self.cache_probes = cache_probes
        self.reuse_checks = reuse_checks
        self._probes = {}
        self._sources = {}
        self._shots = {}
        self._paths = {}
        self._shot_ends = {}

    def _path(self, relative):
        if self.reuse_checks and relative in self._paths:
            return self._paths[relative]
        if self.cache_probes and not self.reuse_checks:
            path = self.store.resolve_media(relative)
            self._paths[relative] = path
            return path
        path = (self.store.root / relative).resolve()
        if not path.is_relative_to(self.store.root) or not path.is_file():
            raise FileNotFoundError(relative)
        self._paths[relative] = path
        return path

    def _info(self, path):
        if not self.cache_probes:
            if path not in self._probes:
                self._probes[path] = probe(path)
            return self._probes[path]
        if self.reuse_checks and path in self._probes:
            return self._probes[path]
        stat = path.stat()
        identity = [str(path), stat.st_size, stat.st_mtime_ns]
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        directory = self.store.root / 'cache' / 'shot-media' / 'probes'
        directory.mkdir(parents=True, exist_ok=True)
        cached = directory / f'{key}.json'
        # Cache files are atomically replaced. A request-local reader can use an
        # existing entry without acquiring thousands of filesystem locks.
        if self.reuse_checks:
            try:
                value = json.loads(cached.read_text())
                if value['identity'] == identity:
                    self._probes[path] = value['media']
                    return value['media']
            except (ValueError, KeyError, OSError):
                pass
        with FileLock(str(cached) + '.lock'):
            if cached.exists():
                try:
                    value = json.loads(cached.read_text())
                    if value['identity'] == identity:
                        self._probes[path] = value['media']
                        return value['media']
                except (ValueError, KeyError, OSError):
                    pass
            info = probe(path)
            partial = cached.with_suffix('.partial')
            partial.write_text(json.dumps({'identity': identity, 'media': info}))
            partial.replace(cached)
            self._probes[path] = info
            return info

    def references(self, source_id: str, start: float = 0, end: float | None = None):
        if (self.reuse_checks or not self.cache_probes) and source_id in self._sources:
            source = self._sources[source_id]
        else:
            source = self.store.one('SELECT * FROM sources WHERE id=?', (source_id,))
            self._sources[source_id] = source
        if source is None:
            raise MediaError('Unknown source')
        duration = float(json.loads(source['media'])['duration'])
        end = duration if end is None else end
        if not all(math.isfinite(v) for v in (start, end)) or not 0 <= start < end <= duration + EPSILON:
            raise MediaError('Invalid source interval')
        cursor, result = start, []
        if (self.cache_probes and not self.reuse_checks) or source_id not in self._shots:
            self._shot_ends.pop(source_id, None)
            self._shots[source_id] = self.store.segments(source_id, 'shot')
        from bisect import bisect_right
        shots = self._shots[source_id]
        if source_id not in self._shot_ends:
            maximum = float('-inf')
            ends = []
            for shot in shots:
                maximum = max(maximum, shot['end'])
                ends.append(maximum)
            self._shot_ends[source_id] = ends
        first = bisect_right(self._shot_ends[source_id], start)
        for shot in shots[first:]:
            if shot['start'] >= end:
                break
            a, b = max(start, shot['start']), min(end, shot['end'])
            if b <= a:
                continue
            # Boundary rounding can leave a sub-microsecond tail in an adjacent
            # shot. Keep genuinely tiny frame lookups, but omit such tails from
            # longer intervals so FFmpeg never receives a phantom input.
            if b - a < EPSILON and end - start > EPSILON:
                continue
            if abs(a - cursor) > EPSILON:
                raise MediaError('Shot coverage has a gap or overlap')
            path = self._path(shot['path'])
            info = self._info(path)
            if info['duration'] + 1 / info['fps'] + EPSILON < b - shot['start']:
                raise MediaError(f"Shot media is truncated: {shot['id']} ({shot['path']}), "
                                 f"need {b - shot['start']:.6f}s, have {info['duration']:.6f}s")
            result.append(ShotReference(shot['id'], shot['path'], a - shot['start'],
                                        b - shot['start'], a - start, a))
            cursor = b
        if abs(cursor - end) > EPSILON:
            raise MediaError('Incomplete shot coverage')
        return result

    def identity(self, source_id, start=0, end=None):
        refs = self.references(source_id, start, end)
        return {"version": VERSION, "refs": [asdict(r) for r in refs],
                "files": [[r.path, self._path(r.path).stat().st_size,
                           self._path(r.path).stat().st_mtime_ns] for r in refs]}

    def prewarm(self, intervals, *, workers=8):
        """Probe all shot files for the given ``(source_id, start, end)`` intervals in
        parallel, populating the shared probe cache. Read-only callers (workspace
        display) otherwise pay one serial ``ffprobe`` per shot file on a cold cache,
        which dominates the first open of a job. Shot paths are gathered straight from
        the segment index so collection never probes; failures are ignored here and the
        normal serial path re-raises them with full context when actually needed."""
        pending = {}
        for source_id, start, end in intervals:
            try:
                shots = self._shots.get(source_id)
                if shots is None:
                    shots = self.store.segments(source_id, 'shot')
                    self._shots[source_id] = shots
                for shot in shots:
                    if shot['start'] >= end or shot['end'] <= start:
                        continue
                    path = self._path(shot['path'])
                    if path not in self._probes:
                        pending[str(path)] = shot['path']
            except (MediaError, FileNotFoundError, OSError, ValueError):
                continue
        relatives = list(pending.values())
        if len(relatives) < 2:
            return
        def warm(relative):
            try:
                self._info(self._path(relative))
            except (MediaError, FileNotFoundError, OSError, ValueError):
                pass
        with ThreadPoolExecutor(max_workers=min(workers, len(relatives))) as pool:
            list(pool.map(warm, relatives))

    def playlist(self, source_id, start=0, end=None, prefix="/api/shots"):
        refs = self.references(source_id, start, end)
        return {"version": VERSION, "duration": sum(r.end - r.start for r in refs),
                "items": [{**asdict(r), "url": f"{prefix}/{r.shot_id}/media?v={self.file_revision(r.path)}"}
                          for r in refs]}

    def file_revision(self, relative):
        path = self._path(relative)
        stat = path.stat()
        return hashlib.sha256(f'{relative}:{stat.st_size}:{stat.st_mtime_ns}'.encode()).hexdigest()[:20]

    def register(self, path: str, source_id: str, start: float, end: float, *, db=None):
        refs = self.references(source_id, start, end)
        execute = db.execute if db is not None else self.store.execute
        current = (db.execute('SELECT version FROM media_references WHERE path=?', (path,)).fetchone()
                   if db is not None else self.store.one('SELECT version FROM media_references WHERE path=?', (path,)))
        version = 'shot-boundary-v1' if current and current['version'] == 'shot-boundary-v1' else VERSION
        execute('INSERT OR REPLACE INTO media_references(path,source_id,start,end,version,refs) '
                           'VALUES(?,?,?,?,?,?)',
                           (path, source_id, start, end, version, json.dumps([asdict(r) for r in refs])))
        return refs

    def _frame_shot(self, source_id, timestamp):
        """Shot reference holding the frame shown at ``timestamp`` (no frame decoding)."""
        source = self.store.one('SELECT media FROM sources WHERE id=?', (source_id,))
        if source is None:
            raise MediaError('Unknown source')
        duration = json.loads(source['media'])['duration']
        if timestamp == duration:
            return self.references(source_id)[-1], True
        return self.references(source_id, timestamp, min(duration, timestamp + EPSILON))[0], False

    def _frame_reference(self, source_id, timestamp):
        from bisect import bisect_right

        from mindex.shared.media import frame_timestamps

        ref, last = self._frame_shot(source_id, timestamp)
        timestamps = frame_timestamps(self._path(ref.path))
        if last:
            local = timestamps[-1]
        else:
            local = timestamps[max(0, bisect_right(timestamps, ref.start + EPSILON) - 1)]
        return ref, local

    def frame_revision(self, source_id: str, timestamp: float) -> str:
        """Revision of the shot file a frame at ``timestamp`` is decoded from; cheap
        enough to validate HTTP caches before any FFmpeg decode."""
        ref, _ = self._frame_shot(source_id, timestamp)
        return self.file_revision(ref.path)

    def frame_bytes(self, source_id, timestamp):
        from mindex.shared.media import command

        ref, local = self._frame_reference(source_id, timestamp)
        raw = command(['ffmpeg', '-v', 'error', '-ss', str(local), '-i',
                       str(self._path(ref.path)), '-frames:v', '1',
                       '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1'])
        if not raw:
            raise MediaError('Shot frame decode failed')
        return raw

    def frame_jpeg(self, source_id: str, timestamp: float, width: int = 768) -> tuple[bytes, str]:
        """Decode the nearest real frame at or before ``timestamp`` as a scaled JPEG.

        Returns ``(jpeg_bytes, revision)``; ``revision`` identifies the shot file the
        frame came from so callers can build stable cache validators."""
        from mindex.shared.media import command

        ref, local = self._frame_reference(source_id, timestamp)
        raw = command(['ffmpeg', '-v', 'error', '-ss', str(local), '-i',
                       str(self._path(ref.path)), '-frames:v', '1',
                       '-vf', f'scale={int(width)}:-2', '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1'])
        if not raw:
            raise MediaError('Shot frame decode failed')
        return raw, self.file_revision(ref.path)

    def frame(self, source_id: str, timestamp: float, output: Path):
        ref, local = self._frame_reference(source_id, timestamp)
        atomic_media(['-ss', str(local), '-i', str(self._path(ref.path)),
                      '-frames:v', '1'], output)

    def _inputs(self, refs, *, audio_only=False, video_only=False):
        args, filters, labels = [], [], []
        include_audio = not video_only and (audio_only or any(self._info(self._path(r.path))["audio"] for r in refs))
        first = self._info(self._path(refs[0].path))
        width, height = first['width'] // 2 * 2, first['height'] // 2 * 2
        for i, ref in enumerate(refs):
            path = self._path(ref.path)
            duration = f'{ref.end - ref.start:.9f}'
            args += ['-ss', f'{ref.start:.9f}', '-t', duration, '-i', str(path)]
            if not audio_only:
                filters.append(f'[{i}:v]trim=duration={duration},setpts=PTS-STARTPTS,'
                               f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
                               f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[v{i}]')
                labels.append(f'[v{i}]')
            if not include_audio:
                continue
            if self._info(path)['audio']:
                filters.append(f'[{i}:a]aresample=16000,aformat=channel_layouts=mono,'
                               f'apad,atrim=duration={duration},asetpts=PTS-STARTPTS[a{i}]')
            else:
                filters.append(f'anullsrc=r=16000:cl=mono,atrim=duration={duration},'
                               f'asetpts=PTS-STARTPTS[a{i}]')
            labels.append(f'[a{i}]')
        filters.append(''.join(labels) + f'concat=n={len(refs)}:v={0 if audio_only else 1}:a={int(include_audio)}'
                       + ('[a]' if audio_only else '[v][a]' if include_audio else '[v]'))
        return args + ['-filter_complex', ';'.join(filters)]

    def audio(self, source_id: str, output: Path, start=0, duration=None):
        refs = self.references(source_id, start, None if duration is None else start + duration)
        atomic_media(self._inputs(refs, audio_only=True) +
                     ['-map', '[a]', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le'], output)

    @contextmanager
    def inference_video(self, source_id, start, end):
        """Read existing shots, rendering only into a disposable inference directory."""
        refs = self.references(source_id, start, end)
        if len(refs) == 1:
            ref = refs[0]
            shot = self.store.one('SELECT start,end FROM segments WHERE id=?', (ref.shot_id,))
            if ref.start == 0 and abs(ref.end - (shot['end'] - shot['start'])) <= EPSILON:
                yield self._path(ref.path)
                return
        with tempfile.TemporaryDirectory(prefix='mindex-embed-') as directory:
            output = Path(directory) / 'window.mp4'
            atomic_media(self._inputs(refs, video_only=True) + ['-map', '[v]', '-an', '-c:v', 'libx264',
                         '-preset', 'fast', '-crf', '20', '-movflags', '+faststart'], output)
            yield output

    def materialize(self, source_id: str, start=0, end=None) -> Path:
        refs = self.references(source_id, start, end)
        if len(refs) == 1:
            ref = refs[0]
            shot = self.store.one('SELECT start,end FROM segments WHERE id=?', (ref.shot_id,))
            if ref.start == 0 and abs(ref.end - (shot['end'] - shot['start'])) <= EPSILON:
                return self._path(ref.path)
        identity = [VERSION, [asdict(r) for r in refs]]
        for ref in refs:
            stat = self._path(ref.path).stat()
            identity.append([stat.st_size, stat.st_mtime_ns])
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        output = self.store.root / 'cache' / 'shot-media' / f'{key}.mp4'
        output.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(output) + '.lock'):
            if not output.exists():
                atomic_media(self._inputs(refs) + ['-map', '[v]'] +
                             (['-map', '[a]'] if any(self._info(self._path(r.path))['audio'] for r in refs) else []) +
                             ['-c:v', 'libx264',
                             '-preset', 'fast', '-crf', '20', '-c:a', 'aac', '-movflags', '+faststart'], output)
            stat = output.stat()
            os.utime(output, ns=(time.time_ns(), stat.st_mtime_ns))
        return output

    def prune_cache(self, *, max_age=7 * 86400, max_bytes=5 * 1024**3):
        """Nonblocking eviction; callers holding a cache lease protect active files."""
        from filelock import Timeout

        directory = self.store.root / 'cache' / 'shot-media'
        files = sorted(directory.glob('*.mp4'), key=lambda p: p.stat().st_atime)
        total = sum(p.stat().st_size for p in files)
        removed = []
        for path in files:
            try:
                with FileLock(str(path) + '.lock', timeout=0):
                    if not path.exists():
                        continue
                    stat = path.stat()
                    if time.time() - stat.st_atime <= max_age and total <= max_bytes:
                        continue
                    path.unlink()
                    total -= stat.st_size
                    removed.append(path.name)
            except Timeout:
                continue
        return removed

    def frames(self, source_id, start=0, end=None, count=12):
        """Sample actual shot PTS, returning absolute source times and JPEG data URLs."""
        import base64

        from mindex.shared.media import command, frame_timestamps

        if count < 1:
            raise MediaError('Frame count must be positive')
        candidates = []
        for ref in self.references(source_id, start, end):
            for timestamp in frame_timestamps(self._path(ref.path)):
                if ref.start <= timestamp < ref.end:
                    candidates.append((ref, timestamp))
        if not candidates:
            raise MediaError('No decodable frames in interval')
        result = []
        count = min(count, len(candidates))
        for i in range(count):
            ref, timestamp = candidates[round(i * (len(candidates) - 1) / max(1, count - 1))]
            raw = command(['ffmpeg', '-v', 'error', '-ss', str(timestamp), '-i',
                           str(self._path(ref.path)), '-frames:v', '1',
                           '-vf', 'scale=768:-2', '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1'])
            if not raw:
                raise MediaError('Shot frame decode failed')
            result.append((ref.absolute(timestamp), 'data:image/jpeg;base64,' + base64.b64encode(raw).decode()))
        return result


def file_response(path: Path, **kwargs):
    """Keep cached media leased until a streaming HTTP response finishes."""
    from starlette.background import BackgroundTask
    from starlette.responses import FileResponse

    path = Path(path)
    headers = dict(kwargs.pop('headers', {}) or {})
    headers.setdefault('Cache-Control', 'no-cache')
    kwargs['headers'] = headers
    if path.parent.name != 'shot-media':
        return FileResponse(path, **kwargs)
    lock = FileLock(str(path) + '.lock', thread_local=False)
    lock.acquire()
    if not path.is_file():
        lock.release()
        raise FileNotFoundError(path)
    return FileResponse(path, background=BackgroundTask(lock.release), **kwargs)
