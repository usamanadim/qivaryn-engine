import unittest
from unittest.mock import MagicMock, patch

from qivaryn.client import InstagramClient
from qivaryn.downloader import QivarynEngine


class BatchDownloadTests(unittest.TestCase):
    def setUp(self):
        self.client = InstagramClient(allow_network_fallbacks=False)
        # Feeds are replaced per test; never let a real request through.
        self.client.fetch_user_raw_posts_paginated = MagicMock(return_value=[])
        self.client.fetch_user_raw_reels_paginated = MagicMock(return_value=[])

        self.mock_db = MagicMock()
        self.mock_db.get_cached_user_id.return_value = "12345"
        self.mock_db.get_all_downloaded_shortcodes.return_value = set()

        with patch("qivaryn.downloader.StateDatabase", return_value=self.mock_db):
            self.engine = QivarynEngine(output_dir="./test_downloads", db_path=":memory:")
            self.engine.db = self.mock_db
            self.engine.client = self.client

    @staticmethod
    def _raw_items(count):
        return [
            {
                "code": f"ITEM_{i}",
                "media_type": 2,
                "video_versions": [{"url": f"http://example.com/v{i}.mp4"}],
            }
            for i in range(count)
        ]

    @staticmethod
    def _normalized(raw):
        return {
            "id": raw["code"],
            "shortcode": raw["code"],
            "media_type": "Reel",
            "video_url": "http://example.com/v.mp4",
            "audio_url": None,
            "timestamp": "2026-01-01",
            "quality": "1080p",
        }

    def test_downloads_exact_count_when_more_items_available(self):
        self.client.fetch_user_raw_posts_paginated.return_value = self._raw_items(10)
        self.client.normalize_item = MagicMock(side_effect=self._normalized)

        with patch.object(self.engine, "_download_media_item", return_value=True) as mock_download:
            self.engine.download_account("testuser", count=4)
            self.assertEqual(mock_download.call_count, 4)
            downloaded_codes = [call.args[0]["id"] for call in mock_download.call_args_list]
            self.assertEqual(downloaded_codes, ["ITEM_0", "ITEM_1", "ITEM_2", "ITEM_3"])

    def test_skips_already_downloaded_items_and_downloads_next_new_items(self):
        # Items 0-3 were downloaded previously (4 items > default threshold 3).
        self.mock_db.get_all_downloaded_shortcodes.return_value = {
            "ITEM_0", "ITEM_1", "ITEM_2", "ITEM_3",
        }
        self.client.fetch_user_raw_posts_paginated.return_value = self._raw_items(10)
        self.client.normalize_item = MagicMock(side_effect=self._normalized)

        with patch.object(self.engine, "_download_media_item", return_value=True) as mock_download:
            # Request 3 new items; it should skip ITEM_0..3 and download ITEM_4..6.
            self.engine.download_account("testuser", count=3)
            self.assertEqual(mock_download.call_count, 3)
            downloaded_codes = [call.args[0]["id"] for call in mock_download.call_args_list]
            self.assertEqual(downloaded_codes, ["ITEM_4", "ITEM_5", "ITEM_6"])

    def test_filter_by_reels_and_videos(self):
        raw_items = [
            {"code": "PHOTO_1", "media_type": 1},
            {"code": "REEL_1", "media_type": 2, "video_versions": []},
            {"code": "CAROUSEL_1", "media_type": 8, "carousel_media": []},
            {"code": "REEL_2", "media_type": 2, "video_versions": []},
            {"code": "REEL_3", "media_type": 2, "video_versions": []},
        ]
        self.client.fetch_user_raw_reels_paginated.return_value = raw_items
        self.client.normalize_item = MagicMock(side_effect=self._normalized)

        with patch.object(self.engine, "_download_media_item", return_value=True) as mock_download:
            self.engine.download_account("testuser", media_type_filter="reels", count=2)
            self.assertEqual(mock_download.call_count, 2)
            downloaded_codes = [call.args[0]["id"] for call in mock_download.call_args_list]
            self.assertEqual(downloaded_codes, ["REEL_1", "REEL_2"])

    def test_filter_by_carousels(self):
        raw_items = [
            {"code": "PHOTO_1", "media_type": 1},
            {"code": "REEL_1", "media_type": 2, "video_versions": []},
            {"code": "CAROUSEL_1", "media_type": 8, "carousel_media": []},
            {"code": "CAROUSEL_2", "media_type": 8, "carousel_media": []},
        ]

        def normalize(raw):
            media_type = "Carousel" if raw["media_type"] == 8 else (
                "Reel" if raw["media_type"] == 2 else "Photo"
            )
            return {
                "id": raw["code"],
                "shortcode": raw["code"],
                "media_type": media_type,
                "timestamp": "2026-01-01",
                "quality": "HQ",
            }

        self.client.fetch_user_raw_posts_paginated.return_value = raw_items
        self.client.normalize_item = MagicMock(side_effect=normalize)

        with patch.object(self.engine, "_download_media_item", return_value=True) as mock_download:
            self.engine.download_account("testuser", media_type_filter="carousels", count=2)
            self.assertEqual(mock_download.call_count, 2)
            downloaded_codes = [call.args[0]["id"] for call in mock_download.call_args_list]
            self.assertEqual(downloaded_codes, ["CAROUSEL_1", "CAROUSEL_2"])

    def test_stops_gracefully_when_feed_ends_before_reaching_count(self):
        self.client.fetch_user_raw_posts_paginated.return_value = self._raw_items(2)
        self.client.normalize_item = MagicMock(side_effect=self._normalized)

        with patch.object(self.engine, "_download_media_item", return_value=True) as mock_download:
            # Request 50 items when only 2 exist.
            self.engine.download_account("testuser", count=50)
            self.assertEqual(mock_download.call_count, 2)

    def test_no_early_exit_scans_past_threshold_items(self):
        self.engine.consecutive_threshold = 0
        self.mock_db.get_all_downloaded_shortcodes.return_value = {
            "ITEM_0", "ITEM_1", "ITEM_2", "ITEM_3", "ITEM_4"
        }
        self.client.fetch_user_raw_posts_paginated.return_value = self._raw_items(7)
        self.client.normalize_item = MagicMock(side_effect=self._normalized)

        with patch.object(self.engine, "_download_media_item", return_value=True) as mock_download:
            self.engine.download_account("testuser", count=None)
            self.assertEqual(mock_download.call_count, 2)
            downloaded = [call.args[0]["id"] for call in mock_download.call_args_list]
            self.assertEqual(downloaded, ["ITEM_5", "ITEM_6"])

    def test_reels_pagination_survives_intermediate_empty_page(self):
        from qivaryn.client import InstagramClient
        client = InstagramClient(allow_network_fallbacks=False)

        resp1 = MagicMock(status_code=200)
        resp1.json.return_value = {
            "items": [{"media": {"code": "REEL_1"}}],
            "paging_info": {"more_available": True, "max_id": "cur2"}
        }

        resp2 = MagicMock(status_code=200)
        resp2.json.return_value = {
            "items": [],
            "paging_info": {"more_available": True, "max_id": "cur3"}
        }

        resp3 = MagicMock(status_code=200)
        resp3.json.return_value = {
            "items": [{"media": {"code": "REEL_2"}}],
            "paging_info": {"more_available": False}
        }

        with patch.object(client.session, "post", side_effect=[resp1, resp2, resp3]):
            yielded = list(client.fetch_user_raw_reels_paginated("123"))

        self.assertEqual(len(yielded), 2)
        self.assertEqual(yielded[0]["code"], "REEL_1")
        self.assertEqual(yielded[1]["code"], "REEL_2")


class ScanAccountTests(unittest.TestCase):
    """Tests for the scan_account / scan_url feature."""

    def setUp(self):
        self.client = MagicMock()
        self.client.item_id = lambda raw: raw.get("code")
        self.client.prefilter = lambda raw, _: True
        self.client.account_label = lambda a: f"@{a}"
        self.client.detect_url = MagicMock(return_value=("reels", "testuser"))
        self.client.default_filter_for = MagicMock(return_value="all")

        self.mock_db = MagicMock()
        self.mock_db.get_cached_user_id.return_value = "12345"
        self.mock_db.get_all_downloaded_shortcodes.return_value = {"ITEM_0", "ITEM_1"}
        self.mock_db.get_file_path.return_value = None

        self.engine = QivarynEngine.__new__(QivarynEngine)
        self.engine.platform = "instagram"
        self.engine.output_dir = "./downloads"
        self.engine.db = self.mock_db
        self.engine.consecutive_threshold = 0
        self.engine.client = self.client
        self.engine._current_account = ""
        self.engine._output_scan = None

    def _raw_items(self, n):
        return [{"code": f"ITEM_{i}"} for i in range(n)]

    def test_scan_account_counts_total_and_downloaded(self):
        raw = self._raw_items(5)
        feed = MagicMock()
        feed.name = "Reels tab"
        feed.media_filter = "reels"
        feed.fetch = MagicMock(return_value=iter(raw))
        self.client.feeds_for = MagicMock(return_value=(feed,))

        result = self.engine.scan_account("testuser", media_type_filter="reels")

        self.assertEqual(result["total"], 5)
        self.assertEqual(result["already_downloaded"], 2)  # ITEM_0, ITEM_1
        self.assertEqual(result["remaining"], 3)

    def test_scan_url_routes_to_scan_account(self):
        feed = MagicMock()
        feed.name = "Reels tab"
        feed.media_filter = "reels"
        feed.fetch = MagicMock(return_value=iter(self._raw_items(3)))
        self.client.feeds_for = MagicMock(return_value=(feed,))

        result = self.engine.scan_url("https://www.instagram.com/testuser/reels/")

        self.assertEqual(result["total"], 3)
        self.assertEqual(result["type"], "reels")

    def test_scan_url_single_item(self):
        self.client.detect_url.return_value = ("single", "ABC123")
        result = self.engine.scan_url("https://www.instagram.com/reel/ABC123/")
        self.assertEqual(result["type"], "single")
        self.assertEqual(result["total"], 1)

    def test_scan_empty_feed(self):
        feed = MagicMock()
        feed.name = "account feed"
        feed.media_filter = "posts"
        feed.fetch = MagicMock(return_value=iter([]))
        self.client.feeds_for = MagicMock(return_value=(feed,))

        result = self.engine.scan_account("testuser", media_type_filter="posts")

        self.assertEqual(result["total"], 0)
        self.assertEqual(result["already_downloaded"], 0)
        self.assertEqual(result["remaining"], 0)


if __name__ == "__main__":
    unittest.main()
