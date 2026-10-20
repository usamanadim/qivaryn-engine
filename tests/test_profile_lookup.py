import unittest
from unittest.mock import MagicMock, patch

import requests

from qivaryn.client import InstagramClient


class ProfileLookupTests(unittest.TestCase):
    def setUp(self):
        # Network fallbacks are disabled so the unit tests never reach the network.
        self.client = InstagramClient(allow_network_fallbacks=False)

    def test_resolves_id_from_profile_api(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": {"user": {"id": "12345"}}}
        with patch.object(self.client.session, "get", return_value=response) as get:
            self.assertEqual(self.client.get_user_id("sample_user"), "12345")

        self.assertEqual(
            get.call_args.kwargs["params"],
            {"username": "sample_user"},
        )

    def test_html_fallback_returns_profile_id_after_api_failure(self):
        api_error = requests.ConnectionError("connection unavailable")
        page = MagicMock(
            status_code=200,
            text='<script>{"profile_id":"12345"}</script>',
        )
        with patch.object(
            self.client.session, "get", side_effect=[api_error, page]
        ):
            self.assertEqual(self.client.get_user_id("sample_user"), "12345")

    def test_failure_reports_api_and_profile_page_status(self):
        api_response = MagicMock(status_code=401)
        api_response.json.return_value = {"message": "login_required"}
        page_response = MagicMock(status_code=403)

        with patch.object(
            self.client.session, "get", side_effect=[api_response, page_response]
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                r"profile API returned HTTP 401 \(login_required\); "
                r"profile page returned HTTP 403",
            ):
                self.client.get_user_id("sample_user")


if __name__ == "__main__":
    unittest.main()
