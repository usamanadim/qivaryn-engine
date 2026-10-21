"""Native Facebook integration.

Facebook never publishes its video metadata through an unauthenticated API, so
this client scrapes the progressive stream URLs Facebook embeds in the video,
reel and watch pages.  Several layouts are probed because Facebook serves
different markup to desktop, mobile and basic-HTML clients.

Known limitation: Facebook aggressively gates page content behind a login wall.
Single videos usually resolve anonymously, but page feeds almost always require
``--cookies``.  When Facebook refuses, the client reports exactly what happened
instead of quietly returning nothing.
"""

import re
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

import requests

from .base import DEFAULT_USER_AGENT, AccountFeed, BasePlatformClient, make_item
from .scrape import first_matching, meta_content, unique_preserving_order, unescape_js

_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "web.facebook.com",
    "mbasic.facebook.com",
    "fb.watch",
    "www.fb.watch",
    "fb.com",
    "www.fb.com",
    "m.fb.com",
}

_SKIP_SEGMENTS = {"groups", "events", "marketplace", "gaming", "watch", "photo",
                  "photos", "login", "help", "policies", "settings", "messages",
                  "hashtag", "search", "notifications", "bookmarks"}

_VIDEO_ID_PATTERNS = (
    r"/videos/(?:[^/\"']+/)?(\d{6,25})",
    r"[?&]v=(\d{6,25})",
    r"/reel(?:s)?/(\d{6,25})",
    r"/watch/?\?v=(\d{6,25})",
    r'"video_id"\s*:\s*"?(\d{6,25})',
    r'"videoId"\s*:\s*"?(\d{6,25})',
    r'"story_fbid"\s*:\s*"?(\d{6,25})',
)

#: Ordered from the highest quality to the most broadly available.
_STREAM_PATTERNS = (
    r'"playable_url_quality_hd"\s*:\s*"(https?[^"]+)"',
    r'"browser_native_hd_url"\s*:\s*"(https?[^"]+)"',
    r'"hd_src(?:_no_ratelimit)?"\s*:\s*"(https?[^"]+)"',
    r'"playable_url"\s*:\s*"(https?[^"]+)"',
    r'"browser_native_sd_url"\s*:\s*"(https?[^"]+)"',
    r'"sd_src(?:_no_ratelimit)?"\s*:\s*"(https?[^"]+)"',
    r'"progressive_url"\s*:\s*"(https?[^"]+)"',
    r'"video_url"\s*:\s*"(https?[^"]+)"',
    r'"(?:src|content)"\s*:\s*"(https?[^"]*?video[^"]*?)"',
)

_LOGIN_MARKERS = (
    'id="login_form"',
    'name="login_form"',
    "you must log in to continue",
    "log in to continue",
    "login.php?next=",
    'id="checkpoint"',
)


class FacebookClient(BasePlatformClient):
    """Resolves Facebook videos and reels, plus best-effort page video feeds."""

    name = "facebook"
    display_name = "Facebook"
    supported_media_types = ("all", "videos")
    filter_aliases = {
        "videos": ("video", "reel"),
        "reels": ("reel", "video"),
    }

    def __init__(self, sessionid: Optional[str] = None, cookies_file: Optional[str] = None,
                 allow_network_fallbacks: bool = True):
        super().__init__(sessionid=sessionid, cookies_file=None,
                         allow_network_fallbacks=allow_network_fallbacks)
        self.cookies_file = cookies_file
        self.session.headers.update({
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        if cookies_file:
            self.load_cookies_from_file(cookies_file, domain="facebook.com")
        if sessionid:
            self.session.cookies.set("c_user", sessionid, domain=".facebook.com")

    # ------------------------------------------------------------------
    # URL handling
    # ------------------------------------------------------------------
    @staticmethod
    def detect_url(url: str) -> Optional[Tuple[str, str]]:
        parsed = urlsplit(str(url).strip())
        if parsed.scheme not in ("http", "https"):
            return None
        host = (parsed.hostname or "").lower()
        if host not in _HOSTS:
            return None

        parts = [part for part in parsed.path.split("/") if part]
        query = parsed.query

        if host in ("fb.watch", "www.fb.watch"):
            return ("single", str(url).strip()) if parts else None

        if parts and parts[0] == "share":
            return "single", str(url).strip()

        video_id = FacebookClient._id_in_path(parts, query)
        if video_id:
            return "single", video_id

        if not parts:
            return None
        head = parts[0]
        if head in _SKIP_SEGMENTS:
            return None
        if head in ("reel", "reels", "watch", "video.php", "permalink.php", "story.php"):
            return None

        tab = parts[1] if len(parts) >= 2 else ""
        identifier = f"{head}/{tab}" if tab in ("videos", "reels") else head
        return "account", identifier

    @staticmethod
    def _id_in_path(parts: List[str], query: str) -> Optional[str]:
        if query:
            match = re.search(r"[?&]v=(\d{6,25})", f"?{query}")
            if match:
                return match.group(1)
        joined = "/" + "/".join(parts)
        for pattern in _VIDEO_ID_PATTERNS:
            match = re.search(pattern, joined)
            if match:
                return match.group(1)
        return None

    def extract_id(self, url_or_id: str) -> str:
        value = str(url_or_id).strip()
        if value.isdigit():
            return value
        found = self._id_in_path(
            [part for part in urlsplit(value).path.split("/") if part], urlsplit(value).query
        )
        if found:
            return found
        tail = [part for part in urlsplit(value).path.split("/") if part]
        return tail[-1] if tail else value

    def item_id(self, raw: dict) -> Optional[str]:
        return raw.get("videoId") or raw.get("id")

    # ------------------------------------------------------------------
    # Media resolution
    # ------------------------------------------------------------------
    def fetch_item(self, identifier: str) -> dict:
        value = str(identifier).strip()
        candidates = []
        if "://" in value:
            candidates.append(value)
        else:
            candidates.extend([
                f"https://www.facebook.com/watch/?v={value}",
                f"https://m.facebook.com/watch/?v={value}",
            ])
        if "://" in value:
            candidates.append(value.replace("www.facebook.com", "m.facebook.com"))

        problems: List[str] = []
        for url in candidates:
            try:
                html, final_url = self._fetch_page(url)
            except RuntimeError as exc:
                problems.append(str(exc))
                continue
            if self._is_login_wall(html, final_url):
                problems.append(f"{url}: Facebook served a login wall")
                continue
            item = self._item_from_html(html, value)
            if item:
                return item
            problems.append(f"{url}: no playable stream found")

        detail = "; ".join(unique_preserving_order(iter(problems))) or "no response"
        hint = (
            " Open the video in a signed-in browser and retry with "
            "--cookies cookies.txt; Facebook requires a session for most videos."
        )
        raise RuntimeError(
            f"Could not resolve Facebook video {identifier} ({detail}).{hint}"
        )

    def normalize_item(self, raw: dict) -> dict:
        """Facebook stream URLs only exist in rendered pages, so fetch on demand."""
        if raw.get("video_url") and raw.get("media_type"):
            return raw
        video_id = str(raw.get("videoId") or raw.get("id") or "")
        if not video_id:
            raise RuntimeError("Facebook feed item did not contain a video ID.")
        item = self.fetch_item(video_id)
        page = str(raw.get("page") or "")
        if page and not item.get("account"):
            item["account"] = page
        return item

    def _item_from_html(self, html: str, identifier: str) -> Optional[dict]:
        stream_url, quality = self._extract_stream(html)
        if not stream_url:
            return None

        video_id = self._id_in_path(
            [part for part in urlsplit(identifier).path.split("/") if part],
            urlsplit(identifier).query,
        ) if "://" in identifier else (identifier if identifier.isdigit() else None)
        if not video_id:
            video_id = first_matching(r'"videoId"\s*:\s*"?(\d{6,25})', html) or identifier

        title = meta_content(html, "og:title") or first_matching(r'"title"\s*:\s*"([^"]{3,200})"', html) or ""
        thumbnail = meta_content(html, "og:image")
        is_reel = "/reel" in identifier or '"is_reel":true' in html
        account = self._page_name_from_html(html)

        return make_item(
            item_id=video_id,
            media_type="Reel" if is_reel else "Video",
            timestamp="",
            video_url=stream_url,
            quality=quality,
            display_url=thumbnail,
            title=title,
            account=account,
            ext="mp4",
            http_headers=self._stream_headers(),
        )

    @staticmethod
    def _page_name_from_html(html: str) -> str:
        """Extracts the page a video belongs to from the rendered page."""
        for pattern in (r'"vanity"\s*:\s*"([^"]+)"', r'"pageVanity"\s*:\s*"([^"]+)"'):
            match = first_matching(pattern, html)
            if match:
                return match

        profile = first_matching(r'"profileUrl"\s*:\s*"([^"]+)"', html)
        if profile:
            return profile.rstrip("/").rsplit("/", 1)[-1]

        canonical = meta_content(html, "og:url") or ""
        if not canonical:
            return ""
        parts = [part for part in urlsplit(canonical).path.split("/") if part]
        if not parts or parts[0].lower() in ("watch", "reel", "reels", "video"):
            return ""
        return parts[0]

    @staticmethod
    def _extract_stream(html: str) -> Tuple[Optional[str], str]:
        labels = {
            _STREAM_PATTERNS[0]: "HD (playable_url_quality_hd)",
            _STREAM_PATTERNS[1]: "HD (browser_native_hd_url)",
            _STREAM_PATTERNS[2]: "HD (hd_src)",
            _STREAM_PATTERNS[3]: "SD (playable_url)",
            _STREAM_PATTERNS[4]: "SD (browser_native_sd_url)",
            _STREAM_PATTERNS[5]: "SD (sd_src)",
            _STREAM_PATTERNS[6]: "progressive",
        }
        for pattern in _STREAM_PATTERNS:
            raw = first_matching(pattern, html)
            if not raw:
                continue
            url = unescape_js(raw)
            if url.startswith("http"):
                return url, labels.get(pattern, "best available")

        og_video = meta_content(html, "og:video") or meta_content(html, "og:video:url")
        if og_video and og_video.startswith("http"):
            return og_video, "og:video (best-effort)"
        return None, ""

    @staticmethod
    def _stream_headers() -> Dict[str, str]:
        return {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": "https://www.facebook.com/",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }

    @staticmethod
    def _is_login_wall(html: str, final_url: str) -> bool:
        if "login" in final_url.lower():
            return True
        lowered = html.lower()
        return any(marker in lowered for marker in _LOGIN_MARKERS)

    def _fetch_page(self, url: str) -> Tuple[str, str]:
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.facebook.com/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
        }
        try:
            response = self.session.get(url, headers=headers, timeout=25)
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not reach Facebook for {url}: {exc}") from exc
        if response.status_code != 200:
            raise RuntimeError(f"Facebook returned HTTP {response.status_code} for {url}")
        return response.text, str(response.url)

    # ------------------------------------------------------------------
    # Account feeds
    # ------------------------------------------------------------------
    def _split_account(self, account: str) -> Tuple[str, str]:
        raw = str(account).strip()
        if "://" in raw:
            parts = [part for part in urlsplit(raw).path.split("/") if part]
            raw = "/".join(parts)
        segments = [part for part in raw.split("/") if part]
        if not segments:
            return raw, ""
        tab = segments[1] if len(segments) >= 2 and segments[1] in ("videos", "reels") else ""
        return segments[0], tab

    def account_label(self, account: str) -> str:
        page, _tab = self._split_account(account)
        return f"Facebook page {page}"

    def resolve_account_id(self, account: str) -> str:
        page, tab = self._split_account(account)
        return f"{page}/{tab}" if tab else page

    def account_feeds(self) -> Tuple[AccountFeed, ...]:
        return (
            AccountFeed(
                "videos tab",
                "videos",
                lambda account_id, max_pages, account_name="": self.iter_page_items(
                    account_name or account_id, max_pages=max_pages),
            ),
        )

    def iter_page_items(
        self, account: str, max_pages: Optional[int] = None
    ) -> Iterator[dict]:
        page, _tab = self._split_account(account)
        seen: set = set()
        collected: List[str] = []

        for base in (f"https://mbasic.facebook.com/{page}/videos",
                     f"https://www.facebook.com/{page}/videos"):
            try:
                html, final_url = self._fetch_page(base)
            except RuntimeError as exc:
                print(f"[!] {exc}")
                continue

            if self._is_login_wall(html, final_url):
                print(
                    f"[!] Facebook served a login wall for {base}. "
                    "Page feeds require --cookies cookies.txt from a signed-in browser."
                )
                continue

            collected = self._ids_in_html(html)
            for video_id in collected:
                if video_id not in seen:
                    seen.add(video_id)
                    yield {
                        "videoId": video_id,
                        "page": page,
                        "isShort": False,
                        "isLive": False,
                    }
            if collected:
                break

        if not collected:
            raise RuntimeError(
                f"Facebook exposed no videos for the page '{page}'. "
                "Public page feeds are almost always behind a login wall; retry "
                "with --cookies cookies.txt exported from a signed-in browser, or "
                "download individual video URLs instead."
            )

        yield from self._paginate_page(page, seen, max_pages)

    def _paginate_page(self, page: str, seen: set, max_pages: Optional[int]) -> Iterator[dict]:
        """Follows mbasic's ``?after=`` cursor as far as Facebook allows."""
        after = None
        scanned = 1
        while max_pages is None or scanned < max_pages:
            url = f"https://mbasic.facebook.com/{page}/videos"
            params = {"after": after} if after else None
            try:
                response = self.session.get(
                    url, params=params, headers={"User-Agent": DEFAULT_USER_AGENT},
                    timeout=25,
                )
            except requests.RequestException:
                return
            if response.status_code != 200:
                return
            html = response.text
            if self._is_login_wall(html, str(response.url)):
                return

            found_any = False
            for video_id in self._ids_in_html(html):
                if video_id not in seen:
                    seen.add(video_id)
                    found_any = True
                    yield {
                        "videoId": video_id,
                        "page": page,
                        "isShort": False,
                        "isLive": False,
                    }
            next_after = first_matching(r"[?&]after=([A-Za-z0-9_\-%]+)", html)
            scanned += 1
            if not found_any or not next_after or next_after == after:
                return
            after = next_after

    @staticmethod
    def _ids_in_html(html: str) -> List[str]:
        ids: List[str] = []
        for pattern in _VIDEO_ID_PATTERNS:
            ids.extend(re.findall(pattern, html))
        return unique_preserving_order(
            item for item in ids if item and item.isdigit() and len(item) >= 6
        )
