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
        self.assertIn(b"librarySearch", resp.data)
        self.assertIn(b"retryFailed", resp.data)
        self.assertIn(b"installFfmpeg", resp.data)
        self.assertIn(b"jobNotice", resp.data)

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

    def test_job_is_idle_before_a_download(self):
        import app as app_module

        app_module._set_job("idle", "")
        resp = self.client.get("/api/job")
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["state"], "idle")
        self.assertFalse(payload["running"])

    def test_library_groups_pages_and_search(self):
        import os
        import tempfile
        from unittest.mock import patch

        import app as app_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = root / "alpha" / "videos"
            newer = root / "beta" / "images"
            older.mkdir(parents=True)
            newer.mkdir(parents=True)
            one = older / "one.mp4"
            two = older / "two.mp4"
            three = newer / "three.jpg"
            one.write_bytes(b"a")
            two.write_bytes(b"b")
            three.write_bytes(b"c")
            os.utime(one, (100, 100))
            os.utime(two, (200, 200))
            os.utime(three, (300, 300))
            with patch.object(app_module, "output_directory", return_value=str(root)):
                first = self.client.get("/api/library?per_page=2")
                second = self.client.get("/api/library?per_page=2&page=2")
                found = self.client.get("/api/library?q=alpha")
            page = first.get_json()
            self.assertEqual([item["name"] for item in page["files"]], ["three.jpg", "two.mp4"])
            self.assertEqual([group["creator"] for group in page["groups"]], ["beta", "alpha"])
            self.assertEqual(page["pages"], 2)
            self.assertEqual(second.get_json()["files"][0]["name"], "one.mp4")
            searched = found.get_json()
            self.assertEqual(searched["total"], 2)
            self.assertEqual(searched["groups"][0]["creator"], "alpha")

    def test_open_file_stays_inside_the_save_folder(self):
        import tempfile
        from unittest.mock import patch

        import app as app_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "alpha" / "videos"
            folder.mkdir(parents=True)
            target = folder / "one.mp4"
            target.write_bytes(b"a")
            outside = Path(tmp).parent / "outside.mp4"
            outside.write_bytes(b"no")
            try:
                with patch.object(app_module, "output_directory", return_value=str(root)), patch.object(
                    app_module, "reveal_path"
                ) as reveal:
                    blocked = self.client.post("/api/open-file", json={"path": "../outside.mp4"})
                    opened = self.client.post("/api/open-file", json={"path": "alpha/videos/one.mp4"})
                self.assertEqual(blocked.status_code, 400)
                self.assertEqual(opened.status_code, 200)
                self.assertEqual(Path(reveal.call_args[0][0]).resolve(), target.resolve())
            finally:
                outside.unlink(missing_ok=True)

    def test_retry_failed_uses_only_failed_posts(self):
        import tempfile
        import threading
        import time
        from unittest.mock import patch

        import app as app_module
        from scripts.download_state import DownloadState

        started = threading.Event()
        seen = {}

        def fake_start(*_args, **kwargs):
            seen["items"] = kwargs.get("retry_items")
            started.set()
            return {
                "stopped": False,
                "failed": 0,
                "saved": 1,
                "skipped": 0,
                "done": 1,
                "total": 1,
                "error": False,
            }

        with tempfile.TemporaryDirectory() as tmp:
            state = DownloadState(state_dir=tmp)
            state.mark_failed("post-1", "no images", kind="images")
            state.mark_completed("post-2")
            try:
                with patch.object(app_module, "download_state", state), patch.object(
                    app_module.downloader, "start_download", side_effect=fake_start
                ):
                    empty = DownloadState(state_dir=tmp + "-empty")
                    with patch.object(app_module, "download_state", empty):
                        missing = self.client.post("/api/retry-failed")
                    self.assertEqual(missing.status_code, 400)
                    with patch.object(app_module, "download_state", state):
                        resp = self.client.post("/api/retry-failed")
                        self.assertEqual(resp.status_code, 200)
                        self.assertTrue(started.wait(2))
                        for _ in range(40):
                            if not app_module.download_lock.locked():
                                break
                            time.sleep(0.05)
                self.assertEqual(seen["items"][0]["post_id"], "post-1")
                self.assertEqual(seen["items"][0]["kind"], "images")
                job = self.client.get("/api/job").get_json()
                self.assertEqual(job["state"], "done")
                self.assertEqual(job["message"], "Download finished.")
            finally:
                if app_module.download_lock.locked():
                    app_module.download_lock.release()
                app_module.cancel_event.clear()
                app_module._set_job("idle", "")

    def test_ffmpeg_install_is_not_started_when_present(self):
        from unittest.mock import patch

        import app as app_module

        with patch.object(app_module, "ffmpeg_available", return_value=True), patch.object(
            app_module.helper_deps, "install_windows_ffmpeg"
        ) as install:
            resp = self.client.post("/api/ffmpeg/install")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ffmpeg_available"])
        install.assert_not_called()

    def test_settings_save_keeps_notes_and_stores_token_in_header(self):
        import os
        import tempfile
        from unittest.mock import patch

        sample = (
            "[Settings]\n"
            "output_dir = downloads\n"
            "auth_token = from-ini-token\n"
            "\n"
            "; Filename Configuration Tutorial\n"
            "; {creator} is the username\n"
            "[Filename]\n"
            "pattern = {creator}_{date}_{id}\n"
            "separator = _\n"
            "\n"
            "[Threads]\n"
            "threads = 3\n"
        )
        keys = [
            "AUTH_TOKEN",
            "DOWNLOADS_DIR",
            "WRITE_METADATA",
            "FILENAME_PATTERN",
            "FILENAME_SEPARATOR",
            "THREAD_COUNT",
        ]
        saved = {key: os.environ.get(key) for key in keys}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg = Path(tmp) / "config.ini"
                header = Path(tmp) / "header.txt"
                cfg.write_text(sample, encoding="utf-8")
                header.write_text("user-agent: test\n", encoding="utf-8")
                with patch("scripts.settings_loader.find_config_file", return_value=cfg), patch(
                    "scripts.api.find_header_file", return_value=header
                ):
                    resp = self.client.post(
                        "/api/settings",
                        json={
                            "filename_pattern": "{id}",
                            "filename_separator": "_",
                            "thread_count": 3,
                            "output_dir": str(Path(tmp) / "saved"),
                            "write_metadata": False,
                            "auth_token": "unit-test-token",
                        },
                    )
                self.assertEqual(resp.status_code, 200)
                config_text = cfg.read_text(encoding="utf-8")
                header_text = header.read_text(encoding="utf-8")
                self.assertIn("; Filename Configuration Tutorial", config_text)
                self.assertIn("; {creator} is the username", config_text)
                self.assertIn("pattern = {id}", config_text)
                self.assertNotIn("auth_token", config_text)
                self.assertNotIn("unit-test-token", config_text)
                self.assertNotIn("from-ini-token", config_text)
                self.assertIn("Token token=unit-test-token", header_text)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

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
