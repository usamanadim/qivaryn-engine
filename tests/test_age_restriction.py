import unittest
from unittest.mock import MagicMock, patch
from qivaryn.client import InstagramClient
from qivaryn.downloader import QivarynEngine


class TestAgeRestrictionAndSession(unittest.TestCase):
    def test_age_restricted_post_without_session(self):
        client = InstagramClient()

        # Mobile API returns 403 login_required
        mock_mobile_resp = MagicMock()
        mock_mobile_resp.status_code = 403
        mock_mobile_resp.json.return_value = {"message": "login_required"}

        # HTML page returns failure_reason: MA with restricted_age: 19
        mock_html_resp = MagicMock()
        mock_html_resp.status_code = 200
        mock_html_resp.text = '{"failure_reason":"MA","restricted_age":19,"page_type":"MEDIA"}'

        with patch.object(client.session, "get", side_effect=[mock_mobile_resp, mock_html_resp, mock_html_resp]):
            with self.assertRaises(RuntimeError) as ctx:
                client.fetch_post_by_shortcode("AgeGatedShortcode")
            self.assertIn("age-restricted", str(ctx.exception).lower())
            self.assertIn("19+", str(ctx.exception))

    def test_age_restricted_post_with_logged_out_session(self):
        client = InstagramClient(sessionid="fake_session_123")

        # Mobile API returns 403 login_required with You've Been Logged Out
        mock_mobile_resp = MagicMock()
        mock_mobile_resp.status_code = 403
        mock_mobile_resp.json.return_value = {
            "message": "login_required",
            "error_title": "You've Been Logged Out",
            "logout_reason": 8,
        }

        # HTML page returns failure_reason: MA with restricted_age: 19
        mock_html_resp = MagicMock()
        mock_html_resp.status_code = 200
        mock_html_resp.text = '{"failure_reason":"MA","restricted_age":19,"page_type":"MEDIA"}'

        with patch.object(client.session, "get", side_effect=[mock_mobile_resp, mock_html_resp, mock_html_resp]):
            with self.assertRaises(RuntimeError) as ctx:
                client.fetch_post_by_shortcode("AgeGatedShortcode")
            self.assertIn("logged out", str(ctx.exception).lower())
            self.assertIn("age-restricted", str(ctx.exception).lower())

    def test_profile_gating_recorded_and_reported(self):
        client = InstagramClient()

        # Mock HTML response for profile lookup containing gating data
        mock_html_resp = MagicMock()
        mock_html_resp.status_code = 200
        mock_html_resp.text = (
            '{"props":{"id":"98765"},'
            '"gating":{"title":"Restricted profile","description":"You must be 19 years old or over to see this profile"}}'
        )

        # Route 1 (web_profile_info) fails with 429
        mock_api_resp = MagicMock()
        mock_api_resp.status_code = 429

        with patch.object(client.session, "get", side_effect=[mock_api_resp, mock_html_resp]):
            uid = client.get_user_id("restricted_user")
            self.assertEqual(uid, "98765")
            self.assertIn("restricted_user", client.restricted_profiles)
            self.assertIn("19 years old", client.restricted_profiles["restricted_user"])

    def test_age_restricted_bulk_reels_without_session(self):
        client = InstagramClient()
        client.restricted_profiles["restricted_user"] = "Restricted profile: 19+ only"

        # Mock clips API returning empty items
        mock_clips_resp = MagicMock()
        mock_clips_resp.status_code = 200
        mock_clips_resp.json.return_value = {"items": [], "paging_info": {"more_available": False}}

        with patch.object(client.session, "get", return_value=mock_clips_resp):
            with self.assertRaises(RuntimeError) as ctx:
                list(client.fetch_user_raw_reels_paginated("98765", username="restricted_user"))
            self.assertIn("age-restricted", str(ctx.exception).lower())
            self.assertIn("active adult session", str(ctx.exception).lower())

    def test_age_restricted_bulk_reels_with_logged_out_session(self):
        client = InstagramClient(sessionid="fake_session_123")
        client.restricted_profiles["restricted_user"] = "Restricted profile: 19+ only"

        # Mock clips API returning 403 login_required
        mock_clips_resp = MagicMock()
        mock_clips_resp.status_code = 403
        mock_clips_resp.json.return_value = {
            "message": "login_required",
            "error_title": "You've Been Logged Out",
            "logout_reason": 8,
        }

        with patch.object(client.session, "get", return_value=mock_clips_resp):
            with self.assertRaises(RuntimeError) as ctx:
                list(client.fetch_user_raw_reels_paginated("98765", username="restricted_user"))
            self.assertIn("logged out", str(ctx.exception).lower())
            self.assertIn("age-restricted", str(ctx.exception).lower())

    def test_age_restricted_bulk_reels_fallback_to_posts(self):
        client = InstagramClient(sessionid="valid_session")

        # Mock clips API returning empty items
        mock_clips_resp = MagicMock()
        mock_clips_resp.status_code = 200
        mock_clips_resp.json.return_value = {"items": [], "paging_info": {"more_available": False}}

        # Mock posts feed returning a video reel
        mock_posts_resp = MagicMock()
        mock_posts_resp.status_code = 200
        mock_posts_resp.json.return_value = {
            "items": [
                {
                    "code": "ReelFromFeed",
                    "media_type": 2,
                    "video_versions": [{"url": "http://video.mp4", "width": 1080, "height": 1920}],
                }
            ],
            "more_available": False,
        }

        with patch.object(client.session, "get", side_effect=[mock_clips_resp, mock_posts_resp]):
            items = list(client.fetch_user_raw_reels_paginated("98765", username="restricted_user"))
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["code"], "ReelFromFeed")


if __name__ == "__main__":
    unittest.main()
