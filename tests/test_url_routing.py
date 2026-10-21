import unittest
from unittest.mock import MagicMock

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

    def test_account_url_splits_the_count_across_feeds(self):
        from qivaryn.platforms.base import AccountFeed

        engine = QivarynEngine.__new__(QivarynEngine)
        engine.platform = "instagram"
        engine.client = MagicMock()
        engine.client.feeds_for.return_value = (
            AccountFeed("account feed", "posts", MagicMock()),
            AccountFeed("Reels tab", "reels", MagicMock()),
        )
        engine.client.account_label.return_value = "@alice"
        engine.db = MagicMock()
        engine.db.get_cached_user_id.return_value = "123"
        engine._download_feed = MagicMock(side_effect=[(4, 0), (1, 0)])

        downloaded = engine.download_account_content("alice", count=5)

        self.assertEqual(downloaded, 5)
        counts = [call.kwargs["count"] for call in engine._download_feed.call_args_list]
        self.assertEqual(counts, [5, 1])


if __name__ == "__main__":
    unittest.main()
