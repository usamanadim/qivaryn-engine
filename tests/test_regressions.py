"""Regression tests for the issues found during the project inspection."""

import inspect
import os
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from qivaryn.cli import cli
from qivaryn.client import InstagramClient
from qivaryn.downloader import QivarynEngine
from qivaryn.files import MEDIA_TYPE_BY_KIND, file_is_present, find_existing_media


class NetworkTimeoutTests(unittest.TestCase):
    """Every outbound HTTP request must carry a timeout so a hung socket cannot block forever."""

    def test_posts_feed_pagination_sends_a_timeout(self):
        client = InstagramClient(allow_network_fallbacks=False)
        page = MagicMock(status_code=200)
        page.json.return_value = {"items": [], "more_available": False}

        with patch.object(client.session, "get", return_value=page) as get:
            list(client.fetch_user_raw_posts_paginated("123"))

        self.assertEqual(get.call_count, 1)
        self.assertIn("timeout", get.call_args.kwargs)
        self.assertGreater(get.call_args.kwargs["timeout"], 0)

    def test_clips_endpoint_sends_a_timeout(self):
        client = InstagramClient(allow_network_fallbacks=False)
        page = MagicMock(status_code=200)
        page.json.return_value = {"items": [], "more_available": False}

        with patch.object(client.session, "post", return_value=page) as post:
            list(client.fetch_user_raw_reels_paginated("123"))

        self.assertEqual(post.call_count, 1)
        self.assertIn("timeout", post.call_args.kwargs)
        self.assertGreater(post.call_args.kwargs["timeout"], 0)


class NoMockDetectionInProductionTests(unittest.TestCase):
    """Production code must not change behaviour based on whether it runs under a mock."""

    def test_client_source_does_not_probe_for_mock_attributes(self):
        with open(inspect.getsourcefile(InstagramClient), "r", encoding="utf-8") as handle:
            source = handle.read()

        self.assertNotIn("assert_called", source)
        self.assertNotIn("hasattr(self.session", source)

    def test_network_fallbacks_can_be_disabled_explicitly(self):
        client = InstagramClient()
        self.assertTrue(client.allow_network_fallbacks)

        offline = InstagramClient(allow_network_fallbacks=False)
        self.assertFalse(offline.allow_network_fallbacks)

    def test_disabled_fallbacks_skip_the_unauthenticated_retry(self):
        client = InstagramClient(sessionid="s", allow_network_fallbacks=False)

        api_error = MagicMock(status_code=401)
        api_error.json.return_value = {"message": "login_required"}
        page = MagicMock(status_code=200, text='<script>{"profile_id":"1"}</script>')

        with patch.object(client.session, "get", side_effect=[api_error, page]), \
             patch("qivaryn.client.requests.get") as clean_get:
            self.assertEqual(client.get_user_id("sample_user"), "1")

        clean_get.assert_not_called()


class PublicMediaParsingTests(unittest.TestCase):
    """The downloader must use the public parsing API instead of a private method."""

    def test_parse_media_item_is_public(self):
        self.assertTrue(callable(InstagramClient.parse_media_item))

    def test_private_alias_remains_available(self):
        self.assertIs(InstagramClient._parse_mobile_media_item, InstagramClient.parse_media_item)

    def test_downloader_calls_the_platform_normalization_hook(self):
        engine = QivarynEngine.__new__(QivarynEngine)
        engine.platform = "instagram"
        engine.output_dir = "./downloads"
        engine.db = MagicMock()
        engine.db.get_all_downloaded_shortcodes.return_value = set()
        engine.consecutive_threshold = 3
        engine.client = MagicMock()
        engine.client.item_id.return_value = "X"
        engine.client.prefilter.return_value = True
        engine.client.matches_filter.return_value = True
        engine.client.normalize_item.return_value = {"id": "X", "media_type": "Reel"}

        raw = {"code": "X", "media_type": 2}
        with patch.object(engine, "_download_media_item", return_value=True):
            engine._scan_items(iter([raw]), "test", "all", 1, False)

        engine.client.normalize_item.assert_called_once_with(raw)

    def test_instagram_normalization_uses_the_public_parser(self):
        client = InstagramClient(allow_network_fallbacks=False)
        raw = {"code": "X", "media_type": 2}

        with patch.object(client, "parse_media_item", return_value={"id": "X"}) as parse:
            client.normalize_item(raw)

        parse.assert_called_once_with(raw)


class ShortcodeValidationTests(unittest.TestCase):
    def test_invalid_character_raises_a_clear_error(self):
        with self.assertRaises(ValueError) as ctx:
            InstagramClient.shortcode_to_id("bad!code")
        self.assertIn("bad!code", str(ctx.exception))

    def test_invalid_shortcode_is_rejected_before_any_request(self):
        client = InstagramClient(allow_network_fallbacks=False)

        with patch.object(client.session, "get") as get:
            with self.assertRaises(RuntimeError) as ctx:
                client.fetch_post_by_shortcode("bad!code")

        get.assert_not_called()
        self.assertIn("not a valid Instagram shortcode", str(ctx.exception))


class CliRoutingTests(unittest.TestCase):
    def test_unknown_command_reports_no_such_command(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["notacommand"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("No such command", result.output)

    def test_bare_url_is_routed_to_the_url_command(self):
        runner = CliRunner()
        mock_engine = MagicMock()

        with patch("qivaryn.cli._platform_for_url", return_value="instagram"), \
             patch("qivaryn.cli._create_engine", return_value=mock_engine):
            result = runner.invoke(cli, ["https://www.instagram.com/username/"])

        self.assertEqual(result.exit_code, 0, result.output)
        mock_engine.download_url.assert_called_once()

    def test_www_prefixed_url_is_routed_to_the_url_command(self):
        runner = CliRunner()
        mock_engine = MagicMock()

        with patch("qivaryn.cli._platform_for_url", return_value="instagram"), \
             patch("qivaryn.cli._create_engine", return_value=mock_engine):
            result = runner.invoke(cli, ["www.instagram.com/username/"])

        self.assertEqual(result.exit_code, 0, result.output)
        mock_engine.download_url.assert_called_once()


class MediaFileHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def _write(self, name, content=b"data"):
        path = os.path.join(self.temp_dir.name, name)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def test_file_is_present_ignores_missing_and_empty_files(self):
        self.assertFalse(file_is_present(os.path.join(self.temp_dir.name, "missing.mp4")))

        empty = self._write("empty.mp4", b"")
        self.assertFalse(file_is_present(empty))

        filled = self._write("filled.mp4", b"12345")
        self.assertTrue(file_is_present(filled))

    def test_find_existing_media_detects_single_items(self):
        self._write("CODE.jpg")
        found = find_existing_media(self.temp_dir.name, "CODE")
        self.assertIsNotNone(found)
        self.assertEqual(found[1], "post")
        self.assertEqual(MEDIA_TYPE_BY_KIND[found[1]], "Post")

    def test_find_existing_media_detects_reels(self):
        self._write("CODE.mp4")
        found = find_existing_media(self.temp_dir.name, "CODE")
        self.assertEqual(found[1], "reel")
        self.assertEqual(MEDIA_TYPE_BY_KIND[found[1]], "Reel")

    def test_find_existing_media_detects_carousels(self):
        self._write("CODE_1.jpg")
        found = find_existing_media(self.temp_dir.name, "CODE")
        self.assertEqual(found[1], "carousel")
        self.assertEqual(MEDIA_TYPE_BY_KIND[found[1]], "Carousel")

    def test_find_existing_media_returns_none_when_absent(self):
        self.assertIsNone(find_existing_media(self.temp_dir.name, "NOTHING"))


class EngineStateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def test_consecutive_threshold_is_coerced_to_an_integer(self):
        engine = QivarynEngine(
            output_dir=self.temp_dir.name,
            db_path=os.path.join(self.temp_dir.name, "state.db"),
            consecutive_threshold="5",
        )
        self.assertEqual(engine.consecutive_threshold, 5)

    def test_default_threshold_is_zero(self):
        engine = QivarynEngine(
            output_dir=self.temp_dir.name,
            db_path=os.path.join(self.temp_dir.name, "state_def.db"),
        )
        self.assertEqual(engine.consecutive_threshold, 0)

    def test_invalid_consecutive_threshold_is_rejected(self):
        with self.assertRaises(ValueError):
            QivarynEngine(
                output_dir=self.temp_dir.name,
                db_path=os.path.join(self.temp_dir.name, "state2.db"),
                consecutive_threshold="not-a-number",
            )

    def test_existing_carousel_on_disk_is_recorded_as_carousel(self):
        db_path = os.path.join(self.temp_dir.name, "state.db")
        engine = QivarynEngine(output_dir=self.temp_dir.name, db_path=db_path)

        with open(os.path.join(self.temp_dir.name, "CODE_1.jpg"), "wb") as handle:
            handle.write(b"slide")

        self.assertTrue(engine._is_item_already_saved("CODE"))
        self.assertEqual(engine.db.get_file_path("CODE"), "carousel_bundle")

    def test_zero_length_carousel_slide_is_not_treated_as_present(self):
        db_path = os.path.join(self.temp_dir.name, "state.db")
        engine = QivarynEngine(output_dir=self.temp_dir.name, db_path=db_path)

        with open(os.path.join(self.temp_dir.name, "CODE_1.jpg"), "wb") as handle:
            handle.write(b"")

        self.assertFalse(engine._is_item_already_saved("CODE"))


class VersionConsistencyTests(unittest.TestCase):
    """The package metadata and the runtime version must never drift apart."""

    def test_pyproject_version_matches_package_version(self):
        import qivaryn

        pyproject = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "pyproject.toml",
        )
        with open(pyproject, "r", encoding="utf-8") as handle:
            content = handle.read()

        match = re.search(r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE)
        self.assertIsNotNone(match, "Could not find a version in pyproject.toml")
        self.assertEqual(match.group(1), qivaryn.__version__)

    def test_distribution_metadata_matches_package_version(self):
        import qivaryn
        from importlib.metadata import PackageNotFoundError, version

        try:
            installed = version("qivaryn-engine")
        except PackageNotFoundError:
            self.skipTest("qivaryn-engine is not installed in this environment")

        self.assertEqual(installed, qivaryn.__version__)


if __name__ == "__main__":
    unittest.main()
