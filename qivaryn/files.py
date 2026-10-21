"""Shared helpers for locating already-downloaded media files on disk.

The downloader and the state database both need to answer the same question:
"does a non-empty media file for this shortcode already exist in the output
directory?".  Keeping that logic in one place avoids the pattern being copied
(and drifting) across modules.
"""

import os
import re
from typing import Optional, Tuple

#: File extensions that Qivaryn writes for downloaded media.
MEDIA_EXTENSIONS = (".mp4", ".webm", ".jpg", ".jpeg", ".webp")

#: Characters that cannot be used in a filename on Windows.
_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Longest title fragment that is appended to a filename.
MAX_TITLE_FRAGMENT = 100

#: Longest account or page name used as a folder name.
MAX_FOLDER_NAME = 80


def sanitize_title(title: str, max_length: int = MAX_TITLE_FRAGMENT) -> str:
    """Turns a media title into a filename fragment that is safe on disk.

    Illegal characters become spaces, runs of whitespace collapse, and the
    result is trimmed to ``max_length`` characters.  Trailing dots and spaces
    are removed because Windows silently drops them.
    """
    text = _ILLEGAL_FILENAME_CHARS.sub(" ", str(title or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_length:
        text = text[:max_length]
    return text.strip(". ")


def sanitize_folder(name: str, max_length: int = MAX_FOLDER_NAME) -> str:
    """Turns an account or page name into a folder name that is safe on disk.

    Returns an empty string when nothing usable is left, which tells the
    caller to store the media directly in the platform folder.
    """
    text = _ILLEGAL_FILENAME_CHARS.sub(" ", str(name or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_length:
        text = text[:max_length]
    text = text.strip(". ")
    if text in (".", ".."):
        return ""
    return text


#: Extensions that always represent a video container.
VIDEO_EXTENSIONS = (".mp4", ".webm")

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
            return candidate, ("reel" if ext in VIDEO_EXTENSIONS else "post")

    for ext in MEDIA_EXTENSIONS:
        candidate = os.path.join(output_dir, f"{shortcode}_1{ext}")
        if file_is_present(candidate):
            return candidate, "carousel"

    return None
