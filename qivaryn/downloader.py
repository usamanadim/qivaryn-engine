import os
import shutil
import subprocess
import requests
from tqdm import tqdm
from typing import Optional
from .client import InstagramClient
from .database import StateDatabase

class QivarynEngine:
    def __init__(self, output_dir: str = "./downloads", db_path: str = "qivaryn_state.db", sessionid: Optional[str] = None, cookies_file: Optional[str] = None, consecutive_threshold: int = 3):
        self.output_dir = output_dir
        self.client = InstagramClient(sessionid=sessionid, cookies_file=cookies_file)
        self.db = StateDatabase(db_path=db_path)
        self.consecutive_threshold = consecutive_threshold
        os.makedirs(self.output_dir, exist_ok=True)

    def _is_item_already_saved(self, shortcode: str) -> bool:
        """Checks if media is actually saved on disk. If file was manually deleted from disk, prunes stale record."""
        if self.db.is_downloaded(shortcode):
            recorded_path = self.db.get_file_path(shortcode)
            if recorded_path and os.path.exists(recorded_path) and os.path.getsize(recorded_path) > 0:
                return True
            if recorded_path == "carousel_bundle":
                for ext in [".mp4", ".jpg", ".jpeg", ".webp"]:
                    if os.path.exists(os.path.join(self.output_dir, f"{shortcode}_1{ext}")):
                        return True
            # Check standard file patterns in output_dir
            for ext in [".mp4", ".jpg", ".jpeg", ".webp"]:
                candidate = os.path.join(self.output_dir, f"{shortcode}{ext}")
                if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
                    return True
                carousel_candidate = os.path.join(self.output_dir, f"{shortcode}_1{ext}")
                if os.path.exists(carousel_candidate) and os.path.getsize(carousel_candidate) > 0:
                    return True

            # The record is in DB, but the file was manually deleted from disk!
            # Automatically remove stale record so user can re-download it without being blocked
            self.db.remove_record(shortcode)
            return False

        # If not recorded in DB, check if file exists on disk
        for ext in [".mp4", ".jpg", ".jpeg", ".webp"]:
            candidate = os.path.join(self.output_dir, f"{shortcode}{ext}")
            if os.path.exists(candidate) and os.path.getsize(candidate) > 0:
                self.db.mark_downloaded(shortcode, "Reel" if ext == ".mp4" else "Post", "", candidate)
                return True
            carousel_candidate = os.path.join(self.output_dir, f"{shortcode}_1{ext}")
            if os.path.exists(carousel_candidate) and os.path.getsize(carousel_candidate) > 0:
                self.db.mark_downloaded(shortcode, "Carousel", "", "carousel_bundle")
                return True
        return False

    def clear_database(self, item: Optional[str] = None, clear_all: bool = False, prune_missing: bool = False) -> int:
        """Removes records from the state database without deleting any media files from disk."""
        if item:
            shortcode = self.client.extract_shortcode_from_url(item)
            success = self.db.remove_record(shortcode)
            if success:
                print(f"[+] Successfully removed record for item: {shortcode} from database.")
                return 1
            else:
                print(f"[!] Item {shortcode} was not found in database.")
                return 0
        elif prune_missing:
            count = self.db.prune_missing(self.output_dir)
            print(f"[+] Pruned {count} missing records whose files were deleted from disk.")
            return count
        elif clear_all:
            count = self.db.clear_all()
            print(f"[+] Successfully cleared entire download history ({count} records removed). Existing media files preserved.")
            return count
        else:
            count = self.db.clear_all()
            print(f"[+] Successfully cleared entire download history ({count} records removed). Existing media files preserved.")
            return count

    def download_single(self, url_or_shortcode: str, force: bool = False) -> bool:
        """Downloads a single Reel, Post, or Carousel by URL or shortcode, skipping existing items instantly."""
        shortcode = self.client.extract_shortcode_from_url(url_or_shortcode)

        # Instant skipping without any API calls or network latency
        if not force and self._is_item_already_saved(shortcode):
            print(f"[>>] Item {shortcode} is already downloaded. Skipping!")
            return True

        print(f"[*] Fetching media metadata for shortcode: {shortcode}...")
        try:
            post = self.client.fetch_post_by_shortcode(shortcode)
        except Exception as e:
            print(f"[-] Failed to fetch item {shortcode}: {e}")
            return False

        if self._download_media_item(post, force=force):
            print(f"[+] Successfully downloaded item: {shortcode} [{post.get('quality', 'HQ')}]")
            return True
        else:
            print(f"[-] Failed to download item: {shortcode}")
            return False

    def download_account(self, username: str, media_type_filter: str = "all", max_pages: Optional[int] = None, force: bool = False):
        """Downloads account content with early-exit resumption and zero redundant processing."""
        cached_id = self.db.get_cached_user_id(username)
        if cached_id:
            user_id = cached_id
            print(f"[+] Using cached User ID {user_id} for @{username}.")
        else:
            print(f"[*] Resolving User ID for @{username}...")
            user_id = self.client.get_user_id(username)
            self.db.cache_user_id(username, user_id)
            print(f"[+] Found User ID: {user_id}. Scanning feed with high-speed resumption...")

        consecutive_known_count = 0
        total_downloaded = 0
        known_shortcodes = self.db.get_all_downloaded_shortcodes()

        for raw_item in self.client.fetch_user_raw_posts_paginated(user_id, max_pages=max_pages):
            shortcode = raw_item.get("code")
            if not shortcode:
                continue

            # ZERO-OVERHEAD SKIP: Check in-memory state before ANY parsing or network calls
            if not force and (shortcode in known_shortcodes or self._is_item_already_saved(shortcode)):
                consecutive_known_count += 1
                if consecutive_known_count >= self.consecutive_threshold:
                    print(f"\n[>>] Early-Exit threshold reached ({self.consecutive_threshold} consecutive known items). Resumption complete instantly!")
                    break
                continue
            else:
                consecutive_known_count = 0

            # Filter media types directly from raw metadata before parsing heavy XML/DASH trees
            raw_media_type = raw_item.get("media_type")
            if media_type_filter == "posts" and raw_media_type == 2:
                continue
            if media_type_filter == "reels" and raw_media_type != 2:
                continue

            # Parse and extract 1080p ONLY for newly identified media items
            post = self.client._parse_mobile_media_item(raw_item)
            m_type = post["media_type"]

            if media_type_filter == "posts" and m_type == "Reel":
                continue
            if media_type_filter == "reels" and m_type != "Reel":
                continue

            if self._download_media_item(post, force=force):
                total_downloaded += 1
                known_shortcodes.add(shortcode)

        print(f"\n[+] Download session complete! Downloaded {total_downloaded} new items for @{username}.")

    def _download_media_item(self, post: dict, force: bool = False) -> bool:
        shortcode = post["shortcode"]
        media_type = post["media_type"]
        timestamp = post["timestamp"]
        quality = post.get("quality", "HQ")

        if media_type == "Carousel" and post.get("children"):
            success_all = True
            for idx, child in enumerate(post["children"], start=1):
                ext = "mp4" if child["is_video"] else "jpg"
                file_path = os.path.join(self.output_dir, f"{shortcode}_{idx}.{ext}")

                if not force and os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                    continue

                if child["is_video"]:
                    if not self._download_video_stream(
                        child["video_url"],
                        child.get("audio_url"),
                        file_path,
                        f"[Carousel {idx}] {shortcode} ({child.get('quality', 'HQ')})",
                        audio_start_ms=child.get("audio_start_ms")
                    ):
                        success_all = False
                else:
                    if not self._download_file(child["display_url"], file_path, f"[Carousel {idx}] {shortcode}"):
                        success_all = False

            if success_all:
                self.db.mark_downloaded(shortcode, media_type, timestamp, "carousel_bundle")
            return success_all

        ext = "mp4" if media_type == "Reel" else "jpg"
        file_path = os.path.join(self.output_dir, f"{shortcode}.{ext}")

        if not force and os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            self.db.mark_downloaded(shortcode, media_type, timestamp, file_path)
            return True

        if media_type == "Reel":
            video_url = post.get("video_url")
            if not video_url:
                return False
            audio_url = post.get("audio_url")
            audio_start_ms = post.get("audio_start_ms")
            desc = f"[{media_type}] {shortcode} ({quality})"
            if self._download_video_stream(video_url, audio_url, file_path, desc, audio_start_ms=audio_start_ms):
                self.db.mark_downloaded(shortcode, media_type, timestamp, file_path)
                return True
            return False

        # Single Photo
        display_url = post.get("display_url")
        if not display_url:
            return False
        if self._download_file(display_url, file_path, f"[{media_type}] {shortcode}"):
            self.db.mark_downloaded(shortcode, media_type, timestamp, file_path)
            return True
        return False

    def _download_video_stream(
        self,
        video_url: str,
        audio_url: Optional[str],
        file_path: str,
        desc: str,
        audio_start_ms: Optional[int] = None,
    ) -> bool:
        """Downloads separate video/audio tracks and merges them with FFmpeg."""
        ffmpeg_bin = shutil.which("ffmpeg")

        if audio_url and audio_url != video_url:
            if not ffmpeg_bin:
                print(
                    f"[-] Cannot merge audio for {desc}: FFmpeg was not found. "
                    "Install FFmpeg and add it to PATH, then retry."
                )
                return False

            temp_v = f"{file_path}.temp_v.mp4"
            temp_a = f"{file_path}.temp_a.m4a"
            temp_output = f"{file_path}.temp_mux.mp4"
            try:
                if not self._download_file(video_url, temp_v, f"{desc} [Video]"):
                    return False
                if not self._download_file(audio_url, temp_a, f"{desc} [Audio]"):
                    return False

                start_sec = (audio_start_ms / 1000.0) if audio_start_ms and audio_start_ms > 0 else 0.0
                audio_input_args = ["-ss", f"{start_sec:.3f}"] if start_sec > 0 else []

                # Attempt 1: lossless stream copy
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
                    "-movflags", "+faststart",
                    temp_output
                ]
                try:
                    result = subprocess.run(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                    )
                except OSError as exc:
                    print(f"[-] Could not run FFmpeg for {desc}: {exc}")
                    return False

                if result.returncode == 0 and os.path.exists(temp_output) and os.path.getsize(temp_output) > 0:
                    try:
                        os.replace(temp_output, file_path)
                    except OSError as exc:
                        print(f"[-] Could not save merged video for {desc}: {exc}")
                        return False
                    return True

                # Attempt 2: if copy failed, transcode audio to high-quality AAC while preserving original video
                fallback_cmd = [
                    ffmpeg_bin, "-y", "-v", "error",
                    "-i", temp_v,
                    *audio_input_args,
                    "-i", temp_a,
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-shortest",
                    "-movflags", "+faststart",
                    temp_output
                ]
                try:
                    fb_result = subprocess.run(
                        fallback_cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                    )
                    if fb_result.returncode == 0 and os.path.exists(temp_output) and os.path.getsize(temp_output) > 0:
                        try:
                            os.replace(temp_output, file_path)
                        except OSError as exc:
                            print(f"[-] Could not save merged video for {desc}: {exc}")
                            return False
                        return True
                except OSError:
                    pass

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

        # Direct download is only safe when Instagram supplied a single stream.
        return self._download_file(video_url, file_path, desc)

    def _download_file(self, url: str, file_path: str, desc: str) -> bool:
        try:
            with self.client.session.get(url, stream=True, timeout=30) as response:
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
                    for chunk in response.iter_content(chunk_size=16384):
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