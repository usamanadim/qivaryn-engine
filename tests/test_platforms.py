"""Tests for the native platform integrations and platform-scoped state."""

import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from qivaryn.cli import _platform_for_url, cli
from qivaryn.database import StateDatabase
from qivaryn.downloader import QivarynEngine
from qivaryn.platforms import (
    ALL_MEDIA_TYPES,
    PLATFORM_NAMES,
    FacebookClient,
    TikTokClient,
    YouTubeClient,
    get_client_class,
    supported_media_types,
)
from qivaryn.platforms.base import AccountFeed, make_item
from qivaryn.platforms.tiktok import MOBILE_USER_AGENT


class PlatformRegistryTests(unittest.TestCase):
    def test_all_documented_platforms_are_registered(self):
        for name in ("instagram", "tiktok", "youtube", "facebook"):
            with self.subTest(platform=name):
                self.assertIn(name, PLATFORM_NAMES)
                self.assertEqual(get_client_class(name).name, name)

    def test_media_types_are_subset_of_the_cli_union(self):
        for name in PLATFORM_NAMES:
            with self.subTest(platform=name):
                self.assertTrue(set(supported_media_types(name)) <= set(ALL_MEDIA_TYPES))

    def test_unknown_platform_raises_a_helpful_error(self):
        with self.assertRaises(KeyError) as ctx:
            get_client_class("myspace")
        self.assertIn("instagram", str(ctx.exception))


class YouTubeUrlDetectionTests(unittest.TestCase):
    def test_detects_supported_youtube_urls(self):
        targets = {
            "https://youtu.be/abcdefghijk": ("single", "abcdefghijk"),
            "https://www.youtube.com/watch?v=abcdefghijk": ("single", "abcdefghijk"),
            "https://www.youtube.com/shorts/abcdefghijk": ("single", "abcdefghijk"),
            "https://www.youtube.com/playlist?list=PL123456": ("playlist", "PL123456"),
            "https://www.youtube.com/@somechannel": ("account", "@somechannel"),
            "https://www.youtube.com/@somechannel/videos": ("account", "@somechannel/videos"),
            "https://www.youtube.com/@somechannel/shorts": ("account", "@somechannel/shorts"),
        }
        for url, expected in targets.items():
            with self.subTest(url=url):
                self.assertEqual(YouTubeClient.detect_url(url), expected)

    def test_channel_id_url_keeps_the_requested_tab(self):
        url = "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv/videos"
        self.assertEqual(
            YouTubeClient.detect_url(url),
            ("account", "channel/UCabcdefghijklmnopqrstuv/videos"),
        )

    def test_does_not_claim_other_hosts(self):
        self.assertIsNone(YouTubeClient.detect_url("https://example.com/watch?v=abcdefghijk"))

    def test_default_filter_matches_the_requested_tab(self):
        client = YouTubeClient(allow_network_fallbacks=False)
        self.assertEqual(client.default_filter_for("@channel/videos"), "videos")
        self.assertEqual(client.default_filter_for("@channel/shorts"), "shorts")
        self.assertEqual(client.default_filter_for("@channel"), "all")


class YouTubeParsingTests(unittest.TestCase):
    def setUp(self):
        self.client = YouTubeClient(allow_network_fallbacks=False)

    def test_grid_contents_extracts_items_and_continuation(self):
        data = {
            "contents": {
                "twoColumnBrowseResultsRenderer": {
                    "tabs": [
                        {
                            "tabRenderer": {
                                "content": {
                                    "richGridRenderer": {
                                        "contents": [
                                            {
                                                "richItemRenderer": {
                                                    "content": {
                                                        "videoRenderer": {"videoId": "abcdefghijk"}
                                                    }
                                                }
                                            },
                                            {
                                                "continuationItemRenderer": {
                                                    "continuationEndpoint": {
                                                        "continuationCommand": {"token": "TOKEN"}
                                                    }
                                                }
                                            },
                                        ]
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        }

        contents, token = self.client._grid_contents(data)
        self.assertEqual(token, "TOKEN")
        items = list(self.client._items_from_contents(contents, "videos"))
        self.assertEqual([item["videoId"] for item in items], ["abcdefghijk"])
        self.assertTrue(items[0]["isShort"] is False)

    def test_shorts_lockup_view_model_is_understood(self):
        entry = {
            "shortsLockupViewModel": {
                "entityId": "shorts-shelf-item-abcdefghijk",
                "overlayMetadata": {"primaryText": {"content": "Short"}},
            }
        }
        self.assertEqual(self.client._video_ids_in(entry), ["abcdefghijk"])

    def test_ciphered_formats_are_reported_as_unavailable(self):
        ciphered = {
            "signatureCipher": "url=https%3A%2F%2Fcdn.example%2Fv&s=signature&sp=sig",
            "mimeType": "video/mp4",
            "height": 720,
        }
        self.assertIsNone(self.client._format_url(ciphered))

    def test_url_without_a_signature_is_used_directly(self):
        ciphered = {"cipher": "url=https%3A%2F%2Fcdn.example%2Fv.mp4"}
        self.assertEqual(self.client._format_url(ciphered), "https://cdn.example/v.mp4")

    def test_adaptive_streams_are_merged_and_prefer_mp4(self):
        streaming = {
            "formats": [
                {
                    "url": "https://cdn.example/360.mp4",
                    "mimeType": 'video/mp4; codecs="avc1.42001E"',
                    "height": 360,
                    "bitrate": 500000,
                    "qualityLabel": "360p",
                }
            ],
            "adaptiveFormats": [
                {
                    "url": "https://cdn.example/1080.mp4",
                    "mimeType": 'video/mp4; codecs="avc1.640028"',
                    "height": 1080,
                    "bitrate": 4000000,
                    "qualityLabel": "1080p",
                },
                {
                    "url": "https://cdn.example/audio.m4a",
                    "mimeType": 'audio/mp4; codecs="mp4a.40.2"',
                    "bitrate": 128000,
                },
            ],
        }

        video_url, audio_url, quality, ext, headers, fallback = self.client._select_streams(
            streaming, "abcdefghijk"
        )
        self.assertEqual(video_url, "https://cdn.example/1080.mp4")
        self.assertEqual(audio_url, "https://cdn.example/audio.m4a")
        self.assertEqual(quality, "1080p")
        self.assertEqual(ext, "mp4")
        self.assertIn("Referer", headers)
        self.assertIsNone(fallback)

    @staticmethod
    def _quality_streaming():
        return {
            "formats": [
                {
                    "url": "https://cdn.example/360.mp4",
                    "mimeType": 'video/mp4; codecs="avc1.42001E"',
                    "height": 360,
                    "bitrate": 500000,
                    "qualityLabel": "360p",
                }
            ],
            "adaptiveFormats": [
                {
                    "url": "https://cdn.example/144.mp4",
                    "mimeType": 'video/mp4; codecs="avc1.42001E"',
                    "height": 144,
                    "bitrate": 200000,
                    "qualityLabel": "144p",
                },
                {
                    "url": "https://cdn.example/480.mp4",
                    "mimeType": 'video/mp4; codecs="avc1.64001F"',
                    "height": 480,
                    "bitrate": 1000000,
                    "qualityLabel": "480p",
                },
                {
                    "url": "https://cdn.example/720.mp4",
                    "mimeType": 'video/mp4; codecs="avc1.64001F"',
                    "height": 720,
                    "bitrate": 2500000,
                    "qualityLabel": "720p",
                },
                {
                    "url": "https://cdn.example/1080.mp4",
                    "mimeType": 'video/mp4; codecs="avc1.640028"',
                    "height": 1080,
                    "bitrate": 4000000,
                    "qualityLabel": "1080p",
                },
                {
                    "url": "https://cdn.example/audio.m4a",
                    "mimeType": 'audio/mp4; codecs="mp4a.40.2"',
                    "bitrate": 128000,
                },
            ],
        }

    def test_quality_720_selects_the_matching_adaptive_stream(self):
        client = YouTubeClient(allow_network_fallbacks=False, quality="720")

        video_url, audio_url, quality, _ext, _headers, fallback = client._select_streams(
            self._quality_streaming(), "abcdefghijk"
        )

        self.assertEqual(video_url, "https://cdn.example/720.mp4")
        self.assertEqual(audio_url, "https://cdn.example/audio.m4a")
        self.assertEqual(quality, "720p")
        self.assertIsNone(fallback)

    def test_quality_falls_back_to_the_closest_lower_height(self):
        client = YouTubeClient(allow_network_fallbacks=False, quality="600")

        video_url, audio_url, quality, _ext, _headers, fallback = client._select_streams(
            self._quality_streaming(), "abcdefghijk"
        )

        self.assertEqual(video_url, "https://cdn.example/480.mp4")
        self.assertEqual(audio_url, "https://cdn.example/audio.m4a")
        self.assertEqual(quality, "480p")
        self.assertEqual(fallback, {"requested": 600, "selected": 480})

    def test_quality_worst_selects_the_lowest_stream(self):
        client = YouTubeClient(allow_network_fallbacks=False, quality="worst")

        video_url, audio_url, quality, _ext, _headers, fallback = client._select_streams(
            self._quality_streaming(), "abcdefghijk"
        )

        self.assertEqual(video_url, "https://cdn.example/144.mp4")
        self.assertEqual(audio_url, "https://cdn.example/audio.m4a")
        self.assertEqual(quality, "144p")
        self.assertIsNone(fallback)

    def test_quality_without_a_lower_stream_uses_the_lowest_available(self):
        client = YouTubeClient(allow_network_fallbacks=False, quality="144")
        streaming = {
            "formats": [
                {
                    "url": "https://cdn.example/360.mp4",
                    "mimeType": 'video/mp4; codecs="avc1.42001E"',
                    "height": 360,
                    "bitrate": 500000,
                    "qualityLabel": "360p",
                }
            ],
            "adaptiveFormats": [
                {
                    "url": "https://cdn.example/720.mp4",
                    "mimeType": 'video/mp4; codecs="avc1.64001F"',
                    "height": 720,
                    "bitrate": 2500000,
                    "qualityLabel": "720p",
                }
            ],
        }

        video_url, audio_url, quality, _ext, _headers, fallback = client._select_streams(
            streaming, "abcdefghijk"
        )

        self.assertEqual(video_url, "https://cdn.example/360.mp4")
        self.assertIsNone(audio_url)
        self.assertEqual(quality, "360p progressive")
        self.assertEqual(
            fallback,
            {"requested": 144, "selected": 360, "no_lower": True},
        )

    def test_quality_accepts_a_p_suffix(self):
        self.assertEqual(
            YouTubeClient(allow_network_fallbacks=False, quality="720p").quality,
            "720",
        )

    def test_invalid_quality_is_rejected(self):
        with self.assertRaises(ValueError):
            YouTubeClient(allow_network_fallbacks=False, quality="ultra")

    def test_normalize_item_uses_the_player_response(self):
        player = {
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {
                "title": "A video",
                "thumbnail": {"thumbnails": [{"url": "https://img.example/t.jpg"}]},
            },
            "streamingData": {
                "formats": [
                    {
                        "url": "https://cdn.example/v.mp4",
                        "mimeType": 'video/mp4; codecs="avc1.42001E"',
                        "height": 720,
                        "bitrate": 900000,
                        "qualityLabel": "720p",
                    }
                ]
            },
            "microformat": {
                "playerMicroformatRenderer": {"publishDate": "2026-01-02"}
            },
        }

        with patch.object(
            self.client, "_fetch_player_response", return_value=(player, "ANDROID")
        ):
            item = self.client.normalize_item({"videoId": "abcdefghijk"})

        self.assertEqual(item["id"], "abcdefghijk")
        self.assertEqual(item["media_type"], "Video")
        self.assertEqual(item["video_url"], "https://cdn.example/v.mp4")
        self.assertEqual(item["timestamp"], "2026-01-02")
        self.assertEqual(item["ext"], "mp4")

    def test_visionos_is_preferred_and_retries_with_a_visitor_id(self):
        client = YouTubeClient(allow_network_fallbacks=False)
        calls = []

        def fake_request(video_id, identity):
            calls.append(identity["name"])
            if len(calls) == 1:
                return {
                    "playabilityStatus": {"status": "LOGIN_REQUIRED"},
                    "responseContext": {"visitorData": "visitor-123"},
                }
            return {
                "playabilityStatus": {"status": "OK"},
                "streamingData": {
                    "formats": [
                        {
                            "url": "https://cdn.example/v.mp4",
                            "mimeType": "video/mp4",
                            "height": 720,
                        }
                    ]
                },
            }

        with patch.object(client, "_request_player", side_effect=fake_request):
            payload, name = client._fetch_player_response("abcdefghijk")

        self.assertEqual(name, "VISIONOS")
        self.assertEqual(calls, ["VISIONOS", "VISIONOS"])
        self.assertEqual(client._visitor_data, "visitor-123")
        self.assertIn("streamingData", payload)

    def test_the_retry_sends_the_visitor_id(self):
        client = YouTubeClient(allow_network_fallbacks=False)
        bodies = []

        def fake_post(endpoint, body, identity):
            bodies.append(body)
            if len(bodies) == 1:
                return {
                    "playabilityStatus": {"status": "LOGIN_REQUIRED"},
                    "responseContext": {"visitorData": "visitor-123"},
                }
            return {
                "playabilityStatus": {"status": "OK"},
                "streamingData": {"formats": []},
            }

        with patch.object(client, "_innertube_post", side_effect=fake_post):
            client._fetch_player_response("abcdefghijk")

        self.assertNotIn("visitorData", bodies[0]["context"]["client"])
        self.assertEqual(bodies[1]["context"]["client"]["visitorData"], "visitor-123")

    def test_other_clients_are_used_when_visionos_has_no_streams(self):
        client = YouTubeClient(allow_network_fallbacks=False)
        names = []

        def fake_request(video_id, identity):
            names.append(identity["name"])
            if identity["name"] == "VISIONOS":
                return {
                    "playabilityStatus": {"status": "LOGIN_REQUIRED"},
                    "responseContext": {"visitorData": "visitor-123"},
                }
            if identity["name"] == "ANDROID":
                return {
                    "playabilityStatus": {"status": "OK"},
                    "streamingData": {
                        "formats": [
                            {
                                "url": "https://cdn.example/v.mp4",
                                "mimeType": "video/mp4",
                                "height": 720,
                            }
                        ]
                    },
                }
            return {"playabilityStatus": {"status": "UNPLAYABLE"}}

        with patch.object(client, "_request_player", side_effect=fake_request):
            _payload, name = client._fetch_player_response("abcdefghijk")

        self.assertEqual(name, "ANDROID")
        self.assertEqual(names[:2], ["VISIONOS", "VISIONOS"])
        self.assertIn("ANDROID", names)


class TikTokTests(unittest.TestCase):
    def setUp(self):
        self.client = TikTokClient(allow_network_fallbacks=False)

    def test_detects_supported_tiktok_urls(self):
        targets = {
            "https://www.tiktok.com/@user/video/7300000000000000000": (
                "single",
                "7300000000000000000",
            ),
            "https://www.tiktok.com/@user/photo/7300000000000000000": (
                "single",
                "7300000000000000000",
            ),
            "https://www.tiktok.com/@user": ("account", "@user"),
            "https://vm.tiktok.com/ZMabc123": (
                "single",
                "https://vm.tiktok.com/ZMabc123",
            ),
        }
        for url, expected in targets.items():
            with self.subTest(url=url):
                self.assertEqual(TikTokClient.detect_url(url), expected)

    def test_does_not_claim_other_hosts(self):
        self.assertIsNone(TikTokClient.detect_url("https://example.com/@user/video/123456"))

    def test_extracts_numeric_id_from_urls(self):
        self.assertEqual(
            self.client.extract_id("https://www.tiktok.com/@user/video/7300000000000000000?x=1"),
            "7300000000000000000",
        )
        self.assertEqual(self.client.extract_id("7300000000000000000"), "7300000000000000000")

    def test_normalizes_video_with_the_highest_bitrate(self):
        raw = {
            "id": "7300000000000000000",
            "desc": "hello",
            "createTime": 1700000000,
            "video": {
                "bitrateInfo": [
                    {"Bitrate": 100, "PlayAddr": {"UrlList": ["https://cdn.example/low.mp4"]}},
                    {"Bitrate": 900, "PlayAddr": {"UrlList": ["https://cdn.example/high.mp4"]}},
                ],
                "playAddr": "https://cdn.example/default.mp4",
                "cover": "https://cdn.example/cover.jpg",
                "duration": 12,
            },
        }

        item = self.client.normalize_item(raw)
        self.assertEqual(item["media_type"], "Video")
        self.assertEqual(item["video_url"], "https://cdn.example/high.mp4")
        self.assertEqual(item["ext"], "mp4")
        self.assertIn("Referer", item["http_headers"])
        self.assertTrue(item["timestamp"].startswith("2023-11-14"))

    def test_normalizes_single_photo_post(self):
        raw = {
            "id": "7300000000000000001",
            "imagePost": {
                "images": [
                    {"imageURL": {"urlList": ["https://cdn.example/small.jpg", "https://cdn.example/large.jpg"]}}
                ]
            },
        }

        item = self.client.normalize_item(raw)
        self.assertEqual(item["media_type"], "Photo")
        self.assertEqual(item["display_url"], "https://cdn.example/large.jpg")
        self.assertEqual(item["ext"], "jpg")

    def test_normalizes_multi_photo_post_as_carousel(self):
        raw = {
            "id": "7300000000000000002",
            "imagePost": {
                "images": [
                    {"imageURL": {"urlList": ["https://cdn.example/1.jpg"]}},
                    {"imageURL": {"urlList": ["https://cdn.example/2.jpg"]}},
                ]
            },
        }

        item = self.client.normalize_item(raw)
        self.assertEqual(item["media_type"], "Carousel")
        self.assertEqual(len(item["children"]), 2)
        self.assertTrue(all(child["is_video"] is False for child in item["children"]))

    def test_reads_the_universal_data_script(self):
        item = {"id": "7300000000000000003", "desc": "from html", "video": {"playAddr": "https://cdn.example/v.mp4"}}
        payload = {
            "__DEFAULT_SCOPE__": {
                "webapp.video-detail": {"itemInfo": {"itemStruct": item}}
            }
        }
        html = (
            '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
            f"{json.dumps(payload)}</script>"
        )

        self.assertEqual(self.client._item_from_html(html), item)

    def test_reads_the_mobile_api_data_script(self):
        item = {"id": "7300000000000000004", "desc": "from mobile html",
                "video": {"playAddr": "https://cdn.example/mobile.mp4"}}
        payload = {"videoDetail": {"itemInfo": {"itemStruct": item}}}
        html = (
            '<script id="api-data" type="application/json">'
            f"{json.dumps(payload)}</script>"
        )

        self.assertEqual(self.client._item_from_html(html), item)

    def test_retries_with_the_desktop_layout(self):
        item = {"id": "7300000000000000005", "desc": "from desktop html",
                "video": {"playAddr": "https://cdn.example/desktop.mp4"}}
        payload = {
            "__DEFAULT_SCOPE__": {
                "webapp.video-detail": {"itemInfo": {"itemStruct": item}}
            }
        }
        desktop = (
            '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
            f"{json.dumps(payload)}</script>"
        )

        with patch.object(
            self.client, "_fetch_page", side_effect=["<html></html>", desktop]
        ) as fetch:
            result = self.client.fetch_item("7300000000000000005")

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result["video_url"], "https://cdn.example/desktop.mp4")

    def test_page_fetch_defaults_to_the_mobile_user_agent(self):
        response = MagicMock(status_code=200, text="<html></html>")
        with patch.object(self.client.session, "get", return_value=response) as get:
            self.client._fetch_page("https://www.tiktok.com/@user")

        self.assertEqual(
            get.call_args.kwargs["headers"]["User-Agent"], MOBILE_USER_AGENT
        )

    def test_profile_pagination_walks_the_timestamp_cursor(self):
        pages = [
            {
                "itemList": [
                    {"id": "111", "createTime": 300},
                    {"id": "222", "createTime": 200},
                ],
                "hasMorePrevious": True,
            },
            {
                "itemList": [{"id": "333", "createTime": 100}],
                "hasMorePrevious": False,
            },
        ]
        cursors = []

        def fake_page(sec_uid, cursor):
            cursors.append(cursor)
            return pages[len(cursors) - 1]

        with patch.object(self.client, "_fetch_item_list_page", side_effect=fake_page):
            items = list(
                self.client._paginate_user_items("user", set(), None, "SEC")
            )

        self.assertEqual([item["id"] for item in items], ["111", "222", "333"])
        self.assertGreater(cursors[0], 1_000_000_000_000)
        self.assertEqual(cursors[1], 200_000)

    def test_item_list_uses_the_creator_endpoint(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"statusCode": 0, "itemList": []}

        with patch.object(self.client.session, "get", return_value=response) as get:
            self.client._fetch_item_list_page("SEC", 123456)

        url = get.call_args.args[0]
        params = get.call_args.kwargs["params"]
        self.assertIn("api/creator/item_list", url)
        self.assertEqual(params["secUid"], "SEC")
        self.assertEqual(params["cursor"], "123456")
        self.assertEqual(params["type"], "1")


class FacebookTests(unittest.TestCase):
    def setUp(self):
        self.client = FacebookClient(allow_network_fallbacks=False)

    def test_detects_supported_facebook_urls(self):
        targets = {
            "https://www.facebook.com/watch/?v=123456789": ("single", "123456789"),
            "https://www.facebook.com/somepage/videos/123456789": ("single", "123456789"),
            "https://www.facebook.com/reel/123456789": ("single", "123456789"),
            "https://www.facebook.com/somepage/": ("account", "somepage"),
            "https://www.facebook.com/somepage/videos/": ("account", "somepage/videos"),
        }
        for url, expected in targets.items():
            with self.subTest(url=url):
                self.assertEqual(FacebookClient.detect_url(url), expected)

    def test_short_links_and_groups(self):
        self.assertEqual(
            FacebookClient.detect_url("https://fb.watch/abcDEF123"),
            ("single", "https://fb.watch/abcDEF123"),
        )
        self.assertIsNone(FacebookClient.detect_url("https://www.facebook.com/groups/123456"))

    def test_extracts_escaped_hd_stream_url(self):
        html = (
            '<script>{"playable_url_quality_hd":'
            '"https:\\/\\/cdn.example.com\\/v.mp4?token=1\\u0026x=2"}</script>'
        )
        url, quality = self.client._extract_stream(html)
        self.assertEqual(url, "https://cdn.example.com/v.mp4?token=1&x=2")
        self.assertIn("HD", quality)

    def test_detects_a_login_wall(self):
        self.assertTrue(
            self.client._is_login_wall("<html>login</html>", "https://www.facebook.com/login/?next=x")
        )
        self.assertFalse(
            self.client._is_login_wall("<html>video</html>", "https://www.facebook.com/watch/?v=1")
        )

    def test_fetch_item_builds_a_normalized_media_item(self):
        html = (
            '<meta property="og:title" content="My video">'
            '<meta property="og:image" content="https://img.example/cover.jpg">'
            '<script>{"playable_url":"https:\\/\\/cdn.example.com\\/sd.mp4"}</script>'
        )

        with patch.object(
            self.client,
            "_fetch_page",
            return_value=(html, "https://www.facebook.com/watch/?v=123456789"),
        ):
            item = self.client.fetch_item("123456789")

        self.assertEqual(item["id"], "123456789")
        self.assertEqual(item["media_type"], "Video")
        self.assertEqual(item["video_url"], "https://cdn.example.com/sd.mp4")
        self.assertEqual(item["display_url"], "https://img.example/cover.jpg")
        self.assertEqual(item["title"], "My video")


class DatabasePlatformScopingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "state.db")
        self.db = StateDatabase(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_identical_ids_on_two_platforms_do_not_collide(self):
        self.db.mark_downloaded("ABC", "Post", "", "instagram/ABC.jpg")
        self.db.mark_downloaded("ABC", "Video", "", "tiktok/ABC.mp4", platform="tiktok")

        self.assertTrue(self.db.is_downloaded("ABC"))
        self.assertTrue(self.db.is_downloaded("ABC", "tiktok"))
        self.assertEqual(self.db.get_file_path("ABC"), "instagram/ABC.jpg")
        self.assertEqual(self.db.get_file_path("ABC", "tiktok"), "tiktok/ABC.mp4")

        self.assertTrue(self.db.remove_record("ABC"))
        self.assertFalse(self.db.is_downloaded("ABC"))
        self.assertTrue(self.db.is_downloaded("ABC", "tiktok"))

    def test_downloaded_lists_are_scoped(self):
        self.db.mark_downloaded("ONE", "Post", "", "ONE.jpg")
        self.db.mark_downloaded("TWO", "Video", "", "TWO.mp4", platform="youtube")

        self.assertEqual(self.db.get_all_downloaded_shortcodes(), {"ONE"})
        self.assertEqual(self.db.get_all_downloaded_shortcodes("youtube"), {"TWO"})
        self.assertEqual(self.db.get_record_count("youtube"), 1)
        self.assertEqual(self.db.get_record_count(), 2)

    def test_clear_all_only_clears_the_requested_platform(self):
        self.db.mark_downloaded("ONE", "Post", "", "ONE.jpg")
        self.db.mark_downloaded("TWO", "Video", "", "TWO.mp4", platform="tiktok")

        removed = self.db.clear_all("tiktok")

        self.assertEqual(removed, 1)
        self.assertTrue(self.db.is_downloaded("ONE"))
        self.assertFalse(self.db.is_downloaded("TWO", "tiktok"))

    def test_legacy_database_is_migrated_to_instagram_rows(self):
        legacy_path = os.path.join(self.temp_dir.name, "legacy.db")
        conn = sqlite3.connect(legacy_path)
        conn.execute(
            "CREATE TABLE downloaded_media (shortcode TEXT PRIMARY KEY, media_type TEXT, "
            "timestamp TEXT, file_path TEXT, downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE cached_users (username TEXT PRIMARY KEY, user_id TEXT, "
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO downloaded_media (shortcode, media_type, file_path) "
            "VALUES ('OLD', 'Post', 'OLD.jpg')"
        )
        conn.execute("INSERT INTO cached_users (username, user_id) VALUES ('alice', '123')")
        conn.commit()
        conn.close()

        db = StateDatabase(legacy_path)

        self.assertTrue(db.is_downloaded("OLD"))
        self.assertFalse(db.is_downloaded("OLD", "tiktok"))
        self.assertEqual(db.get_cached_user_id("alice"), "123")
        check = sqlite3.connect(legacy_path)
        try:
            self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 2)
        finally:
            check.close()


class EnginePlatformTests(unittest.TestCase):
    def _engine(self, platform):
        engine = QivarynEngine.__new__(QivarynEngine)
        engine.platform = platform
        engine.output_dir = "./downloads"
        return engine

    def test_filenames_keep_instagram_layout_and_prefix_new_platforms(self):
        engine = self._engine("instagram")
        self.assertEqual(engine._file_stem("ABC"), "ABC")

        engine = self._engine("tiktok")
        self.assertEqual(engine._file_stem("7300000000000000000"), "tiktok_7300000000000000000")

    def test_unsafe_identifier_characters_are_sanitized(self):
        engine = self._engine("facebook")
        self.assertEqual(engine._file_stem("a/b:c"), "facebook_a_b_c")

    def test_single_download_passes_the_original_url_to_the_client(self):
        engine = self._engine("tiktok")
        engine.client = MagicMock()
        engine.client.extract_id.return_value = "ZMabc123"
        engine.client.fetch_item.return_value = make_item(
            "7300000000000000000", "Video", video_url="https://cdn.example/v.mp4"
        )
        engine.db = MagicMock()

        with patch.object(engine, "_is_item_already_saved", return_value=False), \
             patch.object(engine, "_download_media_item", return_value=True) as download:
            self.assertTrue(engine.download_single("https://vm.tiktok.com/ZMabc123"))

        engine.client.fetch_item.assert_called_once_with("https://vm.tiktok.com/ZMabc123")
        download.assert_called_once()

    def test_download_url_dispatches_a_youtube_playlist(self):
        engine = self._engine("youtube")
        engine.client = MagicMock()
        engine.client.supports_playlists = True
        engine.client.detect_url.return_value = ("playlist", "PL123")
        engine.db = MagicMock()

        with patch.object(engine, "download_playlist", return_value=2) as playlist:
            result = engine.download_url(
                "https://www.youtube.com/playlist?list=PL123", count=5
            )

        self.assertEqual(result, 2)
        playlist.assert_called_once_with(
            "PL123", max_pages=None, force=False, count=5, media_type_filter="all"
        )

    def test_account_download_uses_every_feed_for_the_all_filter(self):
        engine = self._engine("youtube")
        engine.client = MagicMock()
        engine.client.feeds_for.return_value = (
            AccountFeed("Videos tab", "videos", MagicMock()),
            AccountFeed("Shorts tab", "shorts", MagicMock()),
        )
        engine.client.account_label.return_value = "YouTube channel @x"
        engine.db = MagicMock()
        engine.db.get_cached_user_id.return_value = "UC123"

        with patch.object(engine, "_download_feed", side_effect=[(1, 0), (2, 0)]) as feed:
            total = engine.download_account("@x", count=10)

        self.assertEqual(total, 3)
        self.assertEqual(feed.call_count, 2)
        self.assertEqual(
            [call.kwargs["count"] for call in feed.call_args_list], [10, 9]
        )


class CliPlatformTests(unittest.TestCase):
    def test_url_routing_picks_the_owning_platform(self):
        self.assertEqual(
            _platform_for_url("https://www.instagram.com/p/ABC/"), "instagram"
        )
        self.assertEqual(
            _platform_for_url("https://www.tiktok.com/@user/video/7300000000000000000"),
            "tiktok",
        )
        self.assertEqual(
            _platform_for_url("https://youtu.be/abcdefghijk"), "youtube"
        )
        self.assertEqual(
            _platform_for_url("https://www.facebook.com/watch/?v=123456789"), "facebook"
        )

    def test_unknown_url_is_rejected(self):
        with self.assertRaises(Exception) as ctx:
            _platform_for_url("https://example.com/nope")
        self.assertIn("Could not identify", str(ctx.exception))

    def test_type_is_validated_per_platform(self):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["download", "someone", "--platform", "instagram", "--type", "shorts"]
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("not supported by instagram", result.output)

    def test_youtube_accepts_shorts(self):
        runner = CliRunner()
        with patch("qivaryn.cli._create_engine") as create_engine:
            result = runner.invoke(
                cli, ["download", "@someone", "--platform", "youtube", "--type", "shorts"]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        create_engine.assert_called_once()

    def test_sessionid_is_rejected_for_youtube(self):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["single", "--platform", "youtube", "--sessionid", "abc", "abcdefghijk"],
        )
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--sessionid is not supported", result.output)

    def test_platforms_command_lists_every_platform(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["platforms"])

        self.assertEqual(result.exit_code, 0, result.output)
        for name in PLATFORM_NAMES:
            self.assertIn(name, result.output)


if __name__ == "__main__":
    unittest.main()
