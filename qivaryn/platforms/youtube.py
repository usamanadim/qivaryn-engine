"""Native YouTube integration.

Streams are resolved through YouTube's own InnerTube ``player`` endpoint, which
the visionOS, web, Android and iOS clients all use.  Several client identities
are tried in turn because YouTube serves different (and inconsistently gated)
payloads to each one, and the embedded ``ytInitialPlayerResponse`` from the
watch page is used as a last resort.

The visionOS client is tried first: its media URLs serve the whole file through
ordinary range requests, while the Android and iOS clients only allow a small
window at the start of an audio track.

Known limitation: formats whose URL is protected by a ``signatureCipher`` need
YouTube's obfuscated JavaScript player to be executed.  Qivaryn has no
JavaScript engine, so a video offering *only* ciphered formats is reported as
unsupported instead of silently producing a broken file.
"""

import re
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, quote_plus, urlsplit

import requests

from .base import DEFAULT_USER_AGENT, AccountFeed, BasePlatformClient, make_item
from .scrape import first_matching, iter_keyed_objects, json_after, unique_preserving_order

WEB_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
ANDROID_KEY = "AIzaSyA8eiZmM1FaDVjRy-df2KTyQ_vz_yYM39w"
IOS_KEY = "AIzaSyB-63vPrdThhKuerbB2N_l7Kwwcxj6yUAc"

#: Client identities, ordered from the most permissive to the most restricted.
#:
#: ``VISIONOS`` stream URLs are not subject to the small audio range window
#: enforced on Android/iOS URLs, so it is preferred whenever a visitor ID is
#: available.  The first request is expected to fail with ``LOGIN_REQUIRED``;
#: its response still carries the visitor ID used by the successful retry.
INNERTUBE_CLIENTS: Tuple[Dict[str, Any], ...] = (
    {
        "key": WEB_KEY,
        "name": "VISIONOS",
        "client_number": "101",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 "
            "Safari/605.1.15"
        ),
        "requires_visitor_data": True,
        "context": {
            "clientName": "VISIONOS",
            "clientVersion": "1.02",
            "deviceMake": "Apple",
            "deviceModel": "RealityDevice17,1",
            "osName": "visionOS",
            "osVersion": "26.5.23O471",
        },
    },
    {
        "key": ANDROID_KEY,
        "name": "ANDROID",
        "client_number": "3",
        "user_agent": "com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip",
        "context": {
            "clientName": "ANDROID",
            "clientVersion": "20.10.38",
            "androidSdkVersion": 30,
            "osName": "Android",
            "osVersion": "11",
        },
    },
    {
        "key": IOS_KEY,
        "name": "IOS",
        "client_number": "5",
        "user_agent": "com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_1_0 like Mac OS X)",
        "context": {
            "clientName": "IOS",
            "clientVersion": "20.10.4",
            "deviceModel": "iPhone16,2",
            "osName": "iPhone",
            "osVersion": "18.1.0.22B83",
        },
    },
    {
        "key": WEB_KEY,
        "name": "WEB",
        "client_number": "1",
        "user_agent": DEFAULT_USER_AGENT,
        "context": {
            "clientName": "WEB",
            "clientVersion": "2.20240726.00.00",
        },
    },
)

_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ACCOUNT_TABS = ("videos", "shorts", "streams")


class UnsupportedStreamError(RuntimeError):
    """Raised when every available format needs JavaScript signature decoding."""


class YouTubeClient(BasePlatformClient):
    """Resolves YouTube videos, Shorts, playlists and channel feeds."""

    name = "youtube"
    display_name = "YouTube"
    supported_media_types = ("all", "videos", "shorts", "streams")
    supports_playlists = True
    filter_aliases = {
        "videos": ("video",),
        "shorts": ("short",),
        "streams": ("stream", "live"),
    }

    def __init__(self, sessionid: Optional[str] = None, cookies_file: Optional[str] = None,
                 allow_network_fallbacks: bool = True, quality: str = "best"):
        super().__init__(sessionid=sessionid, cookies_file=None,
                         allow_network_fallbacks=allow_network_fallbacks)
        self.cookies_file = cookies_file
        self.quality = self._normalize_quality(quality)
        self.session.headers.update({
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        if cookies_file:
            self.load_cookies_from_file(cookies_file, domain="youtube.com")
        self._channel_cache: Dict[str, str] = {}
        self._visitor_data: Optional[str] = None

    @staticmethod
    def _normalize_quality(quality: str) -> str:
        """Returns ``best``, ``worst`` or a positive height as a string."""
        value = str(quality or "best").strip().lower()
        if value in ("best", "worst"):
            return value
        if value.endswith("p"):
            value = value[:-1]
        if value.isdigit() and int(value) > 0:
            return str(int(value))
        raise ValueError(
            f"Invalid quality '{quality}'. Use 'best', 'worst', or a height "
            "such as 1080 or 720p."
        )

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
        query = parse_qs(parsed.query)

        if host.endswith("youtu.be"):
            if parts and _VIDEO_ID.match(parts[0]):
                return "single", parts[0]
            return None

        if parts:
            head = parts[0].lower()
            if head in ("watch", "embed", "v", "live") and query.get("v"):
                video_id = query["v"][0]
                if _VIDEO_ID.match(video_id):
                    return "single", video_id
            if head in ("shorts", "embed", "live", "v") and len(parts) >= 2:
                if _VIDEO_ID.match(parts[1]):
                    return "single", parts[1]
            if head == "playlist" and query.get("list"):
                return "playlist", query["list"][0]
            if head.startswith("@") or head in ("channel", "c", "user"):
                return YouTubeClient._account_target(head, parts)

        if query.get("v"):
            video_id = query["v"][0]
            if _VIDEO_ID.match(video_id):
                return "single", video_id
        if query.get("list"):
            return "playlist", query["list"][0]
        return None

    @staticmethod
    def _account_target(head: str, parts: List[str]) -> Optional[Tuple[str, str]]:
        if head.startswith("@"):
            handle = parts[0]
            tab = parts[1] if len(parts) >= 2 else ""
            return "account", f"{handle}/{tab}" if tab in _ACCOUNT_TABS else handle
        if len(parts) >= 2:
            tab = parts[2] if len(parts) >= 3 else ""
            identifier = f"{head}/{parts[1]}"
            return "account", f"{identifier}/{tab}" if tab in _ACCOUNT_TABS else identifier
        return None

    def extract_id(self, url_or_id: str) -> str:
        value = str(url_or_id).strip()
        if "://" not in value and "/" not in value:
            return value
        detected = self.detect_url(value)
        if detected:
            if detected[0] == "single":
                return detected[1]
            return detected[1].split("/")[0]
        tail = value.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        return tail or value

    def item_id(self, raw: dict) -> Optional[str]:
        return raw.get("videoId") or raw.get("id")

    def prefilter(self, raw: dict, media_filter: str) -> bool:
        mode = (media_filter or "all").lower()
        if mode == "all":
            return True
        if mode == "shorts":
            return bool(raw.get("isShort")) or raw.get("tab") == "shorts"
        if mode == "streams":
            return bool(raw.get("isLive")) or raw.get("tab") == "streams"
        if mode == "videos":
            return not raw.get("isShort")
        return True

    # ------------------------------------------------------------------
    # Media resolution
    # ------------------------------------------------------------------
    def fetch_item(self, identifier: str) -> dict:
        value = str(identifier).strip()
        video_id = self.extract_id(value)
        if not _VIDEO_ID.match(video_id):
            raise RuntimeError(
                f"'{identifier}' is not a valid YouTube video ID or URL."
            )
        return self.normalize_item({"videoId": video_id, "title": ""})

    def normalize_item(self, raw: dict) -> dict:
        video_id = self.item_id(raw)
        if not video_id:
            raise RuntimeError("YouTube feed item did not contain a video ID.")

        player, client_name = self._fetch_player_response(video_id)
        status = (player.get("playabilityStatus") or {})
        details = player.get("videoDetails") or {}
        streaming = player.get("streamingData") or {}

        if status.get("status") not in (None, "OK") and not streaming:
            raise RuntimeError(self._playability_message(video_id, status))

        if not streaming:
            raise RuntimeError(
                f"YouTube returned no playable streams for {video_id}. "
                "The video may be private, region-locked, a live stream, or "
                "restricted to signed-in adult accounts. Retry with "
                "--cookies cookies.txt exported from a logged-in browser."
            )

        video_url, audio_url, quality, ext, headers, quality_fallback = self._select_streams(
            streaming, video_id
        )
        if not video_url:
            raise RuntimeError(
                f"YouTube offered no directly downloadable stream for {video_id}. "
                "Every available format is signature-protected, which Qivaryn "
                "cannot decode without a JavaScript engine."
            )

        microformat = ((player.get("microformat") or {}).get("playerMicroformatRenderer") or {})
        timestamp = microformat.get("publishDate") or microformat.get("uploadDate") or ""
        title = details.get("title") or raw.get("title") or ""
        display_url = None
        thumbnails = (details.get("thumbnail") or {}).get("thumbnails") or []
        if thumbnails:
            display_url = thumbnails[-1].get("url")

        is_short = raw.get("isShort") or microformat.get("category") == "Shorts"
        media_type = "Short" if is_short else ("Stream" if details.get("isLive") else "Video")
        account = str(details.get("author") or "") or str(
            microformat.get("ownerChannelName") or ""
        )

        item = make_item(
            item_id=video_id,
            media_type=media_type,
            timestamp=str(timestamp),
            video_url=video_url,
            audio_url=audio_url,
            quality=f"{quality} ({client_name})",
            display_url=display_url,
            title=title,
            account=account,
            ext=ext,
            http_headers=headers,
        )
        if quality_fallback:
            item["quality_fallback"] = quality_fallback
        return item

    @staticmethod
    def _playability_message(video_id: str, status: dict) -> str:
        reason = str(status.get("reason") or "").strip()
        state = str(status.get("status") or "").strip()
        hint = (
            "Retry with --cookies cookies.txt exported from a signed-in browser; "
            "YouTube blocks anonymous clients for this content."
        )
        if "bot" in reason.lower():
            return (
                f"YouTube is asking {video_id} to prove it is not a bot "
                f"(client reported: {reason}). {hint}"
            )
        if state == "AGE_VERIFICATION_REQUIRED" or "age" in reason.lower():
            return (
                f"YouTube requires age verification for {video_id} "
                f"({reason or 'AGE_VERIFICATION_REQUIRED'}). {hint}"
            )
        if state in ("LOGIN_REQUIRED", "UNPLAYABLE"):
            return f"YouTube refused to play {video_id}: {reason or state}. {hint}"
        return (
            f"YouTube returned an unusable player response for {video_id} "
            f"({state or 'unknown status'}{': ' + reason if reason else ''}). "
            f"{hint}"
        )

    # ------------------------------------------------------------------
    # InnerTube plumbing
    # ------------------------------------------------------------------
    def _player_context(self, client: Dict[str, Any]) -> dict:
        client_context = dict(
            client["context"], hl="en", gl="US", timeZone="UTC",
            utcOffsetMinutes=0,
        )
        if client.get("requires_visitor_data") and self._visitor_data:
            client_context["visitorData"] = self._visitor_data
        return {
            "context": {
                "client": client_context,
            },
            "videoId": None,
            "contentCheckOk": True,
            "racyCheckOk": True,
        }

    def _request_player(self, video_id: str, client: Dict[str, Any]) -> Optional[dict]:
        body = self._player_context(client)
        body["videoId"] = video_id
        return self._innertube_post("player", body, client)

    def _adopt_visitor_data(self, payload: Optional[dict]) -> bool:
        """Stores the visitor ID carried by a response.

        Returns True when a visitor ID was found, which means the request can
        be retried with it.
        """
        if not isinstance(payload, dict):
            return False
        visitor = (payload.get("responseContext") or {}).get("visitorData")
        if not isinstance(visitor, str) or not visitor:
            return False
        if visitor == self._visitor_data:
            return False
        self._visitor_data = visitor
        return True

    def _innertube_post(self, endpoint: str, body: dict, client: Dict[str, Any]) -> Optional[dict]:
        url = f"https://www.youtube.com/youtubei/v1/{endpoint}?key={client['key']}&prettyPrint=false"
        headers = {
            "User-Agent": client["user_agent"],
            "Content-Type": "application/json",
            "X-YouTube-Client-Name": client["client_number"],
            "X-YouTube-Client-Version": client["context"]["clientVersion"],
            "X-Goog-Api-Format-Version": "2",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.youtube.com",
            "Referer": "https://www.youtube.com/",
        }
        if client.get("requires_visitor_data") and self._visitor_data:
            headers["X-Goog-Visitor-Id"] = self._visitor_data
        try:
            response = self.session.post(url, json=body, headers=headers, timeout=20)
        except requests.RequestException:
            return None
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def _fetch_player_response(self, video_id: str) -> Tuple[dict, str]:
        failures: List[str] = []
        for client in INNERTUBE_CLIENTS:
            payload = self._request_player(video_id, client)

            if client.get("requires_visitor_data"):
                if not self._visitor_data:
                    # The unauthenticated call is rejected, but its response
                    # still supplies the visitor ID required by the retry.
                    if self._adopt_visitor_data(payload):
                        payload = self._request_player(video_id, client)
                elif not (payload and payload.get("streamingData")):
                    # The cached visitor ID may have expired; adopt a fresh one
                    # when the response offers a different one.
                    if self._adopt_visitor_data(payload):
                        payload = self._request_player(video_id, client)

            if not payload:
                failures.append(f"{client['name']}: no response")
                continue
            if payload.get("streamingData"):
                return payload, client["name"]
            status = (payload.get("playabilityStatus") or {}).get("status")
            if status and status != "OK":
                failures.append(f"{client['name']}: {status}")
                continue
            failures.append(f"{client['name']}: no streamingData")

        if self.allow_network_fallbacks:
            html_player = self._player_response_from_html(video_id)
            if html_player and html_player.get("streamingData"):
                return html_player, "watch page"

        raise RuntimeError(
            f"Could not obtain a playable YouTube response for {video_id} "
            f"({'; '.join(failures) or 'no client responded'})."
        )

    def _player_response_from_html(self, video_id: str) -> Optional[dict]:
        url = f"https://www.youtube.com/watch?v={video_id}&hl=en"
        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.youtube.com/",
        }
        try:
            response = self.session.get(url, headers=headers, timeout=20)
        except requests.RequestException:
            return None
        if response.status_code != 200:
            return None
        player = json_after(response.text, "ytInitialPlayerResponse")
        if isinstance(player, dict):
            return player
        return None

    # ------------------------------------------------------------------
    # Stream selection
    # ------------------------------------------------------------------
    def _select_streams(
        self, streaming: dict, video_id: str
    ) -> Tuple[Optional[str], Optional[str], str, str, Dict[str, str], Optional[dict]]:
        progressive = list(streaming.get("formats") or [])
        adaptive = list(streaming.get("adaptiveFormats") or [])
        progressive, adaptive, quality_fallback = self._apply_quality(progressive, adaptive)

        headers = self._stream_headers(video_id)
        best_prog = self._best_progressive(progressive)
        adaptive_result = self._best_adaptive(adaptive)

        if adaptive_result:
            video_fmt, audio_fmt = adaptive_result
            prog_height = int(best_prog.get("height") or 0) if best_prog else 0
            video_height = int(video_fmt.get("height") or 0)
            video_container = self._container_of(video_fmt)

            if best_prog and prog_height >= video_height:
                quality = best_prog.get("qualityLabel") or f"{prog_height}p"
                return (
                    self._format_url(best_prog),
                    None,
                    f"{quality} progressive",
                    self._container_of(best_prog),
                    headers,
                    quality_fallback,
                )

            # Stream copy requires the audio codec to be legal in the chosen
            # container.  ``_best_adaptive`` already prefers a matching audio
            # container; if none exists, keep the video container and let the
            # FFmpeg fallback transcode the audio track.
            ext = video_container or "mp4"
            quality = video_fmt.get("qualityLabel") or f"{video_height}p"
            return (
                self._format_url(video_fmt),
                self._format_url(audio_fmt) if audio_fmt else None,
                quality,
                ext,
                headers,
                quality_fallback,
            )

        if best_prog:
            quality = best_prog.get("qualityLabel") or f"{best_prog.get('height') or '?'}p"
            return (
                self._format_url(best_prog),
                None,
                f"{quality} progressive",
                self._container_of(best_prog),
                headers,
                quality_fallback,
            )

        # Last resort: a video-only adaptive stream with no audio track.
        video_only = self._pick_video(adaptive)
        if video_only:
            quality = video_only.get("qualityLabel") or f"{video_only.get('height') or '?'}p"
            return (
                self._format_url(video_only),
                None,
                f"{quality} video-only",
                self._container_of(video_only),
                headers,
                quality_fallback,
            )
        return None, None, "Unknown", "mp4", headers, quality_fallback

    def _apply_quality(
        self, progressive: List[dict], adaptive: List[dict]
    ) -> Tuple[List[dict], List[dict], Optional[dict]]:
        """Filters video formats to the requested quality.

        Returns the filtered progressive/adaptive lists and a fallback
        description when the exact height was unavailable.  Audio formats are
        always kept so adaptive video can still be paired with a soundtrack.
        """
        heights = self._available_video_heights(progressive, adaptive)
        if not heights or self.quality == "best":
            return progressive, adaptive, None

        if self.quality == "worst":
            target = heights[0]
            return (
                self._filter_streams_by_height(progressive, target),
                self._filter_streams_by_height(adaptive, target),
                None,
            )

        requested = int(self.quality)
        if requested in heights:
            target = requested
            fallback = None
        else:
            lower = [height for height in heights if height < requested]
            if lower:
                target = lower[-1]
                fallback = {"requested": requested, "selected": target}
            else:
                # Nothing lower exists; use the lowest available rather than
                # failing the download outright.
                target = heights[0]
                fallback = {
                    "requested": requested,
                    "selected": target,
                    "no_lower": True,
                }

        return (
            self._filter_streams_by_height(progressive, target),
            self._filter_streams_by_height(adaptive, target),
            fallback,
        )

    def _available_video_heights(
        self, progressive: List[dict], adaptive: List[dict]
    ) -> List[int]:
        heights = set()
        for fmt in list(progressive) + list(adaptive):
            if "video/" not in str(fmt.get("mimeType", "")):
                continue
            if not self._format_url(fmt):
                continue
            height = int(fmt.get("height") or 0)
            if height > 0:
                heights.add(height)
        return sorted(heights)

    @staticmethod
    def _filter_streams_by_height(formats: List[dict], target: int) -> List[dict]:
        filtered = []
        for fmt in formats:
            mime_type = str(fmt.get("mimeType", ""))
            if mime_type.startswith("audio/"):
                filtered.append(fmt)
                continue
            if "video/" in mime_type:
                height = int(fmt.get("height") or 0)
                if height and height <= target:
                    filtered.append(fmt)
                continue
            filtered.append(fmt)
        return filtered

    def _best_progressive(self, formats: List[dict]) -> Optional[dict]:
        candidates = [
            fmt
            for fmt in formats
            if self._format_url(fmt)
            and "video" in str(fmt.get("mimeType", ""))
            and not str(fmt.get("mimeType", "")).startswith("audio")
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda fmt: (
                int(fmt.get("height") or 0),
                int(fmt.get("bitrate") or 0),
                1 if self._container_of(fmt) == "mp4" else 0,
            ),
            reverse=True,
        )
        return candidates[0]

    def _best_adaptive(self, formats: List[dict]) -> Optional[Tuple[dict, Optional[dict]]]:
        video = self._pick_video(formats)
        if not video:
            return None
        container = self._container_of(video)
        audio = self._pick_audio(formats, container) or self._pick_audio(formats)
        return video, audio

    def _pick_video(self, formats: List[dict]) -> Optional[dict]:
        candidates = [
            fmt
            for fmt in formats
            if self._format_url(fmt) and "video/" in str(fmt.get("mimeType", ""))
        ]
        if not candidates:
            return None
        # Sorted descending, so the highest value wins: resolution first, then
        # bitrate, then H.264 (widest player compatibility), then MP4.
        candidates.sort(
            key=lambda fmt: (
                int(fmt.get("height") or 0),
                int(fmt.get("bitrate") or 0),
                1 if "avc1" in str(fmt.get("mimeType", "")) else 0,
                1 if self._container_of(fmt) == "mp4" else 0,
            ),
            reverse=True,
        )
        return candidates[0]

    def _pick_audio(self, formats: List[dict], prefer_container: str = "") -> Optional[dict]:
        candidates = [
            fmt
            for fmt in formats
            if self._format_url(fmt) and str(fmt.get("mimeType", "")).startswith("audio/")
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda fmt: (
                1 if prefer_container and self._container_of(fmt) == prefer_container else 0,
                1 if "mp4a" in str(fmt.get("mimeType", "")) else 0,
                int(fmt.get("bitrate") or 0),
            ),
            reverse=True,
        )
        return candidates[0]

    @staticmethod
    def _container_of(fmt: dict) -> str:
        mime = str(fmt.get("mimeType") or "")
        if "webm" in mime:
            return "webm"
        if "mp4" in mime:
            return "mp4"
        return ""

    @staticmethod
    def _stream_headers(video_id: str) -> Dict[str, str]:
        return {
            "User-Agent": DEFAULT_USER_AGENT,
            "Referer": "https://www.youtube.com/",
            "Origin": "https://www.youtube.com",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }

    @staticmethod
    def _format_url(fmt: dict) -> Optional[str]:
        """Returns a directly downloadable URL, or None when ciphered."""
        direct = fmt.get("url")
        if isinstance(direct, str) and direct.startswith("http"):
            return direct

        cipher = fmt.get("signatureCipher") or fmt.get("cipher")
        if not cipher:
            return None
        params = parse_qs(cipher)
        url = (params.get("url") or [None])[0]
        signature = (params.get("s") or params.get("sig") or [None])[0]
        if url and not signature:
            return url
        # A present ``s`` value must be run through YouTube's obfuscated player.
        return None

    # ------------------------------------------------------------------
    # Account and playlist enumeration
    # ------------------------------------------------------------------
    def _account_targets(self, account: str) -> Tuple[str, Optional[str]]:
        """Splits a detected account identifier into ``(target, tab)``."""
        raw = str(account).strip()
        parts = [part for part in raw.split("/") if part]
        if len(parts) >= 2 and parts[-1] in _ACCOUNT_TABS:
            return "/".join(parts[:-1]), parts[-1]
        if len(parts) >= 2 and parts[0] in ("channel", "c", "user"):
            return "/".join(parts), None
        return raw, None

    def _channel_url(self, channel_id: str, tab: Optional[str]) -> str:
        suffix = f"/{tab}" if tab else ""
        return f"https://www.youtube.com/channel/{channel_id}{suffix}?hl=en"

    def resolve_account_id(self, account: str) -> str:
        target, _tab = self._account_targets(account)
        if target.startswith("channel/"):
            target = target.split("/", 1)[1]
        if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", target):
            return target
        cached = self._channel_cache.get(target.lower())
        if cached:
            return cached

        if target.startswith(("c/", "user/")):
            candidates = [f"https://www.youtube.com/{target}?hl=en"]
        else:
            handle = target if target.startswith("@") else f"@{target}"
            candidates = [f"https://www.youtube.com/{handle}?hl=en"]

        for url in candidates:
            channel_id = self._channel_id_from_page(url)
            if channel_id:
                self._channel_cache[target.lower()] = channel_id
                return channel_id

        raise RuntimeError(
            f"Could not resolve the YouTube channel '{account}'. "
            "Check the handle, channel URL, or channel ID, then try again."
        )

    def _channel_id_from_page(self, url: str) -> Optional[str]:
        try:
            response = self.session.get(url, timeout=20)
        except requests.RequestException:
            return None
        if response.status_code != 200:
            return None
        html = response.text
        for pattern in (
            r'"channelId":"(UC[A-Za-z0-9_-]{22})"',
            r'"externalId":"(UC[A-Za-z0-9_-]{22})"',
            r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"',
            r'"browseId":"(UC[A-Za-z0-9_-]{22})"',
        ):
            found = first_matching(pattern, html)
            if found:
                return found
        return None

    def account_label(self, account: str) -> str:
        return f"YouTube channel {account}"

    def default_filter_for(self, account_identifier: str) -> str:
        _target, tab = self._account_targets(account_identifier)
        return tab or "all"

    def _feed_fetcher(self, tab: str):
        def fetch(account_id: str, max_pages: Optional[int], account_name: str = "") -> Iterator[dict]:
            return self.iter_channel_items(account_id, tab, max_pages=max_pages)

        return fetch

    def account_feeds(self) -> Tuple[AccountFeed, ...]:
        return tuple(
            AccountFeed(f"{tab.title()} tab", tab, self._feed_fetcher(tab))
            for tab in _ACCOUNT_TABS
        )

    def iter_channel_items(
        self, channel_id: str, tab: str = "videos", max_pages: Optional[int] = None
    ) -> Iterator[dict]:
        """Yields ``videoId`` stubs from a channel tab, following continuations."""
        _target, requested_tab = self._account_targets(channel_id)
        tab = requested_tab or tab
        url = self._channel_url(channel_id, tab)

        try:
            response = self.session.get(url, timeout=20)
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not reach YouTube for {channel_id}: {exc}") from exc
        if response.status_code != 200:
            raise RuntimeError(
                f"YouTube returned HTTP {response.status_code} for the {tab} tab of "
                f"{channel_id}. The channel may not exist or may be blocked in your region."
            )

        data = json_after(response.text, "ytInitialData")
        if not isinstance(data, dict):
            raise RuntimeError(
                f"YouTube did not expose the {tab} tab data for {channel_id}. "
                "The channel may be empty or the page layout may have changed."
            )

        pages = 0
        contents, token = self._grid_contents(data)
        while contents is not None:
            for item in self._items_from_contents(contents, tab):
                yield item
            pages += 1
            if not token or (max_pages is not None and pages >= max_pages):
                return
            contents, token = self._next_page(token)

    def iter_playlist_items(
        self, playlist_id: str, max_pages: Optional[int] = None
    ) -> Iterator[dict]:
        url = f"https://www.youtube.com/playlist?list={quote_plus(playlist_id)}&hl=en"
        try:
            response = self.session.get(url, timeout=20)
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not reach YouTube for playlist {playlist_id}: {exc}") from exc
        if response.status_code != 200:
            raise RuntimeError(
                f"YouTube returned HTTP {response.status_code} for playlist {playlist_id}."
            )

        data = json_after(response.text, "ytInitialData")
        if not isinstance(data, dict):
            raise RuntimeError(
                f"YouTube did not expose playlist {playlist_id}. "
                "It may be private, deleted, or region-locked."
            )

        pages = 0
        contents, token = self._playlist_contents(data)
        while contents is not None:
            for item in self._items_from_contents(contents, "playlist"):
                yield item
            pages += 1
            if not token or (max_pages is not None and pages >= max_pages):
                return
            contents, token = self._next_page(token)

    def _grid_contents(self, data: dict) -> Tuple[Optional[List[dict]], Optional[str]]:
        for key in ("richGridRenderer", "gridRenderer", "itemSectionRenderer"):
            for renderer in iter_keyed_objects(data, key):
                contents = renderer.get("contents")
                if isinstance(contents, list) and contents:
                    return contents, self._continuation_token(contents)
        return None, None

    def _playlist_contents(self, data: dict) -> Tuple[Optional[List[dict]], Optional[str]]:
        for renderer in iter_keyed_objects(data, "playlistVideoListRenderer"):
            contents = renderer.get("contents")
            if isinstance(contents, list) and contents:
                return contents, self._continuation_token(contents)
        return None, None

    @staticmethod
    def _continuation_token(contents: List[dict]) -> Optional[str]:
        for entry in reversed(contents):
            command = entry.get("continuationItemRenderer") or {}
            token = (((command.get("continuationEndpoint") or {})
                      .get("continuationCommand") or {}).get("token"))
            if token:
                return token
        return None

    def _next_page(self, token: str) -> Tuple[Optional[List[dict]], Optional[str]]:
        client = next(
            (entry for entry in INNERTUBE_CLIENTS if entry["name"] == "WEB"),
            INNERTUBE_CLIENTS[-1],
        )
        body = {
            "context": {
                "client": dict(client["context"], hl="en", gl="US", timeZone="UTC",
                               utcOffsetMinutes=0),
            },
            "continuation": token,
        }
        payload = self._innertube_post("browse", body, client)
        if not payload:
            return None, None
        for key in ("onResponseReceivedActions", "onResponseReceivedEndpoints"):
            for action in payload.get(key) or []:
                for inner_key in ("appendContinuationItemsAction", "reloadContinuationItemsCommand"):
                    inner = action.get(inner_key) or {}
                    contents = inner.get("continuationItems")
                    if isinstance(contents, list):
                        return contents, self._continuation_token(contents)
        return None, None

    def _items_from_contents(self, contents: List[dict], tab: str) -> Iterator[dict]:
        for entry in contents:
            for video_id in self._video_ids_in(entry):
                yield {
                    "videoId": video_id,
                    "tab": tab,
                    "isShort": tab == "shorts",
                    "isLive": tab == "streams",
                }

    @staticmethod
    def _video_ids_in(entry: Any) -> List[str]:
        if not isinstance(entry, dict):
            return []
        found: List[str] = []
        for renderer_key in ("videoRenderer", "gridVideoRenderer", "reelItemRenderer",
                             "playlistVideoRenderer", "compactVideoRenderer"):
            for renderer in iter_keyed_objects(entry, renderer_key):
                video_id = renderer.get("videoId")
                if isinstance(video_id, str) and _VIDEO_ID.match(video_id):
                    found.append(video_id)
        for model in iter_keyed_objects(entry, "shortsLockupViewModel"):
            entity_id = str(model.get("entityId") or "")
            match = re.search(r"([A-Za-z0-9_-]{11})$", entity_id)
            if match:
                found.append(match.group(1))
        return unique_preserving_order(iter(found))
