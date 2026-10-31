from __future__ import annotations

import sys
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import threading
import time

from MyfansDownloader_unified import _run_cli_entry, _web_requested
from scripts.hls import _popen_until
from scripts.utils import post_is_available


class WebLaunchTests(unittest.TestCase):
    def test_web_launch_does_not_require_ffmpeg_first(self):
        self.assertTrue(_web_requested(["web"]))
        self.assertTrue(_web_requested(["--web"]))
        self.assertFalse(_web_requested([]))
        self.assertFalse(_web_requested(["--cli"]))


class StartDownloadDoneTests(unittest.TestCase):
    def test_start_download_always_emits_done(self):
        from scripts import myfans_dl

        queue = Queue()
        with patch.object(myfans_dl, "load_config", side_effect=RuntimeError("boom")):
            myfans_dl.start_download("user", "videos", "all", queue)
        items = []
        while not queue.empty():
            items.append(queue.get_nowait())
        self.assertIn("DONE", items)
        self.assertTrue(any("boom" in str(item) for item in items))

    def test_start_download_requires_username_for_bulk(self):
        from scripts import myfans_dl

        queue = Queue()
        fake_config = MagicMock()
        with patch.object(myfans_dl, "load_config", return_value=fake_config), patch.object(
            myfans_dl, "output_directory", return_value="downloads"
        ), patch.object(myfans_dl, "read_filename_config", return_value={"pattern": "{id}"}), patch.object(
            myfans_dl, "validate_filename_config", return_value=True
        ), patch.object(
            myfans_dl, "read_headers_from_file", return_value={"user-agent": "test"}
        ), patch.object(
            myfans_dl, "has_auth_token", return_value=False
        ), patch.object(
            myfans_dl, "thread_count", return_value=1
        ):
            myfans_dl.start_download("", "videos", "all", queue)
        items = []
        while not queue.empty():
            items.append(queue.get_nowait())
        self.assertEqual(items[-1], "DONE")
        self.assertTrue(any("username is required" in str(item).lower() for item in items))


class PreviewAndStopTests(unittest.TestCase):
    def _headers(self):
        return (
            patch("scripts.myfans_dl.read_headers_from_file", return_value={"user-agent": "test"}),
            patch("scripts.myfans_dl.has_auth_token", return_value=True),
        )

    def test_preview_counts_posts_already_saved(self):
        from scripts.myfans_dl import preview_target

        state = MagicMock()
        state.is_completed.side_effect = lambda post_id: post_id == "a"
        client = MagicMock()
        client.get_user_by_username.return_value = {"id": "u1", "username": "studio", "name": "Studio"}
        client.iter_all_posts.return_value = iter(
            [{"id": "a", "kind": "video"}, {"id": "b", "kind": "video"}]
        )
        header_patch, token_patch = self._headers()
        with header_patch, token_patch, patch("scripts.myfans_dl.MyFansClient", return_value=client):
            result = preview_target("studio", "videos", "all", download_state=state)
        self.assertTrue(result["ok"])
        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["already_saved"], 1)
        self.assertEqual(result["to_download"], 1)
        self.assertEqual(result["name"], "Studio")

    def test_preview_reports_rejected_token(self):
        import requests
        from scripts.myfans_dl import preview_target

        response = MagicMock()
        response.status_code = 401
        client = MagicMock()
        client.get_user_by_username.side_effect = requests.HTTPError(response=response)
        header_patch, token_patch = self._headers()
        with header_patch, token_patch, patch("scripts.myfans_dl.MyFansClient", return_value=client):
            result = preview_target("studio", "videos", "all")
        self.assertFalse(result["ok"])
        self.assertTrue(result["token_rejected"])
        self.assertNotIn("401", result["error"])

    def test_stop_skips_remaining_posts_and_counts_progress(self):
        from scripts import myfans_dl

        queue = Queue()
        cancel = threading.Event()
        called = []

        def fake_process(post_id, *args, **kwargs):
            called.append(post_id)
            cancel.set()
            return "saved"

        fake_config = MagicMock()
        with patch.object(myfans_dl, "load_config", return_value=fake_config), patch.object(
            myfans_dl, "output_directory", return_value="downloads"
        ), patch.object(myfans_dl, "read_filename_config", return_value={"pattern": "{id}"}), patch.object(
            myfans_dl, "validate_filename_config", return_value=True
        ), patch.object(
            myfans_dl, "read_headers_from_file", return_value={"user-agent": "test"}
        ), patch.object(
            myfans_dl, "has_auth_token", return_value=True
        ), patch.object(
            myfans_dl, "thread_count", return_value=1
        ), patch.object(
            myfans_dl, "_list_posts", return_value=[{"id": "1", "kind": "video"}, {"id": "2", "kind": "video"}]
        ), patch.object(
            myfans_dl, "process_video_post", side_effect=fake_process
        ), patch.object(
            myfans_dl, "MyFansClient"
        ) as client_cls:
            client_cls.return_value.get_user_by_username.return_value = {"id": "u1", "username": "studio"}
            myfans_dl.start_download(
                "studio", "videos", "all", queue, cancel_event=cancel
            )

        items = []
        while not queue.empty():
            items.append(queue.get_nowait())
        progress = [item for item in items if isinstance(item, dict)]
        self.assertEqual(called, ["1"])
        self.assertTrue(any(item.get("total") == 2 and item.get("done") == 1 and item.get("saved") == 1 for item in progress))
        self.assertIn("Stopped.", items)
        self.assertEqual(items[-1], "DONE")

    def test_popen_until_stops_when_cancelled(self):
        cancel = threading.Event()

        def trip():
            time.sleep(0.15)
            cancel.set()

        threading.Thread(target=trip, daemon=True).start()
        code, _stderr = _popen_until(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cancel,
            poll_seconds=0.05,
        )
        self.assertEqual(code, -15)


class LauncherTests(unittest.TestCase):
    def test_cli_entry_swallows_system_exit(self):
        def boom():
            raise SystemExit(1)

        _run_cli_entry(boom)


class AccessLogicTests(unittest.TestCase):
    def test_old_subscribed_check_was_wrong(self):
        post = {"free": False, "id": "abc"}
        old_blocked = post.get("free") is False and not post.get("subscribed")
        self.assertTrue(old_blocked)
        self.assertTrue(post_is_available(post))


if __name__ == "__main__":
    unittest.main()
