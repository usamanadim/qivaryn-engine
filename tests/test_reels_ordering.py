"""Regression tests for Reels ordering, pagination and failure handling.

These cover three guarantees:

* reels are downloaded newest-to-oldest, even when Instagram pins items or
  returns the pages out of order;
* pagination never loses or repeats reels, and a broken later page is
  reported instead of looking like the end of the feed;
* a failed download is retried once and, if it still fails, reported so the
  next run picks it up again.
"""

import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from qivaryn.client import InstagramClient
from qivaryn.downloader import QivarynEngine


def _response(payload, status=200):
    response = MagicMock(status_code=status)
    response.json.return_value = payload
    return response


def _reel(code, taken_at=None, **extra):
    media = {"code": code, "media_type": 2, "video_versions": [{"url": f"http://cdn/{code}.mp4"}]}
    if taken_at is not None:
        media["taken_at"] = taken_at
    media.update(extra)
    return {"media": media}


class ReelsFeedOrderingTests(unittest.TestCase):
    def setUp(self):
        self.client = InstagramClient(allow_network_fallbacks=False)

    def _collect(self, pages):
        """Runs the feed against canned pages and returns the codes and the mock."""
        responses = [_response(page) for page in pages]
        with patch.object(self.client.session, "post", side_effect=responses) as post, \
                patch("qivaryn.client.time.sleep"):
            codes = [
                item["code"]
                for item in self.client.fetch_user_raw_reels_paginated("123")
            ]
        return codes, post

    def test_reels_are_returned_newest_first_across_pages(self):
        pages = [
            {
                "items": [_reel("NEWEST_2", 250), _reel("NEWEST_1", 300)],
                "paging_info": {"more_available": True, "max_id": "page-2"},
            },
            {
                "items": [_reel("MIDDLE", 200)],
                "paging_info": {"more_available": True, "max_id": "page-3"},
            },
            {
                "items": [_reel("OLD", 100)],
                "paging_info": {"more_available": False},
            },
        ]

        codes, _ = self._collect(pages)
        self.assertEqual(codes, ["NEWEST_1", "NEWEST_2", "MIDDLE", "OLD"])

    def test_pinned_reels_are_downloaded_first(self):
        # Instagram puts pinned reels first; they must stay first regardless of date.
        pinned = {"media": {"code": "PINNED", "media_type": 2}, "taken_at": 50, "is_pinned": True}
        pages = [
            {
                "items": [pinned, _reel("RECENT", 500), _reel("OLDER", 400)],
                "paging_info": {"more_available": False},
            }
        ]

        codes, _ = self._collect(pages)
        self.assertEqual(codes, ["PINNED", "RECENT", "OLDER"])

    def test_multiple_pinned_reels_stay_before_newer_ones(self):
        pinned_old = {"media": {"code": "PINNED_OLD", "media_type": 2}, "taken_at": 10, "is_pinned": True}
        pinned_new = {"media": {"code": "PINNED_NEW", "media_type": 2}, "taken_at": 20, "is_pinned": True}
        pages = [
            {
                "items": [pinned_old, _reel("NEWEST", 500), pinned_new, _reel("OLDER", 400)],
                "paging_info": {"more_available": False},
            }
        ]

        codes, _ = self._collect(pages)
        # Pinned reels come first (in API order), then the rest newest-first.
        self.assertEqual(codes[:2], ["PINNED_OLD", "PINNED_NEW"])
        self.assertEqual(codes[2:], ["NEWEST", "OLDER"])

    def test_pinned_reels_via_clips_tab_pinned_user_ids_downloaded_first(self):
        # Native Instagram API structure for pinned reels in clips/user
        pinned = {
            "media": {
                "code": "PINNED_REAL",
                "media_type": 2,
                "taken_at": 50,  # very old date
                "clips_tab_pinned_user_ids": [8708826683],
                "timeline_pinned_user_ids": [],
            }
        }
        pages = [
            {
                "items": [
                    pinned,
                    _reel("REEL_300", 300),
                    _reel("REEL_500", 500),
                    _reel("REEL_400", 400),
                ],
                "paging_info": {"more_available": False},
            }
        ]

        codes, _ = self._collect(pages)
        # Pinned reel first, followed strictly by descending chronological order (500, 400, 300)
        self.assertEqual(codes, ["PINNED_REAL", "REEL_500", "REEL_400", "REEL_300"])

    def test_mixed_pinning_formats_all_downloaded_first_then_chronological(self):
        pin_clips = {
            "media": {
                "code": "PIN_1",
                "media_type": 2,
                "taken_at": 10,
                "clips_tab_pinned_user_ids": [12345],
            }
        }
        pin_timeline = {
            "media": {
                "code": "PIN_2",
                "media_type": 2,
                "taken_at": 20,
                "timeline_pinned_user_ids": [12345],
            }
        }
        pin_bool = {
            "media": {
                "code": "PIN_3",
                "media_type": 2,
                "taken_at": 15,
                "is_pinned_to_profile": True,
            }
        }
        pages = [
            {
                "items": [
                    pin_clips,
                    _reel("REEL_MID", 350),
                    pin_timeline,
                    _reel("REEL_NEW", 800),
                    pin_bool,
                    _reel("REEL_OLD", 100),
                ],
                "paging_info": {"more_available": False},
            }
        ]

        codes, _ = self._collect(pages)
        # All pinned reels come first in feed order
        self.assertEqual(codes[:3], ["PIN_1", "PIN_2", "PIN_3"])
        # All remaining reels strictly descending by timestamp
        self.assertEqual(codes[3:], ["REEL_NEW", "REEL_MID", "REEL_OLD"])

    def test_undated_reels_keep_their_order_after_dated_ones(self):
        pages = [
            {
                "items": [_reel("FIRST"), _reel("SECOND"), _reel("THIRD", 100)],
                "paging_info": {"more_available": False},
            }
        ]

        codes, _ = self._collect(pages)
        self.assertEqual(codes, ["THIRD", "FIRST", "SECOND"])

    def test_repeated_items_across_pages_are_yielded_once(self):
        pages = [
            {
                "items": [_reel("A", 3), _reel("B", 2)],
                "paging_info": {"more_available": True, "max_id": "page-2"},
            },
            {
                "items": [_reel("B", 2), _reel("C", 1)],
                "paging_info": {"more_available": False},
            },
        ]

        codes, _ = self._collect(pages)
        self.assertEqual(codes, ["A", "B", "C"])

    def test_a_repeated_cursor_does_not_loop_forever(self):
        pages = [
            {
                "items": [_reel("A", 2)],
                "paging_info": {"more_available": True, "max_id": "cursor"},
            },
            {
                "items": [_reel("B", 1)],
                "paging_info": {"more_available": True, "max_id": "cursor"},
            },
        ]

        codes, post = self._collect(pages)
        self.assertEqual(codes, ["A", "B"])
        self.assertEqual(post.call_count, 2)


class ReelsPaginationFailureTests(unittest.TestCase):
    def setUp(self):
        self.client = InstagramClient(allow_network_fallbacks=False)

    def test_transient_server_error_is_retried(self):
        page = {
            "items": [_reel("A", 10)],
            "paging_info": {"more_available": False},
        }
        responses = [_response({}, status=503), _response(page)]

        with patch.object(self.client.session, "post", side_effect=responses) as post, \
                patch("qivaryn.client.time.sleep"):
            codes = [
                item["code"]
                for item in self.client.fetch_user_raw_reels_paginated("123")
            ]

        self.assertEqual(codes, ["A"])
        self.assertEqual(post.call_count, 2)

    def test_a_failed_later_page_keeps_earlier_reels_and_reports_truncation(self):
        first_page = {
            "items": [_reel("A", 20), _reel("B", 10)],
            "paging_info": {"more_available": True, "max_id": "page-2"},
        }
        responses = [_response(first_page)] + [
            _response({}, status=500) for _ in range(3)
        ]
        collected = []

        with patch.object(self.client.session, "post", side_effect=responses), \
                patch("qivaryn.client.time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                for item in self.client.fetch_user_raw_reels_paginated(
                    "123", username="someone"
                ):
                    collected.append(item["code"])

        self.assertEqual(collected, ["A", "B"])
        self.assertIn("stopped at page 2", str(ctx.exception))
        self.assertIn("@someone", str(ctx.exception))


class EngineFailureHandlingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        self.client = InstagramClient(allow_network_fallbacks=False)
        self.mock_db = MagicMock()
        self.mock_db.get_cached_user_id.return_value = "12345"
        self.mock_db.get_all_downloaded_shortcodes.return_value = set()

        with patch("qivaryn.downloader.StateDatabase", return_value=self.mock_db):
            self.engine = QivarynEngine(
                output_dir=self.temp_dir.name, db_path=":memory:"
            )
        self.engine.db = self.mock_db
        self.engine.client = self.client

    @staticmethod
    def _raw(code, taken_at=1):
        return {
            "code": code,
            "media_type": 2,
            "video_versions": [{"url": f"http://cdn/{code}.mp4"}],
            "taken_at": taken_at,
        }

    @staticmethod
    def _normalized(raw):
        return {
            "id": raw["code"],
            "shortcode": raw["code"],
            "media_type": "Reel",
            "video_url": "http://cdn/v.mp4",
            "audio_url": None,
            "timestamp": str(raw.get("taken_at", "")),
            "quality": "1080p",
        }

    def test_mid_feed_error_keeps_the_items_already_downloaded(self):
        def truncated_feed():
            yield self._raw("A", 30)
            yield self._raw("B", 20)
            raise RuntimeError("Reels feed stopped at page 2: HTTP 500.")

        self.client.normalize_item = MagicMock(side_effect=self._normalized)
        output = io.StringIO()

        with patch.object(self.engine, "_download_media_item", return_value=True) as download, \
                contextlib.redirect_stdout(output):
            downloaded, skipped = self.engine._scan_items(
                truncated_feed(), "@someone", "reels", None, False
            )

        self.assertEqual((downloaded, skipped), (2, 0))
        self.assertEqual([call.args[0]["id"] for call in download.call_args_list], ["A", "B"])
        self.assertIn("Stopped early", output.getvalue())

    def test_failed_download_is_retried_with_fresh_metadata(self):
        raw = self._raw("R1", 10)
        fresh_raw = self._raw("R1", 10)
        self.client.normalize_item = MagicMock(side_effect=self._normalized)
        self.client.fetch_item = MagicMock(return_value=fresh_raw)

        with patch.object(
            self.engine, "_download_media_item", side_effect=[False, True]
        ) as download:
            downloaded, _ = self.engine._scan_items(
                [raw], "@someone", "reels", None, False
            )

        self.assertEqual(downloaded, 1)
        self.assertEqual(download.call_count, 2)
        self.client.fetch_item.assert_called_once_with("R1")
        self.assertEqual(self.engine.failed_items, [])

    def test_a_reel_that_keeps_failing_is_reported(self):
        raw = self._raw("BROKEN", 10)
        self.client.normalize_item = MagicMock(side_effect=self._normalized)
        self.client.fetch_item = MagicMock(return_value=dict(raw))

        with patch.object(self.engine, "_download_media_item", return_value=False):
            downloaded, _ = self.engine._scan_items(
                [raw], "@someone", "reels", None, False
            )

        self.assertEqual(downloaded, 0)
        self.assertEqual(
            [failure["id"] for failure in self.engine.failed_items], ["BROKEN"]
        )

    def test_html_stub_is_hydrated_before_downloading(self):
        stub = {
            "code": "STUB1",
            "media_type": 2,
            "is_video": True,
            "_needs_full_fetch": True,
        }
        full = self._raw("STUB1", 10)
        self.client.fetch_item = MagicMock(return_value=full)
        self.client.normalize_item = MagicMock(side_effect=self._normalized)

        with patch.object(self.engine, "_download_media_item", return_value=True) as download:
            downloaded, _ = self.engine._scan_items(
                [stub], "@someone", "reels", None, False
            )

        self.assertEqual(downloaded, 1)
        self.client.fetch_item.assert_called_once_with("STUB1")
        self.assertEqual(download.call_args.args[0]["id"], "STUB1")

    def test_count_downloads_the_n_newest_reels_in_order(self):
        pages = [
            {
                "items": [_reel("MIDDLE", 20), _reel("NEWEST", 30)],
                "paging_info": {"more_available": True, "max_id": "page-2"},
            },
            {
                "items": [_reel("OLDEST", 10)],
                "paging_info": {"more_available": False},
            },
        ]
        responses = [_response(page) for page in pages]
        self.client.normalize_item = MagicMock(side_effect=self._normalized)

        with patch.object(self.client.session, "post", side_effect=responses) as post_mock, \
                patch.object(self.engine, "_download_media_item", return_value=True) as download:
            total = self.engine.download_account(
                "testuser", media_type_filter="reels", count=2
            )

        self.assertEqual(total, 2)
        self.assertEqual(
            [call.args[0]["id"] for call in download.call_args_list],
            ["NEWEST", "MIDDLE"],
        )
        self.assertEqual(post_mock.call_count, 1)


class FakeResponse:
    def __init__(self, status_code, body=b""):
        self.status_code = status_code
        self.headers = {"content-length": str(len(body))}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def close(self):
        pass

    def iter_content(self, chunk_size=65536):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]


class DownloadRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        with patch("qivaryn.downloader.StateDatabase", return_value=MagicMock()):
            self.engine = QivarynEngine(
                output_dir=self.temp_dir.name, db_path=":memory:"
            )

    def test_a_transient_download_error_is_retried_once(self):
        failed = FakeResponse(503)
        ok = FakeResponse(200, b"data")
        target = os.path.join(self.temp_dir.name, "reel.mp4")

        with patch.object(
            self.engine.client.session, "get", side_effect=[failed, ok]
        ) as get:
            result = self.engine._download_file(
                "https://cdn.example/reel.mp4", target, "[Reel] test"
            )

        self.assertTrue(result)
        self.assertEqual(get.call_count, 2)
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"data")

    def test_an_expired_url_is_not_requested_twice(self):
        expired = FakeResponse(410)
        target = os.path.join(self.temp_dir.name, "reel.mp4")

        with patch.object(
            self.engine.client.session, "get", return_value=expired
        ) as get:
            result = self.engine._download_file(
                "https://cdn.example/reel.mp4", target, "[Reel] test"
            )

        self.assertFalse(result)
        self.assertEqual(get.call_count, 1)


class MergedVisibleAndHiddenReelsTests(unittest.TestCase):
    """Tests that visible reels (Web GraphQL) and hidden reels (mobile clips)
    are both detected, deduplicated, and yielded in strict chronological order.
    """

    def setUp(self):
        self.client = InstagramClient(allow_network_fallbacks=False)
        self.client.cookies_file = "cookies.txt"

    def _graphql_edge(self, code, taken_at=None, is_pinned=False):
        media = {
            "code": code,
            "pk": f"pk_{code}",
            "media_type": 2,
            "is_pinned": is_pinned,
        }
        if taken_at is not None:
            media["taken_at"] = taken_at
        return {"node": {"media": media}}

    def _graphql_page(self, edges, has_next=False, end_cursor=None):
        return {
            "data": {
                "xdt_api__v1__clips__user__connection_v2": {
                    "edges": edges,
                    "page_info": {
                        "has_next_page": has_next,
                        "end_cursor": end_cursor,
                    },
                }
            }
        }

    def _collect_merged(self, graphql_pages, mobile_pages):
        g_pages = list(graphql_pages)
        m_pages = list(mobile_pages)

        def mock_post(url, *args, **kwargs):
            if "graphql" in url:
                if g_pages:
                    return _response(g_pages.pop(0))
                return _response({})
            elif "clips/user" in url:
                if m_pages:
                    return _response(m_pages.pop(0))
                return _response({"items": [], "paging_info": {"more_available": False}})
            return _response({})

        with patch.object(self.client.session, "post", side_effect=mock_post), \
             patch("qivaryn.client.time.sleep"):
            items = list(self.client.fetch_user_raw_reels_paginated("123"))
        return [item["code"] for item in items]

    def test_hidden_reels_are_merged_in_strict_chronological_order(self):
        # Web GraphQL returns visible reels A (1000) and C (800)
        g_pages = [
            self._graphql_page([
                self._graphql_edge("REEL_A", 1000),
                self._graphql_edge("REEL_C", 800),
            ])
        ]
        # Mobile clips returns visible + hidden reels:
        # A (1000), B_HIDDEN (900), C (800), D_HIDDEN (700)
        m_pages = [
            {
                "items": [
                    _reel("REEL_A", 1000),
                    _reel("REEL_B_HIDDEN", 900),
                    _reel("REEL_C", 800),
                    _reel("REEL_D_HIDDEN", 700),
                ],
                "paging_info": {"more_available": False},
            }
        ]

        codes = self._collect_merged(g_pages, m_pages)
        # All reels detected, deduplicated, and strictly ordered by timestamp
        self.assertEqual(codes, ["REEL_A", "REEL_B_HIDDEN", "REEL_C", "REEL_D_HIDDEN"])

    def test_pinned_reels_precede_both_visible_and_hidden_reels(self):
        g_pages = [
            self._graphql_page([
                self._graphql_edge("REEL_VIS", 500),
            ])
        ]
        m_pages = [
            {
                "items": [
                    {"media": {"code": "PINNED", "media_type": 2, "taken_at": 10, "is_pinned": True}},
                    _reel("REEL_HIDDEN", 600),
                ],
                "paging_info": {"more_available": False},
            }
        ]

        codes = self._collect_merged(g_pages, m_pages)
        # PINNED first, then REEL_HIDDEN (600) before REEL_VIS (500)
        self.assertEqual(codes, ["PINNED", "REEL_HIDDEN", "REEL_VIS"])

    def test_afreen_khan_jump_does_not_skip_chronological_reels(self):
        # Simulating Afreen Khan's account:
        # GraphQL has the true top reels in order: PIN_1, R2, R3, R4, R5, R6
        g_pages = [
            self._graphql_page([
                self._graphql_edge("PIN_1", 10, is_pinned=True),
                self._graphql_edge("R2", 900),
                self._graphql_edge("R3", 850),
                self._graphql_edge("R4", 800),
                self._graphql_edge("R5", 750),
                self._graphql_edge("R6", 700),
            ])
        ]
        # Mobile clips returned PIN_1, but then jumped to R31, R33, R40 (older timestamps)
        m_pages = [
            {
                "items": [
                    {"media": {"code": "PIN_1", "media_type": 2, "taken_at": 10, "is_pinned": True}},
                    _reel("R31", 200),
                    _reel("R33", 180),
                    _reel("R40", 150),
                ],
                "paging_info": {"more_available": False},
            }
        ]

        codes = self._collect_merged(g_pages, m_pages)
        # First 6 reels must be PIN_1, R2, R3, R4, R5, R6 in sequence
        self.assertEqual(codes[:6], ["PIN_1", "R2", "R3", "R4", "R5", "R6"])
        # Followed by the older reels R31, R33, R40
        self.assertEqual(codes[6:], ["R31", "R33", "R40"])

    def test_only_hidden_reels_when_graphql_empty(self):
        g_pages = [
            self._graphql_page([])
        ]
        m_pages = [
            {
                "items": [
                    _reel("H1", 500),
                    _reel("H2", 400),
                ],
                "paging_info": {"more_available": False},
            }
        ]

        codes = self._collect_merged(g_pages, m_pages)
        self.assertEqual(codes, ["H1", "H2"])

    def test_only_visible_reels_when_mobile_empty(self):
        g_pages = [
            self._graphql_page([
                self._graphql_edge("V1", 500),
                self._graphql_edge("V2", 400),
            ])
        ]
        m_pages = [
            {
                "items": [],
                "paging_info": {"more_available": False},
            }
        ]

        codes = self._collect_merged(g_pages, m_pages)
        self.assertEqual(codes, ["V1", "V2"])



if __name__ == "__main__":
    unittest.main()
