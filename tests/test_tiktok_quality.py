"""Tests for the TikTok rendition selection.

TikTok lists several renditions per post and the one with the highest bitrate
is usually the smaller 576x1024 stream, so the resolution has to decide.
"""

import unittest
from unittest.mock import patch

from qivaryn.platforms.tiktok import TikTokClient


def _rendition(gear, width, height, bitrate, url):
    return {
        "GearName": gear,
        "Bitrate": bitrate,
        "PlayAddr": {
            "Width": width,
            "Height": height,
            "UrlList": [url],
        },
    }


def _video(renditions=(), play_addr="https://cdn.example/standard.mp4"):
    video = {"playAddr": play_addr}
    if renditions:
        video["bitrateInfo"] = list(renditions)
    return video


class RenditionSelectionTests(unittest.TestCase):
    def setUp(self):
        self.client = TikTokClient(allow_network_fallbacks=False)

    def test_the_largest_resolution_wins_over_the_highest_bitrate(self):
        video = _video([
            _rendition("lower_540_0", 576, 1024, 887998, "https://cdn.example/540.mp4"),
            _rendition("adapt_lower_720_1", 720, 1280, 609981, "https://cdn.example/720.mp4"),
            _rendition("adapt_540_1", 576, 1024, 519962, "https://cdn.example/540low.mp4"),
        ])

        url, quality = self.client._best_video_url(video)

        self.assertEqual(url, "https://cdn.example/720.mp4")
        self.assertIn("720x1280", quality)
        self.assertIn("609 kbps", quality)

    def test_equal_resolutions_prefer_the_higher_bitrate(self):
        video = _video([
            _rendition("adapt_540_1", 576, 1024, 519962, "https://cdn.example/low.mp4"),
            _rendition("lower_540_0", 576, 1024, 887998, "https://cdn.example/high.mp4"),
        ])

        url, _quality = self.client._best_video_url(video)

        self.assertEqual(url, "https://cdn.example/high.mp4")

    def test_playaddr_is_used_when_no_renditions_are_listed(self):
        url, quality = self.client._best_video_url(_video())

        self.assertEqual(url, "https://cdn.example/standard.mp4")
        self.assertEqual(quality, "default stream")

    def test_renditions_without_a_size_still_win(self):
        video = _video([
            {"GearName": "x", "Bitrate": 100, "PlayAddr": {"UrlList": ["https://cdn.example/a.mp4"]}},
            _rendition("lower_540_0", 576, 1024, 50, "https://cdn.example/b.mp4"),
        ])

        url, _quality = self.client._best_video_url(video)

        self.assertEqual(url, "https://cdn.example/b.mp4")


class SingleItemRenditionLookupTests(unittest.TestCase):
    def setUp(self):
        self.client = TikTokClient(allow_network_fallbacks=False)
        self.renditions = [
            _rendition("lower_540_0", 576, 1024, 887998, "https://cdn.example/540.mp4"),
            _rendition("adapt_lower_720_1", 720, 1280, 609981, "https://cdn.example/720.mp4"),
        ]
        self.item = {
            "id": "7677918565869161749",
            "desc": "a post",
            "createTime": 1700000000,
            "author": {"uniqueId": "someone", "secUid": "SECUID"},
            "video": _video(),
        }

    def _fetch(self):
        with patch.object(self.client, "_fetch_page", return_value="<html></html>"), \
             patch.object(self.client, "_item_from_html", return_value=self.item):
            return self.client.fetch_item("7677918565869161749")

    def test_a_watch_page_without_renditions_uses_the_feed_lookup(self):
        with patch.object(
            self.client, "_renditions_from_feed", return_value=self.renditions
        ) as lookup:
            item = self._fetch()

        lookup.assert_called_once()
        self.assertEqual(item["video_url"], "https://cdn.example/720.mp4")
        self.assertIn("720x1280", item["quality"])

    def test_the_lookup_is_skipped_when_the_page_already_lists_renditions(self):
        self.item["video"]["bitrateInfo"] = list(self.renditions)

        with patch.object(
            self.client, "_renditions_from_feed", return_value=self.renditions
        ) as lookup:
            item = self._fetch()

        lookup.assert_not_called()
        self.assertEqual(item["video_url"], "https://cdn.example/720.mp4")

    def test_a_failed_lookup_falls_back_to_the_standard_stream(self):
        with patch.object(self.client, "_renditions_from_feed", return_value=[]):
            item = self._fetch()

        self.assertEqual(item["video_url"], "https://cdn.example/standard.mp4")
        self.assertIn("default stream", item["quality"])

    def test_the_lookup_result_is_cached(self):
        with patch.object(
            self.client, "_renditions_from_feed", return_value=self.renditions
        ) as lookup:
            self._fetch()
            self._fetch()

        lookup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
