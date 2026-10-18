import unittest
from unittest.mock import MagicMock, patch

from qivaryn.downloader import QivarynEngine


class BatchDownloadTests(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_db = MagicMock()
        self.mock_db.get_cached_user_id.return_value = "12345"
        self.mock_db.get_all_downloaded_shortcodes.return_value = set()

        with patch("qivaryn.downloader.StateDatabase", return_value=self.mock_db), \
             patch("qivaryn.downloader.InstagramClient", return_value=self.mock_client):
            self.engine = QivarynEngine(output_dir="./test_downloads", db_path=":memory:")
            self.engine.db = self.mock_db
            self.engine.client = self.mock_client

    def test_downloads_exact_count_when_more_items_available(self):
        # Create 10 raw items
        raw_items = [
            {"code": f"ITEM_{i}", "media_type": 2, "video_versions": [{"url": f"http://example.com/v{i}.mp4"}]}
            for i in range(10)
        ]
        self.mock_client.fetch_user_raw_posts_paginated.return_value = raw_items
        self.mock_client._parse_mobile_media_item.side_effect = lambda raw: {
            "shortcode": raw["code"],
            "media_type": "Reel",
            "video_url": "http://example.com/v.mp4",
            "audio_url": None,
            "timestamp": "2026-01-01",
            "quality": "1080p",
        }

        with patch.object(self.engine, "_download_media_item", return_value=True) as mock_download:
            self.engine.download_account("testuser", count=4)
            self.assertEqual(mock_download.call_count, 4)
            downloaded_codes = [call.args[0]["shortcode"] for call in mock_download.call_args_list]
            self.assertEqual(downloaded_codes, ["ITEM_0", "ITEM_1", "ITEM_2", "ITEM_3"])

    def test_skips_already_downloaded_items_and_downloads_next_new_items(self):
        # Suppose items 0, 1, 2, 3 were already downloaded previously (4 items > default threshold 3)
        self.mock_db.get_all_downloaded_shortcodes.return_value = {"ITEM_0", "ITEM_1", "ITEM_2", "ITEM_3"}

        raw_items = [
            {"code": f"ITEM_{i}", "media_type": 2, "video_versions": [{"url": f"http://example.com/v{i}.mp4"}]}
            for i in range(10)
        ]
        self.mock_client.fetch_user_raw_posts_paginated.return_value = raw_items
        self.mock_client._parse_mobile_media_item.side_effect = lambda raw: {
            "shortcode": raw["code"],
            "media_type": "Reel",
            "video_url": "http://example.com/v.mp4",
            "audio_url": None,
            "timestamp": "2026-01-01",
            "quality": "1080p",
        }

        with patch.object(self.engine, "_download_media_item", return_value=True) as mock_download:
            # Request 3 new items; it should skip ITEM_0..3 and download ITEM_4, ITEM_5, ITEM_6
            self.engine.download_account("testuser", count=3)
            self.assertEqual(mock_download.call_count, 3)
            downloaded_codes = [call.args[0]["shortcode"] for call in mock_download.call_args_list]
            self.assertEqual(downloaded_codes, ["ITEM_4", "ITEM_5", "ITEM_6"])

    def test_filter_by_reels_and_videos(self):
        raw_items = [
            {"code": "PHOTO_1", "media_type": 1},
            {"code": "REEL_1", "media_type": 2, "video_versions": []},
            {"code": "CAROUSEL_1", "media_type": 8, "carousel_media": []},
            {"code": "REEL_2", "media_type": 2, "video_versions": []},
            {"code": "REEL_3", "media_type": 2, "video_versions": []},
        ]
        self.mock_client.fetch_user_raw_posts_paginated.return_value = raw_items

        def parse_item(raw):
            m_type = "Reel" if raw["media_type"] == 2 else ("Carousel" if raw["media_type"] == 8 else "Photo")
            return {
                "shortcode": raw["code"],
                "media_type": m_type,
                "timestamp": "2026-01-01",
                "quality": "HQ",
            }
        self.mock_client._parse_mobile_media_item.side_effect = parse_item

        with patch.object(self.engine, "_download_media_item", return_value=True) as mock_download:
            self.engine.download_account("testuser", media_type_filter="reels", count=2)
            self.assertEqual(mock_download.call_count, 2)
            downloaded_codes = [call.args[0]["shortcode"] for call in mock_download.call_args_list]
            self.assertEqual(downloaded_codes, ["REEL_1", "REEL_2"])

    def test_filter_by_carousels(self):
        raw_items = [
            {"code": "PHOTO_1", "media_type": 1},
            {"code": "REEL_1", "media_type": 2, "video_versions": []},
            {"code": "CAROUSEL_1", "media_type": 8, "carousel_media": []},
            {"code": "CAROUSEL_2", "media_type": 8, "carousel_media": []},
        ]
        self.mock_client.fetch_user_raw_posts_paginated.return_value = raw_items

        def parse_item(raw):
            m_type = "Carousel" if raw["media_type"] == 8 else ("Reel" if raw["media_type"] == 2 else "Photo")
            return {
                "shortcode": raw["code"],
                "media_type": m_type,
                "timestamp": "2026-01-01",
                "quality": "HQ",
            }
        self.mock_client._parse_mobile_media_item.side_effect = parse_item

        with patch.object(self.engine, "_download_media_item", return_value=True) as mock_download:
            self.engine.download_account("testuser", media_type_filter="carousels", count=2)
            self.assertEqual(mock_download.call_count, 2)
            downloaded_codes = [call.args[0]["shortcode"] for call in mock_download.call_args_list]
            self.assertEqual(downloaded_codes, ["CAROUSEL_1", "CAROUSEL_2"])

    def test_stops_gracefully_when_feed_ends_before_reaching_count(self):
        raw_items = [
            {"code": "REEL_1", "media_type": 2, "video_versions": []},
            {"code": "REEL_2", "media_type": 2, "video_versions": []},
        ]
        self.mock_client.fetch_user_raw_posts_paginated.return_value = raw_items
        self.mock_client._parse_mobile_media_item.return_value = {
            "shortcode": "REEL",
            "media_type": "Reel",
            "timestamp": "2026-01-01",
            "quality": "1080p",
        }

        with patch.object(self.engine, "_download_media_item", return_value=True) as mock_download:
            # Request 50 items when only 2 exist
            self.engine.download_account("testuser", count=50)
            self.assertEqual(mock_download.call_count, 2)


if __name__ == "__main__":
    unittest.main()
