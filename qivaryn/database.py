"""SQLite state persistence, scoped per platform.

Download history is keyed by ``(platform, item_id)`` so one state database can
track every supported service without identifiers from different sites
colliding.  Databases created before multi-platform support are upgraded in
place; their existing rows are attributed to Instagram.
"""

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional, Set, Tuple

from .files import MEDIA_EXTENSIONS, file_is_present

SCHEMA_VERSION = 2

_MEDIA_COLUMNS = (
    "platform TEXT NOT NULL DEFAULT 'instagram'",
    "shortcode TEXT NOT NULL",
    "media_type TEXT",
    "timestamp TEXT",
    "file_path TEXT",
    "downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
)

_USER_COLUMNS = (
    "platform TEXT NOT NULL DEFAULT 'instagram'",
    "username TEXT NOT NULL",
    "user_id TEXT",
    "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
)


class StateDatabase:
    """Manages SQLite state persistence with WAL mode and an in-memory cache."""

    def __init__(self, db_path: str = "qivaryn_state.db"):
        self.db_path = db_path
        self._downloaded_cache: Set[Tuple[str, str]] = set()
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Opens a connection and always closes it.

        ``with sqlite3.connect(...)`` only commits the transaction; it leaves the
        connection (and therefore the database file lock) open.  Closing
        explicitly keeps the state database from locking files on disk.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _init_db(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)) or ".", exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            self._create_schema(conn)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                self._migrate(conn)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION};")
            conn.commit()
        self.reload_cache()

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        cursor = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        )
        return cursor.fetchone() is not None

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> Set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

    def _create_schema(self, conn: sqlite3.Connection):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS downloaded_media ("
            f"{', '.join(_MEDIA_COLUMNS)}, PRIMARY KEY (platform, shortcode))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cached_users ("
            f"{', '.join(_USER_COLUMNS)}, PRIMARY KEY (platform, username))"
        )

    def _migrate(self, conn: sqlite3.Connection):
        """Rebuilds pre-multi-platform tables with the new composite keys."""
        conn.execute("BEGIN")
        try:
            if (
                self._table_exists(conn, "downloaded_media")
                and "platform" not in self._table_columns(conn, "downloaded_media")
            ):
                conn.execute("ALTER TABLE downloaded_media RENAME TO downloaded_media_legacy")
                self._create_schema(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO downloaded_media "
                    "(platform, shortcode, media_type, timestamp, file_path, downloaded_at) "
                    "SELECT 'instagram', shortcode, media_type, timestamp, file_path, downloaded_at "
                    "FROM downloaded_media_legacy"
                )
                conn.execute("DROP TABLE downloaded_media_legacy")

            if (
                self._table_exists(conn, "cached_users")
                and "platform" not in self._table_columns(conn, "cached_users")
            ):
                conn.execute("ALTER TABLE cached_users RENAME TO cached_users_legacy")
                self._create_schema(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO cached_users "
                    "(platform, username, user_id, updated_at) "
                    "SELECT 'instagram', username, user_id, updated_at FROM cached_users_legacy"
                )
                conn.execute("DROP TABLE cached_users_legacy")

            self._create_schema(conn)
        except Exception:
            conn.rollback()
            raise

    def reload_cache(self):
        """Synchronises the in-memory cache with SQLite."""
        with self._connect() as conn:
            cursor = conn.execute("SELECT platform, shortcode FROM downloaded_media")
            self._downloaded_cache = {(row[0], row[1]) for row in cursor.fetchall()}

    # ------------------------------------------------------------------
    # Media history
    # ------------------------------------------------------------------
    def is_downloaded(self, shortcode: str, platform: str = "instagram") -> bool:
        """O(1) in-memory lookup to verify if a media item already exists."""
        return (platform, str(shortcode)) in self._downloaded_cache

    def get_file_path(self, shortcode: str, platform: str = "instagram") -> Optional[str]:
        """Retrieves the stored file path for a downloaded item."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT file_path FROM downloaded_media WHERE platform = ? AND shortcode = ?",
                (platform, str(shortcode)),
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def get_all_downloaded_shortcodes(self, platform: str = "instagram") -> Set[str]:
        """Returns the set of known item identifiers for one platform."""
        return {shortcode for owner, shortcode in self._downloaded_cache if owner == platform}

    def mark_downloaded(
        self,
        shortcode: str,
        media_type: str,
        timestamp: str,
        file_path: str,
        platform: str = "instagram",
    ):
        """Records a successfully downloaded item in SQLite and the cache."""
        key = (platform, str(shortcode))
        self._downloaded_cache.add(key)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO downloaded_media "
                "(platform, shortcode, media_type, timestamp, file_path) "
                "VALUES (?, ?, ?, ?, ?)",
                (platform, str(shortcode), media_type, timestamp, file_path),
            )
            conn.commit()

    def remove_record(self, shortcode: str, platform: str = "instagram") -> bool:
        """Removes one record without touching the media file."""
        self._downloaded_cache.discard((platform, str(shortcode)))
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM downloaded_media WHERE platform = ? AND shortcode = ?",
                (platform, str(shortcode)),
            )
            conn.commit()
            return cursor.rowcount > 0

    def clear_all(self, platform: Optional[str] = None, clear_user_cache: bool = False) -> int:
        """Clears download history, optionally limited to one platform."""
        if platform is None:
            count = len(self._downloaded_cache)
            self._downloaded_cache.clear()
        else:
            count = len(self.get_all_downloaded_shortcodes(platform))
            self._downloaded_cache = {
                key for key in self._downloaded_cache if key[0] != platform
            }

        with self._connect() as conn:
            if platform is None:
                conn.execute("DELETE FROM downloaded_media")
            else:
                conn.execute("DELETE FROM downloaded_media WHERE platform = ?", (platform,))
            if clear_user_cache:
                conn.execute("DELETE FROM cached_users")
            conn.commit()
        return count

    def get_record_count(self, platform: Optional[str] = None) -> int:
        """Returns the number of tracked records, optionally for one platform."""
        if platform is None:
            return len(self._downloaded_cache)
        return len(self.get_all_downloaded_shortcodes(platform))

    def prune_missing(
        self, base_output_dir: Optional[str] = None, platform: str = "instagram"
    ) -> int:
        """Removes records whose media files no longer exist on disk."""
        removed_count = 0
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT shortcode, file_path FROM downloaded_media WHERE platform = ?",
                (platform,),
            )
            rows = cursor.fetchall()

            for shortcode, file_path in rows:
                is_present = False
                if file_path and file_path != "carousel_bundle" and file_is_present(file_path):
                    is_present = True
                elif base_output_dir and os.path.exists(base_output_dir):
                    # Carousel bundles are recorded without a concrete path, so fall
                    # back to probing the output directory for their slides.
                    stems = [shortcode]
                    if platform != "instagram":
                        stems.insert(0, f"{platform}_{shortcode}")
                    for stem in stems:
                        for ext in MEDIA_EXTENSIONS:
                            cand = os.path.join(base_output_dir, f"{stem}{ext}")
                            cand_carousel = os.path.join(base_output_dir, f"{stem}_1{ext}")
                            if file_is_present(cand) or file_is_present(cand_carousel):
                                is_present = True
                                break
                        if is_present:
                            break

                if not is_present:
                    cursor.execute(
                        "DELETE FROM downloaded_media WHERE platform = ? AND shortcode = ?",
                        (platform, shortcode),
                    )
                    self._downloaded_cache.discard((platform, shortcode))
                    removed_count += 1
            conn.commit()
        return removed_count

    # ------------------------------------------------------------------
    # Account ID cache
    # ------------------------------------------------------------------
    def get_cached_user_id(self, username: str, platform: str = "instagram") -> Optional[str]:
        """Retrieves a cached account ID for one platform."""
        with self._connect() as conn:
            cursor = conn.execute(
                "SELECT user_id FROM cached_users WHERE platform = ? AND username = ?",
                (platform, str(username).lower()),
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def cache_user_id(self, username: str, user_id: str, platform: str = "instagram"):
        """Caches an account ID to avoid a redundant profile request."""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cached_users (platform, username, user_id) "
                "VALUES (?, ?, ?)",
                (platform, str(username).lower(), str(user_id)),
            )
            conn.commit()
