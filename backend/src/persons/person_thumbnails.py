"""Small, disk-cached previews for the cast import gallery."""
from __future__ import annotations

import hashlib
import os
import threading

from PIL import Image, ImageOps


def import_thumbnail(store, image):
    with store.materialize_media(image['path']) as source:
        stat = source.stat()
        key = hashlib.sha256(f"v1:{image['path']}:{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()
        target = store.root / 'cache' / 'cast-thumbnails' / (key + '.jpg')
        if target.is_file():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f'.{os.getpid()}.{threading.get_ident()}.tmp')
        try:
            with Image.open(source) as original:
                preview = ImageOps.exif_transpose(original)
                preview.thumbnail((340, 192), Image.Resampling.LANCZOS)
                if preview.mode in ('RGBA', 'LA') or (preview.mode == 'P' and 'transparency' in preview.info):
                    rgba = preview.convert('RGBA')
                    preview = Image.new('RGB', rgba.size, 'white')
                    preview.paste(rgba, mask=rgba.getchannel('A'))
                else:
                    preview = preview.convert('RGB')
                preview.save(temporary, format='JPEG', quality=82)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return target
