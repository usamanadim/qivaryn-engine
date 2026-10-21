import tempfile
import unittest
from unittest.mock import MagicMock, patch

from qivaryn.downloader import QivarynEngine


class QualityFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _engine(self, confirmer):
        with patch("qivaryn.downloader.StateDatabase", return_value=MagicMock()):
            return QivarynEngine(
                platform="youtube",
                output_dir=self.temp_dir.name,
                db_path=":memory:",
                quality="600",
                quality_confirmer=confirmer,
            )

    @staticmethod
    def _item():
        return {
            "id": "abcdefghijk",
            "shortcode": "abcdefghijk",
            "media_type": "Video",
            "timestamp": "",
            "video_url": "https://cdn.example/480.mp4",
            "audio_url": None,
            "quality": "480p",
            "ext": "mp4",
            "http_headers": {},
            "quality_fallback": {"requested": 600, "selected": 480},
        }

    def test_no_cancels_before_the_download_starts(self):
        engine = self._engine(lambda _fallback: False)
        with patch.object(engine, "_download_video_stream") as download:
            result = engine._download_media_item(self._item())

        self.assertIsNone(result)
        download.assert_not_called()

    def test_yes_downloads_at_the_fallback_height(self):
        engine = self._engine(lambda _fallback: True)
        with patch.object(
            engine, "_download_video_stream", return_value=True
        ) as download:
            result = engine._download_media_item(self._item())

        self.assertTrue(result)
        download.assert_called_once()

    def test_without_a_confirmer_the_fallback_is_used(self):
        engine = self._engine(None)
        with patch.object(
            engine, "_download_video_stream", return_value=True
        ) as download:
            result = engine._download_media_item(self._item())

        self.assertTrue(result)
        download.assert_called_once()


if __name__ == "__main__":
    unittest.main()
