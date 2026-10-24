"""Platform-agnostic download engine.

:class:`QivarynEngine` owns everything that is identical across services:
routing, media filtering, batch limits, resume/early-exit behaviour, progress
reporting and state persistence.  Service-specific work lives in the client
classes under :mod:`qivaryn.platforms`.
"""

import os
import re
import shutil
import subprocess
from typing import Callable, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

import requests
from tqdm import tqdm

from .client import InstagramClient
from .database import StateDatabase
from .files import (
    MEDIA_TYPE_BY_KIND,
    file_is_present,
    sanitize_folder,
    sanitize_title,
)

#: Media types that are always stored as still images.
IMAGE_MEDIA_TYPES = {"post", "photo", "image"}

#: Default root folder; every platform gets a folder of its own below it.
DEFAULT_OUTPUT_DIR = os.path.join("Downloads", "Qivaryn")

#: ``Content-Range: bytes 0-1023/12345`` -> total size.
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")


class QivarynEngine:
    """Downloads media from any registered platform."""

    #: Account currently being downloaded; used when an item has no owner.
    _current_account = ""

    #: Cached listing of the files below the output directory.
    _output_scan: Optional[List[Tuple[str, str]]] = None

    def __init__(
        self,
        platform: str = "instagram",
        output_dir: str = DEFAULT_OUTPUT_DIR,
        db_path: str = "qivaryn_state.db",
        sessionid: Optional[str] = None,
        cookies_file: Optional[str] = None,
        consecutive_threshold: int = 0,
        allow_network_fallbacks: bool = True,
        quality: str = "best",
        quality_confirmer: Optional[Callable[[dict], bool]] = None,
    ):
        self.platform = (platform or "instagram").lower()
        self.quality = quality or "best"
        self.quality_confirmer = quality_confirmer
        self.output_dir = output_dir
        self.client = self._build_client(
            sessionid=sessionid,
            cookies_file=cookies_file,
            allow_network_fallbacks=allow_network_fallbacks,
        )
        self.db = StateDatabase(db_path=db_path)
        try:
            self.consecutive_threshold = int(consecutive_threshold)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"consecutive_threshold must be an integer, got {consecutive_threshold!r}."
            ) from exc
        #: Account currently being downloaded; used when an item has no owner.
        self._current_account = ""
        #: Cached listing of the files below the output directory.
        self._output_scan: Optional[List[Tuple[str, str]]] = None
        os.makedirs(self.output_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _client_class(platform: str):
        """Resolves the client class, keeping the Instagram import patchable."""
        if platform == "instagram":
            return InstagramClient
        from .platforms import get_client_class

        return get_client_class(platform)

    def _build_client(self, **options):
        client_class = self._client_class(self.platform)
        if self.platform == "youtube":
            options["quality"] = self.quality
        return client_class(**options)

    @staticmethod
    def detect_url(url: str) -> Optional[Tuple[str, str]]:
        """Classifies Instagram URLs (kept for backwards compatibility)."""
        return InstagramClient.detect_url(url)

    # ------------------------------------------------------------------
    # File naming and saved-state checks
    # ------------------------------------------------------------------
    def _file_stem(self, item_id: str, title: str = "") -> str:
        """Returns the filename stem used for ``item_id`` on this platform.

        Instagram keeps its historical ``<shortcode>`` layout while the other
        platforms are prefixed so two services can never overwrite each other.
        YouTube appends the original video title so downloaded files are
        recognisable without opening them.
        """
        stem = str(item_id)
        if self.platform != "instagram":
            stem = f"{self.platform}_{stem}"
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)
        if self.platform == "youtube":
            fragment = sanitize_title(title)
            if fragment:
                stem = f"{stem}_{fragment}"
        return stem

    # ------------------------------------------------------------------
    # Folder layout
    # ------------------------------------------------------------------
    def _platform_folder(self) -> str:
        """Returns the folder name used for the current platform."""
        return getattr(self.client, "display_name", "") or self.platform.capitalize()

    def _account_folder(self, item: dict) -> str:
        """Returns the folder name used for the account owning the item."""
        account = str(item.get("account") or self._current_account or "").strip()
        if "://" in account:
            account = urlsplit(account).path.rstrip("/").rsplit("/", 1)[-1]
        return sanitize_folder(account.lstrip("@"))

    @staticmethod
    def _instagram_type_folder(media_type: str) -> str:
        """Returns the Instagram folder a media type belongs to."""
        kind = str(media_type or "").lower()
        if kind == "reel":
            return "reels"
        if kind == "carousel":
            return "carousels"
        return "posts"

    def _item_directory(self, item: dict) -> str:
        """Returns the directory an item is stored in, creating it if needed.

        The layout is ``<output>/<Platform>/<account>/<type>`` where the
        account and type folders are only used when they are known.
        """
        parts = [self.output_dir, self._platform_folder()]
        account = self._account_folder(item)
        if account:
            parts.append(account)
        if self.platform == "instagram":
            parts.append(self._instagram_type_folder(item.get("media_type")))
        directory = os.path.join(*parts)
        os.makedirs(directory, exist_ok=True)
        return directory

    def _scan_output_tree(self) -> List[Tuple[str, str]]:
        """Lists every file below the output directory (cached per session)."""
        if self._output_scan is None:
            entries: List[Tuple[str, str]] = []
            for root, _dirs, files in os.walk(self.output_dir):
                for name in files:
                    entries.append((name, os.path.join(root, name)))
            self._output_scan = entries
        return self._output_scan

    def _remember_downloaded_file(self, path: str) -> None:
        """Keeps the cached file listing in sync with a finished download."""
        if self._output_scan is not None:
            self._output_scan.append((os.path.basename(path), path))

    def _find_existing_media(self, item_id: str) -> Optional[str]:
        """Finds the file of an item anywhere below the output directory."""
        stem = self._file_stem(item_id)
        for name, path in self._scan_output_tree():
            if not name.startswith(stem):
                continue
            rest = name[len(stem):]
            if rest and rest[0] not in "._":
                continue
            if file_is_present(path):
                return path
        return None

    def _is_item_already_saved(self, item_id: str) -> bool:
        """Checks whether media is really on disk; prunes stale database rows."""
        if self.db.is_downloaded(item_id, self.platform):
            recorded_path = self.db.get_file_path(item_id, self.platform)
            if recorded_path and recorded_path != "carousel_bundle" and file_is_present(recorded_path):
                return True

            if self._find_existing_media(item_id) is not None:
                return True

            self.db.remove_record(item_id, self.platform)
            return False

        existing = self._find_existing_media(item_id)
        if existing is None:
            return False

        path, kind = existing, "reel"
        stem = self._file_stem(item_id)
        rest = os.path.basename(path)[len(stem):]
        if rest.startswith("_") and rest[1:2].isdigit():
            kind = "carousel"
        self.db.mark_downloaded(
            item_id,
            MEDIA_TYPE_BY_KIND[kind],
            "",
            "carousel_bundle" if kind == "carousel" else path,
            platform=self.platform,
        )
        return True

    # ------------------------------------------------------------------
    # Single items
    # ------------------------------------------------------------------
    def download_single(self, url_or_id: str, force: bool = False) -> bool:
        """Downloads one item by URL or platform identifier."""
        item_id = self.client.extract_id(url_or_id)

        if not force and self._is_item_already_saved(item_id):
            print(f"[>>] Item {item_id} is already downloaded. Skipping!")
            return True

        print(f"[*] Fetching media metadata for {item_id}...")
        try:
            item = self.client.fetch_item(url_or_id)
        except Exception as exc:
            print(f"[-] Failed to fetch item {item_id}: {exc}")
            return False

        resolved_id = str(item.get("id") or item_id)
        if not force and resolved_id != item_id and self._is_item_already_saved(resolved_id):
            print(f"[>>] Item {resolved_id} is already downloaded. Skipping!")
            return True

        result = self._download_media_item(item, force=force)
        if result is None:
            print(f"[-] Download cancelled for item: {resolved_id}")
            return False
        if result:
            print(f"[+] Successfully downloaded item: {resolved_id} [{item.get('quality', 'HQ')}]")
            return True
        print(f"[-] Failed to download item: {resolved_id}")
        return False

    # ------------------------------------------------------------------
    # Accounts and playlists
    # ------------------------------------------------------------------
    def download_account(
        self,
        account: str,
        media_type_filter: str = "all",
        max_pages: Optional[int] = None,
        force: bool = False,
        count: Optional[int] = None,
    ) -> int:
        """Downloads account content with early-exit resumption."""
        return self._download_feeds(
            account,
            media_type_filter=media_type_filter,
            max_pages=max_pages,
            force=force,
            count=count,
        )

    def download_account_reels(
        self,
        account: str,
        max_pages: Optional[int] = None,
        force: bool = False,
        count: Optional[int] = None,
    ) -> int:
        """Downloads from a dedicated Reels/Shorts style feed when one exists."""
        if not any(feed.media_filter == "reels" for feed in self.client.account_feeds()):
            raise ValueError(
                f"{self.platform} does not expose a separate Reels feed. "
                f"Supported media types: {', '.join(self.client.supported_media_types)}."
            )
        return self._download_feeds(
            account,
            media_type_filter="reels",
            max_pages=max_pages,
            force=force,
            count=count,
        )

    def download_account_content(
        self,
        account: str,
        max_pages: Optional[int] = None,
        force: bool = False,
        count: Optional[int] = None,
    ) -> int:
        """Downloads every feed that makes up an account."""
        return self._download_feeds(
            account,
            media_type_filter="all",
            max_pages=max_pages,
            force=force,
            count=count,
        )

    def download_playlist(
        self,
        playlist_id: str,
        max_pages: Optional[int] = None,
        force: bool = False,
        count: Optional[int] = None,
        media_type_filter: str = "all",
    ) -> int:
        """Downloads a platform playlist when the integration supports them."""
        if not getattr(self.client, "supports_playlists", False):
            raise ValueError(f"{self.platform} does not support playlist downloads.")

        print(f"[*] Scanning {self.platform} playlist {playlist_id}...")
        try:
            items = self.client.iter_playlist_items(playlist_id, max_pages=max_pages)
            downloaded, _skipped = self._scan_items(
                items,
                label=f"playlist {playlist_id}",
                media_type_filter=media_type_filter,
                count=count,
                force=force,
            )
        except RuntimeError as exc:
            print(f"[-] Failed to fetch playlist {playlist_id}: {exc}")
            return 0
        if downloaded:
            print(f"\n[+] Download session complete! Downloaded {downloaded} new items from playlist {playlist_id}.")
        return downloaded

    # ------------------------------------------------------------------
    # URL dispatch
    # ------------------------------------------------------------------
    def download_url(
        self,
        url: str,
        max_pages: Optional[int] = None,
        force: bool = False,
        count: Optional[int] = None,
        media_type: Optional[str] = None,
    ):
        """Downloads the content implied by a supported URL."""
        target = self.client.detect_url(url)
        if target is None:
            raise ValueError(f"Unsupported {self.platform} URL: {url}")
        target_type, identifier = target

        if target_type == "single":
            return self.download_single(url, force=force)
        if target_type == "playlist":
            return self.download_playlist(
                identifier, max_pages=max_pages, force=force, count=count,
                media_type_filter=media_type or "all",
            )

        effective_filter = media_type
        if not effective_filter or effective_filter.lower() == "all":
            effective_filter = self.client.default_filter_for(identifier)
        if target_type == "reels":
            effective_filter = "reels"
        return self.download_account(
            identifier,
            media_type_filter=effective_filter,
            max_pages=max_pages,
            force=force,
            count=count,
        )

    # ------------------------------------------------------------------
    # URL / Account scanning (count without downloading)
    # ------------------------------------------------------------------
    def scan_url(self, url: str) -> dict:
        """Scans a URL and returns content counts without downloading."""
        target = self.client.detect_url(url)
        if target is None:
            raise ValueError(f"Unsupported {self.platform} URL: {url}")
        target_type, identifier = target

        if target_type == "single":
            return {"type": "single", "identifier": identifier, "total": 1}

        feed_filter = "reels" if target_type == "reels" else "all"
        return self.scan_account(identifier, media_type_filter=feed_filter)

    def scan_account(
        self,
        account: str,
        media_type_filter: str = "all",
    ) -> dict:
        """Counts total items on an account feed without downloading."""
        feeds = self.client.feeds_for(media_type_filter)
        if not feeds:
            raise ValueError(
                f"{self.platform} does not support account scanning. "
                "Pass a single item URL instead."
            )

        try:
            account_id = self._resolve_account(account)
        except RuntimeError as exc:
            raise ValueError(
                f"Could not resolve {self.client.account_label(account)}: {exc}"
            ) from exc

        label = self.client.account_label(account)
        filter_mode = (media_type_filter or "all").lower()
        total = 0
        already_downloaded = 0
        known = self.db.get_all_downloaded_shortcodes(self.platform)

        for feed in feeds:
            if filter_mode != "all" and feed.media_filter.lower() != filter_mode:
                continue
            feed_name = feed.name
            print(f"[*] Scanning {label} [{feed_name}; filter: {filter_mode}]...")
            try:
                items = feed.fetch(account_id, None, account)
            except RuntimeError as exc:
                print(f"[-] Failed to scan {feed_name} for {label}: {exc}")
                continue

            for raw in items:
                item_id = self.client.item_id(raw)
                if not item_id:
                    continue
                if not self.client.prefilter(raw, filter_mode):
                    continue
                total += 1
                if str(item_id) in known or self._is_item_already_saved(str(item_id)):
                    already_downloaded += 1
                if total % 50 == 0:
                    print(f"    ... scanned {total} items so far")
            break  # Use only the first matching feed to avoid double-counting

        return {
            "type": filter_mode,
            "account": account,
            "label": label,
            "total": total,
            "already_downloaded": already_downloaded,
            "remaining": total - already_downloaded,
        }

    # ------------------------------------------------------------------
    # Feed plumbing
    # ------------------------------------------------------------------
    def _download_feeds(
        self,
        account: str,
        media_type_filter: str = "all",
        max_pages: Optional[int] = None,
        force: bool = False,
        count: Optional[int] = None,
    ) -> int:
        if count is not None and count <= 0:
            raise ValueError("Count must be a positive integer greater than 0.")

        feeds = self.client.feeds_for(media_type_filter)
        if not feeds:
            raise ValueError(
                f"{self.platform} does not support account downloads. "
                "Pass a single item URL instead."
            )

        try:
            account_id = self._resolve_account(account)
        except RuntimeError as exc:
            print(f"\n[-] Could not resolve {self.client.account_label(account)}: {exc}")
            return 0

        label = self.client.account_label(account)
        filter_mode = (media_type_filter or "all").lower()
        total_downloaded = 0
        total_skipped = 0
        previous_account = self._current_account
        self._current_account = account

        try:
            for feed in feeds:
                remaining = None if count is None else count - total_downloaded
                if remaining is not None and remaining <= 0:
                    break
                downloaded, skipped = self._download_feed(
                    account=account,
                    account_id=account_id,
                    feed=feed,
                    filter_mode=filter_mode,
                    max_pages=max_pages,
                    force=force,
                    count=remaining,
                )
                total_downloaded += downloaded
                total_skipped += skipped
        finally:
            self._current_account = previous_account

        if total_downloaded or total_skipped:
            skip_str = (
                f" ({total_skipped} previously downloaded items skipped)"
                if total_skipped else ""
            )
            print(
                f"\n[+] Download session complete! Downloaded {total_downloaded} new "
                f"items{skip_str} for {label}."
            )
        else:
            explanation = self.client.explain_empty_result(account)
            if explanation:
                print(f"\n{explanation}")
            else:
                print(
                    f"\n[!] No downloadable items found for {label} "
                    f"matching filter '{filter_mode}'."
                )
        return total_downloaded

    def _resolve_account(self, account: str) -> str:
        cached = self.db.get_cached_user_id(account, self.platform)
        if cached:
            print(f"[+] Using cached account ID {cached} for {self.client.account_label(account)}.")
            return cached

        print(f"[*] Resolving {self.client.account_label(account)}...")
        account_id = self.client.resolve_account_id(account)
        self.db.cache_user_id(account, account_id, self.platform)
        print(f"[+] Resolved to: {account_id}")
        return account_id

    def _download_feed(
        self,
        account: str,
        account_id: str,
        feed,
        filter_mode: str,
        max_pages: Optional[int],
        force: bool,
        count: Optional[int],
    ) -> Tuple[int, int]:
        label = self.client.account_label(account)
        target = f" (target: {count} new items)" if count is not None else ""
        print(f"[*] Scanning {label} [{feed.name}; filter: {filter_mode}]{target}...")

        try:
            items = feed.fetch(account_id, max_pages, account)
        except RuntimeError as exc:
            print(f"\n[-] Failed to fetch {feed.name} for {label}: {exc}")
            return 0, 0

        try:
            return self._scan_items(items, label, filter_mode, count, force)
        except RuntimeError as exc:
            print(f"\n[-] Failed to fetch {feed.name} for {label}: {exc}")
            return 0, 0

    def _scan_items(
        self,
        items: Iterable[dict],
        label: str,
        media_type_filter: str,
        count: Optional[int],
        force: bool,
    ) -> Tuple[int, int]:
        """Applies filtering, resume checks and batch limits to a raw item stream."""
        filter_mode = (media_type_filter or "all").lower()
        known = self.db.get_all_downloaded_shortcodes(self.platform)
        consecutive_known = 0
        downloaded = 0
        skipped = 0

        for raw in items:
            if count is not None and downloaded >= count:
                break

            item_id = self.client.item_id(raw)
            if not item_id:
                continue
            item_id = str(item_id)

            if not self.client.prefilter(raw, filter_mode):
                continue

            if not force and (item_id in known or self._is_item_already_saved(item_id)):
                skipped += 1
                if count is None and self.consecutive_threshold > 0:
                    consecutive_known += 1
                    if consecutive_known >= self.consecutive_threshold:
                        print(
                            f"\n[>>] Early-Exit threshold reached "
                            f"({self.consecutive_threshold} consecutive known items). "
                            "Resumption complete!\n"
                            "     (To scan past known items and check for older content, use --no-early-exit)"
                        )
                        break
                continue
            consecutive_known = 0

            try:
                item = self.client.normalize_item(raw)
            except Exception as exc:
                print(f"[-] Could not fetch full details for {item_id}: {exc}")
                continue

            if not self.client.matches_filter(item.get("media_type"), filter_mode):
                continue

            result = self._download_media_item(item, force=force)
            if result is None:
                continue
            if result:
                downloaded += 1
                known.add(item_id)
                known.add(str(item.get("id") or item_id))
                if count is not None:
                    print(f"    [Batch Progress] {downloaded}/{count} new items downloaded.")
                    if downloaded >= count:
                        print(f"\n[+] Successfully reached target of {count} new items!")
                        break

        return downloaded, skipped

    # ------------------------------------------------------------------
    # Media writing
    # ------------------------------------------------------------------
    def _download_media_item(self, item: dict, force: bool = False) -> Optional[bool]:
        """Downloads one item; returns None when the user cancels a quality fallback."""
        item_id = str(item.get("id") or item.get("shortcode") or "")
        if not item_id:
            return False

        media_type = item.get("media_type") or "Post"
        timestamp = item.get("timestamp") or ""
        quality = item.get("quality") or "HQ"
        headers = item.get("http_headers") or {}
        stem = self._file_stem(item_id, item.get("title") or "")
        directory = self._item_directory(item)

        if media_type == "Carousel" and item.get("children"):
            success_all = True
            for index, child in enumerate(item["children"], start=1):
                is_video = bool(child.get("is_video"))
                ext = child.get("ext") or ("mp4" if is_video else "jpg")
                file_path = os.path.join(directory, f"{stem}_{index}.{ext}")
                child_headers = child.get("http_headers") or headers

                if not force and file_is_present(file_path):
                    continue

                description = f"[Carousel {index}] {item_id} ({child.get('quality', 'HQ')})"
                if is_video:
                    ok = self._download_video_stream(
                        child.get("video_url"),
                        child.get("audio_url"),
                        file_path,
                        description,
                        audio_start_ms=child.get("audio_start_ms"),
                        headers=child_headers,
                        ext=ext,
                    )
                else:
                    ok = self._download_file(child.get("display_url"), file_path,
                                             description, headers=child_headers)
                if not ok:
                    success_all = False
                elif file_is_present(file_path):
                    self._remember_downloaded_file(file_path)

            if success_all:
                self.db.mark_downloaded(
                    item_id, media_type, timestamp, "carousel_bundle", platform=self.platform
                )
            return success_all

        ext = item.get("ext") or self._default_extension(media_type)
        file_path = os.path.join(directory, f"{stem}.{ext}")

        if not force and file_is_present(file_path):
            self.db.mark_downloaded(item_id, media_type, timestamp, file_path,
                                    platform=self.platform)
            return True

        fallback = item.get("quality_fallback")
        if fallback and not self._confirm_quality_fallback(fallback):
            print(f"[-] Download cancelled for {item_id}: requested quality unavailable.")
            return None

        if item.get("video_url"):
            description = f"[{media_type}] {item_id} ({quality})"
            ok = self._download_video_stream(
                item["video_url"],
                item.get("audio_url"),
                file_path,
                description,
                audio_start_ms=item.get("audio_start_ms"),
                headers=headers,
                ext=ext,
            )
            if ok:
                self.db.mark_downloaded(item_id, media_type, timestamp, file_path,
                                        platform=self.platform)
                self._remember_downloaded_file(file_path)
            return ok

        display_url = item.get("display_url")
        if not display_url:
            print(f"[-] No downloadable URL was found for {item_id}.")
            return False
        if self._download_file(display_url, file_path, f"[{media_type}] {item_id}",
                               headers=headers):
            self.db.mark_downloaded(item_id, media_type, timestamp, file_path,
                                    platform=self.platform)
            self._remember_downloaded_file(file_path)
            return True
        return False

    def _confirm_quality_fallback(self, fallback: dict) -> bool:
        """Returns True when the item may download at the fallback height."""
        if self.quality_confirmer is None:
            requested = fallback.get("requested")
            selected = fallback.get("selected")
            print(
                f"[!] Requested {requested}p is unavailable; "
                f"using {selected}p."
            )
            return True
        return bool(self.quality_confirmer(fallback))

    @staticmethod
    def _default_extension(media_type: str) -> str:
        if media_type.lower() in IMAGE_MEDIA_TYPES:
            return "jpg"
        return "mp4"

    def _download_video_stream(
        self,
        video_url: Optional[str],
        audio_url: Optional[str],
        file_path: str,
        desc: str,
        audio_start_ms: Optional[int] = None,
        headers: Optional[dict] = None,
        ext: str = "mp4",
    ) -> bool:
        """Downloads separate video/audio tracks and merges them with FFmpeg."""
        if not video_url:
            return False

        ffmpeg_bin = shutil.which("ffmpeg")

        if audio_url and audio_url != video_url:
            if not ffmpeg_bin:
                print(
                    f"[-] Cannot merge audio for {desc}: FFmpeg was not found. "
                    "Install FFmpeg and add it to PATH, then retry."
                )
                return False

            audio_ext = "webm" if ext == "webm" else "m4a"
            temp_v = f"{file_path}.temp_v.{ext}"
            temp_a = f"{file_path}.temp_a.{audio_ext}"
            temp_output = f"{file_path}.temp_mux.{ext}"
            container_args = ["-movflags", "+faststart"] if ext == "mp4" else []
            try:
                if not self._download_file(video_url, temp_v, f"{desc} [Video]", headers=headers):
                    return False
                if not self._download_file(audio_url, temp_a, f"{desc} [Audio]", headers=headers):
                    return False

                if not self._audio_matches_video(
                    temp_v, temp_a, audio_url, desc, headers=headers
                ):
                    return False

                start_sec = (audio_start_ms / 1000.0) if audio_start_ms and audio_start_ms > 0 else 0.0
                audio_input_args = ["-ss", f"{start_sec:.3f}"] if start_sec > 0 else []

                # Attempt 1: lossless stream copy.
                cmd = [
                    ffmpeg_bin, "-y", "-v", "error",
                    "-i", temp_v,
                    *audio_input_args,
                    "-i", temp_a,
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-c:v", "copy",
                    "-c:a", "copy",
                    "-shortest",
                    *container_args,
                    temp_output,
                ]
                result = self._run_ffmpeg(cmd, desc)
                if (
                    result is not None
                    and result.returncode == 0
                    and self._adopt_temp_file(temp_output, file_path, desc)
                ):
                    return True

                # Attempt 2: transcode only the audio track if the copy failed.
                # AAC is not legal in WebM, so pick a codec the container accepts.
                if ext == "webm":
                    fallback_audio_args = ["-c:a", "libopus", "-b:a", "160k"]
                else:
                    fallback_audio_args = ["-c:a", "aac", "-b:a", "192k"]
                fallback_cmd = [
                    ffmpeg_bin, "-y", "-v", "error",
                    "-i", temp_v,
                    *audio_input_args,
                    "-i", temp_a,
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-c:v", "copy",
                    *fallback_audio_args,
                    "-shortest",
                    *container_args,
                    temp_output,
                ]
                fallback = self._run_ffmpeg(fallback_cmd, desc, quiet=True)
                if (
                    fallback is not None
                    and fallback.returncode == 0
                    and self._adopt_temp_file(temp_output, file_path, desc)
                ):
                    return True

                if result is not None:
                    error = (result.stderr or "").strip()
                    if error:
                        print(f"[-] FFmpeg could not merge {desc}: {error[-1000:]}")
                    else:
                        print(f"[-] FFmpeg could not merge {desc} (exit code {result.returncode}).")
                return False
            finally:
                for tmp in (temp_v, temp_a, temp_output):
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass

        return self._download_file(video_url, file_path, desc, headers=headers)

    @staticmethod
    def _media_duration(path: str) -> Optional[float]:
        """Returns the duration of a media file in seconds, if it can be read."""
        probe = shutil.which("ffprobe")
        if not probe:
            return None
        try:
            result = subprocess.run(
                [
                    probe, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
        except OSError:
            return None
        try:
            return float((result.stdout or "").strip())
        except (TypeError, ValueError):
            return None

    def _audio_matches_video(
        self,
        video_path: str,
        audio_path: str,
        audio_url: str,
        desc: str,
        headers: Optional[dict] = None,
    ) -> bool:
        """Checks that the audio track covers the whole video.

        YouTube sometimes serves an audio track that is shorter than the
        video.  Merging it with ``-shortest`` would silently cut the video
        short, so the download is retried once and then reported as failed.
        """
        video_duration = self._media_duration(video_path)
        audio_duration = self._media_duration(audio_path)
        if not video_duration or not audio_duration:
            return True
        if abs(video_duration - audio_duration) <= 1.0:
            return True

        print(
            f"[!] The audio track for {desc} is {audio_duration:.1f}s but the "
            f"video is {video_duration:.1f}s. Retrying the audio download."
        )
        self._remove_partial_file(audio_path)
        if not self._download_file(
            audio_url, audio_path, f"{desc} [Audio]", headers=headers
        ):
            return False

        audio_duration = self._media_duration(audio_path)
        if not audio_duration or abs(video_duration - audio_duration) <= 1.0:
            return True
        print(
            f"[-] The audio track for {desc} does not match the video length "
            f"({audio_duration:.1f}s vs {video_duration:.1f}s). Aborting the "
            "merge to avoid a truncated video."
        )
        return False

    @staticmethod
    def _run_ffmpeg(cmd, desc: str, quiet: bool = False):
        try:
            return subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except OSError as exc:
            if not quiet:
                print(f"[-] Could not run FFmpeg for {desc}: {exc}")
            return None

    @staticmethod
    def _adopt_temp_file(temp_output: str, file_path: str, desc: str) -> bool:
        if not (os.path.exists(temp_output) and os.path.getsize(temp_output) > 0):
            return False
        try:
            os.replace(temp_output, file_path)
        except OSError as exc:
            print(f"[-] Could not save merged video for {desc}: {exc}")
            return False
        return True

    def _download_file(
        self,
        url: Optional[str],
        file_path: str,
        desc: str,
        headers: Optional[dict] = None,
    ) -> bool:
        if not url:
            print(f"[-] No download URL available for {desc}.")
            return False

        request_headers = {"Accept-Encoding": "identity"}
        request_headers.update(headers or {})

        if self.platform == "youtube":
            # YouTube's media CDN now rejects full GETs (403) or throttles them
            # to a crawl; bounded range requests are both accepted and fast.
            return self._download_file_in_ranges(
                url, file_path, desc, request_headers
            )

        try:
            with self.client.session.get(
                url, stream=True, timeout=30, headers=request_headers
            ) as response:
                if response.status_code != 200:
                    print(f"[-] Download request failed for {desc}: HTTP {response.status_code}.")
                    return False

                total_size = int(response.headers.get("content-length", 0))
                bytes_written = 0
                with open(file_path, "wb") as file, tqdm(
                    desc=desc,
                    total=total_size,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    leave=False,
                ) as bar:
                    for chunk in response.iter_content(chunk_size=65536):
                        if chunk:
                            file.write(chunk)
                            bytes_written += len(chunk)
                            bar.update(len(chunk))
            if bytes_written == 0:
                print(f"[-] Download returned an empty file for {desc}.")
                os.remove(file_path)
                return False
            return True
        except (requests.RequestException, OSError) as exc:
            print(f"[-] Could not download {desc}: {exc}")
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass
            return False

    @staticmethod
    def _remove_partial_file(file_path: str) -> None:
        if not os.path.exists(file_path):
            return
        try:
            os.remove(file_path)
        except OSError:
            pass

    def _download_file_in_ranges(
        self,
        url: str,
        file_path: str,
        desc: str,
        headers: Optional[dict] = None,
    ) -> bool:
        """Downloads a YouTube stream in bounded range requests.

        YouTube's media CDN rejects a plain full-file GET and throttles the
        ones it accepts, while bounded ranges are served at full speed.  Probe
        the total size, then fetch roughly 5% of the file per request (clamped
        to 512 KiB..16 MiB).
        """
        request_headers = dict(headers or {})
        probe_headers = dict(request_headers, Range="bytes=0-0")
        total_size = 0
        try:
            with self.client.session.get(
                url, headers=probe_headers, stream=True, timeout=30
            ) as probe:
                if probe.status_code not in (200, 206):
                    print(
                        f"[-] Download request failed for {desc}: "
                        f"HTTP {probe.status_code}."
                    )
                    return False
                match = _CONTENT_RANGE.match(probe.headers.get("Content-Range", ""))
                if match and match.group(3) != "*":
                    total_size = int(match.group(3))
        except requests.RequestException as exc:
            print(f"[-] Could not download {desc}: {exc}")
            return False

        if total_size:
            chunk_size = max(
                512 * 1024,
                min(16 * 1024 * 1024, total_size // 20),
            )
        else:
            chunk_size = 1024 * 1024

        bytes_written = 0
        failure: Optional[str] = None
        try:
            with open(file_path, "wb") as file, tqdm(
                desc=desc,
                total=total_size or None,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                leave=False,
            ) as bar:
                while total_size == 0 or bytes_written < total_size:
                    start = bytes_written
                    end = start + chunk_size - 1
                    if total_size:
                        end = min(end, total_size - 1)
                    range_headers = dict(
                        request_headers, Range=f"bytes={start}-{end}"
                    )
                    with self.client.session.get(
                        url, headers=range_headers, stream=True, timeout=30
                    ) as response:
                        if response.status_code == 416:
                            break
                        if response.status_code == 200:
                            # The server ignored Range; this is the full file.
                            for chunk in response.iter_content(chunk_size=65536):
                                if chunk:
                                    file.write(chunk)
                                    bar.update(len(chunk))
                            return True
                        if response.status_code != 206:
                            failure = f"HTTP {response.status_code}"
                            break

                        if not total_size:
                            match = _CONTENT_RANGE.match(
                                response.headers.get("Content-Range", "")
                            )
                            if match and match.group(3) != "*":
                                total_size = int(match.group(3))
                                bar.total = total_size

                        written = 0
                        for chunk in response.iter_content(chunk_size=65536):
                            if chunk:
                                file.write(chunk)
                                written += len(chunk)
                                bar.update(len(chunk))
                        if written == 0:
                            break
                        bytes_written += written
                        if total_size and bytes_written >= total_size:
                            break
                        if not total_size and written < (end - start + 1):
                            break
        except (requests.RequestException, OSError) as exc:
            failure = str(exc)

        if failure:
            print(f"[-] Download request failed for {desc}: {failure}.")
            self._remove_partial_file(file_path)
            return False

        if bytes_written == 0:
            print(f"[-] Download returned an empty file for {desc}.")
            self._remove_partial_file(file_path)
            return False
        if total_size and bytes_written != total_size:
            print(
                f"[-] Incomplete download for {desc}: "
                f"{bytes_written}/{total_size} bytes."
            )
            self._remove_partial_file(file_path)
            return False
        return True

    # ------------------------------------------------------------------
    # History management
    # ------------------------------------------------------------------
    def clear_database(
        self,
        item: Optional[str] = None,
        clear_all: bool = False,
        prune_missing: bool = False,
    ) -> int:
        """Removes records from the state database without deleting media files."""
        if item:
            item_id = self.client.extract_id(item)
            success = self.db.remove_record(item_id, self.platform)
            if success:
                print(f"[+] Successfully removed record for item: {item_id} from database.")
                return 1
            print(f"[!] Item {item_id} was not found in database.")
            return 0
        if prune_missing:
            count = self.db.prune_missing(self.output_dir, self.platform)
            print(f"[+] Pruned {count} missing records whose files were deleted from disk.")
            return count

        count = self.db.clear_all(self.platform)
        print(
            f"[+] Successfully cleared entire download history ({count} records removed). "
            "Existing media files preserved."
        )
        return count
