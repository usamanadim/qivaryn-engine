"""Tests for the platform/account folder layout used for downloads."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from qivaryn.downloader import DEFAULT_OUTPUT_DIR, QivarynEngine


class OutputLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.output_dir = os.path.join(self.temp_dir.name, "Downloads", "Qivaryn")

    def _engine(self, platform):
        with patch("qivaryn.downloader.StateDatabase", return_value=MagicMock()):
            return QivarynEngine(
                platform=platform,
                output_dir=self.output_dir,
                db_path=":memory:",
            )

    def test_the_default_root_is_downloads_qivaryn(self):
        self.assertEqual(
            DEFAULT_OUTPUT_DIR, os.path.join("Downloads", "Qivaryn")
        )

    def test_each_platform_gets_its_own_folder(self):
        engine = self._engine("youtube")
        directory = engine._item_directory({"id": "abc", "account": "Some Channel"})

        self.assertEqual(
            directory,
            os.path.join(self.output_dir, "YouTube", "Some Channel"),
        )
        self.assertTrue(os.path.isdir(directory))

    def test_instagram_splits_posts_reels_and_carousels(self):
        engine = self._engine("instagram")
        base = os.path.join(self.output_dir, "Instagram", "someone")

        self.assertEqual(
            engine._item_directory({"id": "a", "account": "someone", "media_type": "Reel"}),
            os.path.join(base, "reels"),
        )
        self.assertEqual(
            engine._item_directory({"id": "b", "account": "someone", "media_type": "Post"}),
            os.path.join(base, "posts"),
        )
        self.assertEqual(
            engine._item_directory({"id": "c", "account": "someone", "media_type": "Carousel"}),
            os.path.join(base, "carousels"),
        )

    def test_items_without_an_account_stay_in_the_platform_folder(self):
        engine = self._engine("tiktok")
        directory = engine._item_directory({"id": "abc", "account": ""})

        self.assertEqual(directory, os.path.join(self.output_dir, "TikTok"))
        self.assertTrue(os.path.isdir(directory))

    def test_the_requested_account_is_used_when_the_item_has_none(self):
        engine = self._engine("facebook")
        engine._current_account = "https://www.facebook.com/NatGeo"

        directory = engine._item_directory({"id": "abc"})

        self.assertEqual(directory, os.path.join(self.output_dir, "Facebook", "NatGeo"))

    def test_existing_media_is_found_inside_the_account_folders(self):
        engine = self._engine("instagram")
        engine.db.is_downloaded.return_value = False
        target = os.path.join(
            self.output_dir, "Instagram", "someone", "posts", "ABC123.jpg"
        )
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(b"image")

        self.assertTrue(engine._is_item_already_saved("ABC123"))
        engine.db.mark_downloaded.assert_called_once()

    def test_youtube_files_are_found_under_the_channel_folder(self):
        engine = self._engine("youtube")
        engine.db.is_downloaded.return_value = False
        target = os.path.join(
            self.output_dir, "YouTube", "Some Channel", "youtube_ABC123_Title.mp4"
        )
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(b"video")

        self.assertTrue(engine._is_item_already_saved("ABC123"))


if __name__ == "__main__":
    unittest.main()
