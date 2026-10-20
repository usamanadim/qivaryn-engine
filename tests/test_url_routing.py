import unittest
from unittest.mock import MagicMock, patch

from qivaryn.client import InstagramClient
from qivaryn.downloader import QivarynEngine


class InstagramUrlDetectionTests(unittest.TestCase):
    def test_detects_post_reel_profile_reels_tab_and_account_urls(self):
        targets = {
            "https://www.instagram.com/p/POSTCODE/": ("single", "POSTCODE"),
            "https://instagram.com/reel/REELCODE/?igsh=abc": ("single", "REELCODE"),
            "https://www.instagram.com/alice/reels/": ("reels", "alice"),
            "https://www.instagram.com/alice/": ("account", "alice"),
        }

        for url, expected in targets.items():
            with self.subTest(url=url):
                self.assertEqual(InstagramClient.detect_url(url), expected)

    def test_does_not_claim_urls_from_other_hosts_or_unknown_paths(self):
        self.assertIsNone(InstagramClient.detect_url("https://example.com/alice/"))
        self.assertIsNone(InstagramClient.detect_url("https://www.instagram.com/alice/tagged/"))


class InstagramReelsFeedTests(unittest.TestCase):
    def test_reels_feed_unwraps_media_and_follows_cursor(self):
        client = InstagramClient(allow_network_fallbacks=False)
        first_page = MagicMock()
        first_page.status_code = 200
        first_page.json.return_value = {
            "items": [{"media": {"code": "REEL_1"}}, {"media": {"code": "REEL_2"}}],
            "more_available": True,
            "next_max_id": "next-page",
        }
        second_page = MagicMock()
        second_page.status_code = 200
        second_page.json.return_value = {
            "items": [{"media": {"code": "REEL_3"}}],
            "more_available": False,
        }
        client.session.post = MagicMock(side_effect=[first_page, second_page])

        items = list(client.fetch_user_raw_reels_paginated("123"))

        self.assertEqual([item["code"] for item in items], ["REEL_1", "REEL_2", "REEL_3"])
        self.assertEqual(client.session.post.call_args_list[0].kwargs["data"], {
            "target_user_id": "123",
            "page_size": 12,
        })
        self.assertEqual(client.session.post.call_args_list[1].kwargs["data"], {
            "target_user_id": "123",
            "page_size": 12,
            "max_id": "next-page",
        })

    def test_account_url_combines_post_and_reels_counts(self):
        engine = QivarynEngine.__new__(QivarynEngine)
        with patch.object(engine, "download_account", return_value=4) as posts, \
             patch.object(engine, "download_account_reels", return_value=1) as reels:
            downloaded = engine.download_account_content("alice", count=5)

        self.assertEqual(downloaded, 5)
        posts.assert_called_once_with(
            "alice", media_type_filter="posts", max_pages=None, force=False, count=5
        )
        reels.assert_called_once_with(
            "alice", max_pages=None, force=False, count=1
        )


if __name__ == "__main__":
    unittest.main()
