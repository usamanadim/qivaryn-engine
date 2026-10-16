import unittest
from unittest.mock import patch

from click.testing import CliRunner

from qivaryn import InstagramEngine
from qivaryn.cli import PLATFORM_ENGINES, _create_engine, cli
from qivaryn.downloader import QivarynEngine


class PlatformSelectionTests(unittest.TestCase):
    def test_instagram_is_the_default_engine(self):
        self.assertIs(PLATFORM_ENGINES["instagram"], QivarynEngine)

    def test_instagram_platform_selects_instagram_engine(self):
        engine_class = unittest.mock.Mock()
        with patch.dict("qivaryn.cli.PLATFORM_ENGINES", {"instagram": engine_class}):
            _create_engine(platform="instagram", output_dir="media")

        engine_class.assert_called_once_with(output_dir="media")

    def test_single_command_keeps_instagram_as_default(self):
        runner = CliRunner()
        with patch("qivaryn.cli._create_engine") as create_engine:
            result = runner.invoke(cli, ["single", "SHORTCODE"])

        self.assertEqual(result.exit_code, 0, result.output)
        create_engine.assert_called_once_with(
            platform="instagram",
            output_dir="./downloads",
            db_path="qivaryn_state.db",
            sessionid=None,
            cookies_file=None,
        )

    def test_unsupported_platform_is_rejected(self):
        runner = CliRunner()
        with patch("qivaryn.cli._create_engine") as create_engine:
            result = runner.invoke(
                cli, ["single", "--platform", "tiktok", "SHORTCODE"]
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Invalid value for '--platform'", result.output)
        create_engine.assert_not_called()

    def test_platform_engine_alias_is_public(self):
        self.assertIs(InstagramEngine, QivarynEngine)


if __name__ == "__main__":
    unittest.main()
