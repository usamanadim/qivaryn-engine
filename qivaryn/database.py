import sqlite3
import os
from typing import Optional, Set

class StateDatabase:
    """Manages SQLite state persistence with WAL mode and in-memory set caching for instantaneous lookups."""
    def __init__(self, db_path: str = "qivaryn_state.db"):
        self.db_path = db_path
        self._downloaded_cache: Set[str] = set()
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)) or ".", exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS downloaded_media (
                    shortcode TEXT PRIMARY KEY,
                    media_type TEXT,
                    timestamp TEXT,
                    file_path TEXT,
                    downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cached_users (
                    username TEXT PRIMARY KEY,
                    user_id TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            cursor = conn.cursor()
            cursor.execute("SELECT shortcode FROM downloaded_media")
            self._downloaded_cache = {row[0] for row in cursor.fetchall()}

    def reload_cache(self):
        """Synchronizes in-memory cache with SQLite database."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT shortcode FROM downloaded_media")
            self._downloaded_cache = {row[0] for row in cursor.fetchall()}

    def is_downloaded(self, shortcode: str) -> bool:
        """O(1) in-memory lookup to verify if media item already exists without disk I/O."""
        return shortcode in self._downloaded_cache

    def get_file_path(self, shortcode: str) -> Optional[str]:
        """Retrieves stored file path for a downloaded item."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT file_path FROM downloaded_media WHERE shortcode = ?", (shortcode,))
            row = cursor.fetchone()
            return row[0] if row else None

    def get_all_downloaded_shortcodes(self) -> Set[str]:
        """Returns set of all known downloaded shortcodes."""
        return set(self._downloaded_cache)

    def mark_downloaded(self, shortcode: str, media_type: str, timestamp: str, file_path: str):
        """Records successfully downloaded items in SQLite and active in-memory cache."""
        self._downloaded_cache.add(shortcode)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO downloaded_media (shortcode, media_type, timestamp, file_path)
                VALUES (?, ?, ?, ?)
            """, (shortcode, media_type, timestamp, file_path))
            conn.commit()

    def get_cached_user_id(self, username: str) -> Optional[str]:
        """Retrieves user ID from local SQLite cache."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM cached_users WHERE username = ?", (username.lower(),))
            row = cursor.fetchone()
            return row[0] if row else None

    def cache_user_id(self, username: str, user_id: str):
        """Caches user ID locally to avoid redundant profile API requests."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO cached_users (username, user_id)
                VALUES (?, ?)
            """, (username.lower(), str(user_id)))
            conn.commit()

    def remove_record(self, shortcode: str) -> bool:
        """Removes a specific shortcode record from database and in-memory cache without touching media files."""
        self._downloaded_cache.discard(shortcode)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM downloaded_media WHERE shortcode = ?", (shortcode,))
            conn.commit()
            return cursor.rowcount > 0

    def clear_all(self, clear_user_cache: bool = False) -> int:
        """Clears all media download history records from database without deleting media files."""
        count = len(self._downloaded_cache)
        self._downloaded_cache.clear()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM downloaded_media")
            if clear_user_cache:
                cursor.execute("DELETE FROM cached_users")
            conn.commit()
        return count

    def get_record_count(self) -> int:
        """Returns total count of tracked downloaded records."""
        return len(self._downloaded_cache)

    def prune_missing(self, base_output_dir: Optional[str] = None) -> int:
        """Scans database records and removes entries whose media files were deleted from disk."""
        removed_count = 0
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT shortcode, file_path FROM downloaded_media")
            rows = cursor.fetchall()

            for shortcode, file_path in rows:
                is_present = False
                if file_path and os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                    is_present = True
                elif base_output_dir and os.path.exists(base_output_dir):
                    for ext in [".mp4", ".jpg", ".jpeg", ".webp"]:
                        cand = os.path.join(base_output_dir, f"{shortcode}{ext}")
                        cand_carousel = os.path.join(base_output_dir, f"{shortcode}_1{ext}")
                        if (os.path.exists(cand) and os.path.getsize(cand) > 0) or \
                           (os.path.exists(cand_carousel) and os.path.getsize(cand_carousel) > 0):
                            is_present = True
                            break

                if not is_present:
                    cursor.execute("DELETE FROM downloaded_media WHERE shortcode = ?", (shortcode,))
                    self._downloaded_cache.discard(shortcode)
                    removed_count += 1
            conn.commit()
        return removed_count