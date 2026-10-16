import unittest
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from qivaryn.client import InstagramClient
from qivaryn.downloader import QivarynEngine


class DashManifestAndResolutionTests(unittest.TestCase):
    def test_parses_manifest_with_xml_ampersands_and_extracts_1080p(self):
        client = InstagramClient()
        # Manifest containing standard XML &amp; query parameters in BaseURL
        xml_manifest = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011">
  <Period>
    <AdaptationSet contentType="video">
      <Representation id="1" width="720" height="1280" bandwidth="800000" FBQualityLabel="720p">
        <BaseURL>https://cdn.example/720.mp4?cat=1&amp;token=abc</BaseURL>
      </Representation>
      <Representation id="2" width="1080" height="1920" bandwidth="1500000" FBQualityLabel="1080p">
        <BaseURL>https://cdn.example/1080.mp4?cat=1&amp;token=xyz</BaseURL>
      </Representation>
    </AdaptationSet>
    <AdaptationSet contentType="audio">
      <Representation id="3" bandwidth="128000" codecs="mp4a.40.2">
        <BaseURL>https://cdn.example/audio.m4a?cat=2&amp;token=123</BaseURL>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""
        v, a = client._extract_dash_streams(xml_manifest)
        self.assertIsNotNone(v)
        self.assertIsNotNone(a)
        self.assertEqual(v["width"], 1080)
        self.assertEqual(v["height"], 1920)
        self.assertTrue(v["is_1080p"])
        self.assertEqual(v["url"], "https://cdn.example/1080.mp4?cat=1&token=xyz")
        self.assertEqual(a["url"], "https://cdn.example/audio.m4a?cat=2&token=123")

    def test_select_best_video_prefers_1080p_dash(self):
        client = InstagramClient()
        item = {
            "code": "TEST_DASH_1080P",
            "media_type": 2,
            "video_dash_manifest": """<MPD><Period><AdaptationSet contentType="video"><Representation width="1080" height="1920" FBQualityLabel="1080p"><BaseURL>https://cdn.example/1080.mp4</BaseURL></Representation></AdaptationSet><AdaptationSet contentType="audio"><Representation codecs="mp4a.40.2"><BaseURL>https://cdn.example/audio.m4a</BaseURL></Representation></AdaptationSet></Period></MPD>""",
            "video_versions": [
                {"width": 720, "height": 1280, "url": "https://cdn.example/prog_720.mp4"}
            ]
        }
        v_url, a_url, qual = client._select_best_video(item)
        self.assertEqual(v_url, "https://cdn.example/1080.mp4")
        self.assertEqual(a_url, "https://cdn.example/audio.m4a")
        self.assertIn("1080p", qual)

    def test_select_best_video_prefers_1080p_progressive_when_dash_is_720p(self):
        client = InstagramClient()
        item = {
            "code": "TEST_PROG_1080P",
            "media_type": 2,
            "video_dash_manifest": """<MPD><Period><AdaptationSet contentType="video"><Representation width="720" height="1280" FBQualityLabel="720p"><BaseURL>https://cdn.example/720.mp4</BaseURL></Representation></AdaptationSet><AdaptationSet contentType="audio"><Representation codecs="mp4a.40.2"><BaseURL>https://cdn.example/audio.m4a</BaseURL></Representation></AdaptationSet></Period></MPD>""",
            "video_versions": [
                {"width": 1080, "height": 1920, "url": "https://cdn.example/prog_1080.mp4"}
            ]
        }
        v_url, a_url, qual = client._select_best_video(item)
        self.assertEqual(v_url, "https://cdn.example/prog_1080.mp4")
        self.assertEqual(a_url, "https://cdn.example/audio.m4a")
        self.assertIn("1080", qual)

    def test_prefers_clips_standard_aac_over_xheaac_dash(self):
        client = InstagramClient()
        item = {
            "code": "TEST_XHEAAC_AVOIDANCE",
            "media_type": 2,
            "video_dash_manifest": """<MPD><Period><AdaptationSet contentType="video"><Representation width="1080" height="1920" FBQualityLabel="1080p"><BaseURL>https://cdn.example/1080.mp4</BaseURL></Representation></AdaptationSet><AdaptationSet contentType="audio"><Representation codecs="mp4a.40.42" bandwidth="41000"><BaseURL>https://cdn.example/xheaac-audio.m4a</BaseURL></Representation></AdaptationSet></Period></MPD>""",
            "clips_metadata": {
                "original_sound_info": {
                    "progressive_download_url": "https://cdn.example/clean-standard-aac.mp4",
                    "audio_asset_start_time_in_ms": 0
                }
            }
        }
        v_url, a_url, qual = client._select_best_video(item)
        self.assertEqual(v_url, "https://cdn.example/1080.mp4")
        self.assertEqual(a_url, "https://cdn.example/clean-standard-aac.mp4")



class StreamMergeUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.output_path = str(Path(self.temp_dir.name) / "test.mp4")
        self.engine = QivarynEngine.__new__(QivarynEngine)

    def test_ffmpeg_command_construct(self):
        with patch.object(self.engine, "_download_file", return_value=True), \
             patch("shutil.which", return_value="ffmpeg"), \
             patch("subprocess.run") as mock_run, \
             patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=1024), \
             patch("os.replace") as mock_replace:
            mock_run.return_value = MagicMock(returncode=0)
            success = self.engine._download_video_stream(
                video_url="https://cdn.example/v.mp4",
                audio_url="https://cdn.example/a.m4a",
                file_path=self.output_path,
                desc="test reel",
                audio_start_ms=1500
            )
            self.assertTrue(success)
            cmd = mock_run.call_args[0][0]
            self.assertIn("-c:v", cmd)
            self.assertIn("copy", cmd)
            self.assertIn("-ss", cmd)
            self.assertIn("1.500", cmd)


if __name__ == "__main__":
    unittest.main()
