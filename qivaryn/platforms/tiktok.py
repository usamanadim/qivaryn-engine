"""Native TikTok integration.

TikTok serves different HTML to desktop and mobile clients:

* Desktop pages are often a small JavaScript shell with no embedded data.
* Mobile pages embed the full item under ``<script id="api-data">`` and the
  profile owner (including ``secUid``) inside
  ``<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">``.

Both layouts are parsed, and profile pagination uses TikTok's public
``api/creator/item_list`` endpoint with its timestamp cursor.  No cookies are
required for public posts and profiles.  Short links (``vm.tiktok.com`` /
``vt.tiktok.com``) are resolved by following their redirect.
"""

import json
import re
import time
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import requests

from .base import DEFAULT_USER_AGENT, AccountFeed, BasePlatformClient, make_child, make_item
from .scrape import script_json, unique_preserving_order

_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "www.vm.tiktok.com",
    "www.vt.tiktok.com",
}

_SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com", "www.vm.tiktok.com", "www.vt.tiktok.com"}
_VIDEO_ID = re.compile(r"^\d{6,25}$")
_USERNAME = re.compile(r"^[A-Za-z0-9._]{1,24}$")
_ITEM_LINK = re.compile(r"/(@[A-Za-z0-9._]+)/(?:video|photo)/(\d{6,25})")

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)


class TikTokClient(BasePlatformClient):
    """Resolves TikTok videos, photo posts and profile feeds."""

    name = "tiktok"
    display_name = "TikTok"
    supported_media_types = ("all", "videos", "photos")
    filter_aliases = {
        "videos": ("video",),
        "photos": ("photo", "carousel"),
    }

    def __init__(self, sessionid: Optional[str] = None, cookies_file: Optional[str] = None,
                 allow_network_fallbacks: bool = True):
        super().__init__(sessionid=sessionid, cookies_file=None,
                         allow_network_fallbacks=allow_network_fallbacks)
        self.cookies_file = cookies_file
        self.session.headers.update({
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.tiktok.com/",
        })
        if cookies_file:
            self.load_cookies_from_file(cookies_file, domain="tiktok.com")
        if sessionid:
            self.session.cookies.set("sessionid", sessionid, domain=".tiktok.com")
        self._sec_uid_cache: Dict[str, str] = {}
        #: Renditions looked up in an author's feed, keyed by post ID.
        self._rendition_cache: Dict[str, List[dict]] = {}

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

        if host in _SHORT_HOSTS:
            code = [part for part in parsed.path.split("/") if part]
            return ("single", str(url).strip()) if code else None

        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            return None

        if parts[0].startswith("@"):
            username = parts[0]
            if len(parts) >= 3 and parts[1] in ("video", "photo") and _VIDEO_ID.match(parts[2]):
                return "single", parts[2]
            if len(parts) == 1:
                return "account", username
            return None

        # /t/<id> and /v/<id> are internal short paths served by the same host.
        if parts[0] in ("t", "v") and len(parts) >= 2:
            return "single", f"https://www.tiktok.com/{parts[0]}/{parts[1]}"

        query = parse_qs(parsed.query)
        if parts[0] in ("video", "photo") and query.get("item_id"):
            return "single", query["item_id"][0]
        return None

    def extract_id(self, url_or_id: str) -> str:
        value = str(url_or_id).strip()
        match = _ITEM_LINK.search(value)
        if match:
            return match.group(2)
        if _VIDEO_ID.match(value):
            return value
        if "://" in value:
            tail = [part for part in urlsplit(value).path.split("/") if part]
            if tail and _VIDEO_ID.match(tail[-1]):
                return tail[-1]
            return tail[-1] if tail else value
        return value.lstrip("@")

    def item_id(self, raw: dict) -> Optional[str]:
        return raw.get("itemId") or raw.get("id")

    # ------------------------------------------------------------------
    # Media resolution
    # ------------------------------------------------------------------
    def fetch_item(self, identifier: str) -> dict:
        value = str(identifier).strip()
        url = value
        if "://" not in value:
            url = f"https://www.tiktok.com/@i/video/{value}"

        pages = [self._fetch_page(url)]
        item = self._item_from_html(pages[0])
        if item is None:
            # Desktop and mobile responses use different layouts; sometimes only
            # one of them embeds the item payload.
            pages.append(self._fetch_page(url, user_agent=DEFAULT_USER_AGENT))
            item = self._item_from_html(pages[-1])
        if item is None:
            if any(self._looks_gated(page) for page in pages):
                raise RuntimeError(
                    f"TikTok served a verification page instead of {identifier}. "
                    "Retry with --cookies cookies.txt exported from a logged-in browser."
                )
            raise RuntimeError(
                f"TikTok returned no media data for {identifier}. The post may be "
                "private, deleted, region-locked, or TikTok may have changed its "
                "page layout. If the post is age-restricted, retry with "
                "--cookies cookies.txt exported from a logged-in browser."
            )
        return self.normalize_item(self._add_feed_renditions(item))

    def _add_feed_renditions(self, item: dict) -> dict:
        """Adds TikTok's full rendition list to a post fetched from a watch page.

        A watch page only exposes ``playAddr`` (the 576x1024 standard stream),
        while the author's public feed lists every rendition the post was
        uploaded in.  The lookup is skipped when the page already carries the
        renditions or when the author cannot be identified.
        """
        video = item.get("video") or {}
        if video.get("bitrateInfo"):
            return item

        item_id = str(item.get("id") or item.get("itemId") or "")
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        sec_uid = str(author.get("secUid") or "")
        if not item_id or not sec_uid:
            return item

        if item_id not in self._rendition_cache:
            self._rendition_cache[item_id] = self._renditions_from_feed(item_id, sec_uid, item)
        renditions = self._rendition_cache[item_id]
        if not renditions:
            return item

        enriched_video = dict(video)
        enriched_video["bitrateInfo"] = renditions
        enriched = dict(item)
        enriched["video"] = enriched_video
        return enriched

    def _renditions_from_feed(self, item_id: str, sec_uid: str, item: dict) -> List[dict]:
        """Looks a post up in the author's public feed to get its renditions."""
        created = item.get("createTime")
        if isinstance(created, (int, float)) and created > 0:
            cursor = int(created) * 1000 + 1
        else:
            cursor = int(time.time() * 1000)

        for _ in range(3):
            page = self._fetch_item_list_page(sec_uid, cursor)
            if not page:
                return []
            entries = page.get("itemList") or []
            for raw in entries:
                if str(raw.get("id")) == item_id:
                    video = raw.get("video") or {}
                    return list(video.get("bitrateInfo") or [])
            if not entries:
                return []
            try:
                next_cursor = int(entries[-1].get("createTime") or 0) * 1000
            except (TypeError, ValueError):
                return []
            if next_cursor <= 0 or next_cursor >= cursor:
                return []
            cursor = next_cursor
        return []

    def normalize_item(self, raw: dict) -> dict:
        item = raw.get("itemStruct") if isinstance(raw.get("itemStruct"), dict) else raw
        item_id = str(item.get("id") or item.get("itemId") or "")
        if not item_id:
            raise RuntimeError("TikTok feed item did not contain an identifier.")

        desc = str(item.get("desc") or "")
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        account = str(author.get("uniqueId") or author.get("unique_id") or "")
        created = item.get("createTime")
        timestamp = ""
        if isinstance(created, (int, float)) and created > 0:
            timestamp = datetime.fromtimestamp(int(created), tz=timezone.utc).isoformat()

        headers = self._stream_headers()
        images = self._photo_images(item)
        if images:
            if len(images) == 1:
                return make_item(
                    item_id=item_id,
                    media_type="Photo",
                    timestamp=timestamp,
                    display_url=images[0],
                    title=desc,
                    account=account,
                    quality="original resolution",
                    http_headers=headers,
                )
            children = [
                make_child(is_video=False, display_url=url, quality="original resolution",
                           http_headers=headers)
                for url in images
            ]
            return make_item(
                item_id=item_id,
                media_type="Carousel",
                timestamp=timestamp,
                display_url=images[0],
                title=desc,
                account=account,
                quality=f"{len(images)} photos",
                http_headers=headers,
                children=children,
            )

        video = item.get("video") or {}
        best_url, best_quality = self._best_video_url(video)
        if not best_url:
            raise RuntimeError(
                f"TikTok returned no downloadable video stream for {item_id}."
            )

        cover = ""
        for key in ("cover", "originCover", "dynamicCover"):
            value = video.get(key)
            if isinstance(value, str) and value.startswith("http"):
                cover = value
                break

        duration = video.get("duration")
        quality = best_quality
        if isinstance(duration, (int, float)) and duration:
            quality = f"{best_quality}, {int(duration)}s"

        return make_item(
            item_id=item_id,
            media_type="Video",
            timestamp=timestamp,
            video_url=best_url,
            quality=quality,
            display_url=cover or None,
            title=desc,
            account=account,
            ext="mp4",
            http_headers=headers,
        )

    @staticmethod
    def _photo_images(item: dict) -> List[str]:
        image_post = item.get("imagePost")
        if not isinstance(image_post, dict):
            return []
        urls: List[str] = []
        for image in image_post.get("images") or []:
            if not isinstance(image, dict):
                continue
            candidates = ((image.get("imageURL") or {}).get("urlList")) or []
            usable = [url for url in candidates if isinstance(url, str) and url.startswith("http")]
            if usable:
                urls.append(usable[-1])
        return urls

    @staticmethod
    def _best_video_url(video: dict) -> Tuple[Optional[str], str]:
        """Picks the best rendition TikTok offers for a post.

        TikTok lists several renditions per post and the one with the highest
        bitrate is usually the *smaller* 576x1024 stream, so the resolution
        decides and the bitrate only breaks ties.
        """
        best: Optional[str] = None
        best_key: Tuple[int, int] = (0, 0)
        best_bitrate = 0
        best_size = ""
        for entry in video.get("bitrateInfo") or []:
            if not isinstance(entry, dict):
                continue
            play = entry.get("PlayAddr") or {}
            urls = play.get("UrlList") or []
            usable = [url for url in urls if isinstance(url, str) and url.startswith("http")]
            if not usable:
                continue
            bitrate = int(entry.get("Bitrate") or entry.get("bitrate") or 0)
            width = int(play.get("Width") or 0)
            height = int(play.get("Height") or 0)
            key = (width * height, bitrate)
            if best is None or key > best_key:
                best_key = key
                best = usable[0]
                best_bitrate = bitrate
                best_size = f"{width}x{height}" if width and height else ""

        if best:
            details = best_size
            if best_bitrate > 0:
                rate = f"{best_bitrate // 1000} kbps"
                details = f"{details}, {rate}" if details else rate
            return best, f"no watermark ({details})" if details else "no watermark"

        for key in ("playAddr", "downloadAddr"):
            value = video.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value, "default stream"
        return None, "unknown"

    @staticmethod
    def _stream_headers() -> Dict[str, str]:
        return {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": "https://www.tiktok.com/",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }

    # ------------------------------------------------------------------
    # Page fetching and parsing
    # ------------------------------------------------------------------
    def _fetch_page(self, url: str, user_agent: str = MOBILE_USER_AGENT) -> str:
        target = url
        if any(host in url for host in _SHORT_HOSTS):
            target = self._resolve_short_link(url)
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.tiktok.com/",
        }
        try:
            response = self.session.get(target, headers=headers, timeout=25)
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not reach TikTok for {target}: {exc}") from exc
        if response.status_code != 200:
            raise RuntimeError(
                f"TikTok returned HTTP {response.status_code} for {target}. "
                "The content may be unavailable in your region."
            )
        return response.text

    def _resolve_short_link(self, url: str) -> str:
        headers = {"User-Agent": MOBILE_USER_AGENT, "Referer": "https://www.tiktok.com/"}
        try:
            response = self.session.get(url, headers=headers, timeout=25, allow_redirects=True)
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not resolve TikTok short link {url}: {exc}") from exc
        final = str(response.url)
        return final if "tiktok.com" in final else url

    @staticmethod
    def _looks_gated(html: str) -> bool:
        lowered = html.lower()
        return (
            "verify to continue" in lowered
            or "captcha-verify-container" in lowered
            or "tiktok is not available in your region" in lowered
        )

    @staticmethod
    def _scope(html: str) -> Optional[dict]:
        data = script_json(html, "__UNIVERSAL_DATA_FOR_REHYDRATION__")
        if isinstance(data, dict):
            scope = data.get("__DEFAULT_SCOPE__")
            if isinstance(scope, dict):
                return scope
        return None

    @classmethod
    def _item_from_html(cls, html: str) -> Optional[dict]:
        item = cls._item_from_scope(cls._scope(html))
        if item:
            return item

        # Mobile watch pages put the post in this smaller script instead.
        api_data = script_json(html, "api-data")
        if isinstance(api_data, dict):
            detail = api_data.get("videoDetail") or {}
            candidate = (detail.get("itemInfo") or {}).get("itemStruct")
            if isinstance(candidate, dict) and candidate:
                return candidate

        # Older layouts exposed a map of items through SIGI_STATE.
        sigi = script_json(html, "SIGI_STATE")
        if isinstance(sigi, dict):
            module = sigi.get("ItemModule")
            if isinstance(module, dict):
                for candidate in module.values():
                    if isinstance(candidate, dict) and candidate.get("id"):
                        return candidate
        return None

    @classmethod
    def _item_from_scope(cls, scope: Optional[dict]) -> Optional[dict]:
        if not isinstance(scope, dict):
            return None
        for key in ("webapp.video-detail", "webapp.post-detail", "webapp.photo-detail"):
            nested = scope.get(key) or {}
            candidate = (nested.get("itemInfo") or {}).get("itemStruct")
            if isinstance(candidate, dict) and candidate:
                return candidate

        # Some layouts nest the payload under a different scope key.
        stack = [scope]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key, value in current.items():
                    if key == "itemStruct" and isinstance(value, dict) and value.get("id"):
                        return value
                    stack.append(value)
            elif isinstance(current, list):
                stack.extend(current)
        return None

    @staticmethod
    def _first_item_list(data) -> List[dict]:
        """Returns the first ``itemList`` array of item objects found in ``data``."""
        stack = [data]
        while stack:
            current = stack.pop(0)
            if isinstance(current, dict):
                for key, value in current.items():
                    if key == "itemList" and isinstance(value, list):
                        items = [
                            entry
                            for entry in value
                            if isinstance(entry, dict) and entry.get("id")
                        ]
                        if items:
                            return items
                    stack.append(value)
            elif isinstance(current, list):
                stack.extend(current)
        return []

    @staticmethod
    def _profile_items_from_html(html: str, username: str) -> List[dict]:
        scope = TikTokClient._scope(html)
        if scope:
            items = TikTokClient._first_item_list(scope)
            if items:
                return items

        ids = unique_preserving_order(
            match.group(2)
            for match in _ITEM_LINK.finditer(html)
            if match.group(1).lower() == f"@{username}".lower()
        )
        return [{"id": item_id} for item_id in ids]

    # ------------------------------------------------------------------
    # Account feeds
    # ------------------------------------------------------------------
    def _split_account(self, account: str) -> str:
        value = str(account).strip()
        if "://" in value:
            parts = [part for part in urlsplit(value).path.split("/") if part]
            value = parts[0] if parts else value
        return value.lstrip("@")

    def account_label(self, account: str) -> str:
        return f"TikTok profile @{self._split_account(account)}"

    def resolve_account_id(self, account: str) -> str:
        username = self._split_account(account)
        if not _USERNAME.match(username):
            raise RuntimeError(f"'{account}' is not a valid TikTok username.")
        return username

    def account_feeds(self) -> Tuple[AccountFeed, ...]:
        return (
            AccountFeed(
                "profile feed",
                "videos",
                lambda account_id, max_pages, account_name="": self.iter_user_items(
                    account_name or account_id, max_pages=max_pages),
            ),
        )

    def iter_user_items(
        self, account: str, max_pages: Optional[int] = None
    ) -> Iterator[dict]:
        username = self._split_account(account)
        print(f"[*] Scanning TikTok profile @{username}...")
        html = self._fetch_page(f"https://www.tiktok.com/@{username}")

        if self._looks_gated(html) and not self.has_session():
            raise RuntimeError(
                f"TikTok served a verification page for @{username}. "
                "Retry with --cookies cookies.txt exported from a logged-in browser."
            )

        scope = self._scope(html)
        embedded = self._profile_items_from_html(html, username)
        seen = set()
        for item in embedded:
            item_id = str(item.get("id") or "")
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            yield item

        sec_uid = self._sec_uid_from_scope(scope)
        yield from self._paginate_user_items(username, seen, max_pages, sec_uid)

    @staticmethod
    def _sec_uid_from_scope(scope: Optional[dict]) -> Optional[str]:
        if not scope:
            return None
        user = ((scope.get("webapp.user-detail") or {}).get("userInfo") or {}).get("user") or {}
        sec_uid = user.get("secUid")
        return sec_uid if isinstance(sec_uid, str) and sec_uid else None

    def _paginate_user_items(
        self,
        username: str,
        seen: set,
        max_pages: Optional[int],
        sec_uid: Optional[str] = None,
    ) -> Iterator[dict]:
        """Follows TikTok's public item list using its timestamp cursor."""
        sec_uid = sec_uid or self._sec_uid_cache.get(username.lower())
        if not sec_uid:
            scope = self._scope(self._fetch_page(f"https://www.tiktok.com/@{username}"))
            sec_uid = self._sec_uid_from_scope(scope)
        if not sec_uid:
            print(
                "[!] TikTok did not expose a pagination token for this profile; "
                "only the items embedded in the page were scanned."
            )
            return
        self._sec_uid_cache[username.lower()] = sec_uid

        # ``creator/item_list`` expects a millisecond timestamp and returns the
        # posts created before it.  Walking backwards from now mirrors TikTok's
        # own newest-first profile feed.
        cursor = int(time.time() * 1000)
        pages = 0
        while max_pages is None or pages < max_pages:
            page = self._fetch_item_list_page(sec_uid, cursor)
            if not page:
                break
            items = page.get("itemList") or []
            if not items:
                break
            yielded = False
            for item in items:
                item_id = str(item.get("id") or "")
                if not item_id or item_id in seen:
                    continue
                seen.add(item_id)
                yielded = True
                yield item
            pages += 1
            if not yielded or not page.get("hasMorePrevious", True):
                break
            try:
                next_cursor = int(items[-1].get("createTime") or 0) * 1000
            except (TypeError, ValueError):
                next_cursor = 0
            if next_cursor <= 0 or next_cursor >= cursor:
                break
            cursor = next_cursor

    def _fetch_item_list_page(self, sec_uid: str, cursor: int) -> Optional[dict]:
        url = "https://www.tiktok.com/api/creator/item_list/"
        params = {
            "aid": "1988",
            "app_language": "en",
            "app_name": "tiktok_web",
            "browser_language": "en-US",
            "browser_name": "Mozilla",
            "browser_platform": "Win32",
            "browser_version": "5.0 (Windows)",
            "channel": "tiktok_web",
            "count": "15",
            "cursor": str(cursor),
            "device_platform": "web_pc",
            "focus_state": "true",
            "from_page": "user",
            "history_len": "2",
            "is_fullscreen": "false",
            "is_page_visible": "true",
            "language": "en",
            "os": "windows",
            "priority_region": "",
            "referer": "",
            "region": "US",
            "screen_height": "1080",
            "screen_width": "1920",
            "secUid": sec_uid,
            "type": "1",
            "tz_name": "UTC",
            "webcast_language": "en",
        }
        headers = {
            "User-Agent": MOBILE_USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.tiktok.com/",
        }
        try:
            response = self.session.get(url, params=params, headers=headers, timeout=25)
        except requests.RequestException:
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            return None
        if payload.get("statusCode") not in (0, None):
            print(
                "[!] TikTok declined the paginated post list request "
                f"(status {payload.get('statusCode')}). Only the items embedded in "
                "the profile page were scanned; --cookies may extend it."
            )
            return None
        return payload
