"""Shared contract for Qivaryn platform integrations.

Every platform client converts service-specific payloads into the same
normalized media dictionary so that :class:`qivaryn.downloader.QivarynEngine`
can handle routing, filtering, resume logic, progress reporting and state
persistence without knowing anything about the service.

A normalized media item is a plain ``dict`` with these keys::

    id            str   Stable identifier used for filenames and history
    shortcode     str   Backwards-compatible alias of ``id``
    media_type    str   "Post", "Reel", "Video", "Photo" or "Carousel"
    timestamp     str   Upload time as text (may be empty)
    title         str   Human readable title (may be empty)
    account       str   Owner of the media (username, channel or page name)
    video_url     str   Direct video URL, or None
    audio_url     str   Separate audio track URL, or None
    audio_start_ms int  Offset into the audio track, or 0
    quality       str   Short description shown in logs
    quality_fallback dict|None  Set when the requested height was unavailable
    display_url   str   Thumbnail/image URL, or None
    ext           str   Preferred container ("mp4", "webm", ...)
    http_headers  dict  Extra headers required by the media CDN
    children      list  Carousel slides (same shape, without ``children``)
"""

from dataclasses import dataclass
from typing import Callable, Dict, Iterator, Optional, Tuple

import requests

#: Name of the cookie-format file exported by browser extensions.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


@dataclass
class AccountFeed:
    """One ordered source of items for an account or page.

    ``fetch`` accepts ``(account_id, max_pages, account_name)`` and yields raw
    platform payloads.  ``media_filter`` is the ``--type`` value this feed
    satisfies.
    """

    name: str
    media_filter: str
    fetch: Callable[[str, Optional[int], str], Iterator[dict]]


class BasePlatformClient:
    """Base class for every platform integration."""

    #: Platform key used by the CLI, database and filenames.
    name = ""

    #: Folder name used inside the output directory for this platform.
    display_name = ""

    #: ``--type`` values accepted by this platform, most useful first.
    supported_media_types: Tuple[str, ...] = ("all",)

    #: Maps a ``--type`` value onto the normalized media types it accepts.
    filter_aliases: Dict[str, Tuple[str, ...]] = {}

    #: When True the engine delegates byte transfer to ``download_media``.
    native_download = False

    #: When True the engine may call :meth:`iter_playlist_items`.
    supports_playlists = False

    def __init__(
        self,
        sessionid: Optional[str] = None,
        cookies_file: Optional[str] = None,
        allow_network_fallbacks: bool = True,
    ):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
        self.sessionid = sessionid
        self.cookies_file = cookies_file
        self.allow_network_fallbacks = allow_network_fallbacks
        if cookies_file:
            self.load_cookies_from_file(cookies_file)

    # ------------------------------------------------------------------
    # URL handling
    # ------------------------------------------------------------------
    @staticmethod
    def detect_url(url: str) -> Optional[Tuple[str, str]]:
        """Returns ``(kind, identifier)`` for a supported URL, else ``None``."""
        raise NotImplementedError

    def extract_id(self, url_or_id: str) -> str:
        """Extracts the stable item identifier from a URL or a bare identifier."""
        raise NotImplementedError

    def item_id(self, raw: dict) -> Optional[str]:
        """Returns the identifier of a raw feed item without parsing it fully."""
        return raw.get("id") or raw.get("shortcode")

    def prefilter(self, raw: dict, media_filter: str) -> bool:
        """Cheap rejection of raw items that cannot match ``media_filter``."""
        return True

    def matches_filter(self, media_type: str, media_filter: str) -> bool:
        """Returns True when a normalized media type satisfies ``media_filter``."""
        if not media_filter or media_filter.lower() == "all":
            return True
        allowed = self.filter_aliases.get(media_filter.lower())
        if not allowed:
            return True
        return str(media_type or "").lower() in allowed

    # ------------------------------------------------------------------
    # Media resolution
    # ------------------------------------------------------------------
    def fetch_item(self, identifier: str) -> dict:
        """Resolves one item (URL or identifier) into a normalized dictionary."""
        raise NotImplementedError

    def normalize_item(self, raw: dict) -> dict:
        """Converts a raw feed payload into a normalized media dictionary."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Account feeds
    # ------------------------------------------------------------------
    def resolve_account_id(self, account: str) -> str:
        """Converts a human account name into the ID used by feed endpoints."""
        return account

    def account_feeds(self) -> Tuple[AccountFeed, ...]:
        """Returns the ordered feeds that make up an account download."""
        return ()

    def feeds_for(self, media_filter: str) -> Tuple[AccountFeed, ...]:
        """Selects the feeds needed to satisfy ``media_filter``.

        A filter that matches no dedicated feed falls back to the first feed,
        which yields every media type and is narrowed afterwards by
        :meth:`matches_filter`.
        """
        feeds = self.account_feeds()
        if not feeds or not media_filter or media_filter.lower() == "all":
            return feeds
        matched = tuple(
            feed for feed in feeds if feed.media_filter.lower() == media_filter.lower()
        )
        return matched or feeds[:1]

    def account_label(self, account: str) -> str:
        return f"@{account}"

    def default_filter_for(self, account_identifier: str) -> str:
        """Returns the ``--type`` implied by an account URL (often ``all``)."""
        return "all"

    def iter_playlist_items(self, playlist_id: str, max_pages: Optional[int] = None) -> Iterator[dict]:
        """Yields raw items from a playlist. Enabled by ``supports_playlists``."""
        raise NotImplementedError(f"{self.name or 'This platform'} has no playlist support.")

    def explain_empty_result(self, account: str) -> Optional[str]:
        """Optional platform-specific explanation for an empty download."""
        return None

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------
    def has_session(self) -> bool:
        return bool(self.sessionid or self.cookies_file)

    def load_cookies_from_file(self, cookies_file_path: str, domain: str = ""):
        """Loads a Netscape cookies file, optionally filtered to one domain."""
        import os

        from http.cookiejar import LoadError, MozillaCookieJar

        if not os.path.exists(cookies_file_path):
            raise FileNotFoundError(
                f"Cookies file not found: {cookies_file_path}. "
                "Check the path or export a Netscape-format cookies file."
            )
        jar = MozillaCookieJar()
        try:
            jar.load(cookies_file_path, ignore_discard=True, ignore_expires=True)
        except (OSError, LoadError) as exc:
            raise ValueError(
                f"Could not read cookies file '{cookies_file_path}'. "
                "Make sure it is a valid Netscape-format cookies file."
            ) from exc

        matching = [
            cookie
            for cookie in jar
            if not domain or domain in getattr(cookie, "domain", "")
        ]
        for cookie in matching or list(jar):
            self.session.cookies.set_cookie(cookie)
        print(f"[+] Successfully loaded cookies from: {cookies_file_path}")

    # ------------------------------------------------------------------
    # Download hook
    # ------------------------------------------------------------------
    def download_media(self, item: dict, file_path: str) -> bool:
        """Platform-specific downloader used when ``native_download`` is True."""
        raise NotImplementedError(
            f"{self.name or 'This platform'} client does not implement native downloads."
        )


def make_item(
    item_id: str,
    media_type: str,
    timestamp: str = "",
    video_url: Optional[str] = None,
    audio_url: Optional[str] = None,
    audio_start_ms: int = 0,
    quality: str = "Original",
    display_url: Optional[str] = None,
    title: str = "",
    account: str = "",
    ext: Optional[str] = None,
    http_headers: Optional[Dict[str, str]] = None,
    children: Optional[list] = None,
) -> dict:
    """Builds a normalized media dictionary with every expected key present."""
    if not ext:
        ext = "jpg" if str(media_type).lower() in ("post", "photo", "image") else "mp4"
    return {
        "id": str(item_id),
        "shortcode": str(item_id),
        "media_type": media_type,
        "timestamp": str(timestamp or ""),
        "title": title or "",
        "account": account or "",
        "video_url": video_url,
        "audio_url": audio_url,
        "audio_start_ms": audio_start_ms or 0,
        "quality": quality,
        "display_url": display_url,
        "ext": ext,
        "http_headers": dict(http_headers or {}),
        "children": list(children or []),
    }


def make_child(
    is_video: bool,
    display_url: Optional[str] = None,
    video_url: Optional[str] = None,
    audio_url: Optional[str] = None,
    audio_start_ms: int = 0,
    quality: str = "Original",
    http_headers: Optional[Dict[str, str]] = None,
) -> dict:
    """Builds a normalized carousel slide entry."""
    return {
        "display_url": display_url,
        "video_url": video_url,
        "audio_url": audio_url,
        "audio_start_ms": audio_start_ms or 0,
        "quality": quality,
        "is_video": is_video,
        "http_headers": dict(http_headers or {}),
    }
