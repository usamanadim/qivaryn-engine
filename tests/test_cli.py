import unittest
from unittest.mock import patch

from click.testing import CliRunner

from qivaryn import InstagramEngine
from qivaryn.cli import (
    PLATFORM_ENGINES,
    _confirm_quality_fallback,
    _create_engine,
    cli,
)
from qivaryn.downloader import DEFAULT_OUTPUT_DIR, QivarynEngine


class PlatformSelectionTests(unittest.TestCase):
    def test_instagram_is_the_default_engine(self):
        self.assertIs(PLATFORM_ENGINES["instagram"], QivarynEngine)

    def test_instagram_platform_selects_instagram_engine(self):
        engine_class = unittest.mock.Mock()
        with patch.dict("qivaryn.cli.PLATFORM_ENGINES", {"instagram": engine_class}):
            _create_engine(platform="instagram", output_dir="media")

        engine_class.assert_called_once_with(platform="instagram", output_dir="media")

    def test_single_command_keeps_instagram_as_default(self):
        runner = CliRunner()
        with patch("qivaryn.cli._create_engine") as create_engine:
            result = runner.invoke(cli, ["single", "SHORTCODE"])

        self.assertEqual(result.exit_code, 0, result.output)
        create_engine.assert_called_once_with(
            platform="instagram",
            output_dir=DEFAULT_OUTPUT_DIR,
            db_path="qivaryn_state.db",
            sessionid=None,
            cookies_file=None,
            quality="best",
            quality_provided=False,
        )

    def test_unsupported_platform_is_rejected(self):
        runner = CliRunner()
        with patch("qivaryn.cli._create_engine") as create_engine:
            result = runner.invoke(
                cli, ["single", "--platform", "myspace", "SHORTCODE"]
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Invalid value for '--platform'", result.output)
        create_engine.assert_not_called()

    def test_download_command_passes_count_and_media_type(self):
        runner = CliRunner()
        mock_engine = unittest.mock.Mock()
        with patch("qivaryn.cli._create_engine", return_value=mock_engine) as create_engine:
            result = runner.invoke(
                cli, ["download", "testuser", "--type", "reels", "--count", "100"]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        create_engine.assert_called_once()
        mock_engine.download_account.assert_called_once_with(
            "testuser", media_type_filter="reels", max_pages=None, force=False, count=100
        )

    def test_download_command_supports_limit_alias_and_video_type(self):
        runner = CliRunner()
        mock_engine = unittest.mock.Mock()
        with patch("qivaryn.cli._create_engine", return_value=mock_engine):
            result = runner.invoke(
                cli, ["download", "testuser", "--type", "videos", "-n", "50"]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        mock_engine.download_account.assert_called_once_with(
            "testuser", media_type_filter="videos", max_pages=None, force=False, count=50
        )

    def test_download_command_rejects_non_positive_count(self):
        runner = CliRunner()
        with patch("qivaryn.cli._create_engine"):
            result = runner.invoke(
                cli, ["download", "testuser", "--count", "0"]
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("positive integer", result.output)

    def test_universal_command_routes_url_and_download_options(self):
        runner = CliRunner()
        mock_engine = unittest.mock.Mock()
        with patch("qivaryn.cli._platform_for_url", return_value="instagram"), \
             patch("qivaryn.cli._create_engine", return_value=mock_engine) as create_engine:
            result = runner.invoke(
                cli,
                [
                    "https://www.instagram.com/username/",
                    "--count", "10",
                    "--cookies", "cookies.txt",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        create_engine.assert_called_once_with(
            platform="instagram",
            output_dir=DEFAULT_OUTPUT_DIR,
            db_path="qivaryn_state.db",
            sessionid=None,
            cookies_file="cookies.txt",
            consecutive_threshold=0,
            quality="best",
            quality_provided=False,
        )
        mock_engine.download_url.assert_called_once_with(
            "https://www.instagram.com/username/",
            max_pages=None,
            force=False,
            count=10,
            media_type=None,
        )

    def test_youtube_quality_is_forwarded_to_the_engine(self):
        runner = CliRunner()
        mock_engine = unittest.mock.Mock()
        engine_class = unittest.mock.Mock(return_value=mock_engine)
        with patch.dict("qivaryn.cli.PLATFORM_ENGINES", {"youtube": engine_class}):
            result = runner.invoke(
                cli,
                [
                    "single",
                    "https://youtu.be/abcdefghijk",
                    "--platform", "youtube",
                    "--quality", "720",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        engine_class.assert_called_once()
        kwargs = engine_class.call_args.kwargs
        self.assertEqual(kwargs["quality"], "720")
        self.assertTrue(callable(kwargs["quality_confirmer"]))

    def test_quality_warns_and_is_ignored_for_non_youtube(self):
        runner = CliRunner()
        mock_engine = unittest.mock.Mock()
        engine_class = unittest.mock.Mock(return_value=mock_engine)
        with patch.dict("qivaryn.cli.PLATFORM_ENGINES", {"instagram": engine_class}):
            result = runner.invoke(cli, ["single", "SHORTCODE", "--quality", "720"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("only supported for YouTube", result.output)
        kwargs = engine_class.call_args.kwargs
        self.assertNotIn("quality", kwargs)
        self.assertNotIn("quality_confirmer", kwargs)

    def test_explicit_best_quality_warns_on_non_youtube(self):
        runner = CliRunner()
        mock_engine = unittest.mock.Mock()
        engine_class = unittest.mock.Mock(return_value=mock_engine)
        with patch.dict("qivaryn.cli.PLATFORM_ENGINES", {"instagram": engine_class}):
            result = runner.invoke(cli, ["single", "SHORTCODE", "--quality", "best"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("only supported for YouTube", result.output)
        kwargs = engine_class.call_args.kwargs
        self.assertNotIn("quality", kwargs)

    def test_quality_confirmation_mentions_the_lower_resolution(self):
        with patch("qivaryn.cli.click.confirm", return_value=True) as confirm:
            allowed = _confirm_quality_fallback(
                {"requested": 720, "selected": 480}
            )

        self.assertTrue(allowed)
        message = confirm.call_args.args[0]
        self.assertIn("720p", message)
        self.assertIn("480p", message)

    def test_quality_confirmation_handles_no_lower_resolution(self):
        with patch("qivaryn.cli.click.confirm", return_value=False) as confirm:
            allowed = _confirm_quality_fallback(
                {"requested": 144, "selected": 360, "no_lower": True}
            )

        self.assertFalse(allowed)
        self.assertIn("no lower resolution", confirm.call_args.args[0])

    def test_invalid_quality_is_rejected(self):
        runner = CliRunner()
        with patch("qivaryn.cli._create_engine") as create_engine:
            result = runner.invoke(cli, ["single", "SHORTCODE", "--quality", "ultra"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Use 'best', 'worst'", result.output)
        create_engine.assert_not_called()

    def test_quality_accepts_a_p_suffix(self):
        runner = CliRunner()
        with patch("qivaryn.cli._create_engine") as create_engine:
            result = runner.invoke(
                cli,
                [
                    "single", "SHORTCODE",
                    "--platform", "youtube",
                    "--quality", "720p",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(create_engine.call_args.kwargs["quality"], "720")

    def test_url_command_forwards_youtube_quality(self):
        runner = CliRunner()
        mock_engine = unittest.mock.Mock()
        engine_class = unittest.mock.Mock(return_value=mock_engine)
        with patch("qivaryn.cli._platform_for_url", return_value="youtube"), \
             patch.dict("qivaryn.cli.PLATFORM_ENGINES", {"youtube": engine_class}):
            result = runner.invoke(
                cli,
                ["https://youtu.be/abcdefghijk", "--quality", "1080"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        kwargs = engine_class.call_args.kwargs
        self.assertEqual(kwargs["quality"], "1080")

    def test_platform_engine_alias_is_public(self):
        self.assertIs(InstagramEngine, QivarynEngine)


if __name__ == "__main__":
    unittest.main()
