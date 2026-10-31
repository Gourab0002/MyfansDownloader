from flask import Flask, Response, jsonify, render_template, request
import json
import logging
import os
import subprocess
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import urlparse

import requests

import scripts.myfans_dl as downloader
from scripts.api import has_auth_token, read_headers_from_file, write_auth_token
from scripts.download_state import DownloadState
from scripts.paths import ensure_dir, log_file_path
from scripts.settings_loader import current_settings, load_config, output_directory, save_config

log_path = log_file_path()
ensure_dir(log_path.parent)
file_handler = RotatingFileHandler(log_path, maxBytes=10485760, backupCount=5, encoding="utf-8")
console_handler = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)
logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])
logger = logging.getLogger(__name__)

app = Flask(__name__)
progress_queue = Queue()
download_state = DownloadState()
download_lock = threading.Lock()
cancel_event = threading.Event()
MEDIA_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".jpg", ".jpeg", ".png", ".webp", ".gif"}


def ffmpeg_available() -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def reveal_folder(path: Path) -> None:
    folder = str(path)
    if os.name == "nt":
        os.startfile(folder)  # type: ignore[attr-defined]
        return
    command = ["open", folder] if sys.platform == "darwin" else ["xdg-open", folder]
    subprocess.Popen(command)


def list_library(limit: int = 16) -> list:
    root = Path(output_directory(load_config())).resolve()
    found = []
    if not root.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            if path.suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            try:
                stat = path.stat()
                folder = path.parent.resolve().relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            found.append(
                {
                    "name": path.name,
                    "folder": "" if folder == "." else folder,
                    "size": stat.st_size,
                    "modified": int(stat.st_mtime),
                }
            )
            if len(found) >= 200:
                break
        if len(found) >= 200:
            break
    found.sort(key=lambda item: item["modified"], reverse=True)
    return found[:limit]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/status")
def get_status():
    return jsonify(download_state.get_serializable_state())


def _last_path_part(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"https://myfans.jp/{value.lstrip('/')}")
    parts = [part for part in parsed.path.split("/") if part]
    return parts[-1] if parts else value


def _clean_username(value: str) -> str:
    text = (value or "").strip()
    if "://" in text or "/" in text:
        text = _last_path_part(text)
    return text.lstrip("@").strip()


def _clean_post_id(value: str) -> str:
    text = (value or "").strip()
    if "://" in text or "/" in text:
        return _last_path_part(text)
    return text


def _download_fields():
    data = request.get_json(silent=True) or {}
    username = _clean_username(data.get("username") or "")
    post_id = _clean_post_id(data.get("post_id") or "") or None
    post_type = data.get("type") or "videos"
    if post_type not in {"videos", "images"}:
        post_type = "videos"
    download_type = data.get("download_type") or "all"
    if download_type not in {"all", "free", "subscribed", "single"}:
        download_type = "all"
    resolution = data.get("resolution") or "best"
    return username, post_type, download_type, post_id, resolution


@app.route("/api/preview", methods=["POST"])
def preview():
    username, post_type, download_type, post_id, _resolution = _download_fields()
    if not post_id and not username:
        return jsonify({"ok": False, "error": "username or post_id is required"}), 400
    result = downloader.preview_target(
        username,
        post_type,
        download_type,
        post_id=post_id,
        download_state=download_state,
    )
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/download", methods=["POST"])
def start_download():
    username, post_type, download_type, post_id, resolution = _download_fields()

    if not post_id and not username:
        return jsonify({"error": "username or post_id is required"}), 400
    if not download_lock.acquire(blocking=False):
        return jsonify({"error": "A download is already in progress"}), 409
    cancel_event.clear()

    logger.info(
        "Starting download request - Username: %s, Type: %s, Mode: %s, PostID: %s, Resolution: %s",
        username,
        post_type,
        download_type,
        post_id,
        resolution,
    )

    def download_thread():
        try:
            downloader.start_download(
                username,
                post_type,
                download_type,
                progress_queue,
                download_state,
                post_id=post_id,
                resolution=resolution,
                cancel_event=cancel_event,
            )
        except Exception:
            logger.exception("Error in download thread")
            progress_queue.put("Download failed. Check the server log for details.")
            progress_queue.put("DONE")
        finally:
            download_lock.release()

    threading.Thread(target=download_thread, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/download/stop", methods=["POST"])
def stop_download():
    if not download_lock.locked():
        return jsonify({"status": "idle"})
    cancel_event.set()
    logger.info("Stop requested")
    return jsonify({"status": "stopping"})


@app.route("/progress")
def progress():
    def generate():
        try:
            while True:
                try:
                    message = progress_queue.get(timeout=15)
                    if isinstance(message, dict):
                        event_name = str(message.get("event") or "progress")
                        payload = {key: value for key, value in message.items() if key != "event"}
                        yield f"event: {event_name}\ndata: {json.dumps(payload)}\n\n"
                        continue
                    text = str(message).replace("\r", " ").replace("\n", " ")
                    yield f"data: {text}\n\n"
                    if message == "DONE":
                        break
                except Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            return
        except Exception as exc:
            logger.error("Error in progress stream: %s", exc)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/test_post/<post_id>")
def test_post(post_id):
    try:
        session = requests.Session()
        headers = read_headers_from_file("header.txt")
        data, resolution_info, error = downloader.get_video_info(post_id, session, headers)
        if error:
            logger.warning("Post lookup failed for %s: %s", post_id, error)
            return jsonify({"error": "Post is unavailable or has no video"}), 400
        return jsonify(
            {
                "post_type": "video"
                if data.get("videos")
                else "image"
                if data.get("images")
                else "unknown",
                "available": data.get("available", True),
                "is_free": data.get("free", False),
                "available_resolutions": list(resolution_info.keys()) if resolution_info else [],
                "title": data.get("title") or data.get("body", ""),
                "created_at": data.get("published_at") or data.get("created_at", ""),
            }
        )
    except Exception:
        logger.exception("Unexpected error looking up post %s", post_id)
        return jsonify({"error": "An internal error has occurred"}), 500


@app.route("/api/library")
def library():
    return jsonify({"files": list_library()})


@app.route("/api/open-folder", methods=["POST"])
def open_folder():
    folder = Path(output_directory(load_config())).resolve()
    try:
        ensure_dir(folder)
        reveal_folder(folder)
    except OSError as exc:
        logger.warning("Could not open downloads folder: %s", exc)
        return jsonify({"error": "Could not open the downloads folder"}), 500
    return jsonify({"status": "opened"})


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        return settings_api()
    return render_template("settings.html")


@app.route("/api/settings", methods=["GET", "POST"])
def settings_api():
    config = load_config()
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if not config.has_section("Filename"):
            config.add_section("Filename")
        if not config.has_section("Threads"):
            config.add_section("Threads")
        if not config.has_section("Settings"):
            config.add_section("Settings")
        config.set("Filename", "pattern", str(data.get("filename_pattern") or "{creator}_{date}_{id}"))
        config.set("Filename", "separator", str(data.get("filename_separator") or "_"))
        try:
            threads = max(1, int(data.get("thread_count") or 3))
        except (TypeError, ValueError):
            threads = 3
        config.set("Threads", "threads", str(threads))
        output_dir = str(data.get("output_dir") or "").strip()
        if output_dir:
            if any(char in output_dir for char in "\x00\r\n") or len(output_dir) > 240:
                return jsonify({"error": "That folder path cannot be used"}), 400
            config.set("Settings", "output_dir", output_dir)
            os.environ.pop("DOWNLOADS_DIR", None)
            resolved = str(Path(output_directory(config)).resolve())
            config.set("Settings", "output_dir", resolved)
            os.environ["DOWNLOADS_DIR"] = resolved
        if "write_metadata" in data:
            config.set("Settings", "write_metadata", "1" if data.get("write_metadata") else "0")
            os.environ["WRITE_METADATA"] = config.get("Settings", "write_metadata")
        token = (data.get("auth_token") or "").strip()
        if token:
            config.set("Settings", "auth_token", token)
            os.environ["AUTH_TOKEN"] = token
            try:
                write_auth_token(token)
            except OSError as exc:
                logger.warning("Could not write header.txt: %s", exc)
        os.environ["FILENAME_PATTERN"] = config.get("Filename", "pattern")
        os.environ["FILENAME_SEPARATOR"] = config.get("Filename", "separator")
        os.environ["THREAD_COUNT"] = config.get("Threads", "threads")
        save_config(config)
        return jsonify({"status": "success"})

    settings = current_settings(config)
    token_set = bool(str(settings.get("auth_token") or "").strip())
    try:
        headers = read_headers_from_file("header.txt")
        token_set = token_set or has_auth_token(headers)
    except FileNotFoundError:
        pass
    settings.pop("auth_token", None)
    settings["auth_token_set"] = token_set
    settings["ffmpeg_available"] = ffmpeg_available()
    return jsonify(settings)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
