"""Bulk download controller, progress statistics, and interactive controls.

Provides real-time progress statistics, thread-safe pause/resume/stop controls,
and immediate cancellation with incomplete file cleanup for bulk downloads.
"""

import os
import queue
import sys
import threading
import time
from typing import Callable, List, Optional


class DownloadStats:
    """Thread-safe statistics tracking for active bulk downloads."""

    def __init__(self, target_count: Optional[int] = None, item_label: str = "Reels"):
        self.lock = threading.Lock()
        self.total_reels: int = 0
        self.downloaded_reels: int = 0
        self.remaining_reels: int = 0
        self.already_downloaded: int = 0
        self.target_count: Optional[int] = target_count
        self.item_label: str = item_label

    def set_target(self, target_count: Optional[int], item_label: Optional[str] = None):
        with self.lock:
            self.target_count = target_count
            if item_label:
                self.item_label = item_label
            self._recalculate_remaining_locked()

    def update(
        self,
        total_detected: Optional[int] = None,
        downloaded: Optional[int] = None,
        already_downloaded: Optional[int] = None,
    ):
        with self.lock:
            if total_detected is not None:
                self.total_reels = total_detected
            if downloaded is not None:
                self.downloaded_reels = downloaded
            if already_downloaded is not None:
                self.already_downloaded = already_downloaded
            self._recalculate_remaining_locked()

    def _recalculate_remaining_locked(self):
        if self.target_count is not None:
            self.remaining_reels = max(0, self.target_count - self.downloaded_reels)
        else:
            self.remaining_reels = max(
                0, self.total_reels - (self.already_downloaded + self.downloaded_reels)
            )

    def to_dict(self) -> dict:
        with self.lock:
            return {
                "total_reels": self.total_reels,
                "downloaded_reels": self.downloaded_reels,
                "remaining_reels": self.remaining_reels,
                "already_downloaded": self.already_downloaded,
                "target_count": self.target_count,
                "item_label": self.item_label,
            }

    def format_overview(self, title: str = "Download Progress Overview") -> str:
        with self.lock:
            lbl = self.item_label
            lines = [
                "=" * 50,
                f"  {title}",
                "  " + "-" * 48,
                f"  Total {lbl}:       {self.total_reels}",
                f"  Downloaded {lbl}:  {self.downloaded_reels}",
                f"  Remaining {lbl}:   {self.remaining_reels}",
                "=" * 50,
            ]
            return "\n".join(lines)

    def format_inline(self) -> str:
        with self.lock:
            lbl = self.item_label
            return (
                f"[Progress] Total {lbl}: {self.total_reels} | "
                f"Downloaded {lbl}: {self.downloaded_reels} | "
                f"Remaining {lbl}: {self.remaining_reels}"
            )


class DownloadController:
    """Controls bulk download execution with pause, resume, and stop controls."""

    def __init__(self):
        self._pause_event = threading.Event()
        self._pause_event.set()  # Not paused by default
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._state = "RUNNING"
        self._listeners: List[Callable[[str], None]] = []
        self.stats = DownloadStats()
        self.active_file_paths: List[str] = []

    @property
    def is_paused(self) -> bool:
        return not self._pause_event.is_set() and not self._stop_event.is_set()

    @property
    def is_stopped(self) -> bool:
        return self._stop_event.is_set()

    @property
    def state(self) -> str:
        with self._state_lock:
            return self._state

    def reset(self):
        """Resets controller state for a new download run."""
        with self._state_lock:
            self._state = "RUNNING"
            self._pause_event.set()
            self._stop_event.clear()
            self.active_file_paths.clear()

    def pause(self):
        """Pauses the bulk download process."""
        with self._state_lock:
            if self._state == "STOPPED":
                return
            self._state = "PAUSED"
            self._pause_event.clear()
        print("\n[||] Bulk download PAUSED. Press 'r' to Resume or 's'/'q' to Stop.")
        self._notify("PAUSED")

    def resume(self):
        """Resumes downloading from where it was paused."""
        with self._state_lock:
            if self._state == "STOPPED":
                return
            self._state = "RUNNING"
            self._pause_event.set()
        print("\n[>] Bulk download RESUMED.")
        self._notify("RESUMED")

    def stop(self):
        """Immediately terminates the bulk download and cancels any active reel."""
        with self._state_lock:
            self._state = "STOPPED"
            self._stop_event.set()
            self._pause_event.set()  # Unblock any thread waiting on pause
        print("\n[■] Bulk download STOPPED by user. Incomplete downloads cancelled.")
        self.cleanup_active_files()
        self._notify("STOPPED")

    def register_active_file(self, path: str):
        """Tracks a partially downloaded file so it can be cleaned up on Stop."""
        with self._state_lock:
            if path and path not in self.active_file_paths:
                self.active_file_paths.append(path)

    def unregister_active_file(self, path: str):
        """Unregisters a file after it has successfully finished or was cleaned up."""
        with self._state_lock:
            if path in self.active_file_paths:
                self.active_file_paths.remove(path)

    def cleanup_active_files(self):
        """Deletes any incomplete files currently registered."""
        with self._state_lock:
            for path in list(self.active_file_paths):
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            self.active_file_paths.clear()

    def wait_if_paused(self, check_interval: float = 0.2) -> bool:
        """Blocks while paused until resumed or stopped. Returns False if stopped, True if running."""
        while not self._pause_event.is_set():
            if self._stop_event.is_set():
                return False
            self._pause_event.wait(timeout=check_interval)
        return not self._stop_event.is_set()

    def add_listener(self, callback: Callable[[str], None]):
        self._listeners.append(callback)

    def _notify(self, state: str):
        for listener in self._listeners:
            try:
                listener(state)
            except Exception:
                pass


class KeyboardListener(threading.Thread):
    """Listens for keyboard commands (p=pause, r=resume, s/q=stop) in interactive terminals."""

    def __init__(self, controller: DownloadController):
        super().__init__(daemon=True)
        self.controller = controller
        self.running = True

    def stop(self):
        self.running = False

    def run(self):
        if not sys.stdin or not hasattr(sys.stdin, "isatty") or not sys.stdin.isatty():
            return

        print("[Controls] Press 'p' to Pause | 'r' to Resume | 's' or 'q' to Stop\n")

        if sys.platform == "win32":
            try:
                import msvcrt
            except ImportError:
                return

            while self.running and not self.controller.is_stopped:
                try:
                    if msvcrt.kbhit():
                        ch = msvcrt.getch()
                        try:
                            key = ch.decode("utf-8", errors="ignore").lower()
                        except Exception:
                            continue
                        if key == "p":
                            self.controller.pause()
                        elif key in ("r", "c"):
                            self.controller.resume()
                        elif key in ("s", "q"):
                            self.controller.stop()
                            break
                        elif key == " ":
                            if self.controller.is_paused:
                                self.controller.resume()
                            else:
                                self.controller.pause()
                    time.sleep(0.08)
                except Exception:
                    break
        else:
            try:
                import select
            except ImportError:
                return

            while self.running and not self.controller.is_stopped:
                try:
                    rlist, _, _ = select.select([sys.stdin], [], [], 0.1)
                    if rlist:
                        line = sys.stdin.readline().strip().lower()
                        if "p" in line:
                            self.controller.pause()
                        elif "r" in line or "c" in line:
                            self.controller.resume()
                        elif "s" in line or "q" in line:
                            self.controller.stop()
                            break
                except Exception:
                    break


class PrefetchItemStream:
    """Streams feed items while detecting total available items in real time."""

    def __init__(
        self,
        source_iterable,
        controller: DownloadController,
        known_shortcodes: Optional[set] = None,
        max_buffer: int = 5000,
    ):
        self.source = source_iterable
        self.controller = controller
        self.known_shortcodes = known_shortcodes or set()
        self.queue: queue.Queue = queue.Queue(maxsize=max_buffer)
        self.detected_count = 0
        self.done = False
        self.error: Optional[Exception] = None
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def _worker(self):
        try:
            for item in self.source:
                if self.controller.is_stopped:
                    break
                self.controller.wait_if_paused()
                self.detected_count += 1
                self.controller.stats.update(total_detected=self.detected_count)
                self.queue.put(item)
        except Exception as exc:
            self.error = exc
        finally:
            self.done = True
            self.queue.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            if self.controller.is_stopped:
                raise StopIteration
            self.controller.wait_if_paused()
            try:
                item = self.queue.get(timeout=0.2)
                if item is None:
                    if self.error:
                        raise self.error
                    raise StopIteration
                return item
            except queue.Empty:
                if self.done:
                    if self.error:
                        raise self.error
                    raise StopIteration
