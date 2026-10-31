from __future__ import annotations

import configparser
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.download_state import DownloadState
from scripts.settings_loader import load_config, migrate_token_out_of_config, save_config


class ConfigPreserveTests(unittest.TestCase):
    def test_save_does_not_drop_sections(self):
        config = configparser.ConfigParser()
        config["Settings"] = {"output_dir": "downloads", "auth_token": ""}
        config["Filename"] = {"pattern": "{creator}_{date}_{id}", "separator": "_"}
        config["Threads"] = {"threads": "3"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.ini"
            with patch("scripts.settings_loader.find_config_file", return_value=path):
                save_config(config)
                loaded = configparser.ConfigParser()
                loaded.read(path, encoding="utf-8")
            self.assertEqual(loaded.get("Settings", "output_dir"), "downloads")
            self.assertEqual(loaded.get("Filename", "pattern"), "{creator}_{date}_{id}")
            self.assertEqual(loaded.get("Threads", "threads"), "3")

    def test_save_keeps_notes_and_drops_the_token(self):
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
            "numbers = 1234\n"
            "\n"
            "[Threads]\n"
            "threads = 3\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.ini"
            path.write_text(sample, encoding="utf-8")
            with patch("scripts.settings_loader.find_config_file", return_value=path):
                config = load_config()
                config.set("Filename", "pattern", "{id}")
                save_config(config)
            text = path.read_text(encoding="utf-8")
        self.assertIn("; Filename Configuration Tutorial", text)
        self.assertIn("; {creator} is the username", text)
        self.assertIn("pattern = {id}", text)
        self.assertIn("numbers = 1234", text)
        self.assertNotIn("auth_token", text)
        self.assertNotIn("from-ini-token", text)

    def test_migrate_leaves_an_existing_header_token_in_place(self):
        sample = (
            "[Settings]\n"
            "output_dir = downloads\n"
            "auth_token = from-ini-token\n"
            "\n"
            "; keep this note\n"
            "[Filename]\n"
            "pattern = {creator}_{date}_{id}\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.ini"
            header = Path(tmp) / "header.txt"
            cfg.write_text(sample, encoding="utf-8")
            header.write_text("authorization: Token token=already-there\n", encoding="utf-8")
            with patch("scripts.settings_loader.find_config_file", return_value=cfg), patch(
                "scripts.api.find_header_file", return_value=header
            ):
                migrate_token_out_of_config()
            self.assertEqual(header.read_text(encoding="utf-8"), "authorization: Token token=already-there\n")
            text = cfg.read_text(encoding="utf-8")
        self.assertIn("; keep this note", text)
        self.assertNotIn("from-ini-token", text)
        self.assertNotIn("auth_token", text)


class DownloadStateTests(unittest.TestCase):
    def test_serializable_snapshot_is_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = DownloadState(state_dir=tmp)
            state.mark_completed("abc")
            snapshot = state.get_serializable_state()
            self.assertIn("abc", snapshot["completed_files"])
            snapshot["completed_files"].append("mutated")
            self.assertNotIn("mutated", state.get_serializable_state()["completed_files"])

    def test_completed_post_is_removed_from_the_retry_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = DownloadState(state_dir=tmp)
            state.mark_failed("abc", "download failed", kind="videos", resolution="hd")
            failed = state.failed_posts()
            self.assertEqual(failed[0]["kind"], "videos")
            self.assertEqual(failed[0]["resolution"], "hd")
            state.mark_completed("abc")
            self.assertEqual(state.failed_posts(), [])


if __name__ == "__main__":
    unittest.main()
