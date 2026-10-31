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
        self.assertIn(b"Start download", resp.data)
        self.assertIn(b"Activity", resp.data)
        self.assertIn(b"app.css", resp.data)
        self.assertIn(b"stopBtn", resp.data)
        self.assertIn(b"previewCard", resp.data)

    def test_preview_requires_target(self):
        resp = self.client.post("/api/preview", json={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_stop_is_idle_when_nothing_is_running(self):
        import app as app_module

        app_module.cancel_event.clear()
        resp = self.client.post("/download/stop")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "idle")
        self.assertFalse(app_module.cancel_event.is_set())

    def test_stop_sets_cancel_while_a_download_is_running(self):
        import app as app_module

        app_module.cancel_event.clear()
        acquired = app_module.download_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            resp = self.client.post("/download/stop")
            self.assertEqual(resp.get_json()["status"], "stopping")
            self.assertTrue(app_module.cancel_event.is_set())
        finally:
            app_module.cancel_event.clear()
            app_module.download_lock.release()

    def test_open_folder_ignores_client_path(self):
        import tempfile
        from unittest.mock import patch

        import app as app_module

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(app_module, "output_directory", return_value=tmp), patch.object(
                app_module, "reveal_folder"
            ) as reveal:
                resp = self.client.post("/api/open-folder", json={"path": "C:/Windows/System32"})
            self.assertEqual(resp.status_code, 200)
            opened = Path(reveal.call_args[0][0]).resolve()
            self.assertEqual(opened, Path(tmp).resolve())

    def test_library_reads_download_folder_only(self):
        import tempfile
        from unittest.mock import patch

        import app as app_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clip.mp4").write_bytes(b"x")
            (root / "note.txt").write_text("no", encoding="utf-8")
            nested = root / "creator" / "videos"
            nested.mkdir(parents=True)
            (nested / "still.jpg").write_bytes(b"y")
            with patch.object(app_module, "output_directory", return_value=str(root)):
                resp = self.client.get("/api/library")
            self.assertEqual(resp.status_code, 200)
            names = {item["name"] for item in resp.get_json()["files"]}
            self.assertEqual(names, {"clip.mp4", "still.jpg"})

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
