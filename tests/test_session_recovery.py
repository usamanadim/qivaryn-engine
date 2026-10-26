"""Tests for recovering reels when Instagram reports a logged-out session.

A valid cookies.txt can still get a per-media 403 ("You've Been Logged Out")
for a reel that is age-restricted, removed, or temporarily throttled.  The
client must try the web route, keep the session, and only blame the cookies
when a session check actually fails.
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

from qivaryn.client import InstagramClient


def _response(payload=None, status=200, text=""):
    response = MagicMock(status_code=status, text=text)
    response.json.return_value = payload if payload is not None else {}
    return response


def _reel_payload(code="REEL_1"):
    return {
        "items": [
            {
                "media": {
                    "code": code,
                    "media_type": 2,
                    "taken_at": 1700000000,
                    "video_versions": [
                        {"url": "https://cdn.example/reel.mp4", "width": 720, "height": 1280}
                    ],
                }
            }
        ],
        "paging_info": {"more_available": False},
    }


class AppIdHeaderTests(unittest.TestCase):
    def test_api_requests_carry_the_mobile_app_id(self):
        client = InstagramClient(allow_network_fallbacks=False)
        self.assertEqual(
            client.session.headers["X-IG-App-ID"], InstagramClient.MOBILE_APP_ID
        )

    def test_web_headers_use_the_web_app_id(self):
        client = InstagramClient(allow_network_fallbacks=False)
        headers = client._web_api_headers()
        self.assertEqual(headers["X-IG-App-ID"], InstagramClient.WEB_APP_ID)
        self.assertIn("Chrome", headers["User-Agent"])

    def test_web_headers_drop_the_mobile_app_headers(self):
        client = InstagramClient(allow_network_fallbacks=False)
        prepared = client.session.prepare_request(
            requests.Request(
                "GET",
                "https://www.instagram.com/",
                headers=client._web_api_headers(),
            )
        )
        self.assertNotIn("X-IG-Capabilities", prepared.headers)
        self.assertNotIn("X-IG-Connection-Type", prepared.headers)
        self.assertEqual(
            prepared.headers["X-IG-App-ID"], InstagramClient.WEB_APP_ID
        )


class SessionCheckTests(unittest.TestCase):
    def test_a_logged_out_200_response_is_not_treated_as_valid(self):
        client = InstagramClient(sessionid="s", allow_network_fallbacks=False)
        not_logged_in = _response(
            {"message": "something went wrong", "status": "fail", "status_code": "200"}
        )

        with patch.object(client.session, "get", return_value=not_logged_in):
            self.assertFalse(client._session_is_valid())

    def test_a_real_user_payload_marks_the_session_valid(self):
        client = InstagramClient(sessionid="s", allow_network_fallbacks=False)
        logged_in = _response(
            {"status": "ok", "user": {"pk": "123", "username": "someone"}}
        )

        with patch.object(client.session, "get", return_value=logged_in):
            self.assertTrue(client._session_is_valid())

    def test_browser_cookie_session_probe_asks_the_website(self):
        client = InstagramClient(allow_network_fallbacks=False)
        client.cookies_file = "cookies.txt"
        logged_in = _response(
            {"status": "ok", "user": {"pk": "123", "username": "someone"}}
        )

        with patch.object(client.session, "get", return_value=logged_in) as get:
            self.assertTrue(client._session_is_valid())

        self.assertTrue(
            get.call_args.args[0].startswith("https://www.instagram.com/")
        )
        self.assertEqual(
            get.call_args.kwargs["headers"]["X-IG-App-ID"],
            InstagramClient.WEB_APP_ID,
        )

    def test_www_only_cookies_are_mirrored_to_the_api_domain(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "cookies.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    "# Netscape HTTP Cookie File\n"
                    "www.instagram.com\tFALSE\t/\tFALSE\t9999999999\tsessionid\tfake-session\n"
                    "www.instagram.com\tFALSE\t/\tFALSE\t9999999999\tcsrftoken\tfake-csrf\n"
                )
            client = InstagramClient(cookies_file=path, allow_network_fallbacks=False)

        request = requests.Request(
            "GET", "https://i.instagram.com/api/v1/accounts/current_user/"
        )
        prepared = client.session.prepare_request(request)
        cookie_header = prepared.headers.get("Cookie", "")
        self.assertIn("sessionid=fake-session", cookie_header)
        self.assertIn("csrftoken=fake-csrf", cookie_header)


class MediaInfoRecoveryTests(unittest.TestCase):
    def test_browser_cookie_sessions_ask_the_website_first(self):
        client = InstagramClient(allow_network_fallbacks=False)
        client.cookies_file = "cookies.txt"
        recovered = _response(
            {
                "items": [
                    {
                        "code": "COOKIE",
                        "media_type": 2,
                        "video_versions": [
                            {"url": "https://cdn.example/c.mp4", "width": 720, "height": 1280}
                        ],
                    }
                ]
            }
        )

        with patch.object(client.session, "get", return_value=recovered) as get:
            item = client.fetch_post_by_shortcode("COOKIE")

        self.assertEqual(item["shortcode"], "COOKIE")
        self.assertEqual(
            get.call_args.kwargs["headers"]["X-IG-App-ID"],
            InstagramClient.WEB_APP_ID,
        )
        self.assertTrue(
            get.call_args.args[0].startswith("https://www.instagram.com/api/v1/media/")
        )

    def test_web_identity_retry_recovers_a_reel_the_app_api_refused(self):
        client = InstagramClient(allow_network_fallbacks=True)
        refused = _response(
            {
                "message": "login_required",
                "error_title": "You've Been Logged Out",
                "logout_reason": 8,
            },
            status=403,
        )
        recovered = _response(
            {
                "items": [
                    {
                        "code": "RECOVERED",
                        "media_type": 2,
                        "video_versions": [
                            {"url": "https://cdn.example/r.mp4", "width": 720, "height": 1280}
                        ],
                    }
                ]
            }
        )

        with patch.object(client.session, "get", side_effect=[refused, recovered]) as get:
            item = client.fetch_post_by_shortcode("RECOVERED")

        self.assertEqual(item["shortcode"], "RECOVERED")
        self.assertEqual(len(get.call_args_list), 2)
        web_headers = get.call_args_list[1].kwargs["headers"]
        self.assertEqual(web_headers["X-IG-App-ID"], InstagramClient.WEB_APP_ID)
        self.assertEqual(web_headers["X-IG-WWW-Claim"], "0")

    def test_embed_page_recovers_a_reel_when_both_apis_refuse(self):
        client = InstagramClient(allow_network_fallbacks=True)
        refused = _response(
            {
                "message": "login_required",
                "error_title": "You've Been Logged Out",
                "logout_reason": 8,
            },
            status=403,
        )
        identity_refused = _response({"message": "login_required"}, status=403)
        embed_html = _response(
            status=200,
            text=(
                '<script>{"video_url":"https:\\/\\/cdn.example\\/embed.mp4",'
                '"display_url":"https:\\/\\/cdn.example\\/cover.jpg"}</script>'
            ),
        )

        with patch.object(
            client.session, "get",
            side_effect=[refused, identity_refused, embed_html],
        ):
            item = client.fetch_post_by_shortcode("EMBEDDED")

        self.assertEqual(item["shortcode"], "EMBEDDED")
        self.assertEqual(item["video_url"], "https://cdn.example/embed.mp4")
        self.assertEqual(item["display_url"], "https://cdn.example/cover.jpg")

    def test_web_json_route_is_the_third_recovery_attempt(self):
        client = InstagramClient(allow_network_fallbacks=True)
        refused = _response(
            {
                "message": "login_required",
                "error_title": "You've Been Logged Out",
                "logout_reason": 8,
            },
            status=403,
        )
        identity_refused = _response({"message": "login_required"}, status=403)
        embed_refused = _response(status=404, text="")
        web_json = _response(
            {
                "items": [
                    {
                        "code": "WEBJSON",
                        "media_type": 2,
                        "video_versions": [
                            {"url": "https://cdn.example/w.mp4", "width": 720, "height": 1280}
                        ],
                    }
                ]
            }
        )

        with patch.object(
            client.session, "get",
            side_effect=[refused, identity_refused, embed_refused, embed_refused, web_json],
        ):
            item = client.fetch_post_by_shortcode("WEBJSON")

        self.assertEqual(item["shortcode"], "WEBJSON")

    def test_graphql_route_recovers_a_reel_when_the_other_web_routes_fail(self):
        client = InstagramClient(
            sessionid="valid", allow_network_fallbacks=True
        )
        refused = _response({"message": "login_required"}, status=403)
        empty_page = _response(status=404, text="")
        graphql = _response(
            {
                "data": {
                    "xdt_shortcode_media": {
                        "shortcode": "GRAPHQL",
                        "video_url": "https://cdn.example/g.mp4",
                        "display_url": "https://cdn.example/g.jpg",
                        "taken_at_timestamp": 1700000000,
                        "dimensions": {"width": 720, "height": 1280},
                        "owner": {"username": "someone"},
                    }
                }
            }
        )

        with patch.object(
            client.session,
            "get",
            side_effect=[
                refused,      # app API
                refused,      # website API
                empty_page,   # embed: reel
                empty_page,   # embed: p
                empty_page,   # web JSON: reel
                empty_page,   # web JSON: p
                graphql,      # GraphQL
            ],
        ):
            item = client.fetch_post_by_shortcode("GRAPHQL")

        self.assertEqual(item["shortcode"], "GRAPHQL")
        self.assertEqual(item["video_url"], "https://cdn.example/g.mp4")
        self.assertEqual(item["account"], "someone")
        self.assertEqual(item["timestamp"], "1700000000")

    def test_web_json_route_is_not_used_when_fallbacks_are_disabled(self):
        client = InstagramClient(sessionid="valid", allow_network_fallbacks=False)
        refused = _response(
            {
                "message": "login_required",
                "error_title": "You've Been Logged Out",
                "logout_reason": 8,
            },
            status=403,
        )
        login_wall = _response(status=200, text="<html>Please log in</html>")

        with patch.object(client, "_session_is_valid", return_value=False), \
                patch.object(
                    client.session, "get",
                    side_effect=[refused, login_wall, login_wall],
                ) as get:
            with self.assertRaises(RuntimeError):
                client.fetch_post_by_shortcode("REFUSED")

        for call in get.call_args_list:
            self.assertNotIn("__a", call.kwargs.get("params") or {})

    def test_a_valid_session_does_not_get_blamed_for_a_refused_reel(self):
        client = InstagramClient(sessionid="valid", allow_network_fallbacks=False)
        refused = _response(
            {
                "message": "login_required",
                "error_title": "You've Been Logged Out",
                "logout_reason": 8,
            },
            status=403,
        )
        login_wall = _response(status=200, text="<html>Please log in</html>")

        with patch.object(client, "_session_is_valid", return_value=True), \
                patch.object(
                    client.session, "get",
                    side_effect=[refused, login_wall, login_wall],
                ):
            with self.assertRaises(RuntimeError) as ctx:
                client.fetch_post_by_shortcode("REFUSED")

        message = str(ctx.exception).lower()
        self.assertIn("session is still valid", message)
        self.assertIn("refused access", message)
        self.assertNotIn("expired or logged out", message)


class ReelsFeedSessionRecoveryTests(unittest.TestCase):
    def test_browser_cookie_sessions_ask_the_website_first(self):
        client = InstagramClient(allow_network_fallbacks=False)
        client.cookies_file = "cookies.txt"
        recovered = _response(_reel_payload())

        with patch.object(client.session, "post", return_value=recovered) as post:
            items = list(client.fetch_user_raw_reels_paginated("123"))

        self.assertEqual([item["code"] for item in items], ["REEL_1"])
        self.assertEqual(
            post.call_args.kwargs["headers"]["X-IG-App-ID"],
            InstagramClient.WEB_APP_ID,
        )

    def test_feed_retries_as_the_website_before_going_anonymous(self):
        client = InstagramClient(sessionid="valid", allow_network_fallbacks=True)
        refused = _response(
            {
                "message": "login_required",
                "error_title": "You've Been Logged Out",
                "logout_reason": 8,
            },
            status=403,
        )
        recovered = _response(_reel_payload())

        with patch.object(
            client.session, "post", side_effect=[refused, recovered]
        ) as post, patch("qivaryn.client.requests.post") as anonymous_post:
            items = list(client.fetch_user_raw_reels_paginated("123"))

        self.assertEqual([item["code"] for item in items], ["REEL_1"])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            post.call_args_list[1].kwargs["headers"]["X-IG-App-ID"],
            InstagramClient.WEB_APP_ID,
        )
        anonymous_post.assert_not_called()

    def test_anonymous_retry_is_still_used_for_public_profiles(self):
        client = InstagramClient(allow_network_fallbacks=True)
        refused = _response({"message": "login_required"}, status=403)
        web_refused = _response({"message": "login_required"}, status=403)
        anonymous_ok = _response(_reel_payload())

        with patch.object(
            client.session, "post", side_effect=[refused, web_refused]
        ), patch(
            "qivaryn.client.requests.post", return_value=anonymous_ok
        ) as anonymous_post:
            items = list(client.fetch_user_raw_reels_paginated("123"))

        self.assertEqual([item["code"] for item in items], ["REEL_1"])
        anonymous_post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
