from flask import Flask, Response, jsonify, render_template, request
import json
import logging
import math
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
from helpers import deps as helper_deps
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
ffmpeg_lock = threading.Lock()
job_notice_lock = threading.Lock()
job_notice = {"state": "idle", "message": "", "progress": ""}
MEDIA_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".jpg", ".jpeg", ".png", ".webp", ".gif"}
LIBRARY_PAGE_SIZE = 16
LIBRARY_SCAN_LIMIT = 5000


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


def reveal_path(path: Path) -> None:
    target = str(path)
    if os.name == "nt":
        os.startfile(target)  # type: ignore[attr-defined]
        return
    command = ["open", target] if sys.platform == "darwin" else ["xdg-open", target]
    subprocess.Popen(command)


def reveal_folder(path: Path) -> None:
    reveal_path(path)


def _set_job(state: str, message: str, progress: str = "") -> None:
    with job_notice_lock:
        job_notice["state"] = state
        job_notice["message"] = message
        job_notice["progress"] = progress


def _job_payload() -> dict:
    with job_notice_lock:
        payload = dict(job_notice)
    payload["running"] = download_lock.locked()
    if payload["running"]:
        payload["state"] = "running"
        if not payload["message"]:
            payload["message"] = "Download in progress."
    return payload


def _finish_job(summary: dict) -> None:
    summary = summary or {}
    failed = int(summary.get("failed") or 0)
    saved = int(summary.get("saved") or 0)
    if summary.get("stopped"):
        _set_job("stopped", "Stopped.")
        return
    if failed and saved:
        noun = "post" if failed == 1 else "posts"
        _set_job("error", f"Finished. {failed} {noun} failed.")
        return
    if failed:
        noun = "post" if failed == 1 else "posts"
        _set_job("error", f"{failed} {noun} failed.")
        return
    if summary.get("error"):
        _set_job("error", str(summary.get("message") or "Download failed."))
        return
    _set_job("done", "Download finished.")


def _spawn_download(target) -> bool:
    if not download_lock.acquire(blocking=False):
        return False
    cancel_event.clear()

    def runner():
        _set_job("running", "Download in progress.")
        summary = {"error": True, "stopped": False, "failed": 1, "saved": 0, "message": "Download failed."}
        try:
            result = target()
            if isinstance(result, dict):
                summary = result
        except Exception:
            logger.exception("Error in download thread")
            progress_queue.put("Download failed. Check the server log for details.")
            progress_queue.put("DONE")
        finally:
            _finish_job(summary)
            download_lock.release()

    threading.Thread(target=runner, daemon=True).start()
    return True


def _library_root() -> Path:
    return Path(output_directory(load_config())).resolve()


def _scan_library(root: Path) -> list:
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
                relative = path.resolve().relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            folder = Path(relative).parent.as_posix()
            folder = "" if folder == "." else folder
            creator = "Other files" if not folder else relative.split("/", 1)[0]
            found.append(
                {
                    "name": path.name,
                    "folder": folder,
                    "creator": creator,
                    "path": relative,
                    "size": stat.st_size,
                    "modified": int(stat.st_mtime),
                }
            )
            if len(found) >= LIBRARY_SCAN_LIMIT:
                return found
    return found


def list_library(query: str = "", page: int = 1, per_page: int = LIBRARY_PAGE_SIZE) -> dict:
    files = _scan_library(_library_root())
    needle = (query or "").strip().lower()[:80]
    if needle:
        files = [
            item
            for item in files
            if needle in item["name"].lower()
            or needle in item["creator"].lower()
            or needle in item["folder"].lower()
        ]
    files.sort(key=lambda item: (item["modified"], item["name"]), reverse=True)
    per_page = min(max(int(per_page or LIBRARY_PAGE_SIZE), 1), 60)
    total = len(files)
    pages = max(1, math.ceil(total / per_page)) if total else 1
    page = min(max(int(page or 1), 1), pages)
    visible = files[(page - 1) * per_page : page * per_page]
    groups = []
    grouped = {}
    for item in visible:
        bucket = grouped.get(item["creator"])
        if bucket is None:
            bucket = {"creator": item["creator"], "files": []}
            grouped[item["creator"]] = bucket
            groups.append(bucket)
        bucket["files"].append(item)
    return {
        "files": visible,
        "groups": groups,
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": pages,
        "query": needle,
    }


def _file_inside_library(rel: str):
    text = (rel or "").strip().replace("\\", "/")
    if not text or text.startswith("/") or ":" in text or "\x00" in text:
        return None
    root = _library_root()
    candidate = (root / text).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file() or candidate.suffix.lower() not in MEDIA_EXTENSIONS:
        return None
    return candidate


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

    logger.info(
        "Starting download request - Username: %s, Type: %s, Mode: %s, PostID: %s, Resolution: %s",
        username,
        post_type,
        download_type,
        post_id,
        resolution,
    )

    def target():
        return downloader.start_download(
            username,
            post_type,
            download_type,
            progress_queue,
            download_state,
            post_id=post_id,
            resolution=resolution,
            cancel_event=cancel_event,
        )

    if not _spawn_download(target):
        return jsonify({"error": "A download is already in progress"}), 409
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


@app.route("/api/job")
def job_status():
    return jsonify(_job_payload())


@app.route("/api/retry-failed", methods=["POST"])
def retry_failed():
    items = download_state.failed_posts()
    if not items:
        return jsonify({"error": "No failed posts to retry."}), 400

    def target():
        return downloader.start_download(
            "",
            "videos",
            "all",
            progress_queue,
            download_state,
            cancel_event=cancel_event,
            retry_items=items,
        )

    if not _spawn_download(target):
        return jsonify({"error": "A download is already in progress"}), 409
    return jsonify({"status": "started", "count": len(items)})


@app.route("/api/library")
def library():
    try:
        page = int(request.args.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(request.args.get("per_page") or LIBRARY_PAGE_SIZE)
    except (TypeError, ValueError):
        per_page = LIBRARY_PAGE_SIZE
    return jsonify(list_library(query=request.args.get("q") or "", page=page, per_page=per_page))


@app.route("/api/open-folder", methods=["POST"])
def open_folder():
    folder = _library_root()
    try:
        ensure_dir(folder)
        reveal_folder(folder)
    except OSError as exc:
        logger.warning("Could not open downloads folder: %s", exc)
        return jsonify({"error": "Could not open the downloads folder"}), 500
    return jsonify({"status": "opened"})


@app.route("/api/open-file", methods=["POST"])
def open_file():
    data = request.get_json(silent=True) or {}
    target = _file_inside_library(str(data.get("path") or ""))
    if target is None:
        return jsonify({"error": "That file is not in the save folder."}), 400
    try:
        reveal_path(target)
    except OSError as exc:
        logger.warning("Could not open a library file: %s", exc)
        return jsonify({"error": "Could not open that file."}), 500
    return jsonify({"status": "opened"})


@app.route("/api/ffmpeg/install", methods=["POST"])
def install_ffmpeg_route():
    if ffmpeg_available():
        return jsonify({"ffmpeg_available": True, "message": "FFmpeg is already installed."})
    if os.name != "nt":
        return jsonify({"error": "Install FFmpeg, then open this app again."}), 400
    if not ffmpeg_lock.acquire(blocking=False):
        return jsonify({"error": "FFmpeg install is already running."}), 409
    try:
        try:
            ok = bool(helper_deps.install_windows_ffmpeg())
        except Exception:
            logger.exception("FFmpeg install failed")
            ok = False
    finally:
        ffmpeg_lock.release()
    if not ok or not ffmpeg_available():
        return jsonify({"error": "FFmpeg could not be installed."}), 500
    return jsonify({"ffmpeg_available": True, "message": "FFmpeg is ready."})


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
            try:
                write_auth_token(token)
            except OSError as exc:
                logger.warning("Could not write header.txt: %s", exc)
                return jsonify({"error": "Could not save the token."}), 500
            os.environ["AUTH_TOKEN"] = token
        elif config.has_option("Settings", "auth_token"):
            existing = config.get("Settings", "auth_token", fallback="").strip()
            if existing:
                try:
                    present = has_auth_token(read_headers_from_file("header.txt"))
                except FileNotFoundError:
                    present = False
                if not present:
                    try:
                        write_auth_token(existing)
                    except OSError as exc:
                        logger.warning("Could not write header.txt: %s", exc)
                        return jsonify({"error": "Could not save the token."}), 500
        if config.has_option("Settings", "auth_token"):
            config.remove_option("Settings", "auth_token")
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
    ffmpeg_ok = ffmpeg_available()
    settings["ffmpeg_available"] = ffmpeg_ok
    settings["ffmpeg_can_install"] = os.name == "nt" and not ffmpeg_ok
    return jsonify(settings)


if __name__ == "__main__":
    from scripts.settings_loader import migrate_token_out_of_config

    migrate_token_out_of_config()
    app.run(host="0.0.0.0", port=5000)
