"""Tests for the YouTube media download path.

YouTube's CDN rejects plain full-file GETs and throttles them, so every media
request has to use bounded range requests.
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from qivaryn.downloader import QivarynEngine


class FakeResponse:
    def __init__(self, status_code, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def close(self):
        pass

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]


class RangeOnlySession:
    """Serves a byte array but rejects requests without a Range header."""

    def __init__(self, data, fail_after=None):
        self.data = data
        self.fail_after = fail_after
        self.requests = []

    def get(self, url, headers=None, stream=False, timeout=None):
        headers = dict(headers or {})
        self.requests.append(headers)
        range_header = headers.get("Range")
        if not range_header:
            return FakeResponse(403, {"Content-Length": "0"}, b"")
        if self.fail_after is not None and len(self.requests) > self.fail_after:
            return FakeResponse(403, {"Content-Length": "0"}, b"")

        start_text, _, end_text = range_header.replace("bytes=", "").partition("-")
        start = int(start_text)
        end = int(end_text) if end_text else len(self.data) - 1
        chunk = self.data[start:end + 1]
        return FakeResponse(
            206,
            {
                "Content-Range": f"bytes {start}-{end}/{len(self.data)}",
                "Content-Length": str(len(chunk)),
            },
            chunk,
        )


class YouTubeRangeDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data = bytes(range(256)) * (3 * 1024 * 1024 // 256)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _engine(self, session):
        with patch("qivaryn.downloader.StateDatabase", return_value=MagicMock()):
            engine = QivarynEngine(
                platform="youtube",
                output_dir=self.temp_dir.name,
                db_path=":memory:",
            )
        engine.client.session = session
        return engine

    def test_youtube_downloads_use_bounded_ranges(self):
        session = RangeOnlySession(self.data)
        engine = self._engine(session)
        target = os.path.join(self.temp_dir.name, "video.mp4")

        result = engine._download_file(
            "https://cdn.example/v.mp4", target, "[Video] test"
        )

        self.assertTrue(result)
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), self.data)
        self.assertTrue(session.requests)
        for headers in session.requests:
            self.assertIn("Range", headers)

    def test_a_failed_chunk_removes_the_partial_file(self):
        session = RangeOnlySession(self.data, fail_after=1)
        engine = self._engine(session)
        target = os.path.join(self.temp_dir.name, "video.mp4")

        result = engine._download_file(
            "https://cdn.example/v.mp4", target, "[Video] test"
        )

        self.assertFalse(result)
        self.assertFalse(os.path.exists(target))

    def test_the_filename_contains_the_video_title(self):
        engine = self._engine(RangeOnlySession(self.data))
        item = {
            "id": "kbRtf6gxjQs",
            "media_type": "Video",
            "timestamp": "",
            "video_url": "https://cdn.example/v.mp4",
            "audio_url": None,
            "quality": "1080p60",
            "ext": "mp4",
            "title": "JABSE TUNE Mujhe Deewana (Remix)",
            "http_headers": {},
        }

        with patch.object(
            engine, "_download_video_stream", return_value=True
        ) as download:
            result = engine._download_media_item(item)

        self.assertTrue(result)
        path = download.call_args[0][2]
        self.assertEqual(
            os.path.basename(path),
            "youtube_kbRtf6gxjQs_JABSE TUNE Mujhe Deewana (Remix).mp4",
        )

    def test_titles_are_stripped_of_illegal_filename_characters(self):
        engine = self._engine(RangeOnlySession(self.data))

        self.assertEqual(
            engine._file_stem("kbRtf6gxjQs", 'A/B: "C" | D? E* F'),
            "youtube_kbRtf6gxjQs_A B C D E F",
        )

    def test_titles_are_ignored_for_other_platforms(self):
        engine = self._engine(RangeOnlySession(self.data))
        engine.platform = "tiktok"

        self.assertEqual(
            engine._file_stem("7300000000000000000", "Some caption"),
            "tiktok_7300000000000000000",
        )


if __name__ == "__main__":
    unittest.main()
