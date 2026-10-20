"""Shared helpers for locating already-downloaded media files on disk.

The downloader and the state database both need to answer the same question:
"does a non-empty media file for this shortcode already exist in the output
directory?".  Keeping that logic in one place avoids the pattern being copied
(and drifting) across modules.
"""

import os
from typing import Optional, Tuple

#: File extensions that Qivaryn writes for downloaded media.
MEDIA_EXTENSIONS = (".mp4", ".jpg", ".jpeg", ".webp")

#: Maps the kind of file found on disk to the media type recorded in the database.
MEDIA_TYPE_BY_KIND = {
    "reel": "Reel",
    "post": "Post",
    "carousel": "Carousel",
}


def file_is_present(path: str) -> bool:
    """Returns True when ``path`` is a non-empty regular file.

    Uses a single ``stat`` call (via ``getsize``) instead of the usual
    ``exists`` + ``getsize`` pair, halving the syscall count on the hot
    "is this already downloaded?" path.
    """
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def find_existing_media(output_dir: str, shortcode: str) -> Optional[Tuple[str, str]]:
    """Finds an already-downloaded file belonging to ``shortcode``.

    Single items are stored as ``<shortcode>.<ext>`` while carousel slides use
    ``<shortcode>_<index>.<ext>``.  Returns a ``(path, kind)`` tuple where
    ``kind`` is one of ``"reel"``, ``"post"`` or ``"carousel"``, or ``None``
    when nothing is stored on disk.
    """
    for ext in MEDIA_EXTENSIONS:
        candidate = os.path.join(output_dir, f"{shortcode}{ext}")
        if file_is_present(candidate):
            return candidate, ("reel" if ext == ".mp4" else "post")

    for ext in MEDIA_EXTENSIONS:
        candidate = os.path.join(output_dir, f"{shortcode}_1{ext}")
        if file_is_present(candidate):
            return candidate, "carousel"

    return None
