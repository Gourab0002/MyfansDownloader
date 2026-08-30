from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class FlaskRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import app

        app.config["TESTING"] = True
        cls.client = app.test_client()

    def test_index_ok(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Download Content", resp.data)

    def test_settings_page_is_html_not_json(self):
        resp = self.client.get("/settings")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Settings", resp.data)
        self.assertFalse(resp.is_json)

    def test_settings_api_json(self):
        resp = self.client.get("/api/settings")
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertIn("filename_pattern", payload)
        self.assertIn("thread_count", payload)
        self.assertIn("auth_token_set", payload)
        self.assertNotIn("auth_token", payload)

    def test_download_requires_target(self):
        resp = self.client.post("/download", json={})
        self.assertEqual(resp.status_code, 400)

    def test_test_post_hides_exception_details(self):
        from unittest.mock import patch

        import app as app_module

        with patch.object(app_module, "read_headers_from_file", return_value={}), patch.object(
            app_module.downloader, "get_video_info", side_effect=RuntimeError("secret traceback details")
        ):
            resp = self.client.get("/test_post/abc")
        self.assertEqual(resp.status_code, 500)
        payload = resp.get_json()
        self.assertEqual(payload["error"], "An internal error has occurred")
        self.assertNotIn("secret traceback details", resp.get_data(as_text=True))

    def test_test_post_hides_lookup_error_details(self):
        from unittest.mock import patch

        import app as app_module

        with patch.object(app_module, "read_headers_from_file", return_value={}), patch.object(
            app_module.downloader,
            "get_video_info",
            return_value=(None, None, "HTTPSConnectionPool secret"),
        ):
            resp = self.client.get("/test_post/abc")
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("HTTPSConnectionPool", resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["error"], "Post is unavailable or has no video")


if __name__ == "__main__":
    unittest.main()
