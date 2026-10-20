import requests
import json
import re
import os
import base64
import html
import xml.etree.ElementTree as ET
from http.cookiejar import LoadError, MozillaCookieJar
from typing import Any, Dict, Optional, Generator, Tuple
from urllib.parse import urljoin, urlsplit

class InstagramClient:
    """Handles mobile API communication, robust 1080p DASH/progressive stream extraction, and profile lookups."""

    MOBILE_USER_AGENT = "Instagram 315.0.0.31.109 Android (33/13; 420dpi; 1080x2400; Google/google; Pixel 7; panther; en_US; 539395211)"
    DESKTOP_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

    @staticmethod
    def detect_url(url: str) -> Optional[Tuple[str, str]]:
        """Returns the Instagram target kind and identifier for a supported URL."""
        parsed = urlsplit(url.strip())
        if parsed.scheme not in ("http", "https") or parsed.hostname not in {
            "instagram.com", "www.instagram.com", "m.instagram.com"
        }:
            return None

        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 2 and parts[0].lower() in ("p", "tv", "reel", "reels"):
            return "single", parts[1]
        if len(parts) == 2 and parts[1].lower() == "reels":
            return "reels", parts[0]
        if len(parts) == 1:
            return "account", parts[0]
        return None

    def __init__(
        self,
        sessionid: Optional[str] = None,
        cookies_file: Optional[str] = None,
        allow_network_fallbacks: bool = True,
    ):
        """Creates a client for the Instagram mobile/web APIs.

        ``allow_network_fallbacks`` controls the secondary routes that reach out
        to Instagram without the current session (clean unauthenticated retries
        and HTML timeline scraping).  They exist to work around expired
        sessions, but they cost extra requests, so tests and offline tooling
        can disable them explicitly instead of the client guessing whether it
        is running under a mock.
        """
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.MOBILE_USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "X-IG-Capabilities": "36r/F/8=",
            "X-IG-Connection-Type": "WIFI",
            "X-IG-Bandwidth-Speed-KBPS": "25000",
            "X-IG-Connection-Speed": "25000kbps",
            "X-FB-HTTP-Engine": "Liger",
        })

        self.cookies_file = cookies_file
        self.sessionid = sessionid
        self.allow_network_fallbacks = allow_network_fallbacks
        self.restricted_profiles: Dict[str, str] = {}

        if cookies_file:
            self.load_cookies_from_file(cookies_file)

        if sessionid:
            self.session.cookies.set("sessionid", sessionid, domain=".instagram.com")

        csrf_token = self.session.cookies.get("csrftoken", domain=".instagram.com") or \
                     self.session.cookies.get("csrftoken", domain="instagram.com")
        if csrf_token:
            self.session.headers.update({"X-CSRFToken": csrf_token})

    def has_session(self) -> bool:
        """Returns True if the client was configured with a sessionid or cookie file."""
        if self.sessionid or self.cookies_file:
            return True
        for cookie in self.session.cookies:
            if cookie.name == "sessionid":
                return True
        return False

    def load_cookies_from_file(self, cookies_file_path: str):
        if not os.path.exists(cookies_file_path):
            raise FileNotFoundError(
                f"Cookies file not found: {cookies_file_path}. "
                "Check the path, export a Netscape-format cookies file, "
                "or omit --cookies for public content."
            )
        cj = MozillaCookieJar()
        try:
            cj.load(cookies_file_path, ignore_discard=True, ignore_expires=True)
        except (OSError, LoadError) as exc:
            raise ValueError(
                f"Could not read cookies file '{cookies_file_path}'. "
                "Make sure it is a valid Netscape-format cookies file."
            ) from exc
        loaded_count = 0
        for cookie in cj:
            if "instagram.com" in getattr(cookie, "domain", ""):
                self.session.cookies.set_cookie(cookie)
                loaded_count += 1
        if loaded_count == 0:
            self.session.cookies.update(cj)
        print(f"[+] Successfully loaded cookies from: {cookies_file_path}")

    @staticmethod
    def shortcode_to_id(shortcode: str) -> int:
        """Converts an Instagram shortcode into its numeric media id."""
        alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
        media_id = 0
        for char in shortcode:
            index = alphabet.find(char)
            if index < 0:
                raise ValueError(
                    f"Invalid shortcode {shortcode!r}: character {char!r} is not a "
                    "valid base64 shortcode character."
                )
            media_id = (media_id * 64) + index
        return media_id

    def check_profile_restriction(self, username: str) -> Optional[str]:
        """Checks if a user profile is age-restricted or gated, returning the reason or None."""
        if username.lower() in self.restricted_profiles:
            return self.restricted_profiles[username.lower()]
        headers = {
            "User-Agent": self.DESKTOP_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.instagram.com/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
        }
        url = f"https://www.instagram.com/{username}/"
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200 and "gating" in resp.text:
                g_match = re.search(r'"gating"\s*:\s*\{"title"\s*:\s*"([^"]+)",\s*"description"\s*:\s*"([^"]+)"', resp.text)
                if g_match:
                    reason = f"{g_match.group(1)}: {g_match.group(2)}"
                    self.restricted_profiles[username.lower()] = reason
                    return reason
        except requests.RequestException:
            pass
        return None

    def get_user_id(self, username: str) -> str:
        """Resolves Instagram user ID with multiple web and HTML fallback routes."""
        failures = []

        # Route 1: web_profile_info
        api_headers = {
            "User-Agent": self.DESKTOP_USER_AGENT,
            "X-IG-App-ID": "936619743392459",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://www.instagram.com/{username}/",
        }
        try:
            resp = self.session.get(
                "https://www.instagram.com/api/v1/users/web_profile_info/",
                params={"username": username},
                headers=api_headers,
                timeout=10,
            )
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    failures.append("profile API returned invalid JSON")
                else:
                    user = (data.get("data") or {}).get("user") if isinstance(data, dict) else None
                    if isinstance(user, dict):
                        user_id = user.get("id") or user.get("pk")
                        if user_id:
                            return str(user_id)
                    failures.append("profile API response did not contain a user ID")
            else:
                failures.append(f"profile API returned HTTP {resp.status_code}")
                try:
                    error_data = resp.json()
                except ValueError:
                    error_data = {}
                if isinstance(error_data, dict):
                    message = error_data.get("message") or error_data.get("error_type")
                    if message:
                        failures[-1] += f" ({str(message)[:120]})"
        except requests.RequestException as exc:
            failures.append(f"profile API request failed ({exc})")

        # Helper to extract user ID from HTML
        def _extract_id_from_html(html_text: str) -> Optional[str]:
            patterns = [
                r'"profile_id":"(\d+)"',
                r'"page_id":"profilePage_(\d+)"',
                r'"target_id":"(\d+)"',
                r'"props":\s*\{\s*"id":"(\d+)"',
                r'"props":.*?id":"(\d+)"',
                r'<meta property="instapp:owner_user_id" content="(\d+)"',
                r'instagram://user\?username=[^&"\'\s]+&id=(\d+)',
                r'"user_id":"(\d+)"',
            ]
            for pattern in patterns:
                matches = re.findall(pattern, html_text)
                if matches:
                    return str(matches[0])
            return None

        # Route 2: HTML scrape fallback with desktop document headers
        html_headers = {
            "User-Agent": self.DESKTOP_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.instagram.com/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
        }
        url_html = f"https://www.instagram.com/{username}/"

        try:
            resp_html = self.session.get(url_html, headers=html_headers, timeout=10)
            if resp_html.status_code == 200:
                if "gating" in resp_html.text:
                    g_match = re.search(r'"gating"\s*:\s*\{"title"\s*:\s*"([^"]+)",\s*"description"\s*:\s*"([^"]+)"', resp_html.text)
                    if g_match:
                        self.restricted_profiles[username.lower()] = f"{g_match.group(1)}: {g_match.group(2)}"
                uid = _extract_id_from_html(resp_html.text)
                if uid:
                    return uid

            # If not found (e.g. redirected to homepage by expired session cookies),
            # retry with a clean unauthenticated request.
            if self.allow_network_fallbacks:
                try:
                    clean_resp = requests.get(url_html, headers=html_headers, timeout=10)
                    if clean_resp.status_code == 200:
                        if "gating" in clean_resp.text:
                            g_match = re.search(r'"gating"\s*:\s*\{"title"\s*:\s*"([^"]+)",\s*"description"\s*:\s*"([^"]+)"', clean_resp.text)
                            if g_match:
                                self.restricted_profiles[username.lower()] = f"{g_match.group(1)}: {g_match.group(2)}"
                        uid = _extract_id_from_html(clean_resp.text)
                        if uid:
                            return uid
                except requests.RequestException:
                    pass

            if resp_html.status_code == 200:
                failures.append("profile page did not contain a recognizable user ID")
            else:
                failures.append(f"profile page returned HTTP {resp_html.status_code}")
        except requests.RequestException as exc:
            failures.append(f"profile page request failed ({exc})")

        detail = "; ".join(failures)
        raise RuntimeError(
            f"Failed to fetch profile ID for @{username}: {detail}. "
            "Check that the username is correct and public, or that cookies.txt "
            "contains a current Netscape-format session for an account that can "
            "view the profile."
        )

    def fetch_post_by_shortcode(self, shortcode: str) -> dict:
        """Fetches media info via mobile API, falling back to the post's HTML page."""
        try:
            media_id = self.shortcode_to_id(shortcode)
        except ValueError as exc:
            raise RuntimeError(
                f"'{shortcode}' is not a valid Instagram shortcode, so it cannot be "
                "downloaded. Pass a post URL or a valid shortcode."
            ) from exc
        url = f"https://i.instagram.com/api/v1/media/{media_id}/info/"
        response = self.session.get(url, timeout=15)
        login_required = False
        if response.status_code == 200:
            try:
                return self.parse_media_item(response.json()["items"][0])
            except (KeyError, IndexError, ValueError):
                pass  # Malformed response – fall through to HTML route
        else:
            try:
                error_data = response.json()
            except ValueError:
                error_data = {}
            if isinstance(error_data, dict):
                login_required = (
                    error_data.get("message") == "login_required"
                    or error_data.get("error_type") == "login_required"
                    or "logged out" in str(error_data.get("error_title", "")).lower()
                )

        # Fallback: scrape the reel/post HTML page for embedded media JSON
        print(f"[!] Mobile API returned {response.status_code} for {shortcode}. Trying HTML fallback...")
        try:
            return self._fetch_post_from_html(shortcode)
        except RuntimeError as exc:
            err_msg = str(exc)
            error_title = error_data.get("error_title") if isinstance(error_data, dict) else ""
            is_logged_out = (
                "logged out" in str(error_title).lower()
                or (isinstance(error_data, dict) and error_data.get("logout_reason") is not None)
            )

            # Case 1: Post is age-gated / mature audience
            if "age-restricted" in err_msg.lower() or "mature audience" in err_msg.lower():
                if is_logged_out and (self.cookies_file or self.sessionid):
                    raise RuntimeError(
                        f"Item {shortcode} is age-restricted (requires a verified 19+ account). "
                        "The session provided via cookies/sessionid was rejected as EXPIRED or LOGGED OUT "
                        f"(Instagram reported: \"{error_title or 'You have been logged out'}\"). "
                        "Please log into Instagram in your browser and export fresh cookies to cookies.txt."
                    ) from exc
                if not self.has_session():
                    raise RuntimeError(
                        f"Item {shortcode} is age-restricted (requires a verified 19+ account). "
                        "Instagram strictly blocks unauthenticated public access to this content. "
                        "Please provide an active adult session using --cookies cookies.txt or --sessionid SESSION_ID."
                    ) from exc
                raise

            # Case 2: Session was explicitly rejected as logged out / expired
            if is_logged_out and (self.cookies_file or self.sessionid):
                raise RuntimeError(
                    f"Your Instagram session is expired or logged out "
                    f"(Instagram reported: \"{error_title or 'You have been logged out'}\"). "
                    "Please log into Instagram in your browser and export fresh cookies to cookies.txt."
                ) from exc

            # Case 3: Standard login required
            if login_required:
                raise RuntimeError(
                    f"Instagram requires an authenticated session to fetch {shortcode} "
                    f"(mobile API returned HTTP {response.status_code} login_required, "
                    "and the web page exposed no media). Retry with --cookies cookies.txt "
                    "or --sessionid SESSION_ID using a valid account that can view the post. "
                    "The post may also be private, removed, or unavailable."
                ) from exc
            raise

    def _fetch_post_from_html(self, shortcode: str) -> dict:
        """
        Scrapes the Instagram reel/post page with desktop headers and extracts media info
        from embedded JSON blobs.  Handles several known embedding patterns:
          1. window.__additionalDataLoaded(...)  – historical but sometimes present
          2. <script type="application/json" data-content-type="..."> blocks
          3. Large JSON blobs containing 'video_versions' or 'video_url'
          4. Open-Graph <meta property="og:video"> as a last-resort plain-URL fallback
        """
        headers = {
            "User-Agent": self.DESKTOP_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.instagram.com/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
        }

        def _search_script_blocks(html_source: str) -> Optional[dict]:
            script_blocks = re.findall(
                r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
                html_source, re.DOTALL
            )
            for raw_block in script_blocks:
                raw_block = raw_block.strip()
                if not raw_block or raw_block[0] not in ("{", "["):
                    continue
                try:
                    data = json.loads(raw_block)
                except (json.JSONDecodeError, ValueError):
                    continue
                item = self._dig_for_media_item(data, shortcode)
                if item:
                    return item
            return None

        last_html = ""
        for path_prefix in ("p", "reel"):
            page_url = f"https://www.instagram.com/{path_prefix}/{shortcode}/"
            try:
                resp = self.session.get(page_url, headers=headers, timeout=15)
            except Exception as exc:
                raise RuntimeError(f"HTML fetch failed for {shortcode}: {exc}")

            html = resp.text if resp.status_code == 200 else ""
            if html:
                last_html = html
            item = _search_script_blocks(html) if html else None

            # If session headers/cookies prevented SSR, retry with clean unauthenticated request
            if not item and self.allow_network_fallbacks:
                try:
                    clean_resp = requests.get(page_url, headers=headers, timeout=15)
                    if clean_resp.status_code == 200:
                        html = clean_resp.text
                        last_html = html
                        item = _search_script_blocks(html)
                except requests.RequestException:
                    pass

            if item:
                return self.parse_media_item(item)

            if not html:
                continue

            # --- Pattern 2: window.__additionalDataLoaded('...',{...}) ------------
            adl_match = re.search(
                r"window\.__additionalDataLoaded\s*\(\s*['\"][^'\"]*['\"],\s*(\{.*?\})\s*\)\s*;",
                html, re.DOTALL
            )
            if adl_match:
                try:
                    data = json.loads(adl_match.group(1))
                    item = self._dig_for_media_item(data, shortcode)
                    if item:
                        return self.parse_media_item(item)
                except (json.JSONDecodeError, ValueError):
                    pass

            # --- Pattern 3: Any JSON blob containing 'video_versions' -----------
            for candidate in re.findall(r'\{[^{}]{200,}\}', html):
                if '"video_versions"' not in candidate and '"video_url"' not in candidate:
                    continue
                try:
                    data = json.loads(candidate)
                    item = self._dig_for_media_item(data, shortcode)
                    if item:
                        return self.parse_media_item(item)
                    # Maybe the blob IS the item itself
                    if "video_versions" in data or "video_url" in data:
                        data.setdefault("code", shortcode)
                        return self.parse_media_item(data)
                except (json.JSONDecodeError, ValueError):
                    continue

            # --- Pattern 4: og:video meta tag – plain URL, no quality info ------
            og_video = re.search(r'<meta[^>]+property=["\']og:video["\'][^>]+content=["\']([^"\']+)["\']', html)
            if og_video:
                video_url = og_video.group(1)
                og_audio = re.search(r'<meta[^>]+property=["\']og:audio["\'][^>]+content=["\']([^"\']+)["\']', html)
                audio_url = og_audio.group(1) if og_audio else None
                og_image = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
                display_url = og_image.group(1) if og_image else None
                print(f"[!] HTML fallback: only og:video URL recovered for {shortcode} (no DASH, best-effort 720p).")
                return {
                    "shortcode": shortcode,
                    "media_type": "Reel",
                    "timestamp": "",
                    "video_url": video_url,
                    "audio_url": audio_url,
                    "quality": "og:video (best-effort)",
                    "display_url": display_url,
                    "children": [],
                }

        if last_html:
            age_match = re.search(r'"restricted_age"\s*:\s*(\d+)', last_html)
            if age_match:
                min_age = age_match.group(1)
                raise RuntimeError(
                    f"Item {shortcode} is age-restricted ({min_age}+ / Mature Audience). "
                    "Instagram prohibits unauthenticated access to this media."
                )
            if '"failure_reason":"MA"' in last_html:
                raise RuntimeError(
                    f"Item {shortcode} is restricted to Mature Audiences (18+/19+). "
                    "Instagram prohibits unauthenticated access to this media."
                )

        raise RuntimeError(
            f"Failed to fetch media for {shortcode}: Instagram's mobile API and web page "
            "returned no media. The post may be private, removed, or unavailable to this "
            "session. If it should be accessible, retry with --cookies cookies.txt or "
            "--sessionid SESSION_ID using a valid account."
        )

    def _dig_for_media_item(self, data: Any, shortcode: str) -> Optional[dict]:
        """
        Recursively walks a parsed JSON structure looking for an Instagram media item dict
        that matches the given shortcode or contains 'video_versions'/'image_versions2'.
        Returns the raw item dict or None.
        """
        if not isinstance(data, dict):
            if isinstance(data, list):
                for element in data:
                    found = self._dig_for_media_item(element, shortcode)
                    if found:
                        return found
            return None

        # Direct hit: dict has the media item fields we need
        if ("video_versions" in data or "image_versions2" in data or "carousel_media" in data or "video_dash_manifest" in data or "dash_manifest" in data) and \
           (data.get("code") == shortcode or not data.get("code")):
            if not data.get("code"):
                data = dict(data)
                data["code"] = shortcode
            return data

        # Recurse into all dict values
        for value in data.values():
            found = self._dig_for_media_item(value, shortcode)
            if found:
                return found

        return None

    def _fetch_profile_timeline_from_html(self, username: str) -> Generator[dict, None, None]:
        """Extracts initial posts grid from profile page SSR data."""
        headers = {
            "User-Agent": self.DESKTOP_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.instagram.com/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
        }
        desktop_session = requests.Session()
        for cookie in self.session.cookies:
            if "instagram.com" in getattr(cookie, "domain", ""):
                desktop_session.cookies.set_cookie(cookie)

        seen_codes = set()
        for path_prefix in (f"{username}/", f"{username}/reels/"):
            url = f"https://www.instagram.com/{path_prefix}"
            resp = None
            try:
                if self.has_session():
                    resp = desktop_session.get(url, headers=headers, timeout=15)
                if not resp or resp.status_code != 200 or len(resp.text) < 1000:
                    resp = requests.get(url, headers=headers, timeout=15)
            except Exception:
                try:
                    resp = requests.get(url, headers=headers, timeout=15)
                except Exception:
                    continue

            if not resp or resp.status_code != 200:
                continue

            if "gating" in resp.text:
                g_match = re.search(r'"gating"\s*:\s*\{"title"\s*:\s*"([^"]+)",\s*"description"\s*:\s*"([^"]+)"', resp.text)
                if g_match:
                    self.restricted_profiles[username.lower()] = f"{g_match.group(1)}: {g_match.group(2)}"

            scripts = re.findall(r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>', resp.text, re.DOTALL)
            for s in scripts:
                if "polaris_ordered_timeline_connection" in s or '"code":' in s:
                    try:
                        data = json.loads(s)
                        def find_timeline(obj):
                            if isinstance(obj, dict):
                                if "polaris_ordered_timeline_connection" in obj:
                                    return obj["polaris_ordered_timeline_connection"]
                                for v in obj.values():
                                    res = find_timeline(v)
                                    if res:
                                        return res
                            elif isinstance(obj, list):
                                for item in obj:
                                    res = find_timeline(item)
                                    if res:
                                        return res
                            return None
                        timeline = find_timeline(data)
                        if timeline:
                            edges = timeline.get("edges", [])
                            for edge in edges:
                                node = edge.get("node", {})
                                code = node.get("code")
                                if code and code not in seen_codes:
                                    seen_codes.add(code)
                                    is_carousel = node.get("__typename") == "XIGPolarisCarouselMedia"
                                    is_video = node.get("__typename") == "XIGPolarisVideoMedia" or bool(node.get("is_video"))
                                    m_type = 8 if is_carousel else (2 if is_video else 1)
                                    yield {
                                        "code": code,
                                        "pk": node.get("pk") or node.get("id"),
                                        "media_type": m_type,
                                        "is_video": is_video,
                                        "_needs_full_fetch": True,
                                    }
                    except Exception:
                        pass

    def fetch_user_raw_posts_paginated(
        self,
        user_id: str,
        max_pages: Optional[int] = None,
        username: Optional[str] = None,
    ) -> Generator[dict, None, None]:
        """Yields raw feed items instantly for lightning-fast resumption checks."""
        max_id = None
        pages_fetched = 0
        while True:
            if max_pages is not None:
                if max_pages <= 0:
                    return
                if pages_fetched >= max_pages:
                    break
            url = f"https://i.instagram.com/api/v1/feed/user/{user_id}/"
            params = {"max_id": max_id} if max_id else {}
            response = self.session.get(url, params=params, timeout=15)
            if response.status_code != 200:
                if pages_fetched == 0 and username and self.allow_network_fallbacks:
                    timeline_items = list(self._fetch_profile_timeline_from_html(username))
                    if timeline_items:
                        for item in timeline_items:
                            yield item
                        return

                if pages_fetched == 0:
                    err_hint = ""
                    try:
                        err_json = response.json()
                        msg = err_json.get("message") or err_json.get("error_title")
                        if msg:
                            err_hint = f" ({msg})"
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"Failed to fetch posts feed for user {user_id}: "
                        f"Instagram returned HTTP {response.status_code}{err_hint}. "
                        "Accessing an account's posts grid requires an active, authenticated Instagram session. "
                        "The session in cookies.txt appears to be expired or logged out. "
                        "Please export a fresh cookies.txt file from your browser while logged in."
                    )
                break
            result = response.json()
            items = result.get("items", [])
            if not items:
                break
            for item in items:
                yield item
            if not result.get("more_available", False):
                break
            max_id = result.get("next_max_id")
            pages_fetched += 1

    def fetch_user_raw_reels_paginated(
        self,
        user_id: str,
        max_pages: Optional[int] = None,
        username: Optional[str] = None,
    ) -> Generator[dict, None, None]:
        """Yields raw media from the profile's dedicated Reels tab."""
        max_id = None
        pages_fetched = 0
        total_items_yielded = 0
        api_error_title = ""
        api_logout_reason = None
        api_status_code = 200

        while max_pages is None or pages_fetched < max_pages:
            data = {"target_user_id": user_id, "page_size": 12}
            if max_id:
                data["max_id"] = max_id
            response = self.session.post(
                "https://i.instagram.com/api/v1/clips/user/",
                data=data,
                timeout=15,
            )
            api_status_code = response.status_code
            if response.status_code in (401, 403):
                try:
                    err_json = response.json()
                    api_error_title = err_json.get("error_title", "")
                    api_logout_reason = err_json.get("logout_reason")
                except Exception:
                    pass

                # Attempt an unauthenticated fallback for expired sessions.
                if self.allow_network_fallbacks:
                    clean_headers = {
                        "User-Agent": self.MOBILE_USER_AGENT,
                        "Accept-Language": "en-US,en;q=0.9",
                        "X-IG-Capabilities": "36r/F/8=",
                        "X-IG-Connection-Type": "WIFI",
                        "X-FB-HTTP-Engine": "Liger",
                    }
                    try:
                        clean_resp = requests.post(
                            "https://i.instagram.com/api/v1/clips/user/",
                            data=data,
                            headers=clean_headers,
                            timeout=15,
                        )
                        if clean_resp.status_code == 200:
                            response = clean_resp
                    except Exception:
                        pass

            if response.status_code != 200:
                break

            try:
                result = response.json()
            except Exception:
                break
            items = result.get("items", [])
            if not items:
                break
            for item in items:
                media = item.get("media") or item.get("clip")
                total_items_yielded += 1
                yield media if isinstance(media, dict) else item

            paging_info = result.get("paging_info") or {}
            more_available = result.get("more_available") or paging_info.get("more_available")
            next_max_id = result.get("next_max_id") or paging_info.get("max_id") or paging_info.get("end_cursor")
            if not more_available or not next_max_id or next_max_id == max_id:
                break
            max_id = next_max_id
            pages_fetched += 1

        # If zero items yielded, try fallbacks before checking age restriction
        if total_items_yielded == 0:
            # Fallback 1: Posts feed for Reels (media_type == 2 or video_versions)
            try:
                feed_items = self.fetch_user_raw_posts_paginated(user_id, max_pages=max_pages, username=username)
                found_any = False
                for p in feed_items:
                    if p.get("media_type") == 2 or "video_versions" in p or p.get("is_video"):
                        found_any = True
                        total_items_yielded += 1
                        yield p
                if found_any:
                    return
            except Exception:
                pass

            # Fallback 2: Check profile timeline from HTML
            if username and self.allow_network_fallbacks:
                try:
                    html_items = list(self._fetch_profile_timeline_from_html(username))
                    found_any = False
                    for h in html_items:
                        if h.get("media_type") == 2 or h.get("is_video"):
                            found_any = True
                            total_items_yielded += 1
                            yield h
                    if found_any:
                        return
                except Exception:
                    pass

            # If still 0 items, check if profile is age-restricted
            restriction = None
            if username:
                restriction = self.restricted_profiles.get(username.lower()) or self.check_profile_restriction(username)

            if restriction:
                is_logged_out = (
                    "logged out" in str(api_error_title).lower()
                    or api_logout_reason is not None
                )
                if is_logged_out and (self.cookies_file or self.sessionid):
                    reported_err = api_error_title or 'You have been logged out'
                    raise RuntimeError(
                        f"Cannot download Reels for @{username}: Account is age-restricted ({restriction}). "
                        "The session provided via cookies/sessionid was rejected as EXPIRED or LOGGED OUT "
                        f"(Instagram reported: '{reported_err}'). "
                        "Please log into Instagram in your browser and export fresh cookies to cookies.txt."
                    )
                if not self.has_session():
                    raise RuntimeError(
                        f"Cannot download Reels for @{username}: Account is age-restricted ({restriction}). "
                        "Instagram strictly blocks unauthenticated public access to this profile. "
                        "Please provide an active adult session using --cookies cookies.txt or --sessionid SESSION_ID."
                    )
                if self.has_session():
                    raise RuntimeError(
                        f"Cannot download Reels for @{username}: Account is age-restricted ({restriction}). "
                        "Your session was either rejected as expired/logged out or belongs to an account under the required age (19+). "
                        "Please ensure you are logged into a verified adult account and export fresh cookies."
                    )

            if api_status_code not in (200,):
                raise RuntimeError(
                    f"Failed to fetch Reels for user {user_id}: "
                    f"Instagram returned HTTP {api_status_code}."
                )

    @staticmethod
    def _parse_dash_manifest_to_root(manifest_input: Any) -> Optional[ET.Element]:
        if not manifest_input:
            return None
        candidates = []
        if isinstance(manifest_input, bytes):
            try:
                candidates.append(manifest_input.decode("utf-8"))
            except Exception:
                pass
        elif isinstance(manifest_input, str):
            s = manifest_input.strip()
            if s:
                candidates.append(s)
                if not s.startswith("<") and not s.startswith("&lt;"):
                    try:
                        decoded = base64.b64decode(s).decode("utf-8").strip()
                        if decoded:
                            candidates.append(decoded)
                    except Exception:
                        pass
                if "&lt;" in s:
                    candidates.append(html.unescape(s))

        for cand in candidates:
            # 1. Direct parse (keeps well-formed &amp; in URLs intact)
            try:
                return ET.fromstring(cand)
            except (ET.ParseError, ValueError):
                pass
            # 2. Fix bare ampersands in unescaped XML
            try:
                sanitized = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", cand)
                return ET.fromstring(sanitized)
            except (ET.ParseError, ValueError):
                pass
        return None

    def _extract_dash_streams(self, manifest_input: Any) -> Tuple[Optional[dict], Optional[dict]]:
        """Parses Base64 or XML MPEG-DASH manifests and extracts highest 1080p video & HQ audio streams."""
        root = self._parse_dash_manifest_to_root(manifest_input)
        if root is None:
            return None, None
        try:
            video_reps = []
            audio_reps = []
            parent_by_element = {
                child: parent
                for parent in root.iter()
                for child in parent
            }

            for elem in root.iter():
                tag = elem.tag.split("}")[-1]
                if tag == "Representation":
                    ancestors = []
                    parent = parent_by_element.get(elem)
                    while parent is not None:
                        ancestors.append(parent)
                        parent = parent_by_element.get(parent)
                    ancestors.reverse()

                    def inherited_attribute(name: str) -> str:
                        for source in (elem, *reversed(ancestors)):
                            value = source.attrib.get(name)
                            if value:
                                return value
                        return ""

                    # Direct tag-level check for audio vs video (not inherited from root)
                    elem_mime = (elem.attrib.get("mimeType") or "").lower()
                    elem_codecs = (elem.attrib.get("codecs") or "").lower()
                    elem_content_type = (elem.attrib.get("contentType") or "").lower()

                    adapt_set = ancestors[-1] if ancestors and ancestors[-1].tag.split("}")[-1] == "AdaptationSet" else None
                    adapt_mime = (adapt_set.attrib.get("mimeType") or "").lower() if adapt_set is not None else ""
                    adapt_codecs = (adapt_set.attrib.get("codecs") or "").lower() if adapt_set is not None else ""
                    adapt_content_type = (adapt_set.attrib.get("contentType") or "").lower() if adapt_set is not None else ""

                    mime = elem_mime or adapt_mime or inherited_attribute("mimeType").lower()
                    codecs = elem_codecs or adapt_codecs or inherited_attribute("codecs").lower()
                    content_type = elem_content_type or adapt_content_type or inherited_attribute("contentType").lower()

                    try:
                        bw = int(inherited_attribute("bandwidth") or 0)
                    except ValueError:
                        bw = 0
                    try:
                        w = int(elem.attrib.get("width") or inherited_attribute("width") or 0)
                    except ValueError:
                        w = 0
                    try:
                        h = int(elem.attrib.get("height") or inherited_attribute("height") or 0)
                    except ValueError:
                        h = 0
                    quality_label = inherited_attribute("FBQualityLabel")

                    url = ""
                    for source in (*ancestors, elem):
                        for child in source:
                            if child.tag.split("}")[-1] == "BaseURL" and child.text:
                                url = urljoin(url, child.text.strip())
                                break
                    if not url:
                        continue

                    # Audio indicators
                    has_audio_child = any(
                        child.tag.split("}")[-1] in ("AudioChannelConfiguration", "AudioChannel")
                        for source in (elem, adapt_set) if source is not None
                        for child in source
                    )
                    has_audio_sampling = bool(elem.attrib.get("audioSamplingRate") or (adapt_set is not None and adapt_set.attrib.get("audioSamplingRate")))
                    has_audio_codec = any(c in codecs for c in ("mp4a", "aac", "opus", "vorbis", "ac-3", "ec-3", "flac", "alac", "pcm", "wav"))
                    has_audio_id = "audio" in (elem.attrib.get("id") or "").lower()
                    audio_mime_or_type = content_type == "audio" or "audio" in mime or elem_mime.startswith("audio/") or adapt_mime.startswith("audio/")

                    # Video indicators
                    has_video_codec = any(c in codecs for c in ("avc", "hvc", "hev", "vp9", "vp09", "av01", "mp4v", "264", "265"))
                    has_video_dims = (w > 0 and h > 0)
                    has_video_fps = bool(inherited_attribute("frameRate") or inherited_attribute("maxFrameRate"))
                    has_video_label = bool(quality_label)
                    video_mime_or_type = content_type == "video" or elem_mime.startswith("video/") or adapt_mime.startswith("video/")

                    is_audio = (
                        audio_mime_or_type
                        or has_audio_child
                        or has_audio_sampling
                        or (has_audio_codec and not has_video_codec)
                        or (has_audio_id and not has_video_dims and not has_video_codec)
                    ) and not (has_video_dims or has_video_codec or has_video_fps or has_video_label)

                    is_video = (
                        video_mime_or_type
                        or has_video_dims
                        or has_video_codec
                        or has_video_fps
                        or has_video_label
                    ) and not is_audio

                    if is_audio:
                        is_xheaac = any(x in codecs for x in ("40.42", "xheaac", "usac"))
                        audio_reps.append({
                            "url": url,
                            "bandwidth": bw,
                            "codecs": codecs,
                            "is_xheaac": is_xheaac,
                        })
                    elif is_video:
                        is_1080 = ("1080" in quality_label) or min(w, h) >= 1080
                        video_reps.append({
                            "url": url,
                            "width": w,
                            "height": h,
                            "bandwidth": bw,
                            "quality_label": quality_label,
                            "is_1080p": is_1080,
                            "pixels": w * h,
                        })

            best_video = None
            if video_reps:
                video_reps.sort(key=lambda x: (1 if x["is_1080p"] else 0, x["pixels"], x["bandwidth"]), reverse=True)
                best_video = video_reps[0]

            best_audio = None
            if audio_reps:
                # Prefer standard playable AAC (AAC-LC, HE-AAC) over proprietary xHE-AAC
                audio_reps.sort(key=lambda x: (0 if x.get("is_xheaac") else 1, x["bandwidth"]), reverse=True)
                best_audio = audio_reps[0]

            return best_video, best_audio
        except Exception:
            return None, None

    def _extract_audio_info(self, item: dict) -> Tuple[Optional[str], Optional[int]]:
        """
        Extracts the best audio stream URL and optional start offset (in milliseconds)
        from an Instagram media dictionary. Searches DASH manifests, clips metadata
        (original sound and music), music metadata, audio versions, and direct fields.
        """
        if not isinstance(item, dict):
            return None, None

        clips = item.get("clips_metadata")
        if isinstance(clips, dict):
            audio_type = str(clips.get("audio_type", "")).lower()

            orig_info = clips.get("original_sound_info")
            orig_url = None
            orig_start = 0
            if isinstance(orig_info, dict):
                if orig_info.get("dash_manifest"):
                    _, o_dash_a = self._extract_dash_streams(orig_info.get("dash_manifest"))
                    if o_dash_a and not o_dash_a.get("is_xheaac") and o_dash_a.get("url"):
                        orig_url = o_dash_a["url"]
                if not orig_url:
                    orig_url = (
                        orig_info.get("fast_start_progressive_download_url")
                        or orig_info.get("progressive_download_url")
                        or orig_info.get("audio_src")
                    )
                orig_start = int(orig_info.get("audio_asset_start_time_in_ms") or 0)

            music_info = clips.get("music_info")
            music_url = None
            music_start = 0
            if isinstance(music_info, dict):
                asset = music_info.get("music_asset_info") or music_info
                if isinstance(asset, dict):
                    if asset.get("dash_manifest"):
                        _, m_dash_a = self._extract_dash_streams(asset.get("dash_manifest"))
                        if m_dash_a and not m_dash_a.get("is_xheaac") and m_dash_a.get("url"):
                            music_url = m_dash_a["url"]
                    if not music_url:
                        music_url = (
                            asset.get("fast_start_progressive_download_url")
                            or asset.get("progressive_download_url")
                            or asset.get("reactive_audio_download_url")
                        )
                    music_start = int(asset.get("audio_asset_start_time_in_ms") or 0)

            if "music" in audio_type:
                if music_url:
                    return music_url, music_start
                if orig_url:
                    return orig_url, orig_start
            else:
                if orig_url:
                    return orig_url, orig_start
                if music_url:
                    return music_url, music_start

        # 2. Check all DASH manifest candidates (prefer standard AAC)
        manifest_candidates = [
            item.get("audio_dash_manifest"),
            item.get("dash_manifest"),
            item.get("video_dash_manifest"),
        ]
        for m_cand in manifest_candidates:
            if m_cand:
                _, dash_a = self._extract_dash_streams(m_cand)
                if dash_a and dash_a.get("url") and not dash_a.get("is_xheaac"):
                    return dash_a["url"], 0

        # 3. Check music_metadata
        music_meta = item.get("music_metadata")
        if isinstance(music_meta, dict):
            m_info = music_meta.get("music_info") or music_meta
            if isinstance(m_info, dict):
                asset = m_info.get("music_asset_info") or m_info
                if isinstance(asset, dict):
                    m_url = (
                        asset.get("fast_start_progressive_download_url")
                        or asset.get("progressive_download_url")
                    )
                    if m_url:
                        m_start = int(asset.get("audio_asset_start_time_in_ms") or 0)
                        return m_url, m_start

        # 4. Check audio_versions
        audio_versions = item.get("audio_versions")
        if isinstance(audio_versions, list):
            for v in audio_versions:
                if isinstance(v, dict):
                    v_url = (
                        v.get("fast_start_progressive_download_url")
                        or v.get("progressive_download_url")
                        or v.get("url")
                    )
                    if v_url:
                        v_start = int(v.get("audio_asset_start_time_in_ms") or 0)
                        return v_url, v_start

        # 5. Check direct audio fields on item
        for key in ("audio_url", "audio_src"):
            direct_val = item.get(key)
            if isinstance(direct_val, str) and direct_val.startswith("http"):
                return direct_val, 0

        audio_obj = item.get("audio")
        if isinstance(audio_obj, dict):
            a_url = audio_obj.get("audio_src") or audio_obj.get("progressive_download_url") or audio_obj.get("url")
            if isinstance(a_url, str) and a_url.startswith("http"):
                return a_url, 0

        # 6. Deep search in nested structures
        found = self._dig_for_audio_url(item)
        if found:
            return found

        return None, None

    def _dig_for_audio_url(self, data: Any) -> Optional[Tuple[str, int]]:
        if isinstance(data, dict):
            for key in ("fast_start_progressive_download_url", "progressive_download_url", "audio_src"):
                val = data.get(key)
                if isinstance(val, str) and val.startswith("http"):
                    offset = int(data.get("audio_asset_start_time_in_ms") or 0)
                    return val, offset
            for k, val in data.items():
                if k in ("image_versions2", "user", "owner"):
                    continue
                res = self._dig_for_audio_url(val)
                if res:
                    return res
        elif isinstance(data, list):
            for elem in data:
                res = self._dig_for_audio_url(elem)
                if res:
                    return res
        return None

    def _select_best_video(self, item: dict) -> Tuple[Optional[str], Optional[str], str]:
        """Returns (video_url, audio_url, quality_description) guaranteeing Full HD 1080p when available."""
        video_versions = item.get("video_versions", [])
        best_prog = None
        if video_versions:
            sorted_v = sorted(video_versions, key=lambda x: (x.get("width", 0) * x.get("height", 0), x.get("bandwidth", 0)), reverse=True)
            best_prog = sorted_v[0]

        # Inspect all potential DASH manifests to find the highest-resolution video and highest-bandwidth audio
        dash_v = None
        dash_a = None
        for m_key in ("video_dash_manifest", "dash_manifest", "audio_dash_manifest"):
            m_val = item.get(m_key)
            if m_val:
                v_cand, a_cand = self._extract_dash_streams(m_val)
                if v_cand:
                    if not dash_v or v_cand.get("pixels", 0) > dash_v.get("pixels", 0):
                        dash_v = v_cand
                if a_cand:
                    if not dash_a or a_cand.get("bandwidth", 0) > dash_a.get("bandwidth", 0):
                        dash_a = a_cand

        # Resolve audio URL across all available metadata:
        # 1. Canonical synchronized audio: The progressive stream (best_prog) contains the finalized,
        #    frame-accurate master audio track of the Reel (matching dialogue, voiceovers, sound effects,
        #    and music cuts exactly as played on Instagram).
        # 2. Secondary choice: Standalone DASH audio stream (standard AAC)
        # 3. Fallback: clips_metadata / music_info (when progressive stream is unavailable)
        audio_url = None
        if item.get("has_audio") is False:
            audio_url = None
        elif best_prog:
            audio_url = best_prog["url"]
        elif dash_a and not dash_a.get("is_xheaac"):
            audio_url = dash_a["url"]
        else:
            extracted_audio_url, _ = self._extract_audio_info(item)
            if extracted_audio_url:
                audio_url = extracted_audio_url
            elif dash_a:
                audio_url = dash_a["url"]

        dash_pixels = (dash_v.get("width", 0) * dash_v.get("height", 0)) if dash_v else 0
        prog_pixels = (best_prog.get("width", 0) * best_prog.get("height", 0)) if best_prog else 0

        dash_is_1080 = bool(
            dash_v and (
                dash_v.get("is_1080p")
                or "1080" in str(dash_v.get("quality_label", ""))
                or min(dash_v.get("width", 0), dash_v.get("height", 0)) >= 1080
                or dash_pixels >= 1166400
            )
        )
        prog_is_1080 = bool(
            best_prog and (
                "1080" in str(best_prog.get("quality_label", ""))
                or min(best_prog.get("width", 0), best_prog.get("height", 0)) >= 1080
                or prog_pixels >= 1166400
            )
        )

        # Decide whether DASH or Progressive provides the superior video stream.
        # NEVER downgrade from 1080p to a lower resolution.
        use_dash_video = False
        if dash_v and not best_prog:
            use_dash_video = True
        elif dash_v and best_prog:
            if dash_is_1080 and not prog_is_1080:
                use_dash_video = True
            elif not dash_is_1080 and prog_is_1080:
                use_dash_video = False
            elif dash_pixels > prog_pixels:
                use_dash_video = True
            elif prog_pixels > dash_pixels:
                use_dash_video = False
            else:
                # Same resolution: prefer DASH pair if DASH audio is present, else progressive
                if dash_a:
                    use_dash_video = True
                else:
                    use_dash_video = False

        if use_dash_video and dash_v:
            label = dash_v.get("quality_label") or f"{dash_v.get('width')}x{dash_v.get('height')}"
            quality = "1080p DASH HQ" if dash_is_1080 else "DASH HQ"

            # If no separate audio track was discovered, borrow audio track from progressive stream if available
            if not audio_url and best_prog and item.get("has_audio") is not False:
                audio_url = best_prog["url"]

            return dash_v["url"], audio_url, f"{label} ({quality})"

        if best_prog:
            label = f"{best_prog.get('width')}x{best_prog.get('height')}"
            quality = f"{label} (1080p Progressive)" if prog_is_1080 else label
            return best_prog["url"], audio_url, quality

        if dash_v:
            label = dash_v.get("quality_label") or f"{dash_v.get('width')}x{dash_v.get('height')}"
            quality = "1080p DASH HQ" if dash_is_1080 else "DASH HQ"
            return dash_v["url"], audio_url, f"{label} ({quality})"

        return None, None, "Unknown"

    def parse_media_item(self, item: dict) -> dict:
        """Normalizes a raw Instagram media payload into Qivaryn's media dict.

        Returns a dictionary with the ``shortcode``, ``media_type``,
        ``timestamp``, ``video_url``, ``audio_url``, ``audio_start_ms``,
        ``quality``, ``display_url`` and ``children`` keys.
        """
        shortcode = item.get("code")
        media_type_code = item.get("media_type")
        timestamp = str(item.get("taken_at", ""))
        media_type = "Post"
        video_url = None
        audio_url = None
        audio_start_ms = 0
        quality = "Original"
        display_url = None
        children_media = []

        if "image_versions2" in item:
            cands = item["image_versions2"].get("candidates", [])
            if cands:
                sorted_cands = sorted(cands, key=lambda x: x.get("width", 0) * x.get("height", 0), reverse=True)
                display_url = sorted_cands[0].get("url")

        if media_type_code == 2 or "video_versions" in item:
            media_type = "Reel"
            video_url, audio_url, quality = self._select_best_video(item)
            prog_urls = [v.get("url") for v in item.get("video_versions", [])]
            if audio_url and audio_url in prog_urls:
                audio_start_ms = 0
            else:
                _, audio_start_ms = self._extract_audio_info(item)

        elif media_type_code == 8 and "carousel_media" in item:
            media_type = "Carousel"
            parent_audio_url, parent_audio_start = self._extract_audio_info(item)
            for child in item["carousel_media"]:
                c_vid = None
                c_audio = None
                c_audio_start = 0
                c_img = None
                c_qual = "Original"
                if "image_versions2" in child:
                    c_cands = child["image_versions2"].get("candidates", [])
                    if c_cands:
                        sorted_c = sorted(c_cands, key=lambda x: x.get("width", 0) * x.get("height", 0), reverse=True)
                        c_img = sorted_c[0].get("url")
                if child.get("media_type") == 2 or "video_versions" in child:
                    c_vid, c_audio, c_qual = self._select_best_video(child)
                    c_prog_urls = [v.get("url") for v in child.get("video_versions", [])]
                    if c_audio and c_audio in c_prog_urls:
                        c_audio_start = 0
                    elif not c_audio and parent_audio_url:
                        c_audio = parent_audio_url
                        c_audio_start = parent_audio_start
                    else:
                        _, c_audio_start = self._extract_audio_info(child)
                children_media.append({
                    "display_url": c_img,
                    "video_url": c_vid,
                    "audio_url": c_audio,
                    "audio_start_ms": c_audio_start,
                    "quality": c_qual,
                    "is_video": c_vid is not None
                })

        return {
            "shortcode": shortcode,
            "media_type": media_type,
            "timestamp": timestamp,
            "video_url": video_url,
            "audio_url": audio_url,
            "audio_start_ms": audio_start_ms,
            "quality": quality,
            "display_url": display_url,
            "children": children_media,
        }

    # Backwards-compatible alias for the previous private method name.
    _parse_mobile_media_item = parse_media_item

    @staticmethod
    def extract_shortcode_from_url(url_or_shortcode: str) -> str:
        value = str(url_or_shortcode).strip()
        if "/" not in value:
            return value
        match = re.search(r"/(?:p|tv|reel|reels)/([A-Za-z0-9_-]+)", value)
        if match:
            return match.group(1)

        tail = value.split("?")[0].rstrip("/")
        tail = tail.split("#")[0].rstrip("/")
        last_part = tail.rsplit("/", 1)[-1] if tail else ""
        if re.fullmatch(r"[A-Za-z0-9_-]+", last_part):
            return last_part
        return value