from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests
from tqdm import tqdm

from scripts.api import (
    MyFansClient,
    collect_image_urls,
    get_video_info,
    has_auth_token,
    media_headers,
    read_headers_from_file,
    videos_from_payload,
)
from scripts.filename_utils import (
    generate_filename,
    generate_metadata,
    read_filename_config,
    update_file_date,
    validate_filename_config,
)
from scripts.hls import download_stream
from scripts.settings_loader import load_config, output_directory, segment_thread_count, thread_count
from scripts.utils import (
    emit,
    height_for_resolution,
    normalize_resolution,
    pick_video_variant,
    post_is_available,
    setup_logging,
    verify_video_file,
)

logger = setup_logging()


def _stopped(cancel_event) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def emit_progress(progress_queue, **payload) -> None:
    if progress_queue is None:
        return
    progress_queue.put({"event": "progress", **payload})


def _extension_from_url(url: str, default: str = ".jpg") -> str:
    path = urlparse(url).path
    ext = os.path.splitext(path)[1]
    if ext and len(ext) <= 5:
        return ext
    return default


def _filter_posts(posts: List[Dict[str, Any]], download_type: str) -> List[Dict[str, Any]]:
    if download_type == "free":
        return [post for post in posts if post.get("free")]
    if download_type in {"subscribed", "subscribe"}:
        return [post for post in posts if not post.get("free")]
    return posts


def _list_posts(client: MyFansClient, user: Dict[str, Any], kind: str, cancel_event=None) -> List[Dict[str, Any]]:
    posts = []
    for post in client.iter_all_posts(user, kind=kind):
        if _stopped(cancel_event):
            break
        if post.get("kind") == kind or not post.get("kind"):
            posts.append(post)
    return posts


def download_image_bytes(session: requests.Session, url: str, headers: Dict[str, str], dest: str) -> bool:
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    response = session.get(url, headers=headers, timeout=60)
    response.raise_for_status()
    with open(dest, "wb") as handle:
        handle.write(response.content)
    return os.path.getsize(dest) > 0


def process_image_post(
    post_id: str,
    client: MyFansClient,
    output_dir: str,
    filename_config: Dict[str, str],
    progress_queue=None,
    download_state=None,
    cancel_event=None,
) -> str:
    try:
        if _stopped(cancel_event):
            return "stopped"
        if download_state and download_state.is_completed(post_id):
            emit(progress_queue, f"Already saved: {post_id}")
            return "skipped"
        data = client.get_post(post_id)
        if not post_is_available(data):
            emit(progress_queue, f"No access to image post {post_id}", "error")
            if download_state:
                download_state.mark_failed(post_id, "no access")
            return "failed"
        urls = collect_image_urls(data)
        if not urls:
            emit(progress_queue, f"No images found for post {post_id}", "error")
            if download_state:
                download_state.mark_failed(post_id, "no images")
            return "failed"
        username = (data.get("user") or {}).get("username") or "unknown"
        folder = os.path.join(output_dir, username, "images")
        os.makedirs(folder, exist_ok=True)
        wrote_new = False
        for idx, url in enumerate(urls):
            if _stopped(cancel_event):
                return "stopped"
            ext = _extension_from_url(url)
            index = idx + 1 if len(urls) > 1 else None
            filename = generate_filename(data, filename_config, folder, ext=ext, index=index)
            full_path = os.path.join(folder, filename)
            if os.path.exists(full_path) and os.path.getsize(full_path) > 0:
                update_file_date(data, full_path)
                generate_metadata(data, filename, folder, ext.lstrip("."))
                continue
            if not download_image_bytes(client.session, url, media_headers(client.headers), full_path):
                raise RuntimeError(f"Empty image download for {url}")
            wrote_new = True
            update_file_date(data, full_path)
            generate_metadata(data, filename, folder, ext.lstrip("."))
            emit(progress_queue, f"Downloaded image: {filename}")
        if download_state:
            download_state.mark_completed(post_id)
        if not wrote_new:
            emit(progress_queue, f"Already saved: {post_id}")
            return "skipped"
        return "saved"
    except Exception as exc:
        if _stopped(cancel_event):
            return "stopped"
        emit(progress_queue, f"Error downloading images for post {post_id}: {exc}", "error")
        if download_state:
            download_state.mark_failed(post_id, str(exc))
        return "failed"


def handle_image_download(post_id, session, headers, output_dir, filename_config, progress_queue=None):
    client = MyFansClient(headers=headers, session=session)
    return process_image_post(post_id, client, output_dir, filename_config, progress_queue)


def process_video_post(
    post_id: str,
    client: MyFansClient,
    selected_resolution: str,
    output_dir: str,
    filename_config: Dict[str, str],
    progress_queue=None,
    download_state=None,
    cancel_event=None,
) -> str:
    try:
        if _stopped(cancel_event):
            return "stopped"
        if download_state and download_state.is_completed(post_id):
            emit(progress_queue, f"Already saved: {post_id}")
            return "skipped"
        if download_state:
            download_state.add_download(post_id, status="in_progress")
        data = client.get_post(post_id)
        if not post_is_available(data):
            emit(progress_queue, f"No access to post {post_id}", "error")
            if download_state:
                download_state.mark_failed(post_id, "no access")
            return "failed"
        try:
            video_payload = client.get_post_videos(post_id)
        except requests.RequestException:
            video_payload = data.get("videos")
        variants = videos_from_payload(video_payload) or videos_from_payload(data.get("videos"))
        main_variants = [item for item in variants if item.get("source") != "trial"]
        chosen = pick_video_variant(main_variants, selected_resolution)
        if not chosen:
            emit(progress_queue, f"No video URL for post {post_id} (locked or unavailable)", "error")
            if download_state:
                download_state.mark_failed(post_id, "no video url")
            return "failed"

        username = (data.get("user") or {}).get("username") or "unknown"
        folder = os.path.join(output_dir, username, "videos")
        os.makedirs(folder, exist_ok=True)
        filename = generate_filename(data, filename_config, folder, ext=".mp4")
        full_path = os.path.join(folder, filename)

        if os.path.exists(full_path) and verify_video_file(full_path):
            generate_metadata(data, filename, folder)
            update_file_date(data, full_path)
            emit(progress_queue, f"Already saved: {post_id}")
            if download_state:
                download_state.mark_completed(post_id)
            return "skipped"
        if os.path.exists(full_path):
            emit(progress_queue, f"Corrupted file found, will redownload: {filename}", "warning")
            try:
                os.remove(full_path)
            except OSError:
                pass

        def on_progress(message: str) -> None:
            emit(progress_queue, message)

        success = download_stream(
            chosen["url"],
            full_path,
            media_headers(client.headers),
            post_id,
            requested_height=height_for_resolution(selected_resolution),
            progress=on_progress,
            segment_threads=segment_thread_count(),
            cancel_event=cancel_event,
        )
        if _stopped(cancel_event):
            return "stopped"
        if success:
            generate_metadata(data, filename, folder)
            update_file_date(data, full_path)
            emit(progress_queue, f"Successfully downloaded video: {filename}")
            if download_state:
                download_state.mark_completed(post_id)
            return "saved"
        emit(progress_queue, f"Failed to download video for post {post_id}", "error")
        if download_state:
            download_state.mark_failed(post_id, "download failed")
        return "failed"
    except Exception as exc:
        if _stopped(cancel_event):
            return "stopped"
        emit(progress_queue, f"Error processing post {post_id}: {exc}", "error")
        if download_state:
            download_state.mark_failed(post_id, str(exc))
        return "failed"


def process_post_id(
    input_post_id,
    session,
    headers,
    selected_resolution,
    output_dir,
    filename_config,
    progress_bar=None,
    progress_queue=None,
):
    client = MyFansClient(headers=headers, session=session)
    try:
        return process_video_post(
            input_post_id,
            client,
            selected_resolution,
            output_dir,
            filename_config,
            progress_queue=progress_queue,
        )
    finally:
        if progress_bar:
            progress_bar.update(1)


class _Tally:
    def __init__(self, progress_queue, total: int):
        self.progress_queue = progress_queue
        self.counts = {"done": 0, "total": total, "skipped": 0, "saved": 0, "failed": 0}
        self._lock = threading.Lock()
        emit_progress(progress_queue, **self.counts)

    def record(self, status: str) -> None:
        if status == "stopped":
            return
        with self._lock:
            self.counts["done"] += 1
            key = status if status in {"skipped", "saved", "failed"} else "failed"
            self.counts[key] += 1
            snapshot = dict(self.counts)
        emit_progress(self.progress_queue, **snapshot)

    def mark_stopped(self) -> None:
        emit_progress(self.progress_queue, stopped=True, **self.counts)


def _drive_posts(post_ids, run_one, tally: _Tally, workers: int, cancel_event, progress_queue, desc: str) -> bool:
    progress_bar = tqdm(total=len(post_ids), desc=desc, unit="post")
    try:
        if workers == 1:
            for post_id in post_ids:
                if _stopped(cancel_event):
                    break
                status = run_one(post_id)
                progress_bar.update(1)
                if status != "stopped":
                    tally.record(status)
                if status == "stopped" or _stopped(cancel_event):
                    break
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            executor = ThreadPoolExecutor(max_workers=workers)
            try:
                futures = []
                for post_id in post_ids:
                    if _stopped(cancel_event):
                        break
                    futures.append(executor.submit(run_one, post_id))
                for future in as_completed(futures):
                    try:
                        status = future.result()
                    except Exception as exc:
                        emit(progress_queue, f"An error occurred during download: {exc}", "error")
                        status = "failed"
                    progress_bar.update(1)
                    if status != "stopped":
                        tally.record(status)
                    if status == "stopped" or _stopped(cancel_event):
                        break
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
    finally:
        progress_bar.close()
    if _stopped(cancel_event):
        tally.mark_stopped()
        return True
    return False


def download_videos_concurrently(
    session,
    post_ids,
    selected_resolution,
    output_dir,
    filename_config,
    progress_queue=None,
    max_workers=1,
    download_state=None,
    headers=None,
    cancel_event=None,
) -> bool:
    headers = headers or read_headers_from_file("header.txt")
    shared = MyFansClient(headers=headers, session=session)
    total = len(post_ids)
    emit(progress_queue, f"Starting download of {total} video posts...")
    workers = max(1, int(max_workers or 1))
    tally = _Tally(progress_queue, total)

    def run_one(post_id: str) -> str:
        if _stopped(cancel_event):
            return "stopped"
        client = shared if workers == 1 else MyFansClient(headers=headers)
        return process_video_post(
            post_id,
            client,
            selected_resolution,
            output_dir,
            filename_config,
            progress_queue,
            download_state,
            cancel_event,
        )

    stopped = _drive_posts(
        post_ids, run_one, tally, workers, cancel_event, progress_queue, "Downloading videos"
    )
    if not stopped:
        emit(progress_queue, "Download process completed")
    return stopped


def download_images_concurrently(
    session,
    post_ids,
    output_dir,
    filename_config,
    progress_queue=None,
    download_state=None,
    max_workers=1,
    headers=None,
    cancel_event=None,
) -> bool:
    headers = headers or read_headers_from_file("header.txt")
    shared = MyFansClient(headers=headers, session=session)
    total = len(post_ids)
    emit(progress_queue, f"Starting download of {total} image posts...")
    workers = max(1, int(max_workers or 1))
    tally = _Tally(progress_queue, total)

    def run_one(post_id: str) -> str:
        if _stopped(cancel_event):
            return "stopped"
        client = shared if workers == 1 else MyFansClient(headers=headers)
        return process_image_post(
            post_id,
            client,
            output_dir,
            filename_config,
            progress_queue,
            download_state,
            cancel_event,
        )

    stopped = _drive_posts(
        post_ids, run_one, tally, workers, cancel_event, progress_queue, "Downloading images"
    )
    if not stopped:
        emit(progress_queue, "Image download process completed")
    return stopped


def download_single_file(session, post_id, selected_resolution, output_dir, filename_config):
    headers = read_headers_from_file("header.txt")
    client = MyFansClient(headers=headers, session=session)
    process_video_post(post_id, client, selected_resolution, output_dir, filename_config)


def _preview_error(token_set: bool, message: str, token_rejected: bool = False) -> Dict[str, Any]:
    return {
        "ok": False,
        "token_set": token_set,
        "token_rejected": token_rejected,
        "error": message,
    }


def _http_status(exc: requests.HTTPError):
    return getattr(getattr(exc, "response", None), "status_code", None)


def _preview_post(client, post_id: str, kind: str, token_set: bool, download_state) -> Dict[str, Any]:
    try:
        data = client.get_post(post_id)
    except requests.HTTPError as exc:
        if _http_status(exc) in (401, 403):
            return _preview_error(token_set, "The token was rejected.", token_rejected=True)
        return _preview_error(token_set, "That post is unavailable.")
    except requests.RequestException as exc:
        logger.warning("Post preview failed: %s", exc)
        return _preview_error(token_set, "Could not reach MyFans.")
    if not isinstance(data, dict):
        return _preview_error(token_set, "That post is unavailable.")

    title = str(data.get("title") or data.get("body") or "This post").strip().splitlines()
    title = title[0][:120] if title else "This post"
    post_kind = data.get("kind") or ""
    has_images = bool(data.get("images")) or post_kind == "image"
    has_video = bool(data.get("videos")) or post_kind == "video"
    if kind == "video" and not has_video:
        try:
            has_video = bool(videos_from_payload(client.get_post_videos(post_id)))
        except requests.HTTPError as exc:
            if _http_status(exc) in (401, 403):
                return _preview_error(token_set, "The token was rejected.", token_rejected=True)
        except requests.RequestException as exc:
            logger.info("Video lookup failed during preview: %s", exc)
    wanted_ok = has_images if kind == "image" else has_video
    available = post_is_available(data) and wanted_ok
    already = bool(download_state and download_state.is_completed(str(post_id)))
    return {
        "ok": True,
        "mode": "post",
        "post_id": post_id,
        "title": title,
        "post_type": "images" if kind == "image" else "videos",
        "available": available,
        "matched": 1 if wanted_ok else 0,
        "already_saved": 1 if already else 0,
        "to_download": 1 if available and not already else 0,
        "token_set": token_set,
        "token_rejected": False,
    }


def preview_target(
    username: str,
    post_type: str,
    download_type: str,
    post_id: Optional[str] = None,
    download_state=None,
) -> Dict[str, Any]:
    try:
        headers = read_headers_from_file("header.txt")
    except FileNotFoundError:
        headers = {}
    token_set = has_auth_token(headers)
    session = requests.Session()
    session.headers.update(headers)
    client = MyFansClient(headers=headers, session=session)
    kind = "image" if post_type == "images" else "video"
    if post_id:
        return _preview_post(client, post_id, kind, token_set, download_state)
    if not username:
        return _preview_error(token_set, "Enter a creator name.")
    try:
        user = client.get_user_by_username(username)
    except requests.HTTPError as exc:
        status = _http_status(exc)
        if status in (401, 403):
            return _preview_error(token_set, "The token was rejected.", token_rejected=True)
        if status == 404:
            return _preview_error(token_set, "No creator was found with that name.")
        logger.warning("Creator preview failed: %s", exc)
        return _preview_error(token_set, "Could not reach MyFans.")
    except requests.RequestException as exc:
        logger.warning("Creator preview failed: %s", exc)
        return _preview_error(token_set, "Could not reach MyFans.")
    if not isinstance(user, dict) or not user.get("id"):
        return _preview_error(token_set, "No creator was found with that name.")

    posts = _filter_posts(_list_posts(client, user, kind), download_type)
    ids = [str(post.get("id")) for post in posts if post.get("id")]
    already = 0
    if download_state is not None:
        already = sum(1 for item in ids if download_state.is_completed(item))
    matched = len(ids)
    return {
        "ok": True,
        "mode": "creator",
        "username": user.get("username") or username,
        "name": user.get("name") or user.get("username") or username,
        "post_type": "images" if kind == "image" else "videos",
        "matched": matched,
        "already_saved": already,
        "to_download": max(0, matched - already),
        "token_set": token_set,
        "token_rejected": False,
    }


def start_download(
    username,
    post_type,
    download_type,
    progress_queue,
    download_state=None,
    post_id=None,
    resolution="best",
    cancel_event=None,
):
    stopped = False
    try:
        config = load_config()
        output_dir = output_directory(config)
        filename_config = read_filename_config(config)
        validate_filename_config(filename_config)
        headers = read_headers_from_file("header.txt")
        if not has_auth_token(headers):
            emit(
                progress_queue,
                "Warning: no authorization token set. Only free/public content will download.",
                "warning",
            )
        session = requests.Session()
        session.headers.update(headers)
        client = MyFansClient(headers=headers, session=session)
        workers = thread_count(config)
        resolution = normalize_resolution(resolution or "best")

        if post_id:
            emit(progress_queue, f"Starting download for post ID: {post_id}")
            emit_progress(progress_queue, done=0, total=1, skipped=0, saved=0, failed=0)
            if post_type == "images":
                status = process_image_post(
                    post_id,
                    client,
                    output_dir,
                    filename_config,
                    progress_queue,
                    download_state,
                    cancel_event,
                )
            else:
                status = process_video_post(
                    post_id,
                    client,
                    resolution,
                    output_dir,
                    filename_config,
                    progress_queue,
                    download_state,
                    cancel_event,
                )
            if status == "stopped":
                stopped = True
            else:
                summary = {"done": 1, "total": 1, "skipped": 0, "saved": 0, "failed": 0}
                if status in summary:
                    summary[status] = 1
                else:
                    summary["failed"] = 1
                emit_progress(progress_queue, **summary)
            return

        if not username:
            emit(progress_queue, "Error: username is required for bulk downloads", "error")
            return

        if _stopped(cancel_event):
            stopped = True
            return

        emit(progress_queue, f"Starting download for user: {username}, type: {post_type}, mode: {download_type}")
        try:
            user = client.get_user_by_username(username)
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (401, 403):
                emit(progress_queue, "API returned 401/403. Check the token in header.txt.", "error")
                return
            raise
        user_id = user.get("id")
        if not user_id:
            emit(progress_queue, "Failed to retrieve user ID. Check the username and token.", "error")
            return
        emit(progress_queue, f"Found user ID: {user_id}")

        kind = "image" if post_type == "images" else "video"
        emit(progress_queue, f"Fetching {kind} posts...")
        posts = _list_posts(client, user, kind, cancel_event=cancel_event)
        if _stopped(cancel_event):
            stopped = True
            return
        posts = _filter_posts(posts, download_type)
        emit(progress_queue, f"Total {kind} posts matched: {len(posts)}")
        post_ids = [post.get("id") for post in posts if post.get("id")]
        if not post_ids:
            emit(progress_queue, "No posts match the selected criteria.")
            return
        if kind == "video":
            stopped = download_videos_concurrently(
                session,
                post_ids,
                resolution,
                output_dir,
                filename_config,
                progress_queue,
                max_workers=workers,
                download_state=download_state,
                headers=headers,
                cancel_event=cancel_event,
            )
        else:
            stopped = download_images_concurrently(
                session,
                post_ids,
                output_dir,
                filename_config,
                progress_queue,
                download_state,
                max_workers=workers,
                headers=headers,
                cancel_event=cancel_event,
            )
    except Exception as exc:
        if _stopped(cancel_event):
            stopped = True
        else:
            emit(progress_queue, f"Error: {exc}", "error")
    finally:
        if stopped:
            emit(progress_queue, "Stopped.")
        if progress_queue is not None:
            progress_queue.put("DONE")


def _prompt_resolution() -> str:
    print("Select quality:")
    print("1. Best available")
    print("2. 2160p (4K)")
    print("3. 1080p")
    print("4. 720p")
    print("5. 480p")
    print("6. 360p")
    choice = input("Enter your choice (1-6, default 1): ").strip() or "1"
    return {
        "1": "best",
        "2": "uhd",
        "3": "fhd",
        "4": "hd",
        "5": "sd",
        "6": "ld",
    }.get(choice, "best")


def main() -> None:
    config = load_config()
    output_dir = output_directory(config)
    filename_config = read_filename_config(config)
    validate_filename_config(filename_config)
    try:
        headers = read_headers_from_file("header.txt")
    except FileNotFoundError as exc:
        print(exc)
        print("Create header.txt with: authorization: Token token=YOUR_TOKEN")
        sys.exit(1)
    if not has_auth_token(headers):
        print("Warning: authorization token is empty. Free content may still download.")

    session = requests.Session()
    session.headers.update(headers)
    client = MyFansClient(headers=headers, session=session)

    print("Select an option:")
    print("1. Download all video posts")
    print("2. Download a single video post by ID")
    print("3. List video post IDs")
    choice = input("Enter your choice (1/2/3): ").strip()

    if choice == "2":
        post_id = input("Enter the post ID to download: ").strip()
        if not post_id:
            print("Post ID is required.")
            return
        download_single_file(session, post_id, _prompt_resolution(), output_dir, filename_config)
        return
    if choice not in {"1", "3"}:
        print("Invalid choice.")
        return

    while True:
        name_creator = input("Enter a creator's username (without @) or type '0' to exit: ").strip()
        if name_creator == "0":
            return
        try:
            user = client.get_user_by_username(name_creator)
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (401, 403):
                print("API returned 401/403. Check the token in header.txt.")
            else:
                print(f"An error occurred while connecting to the API: {exc}")
            continue
        except requests.RequestException as exc:
            print(f"An error occurred while connecting to the API: {exc}")
            continue
        if user.get("id"):
            break
        print("Failed to retrieve user ID. Please try again.")

    print("Fetching posts...")
    posts = _list_posts(client, user, "video")
    print(f"Total video posts found: {len(posts)}")
    print("Select which posts to download:")
    print("1. Free posts only")
    print("2. Subscribe posts only")
    print("3. All posts")
    save_choice = input("Enter your choice (1/2/3): ").strip()
    mapping = {"1": "free", "2": "subscribed", "3": "all"}
    filtered = _filter_posts(posts, mapping.get(save_choice, "all"))
    post_ids = [post.get("id") for post in filtered if post.get("id")]
    if not post_ids:
        print("No posts match the selected criteria.")
        return
    if choice == "3":
        print("\n".join(str(post_id) for post_id in post_ids))
        print(f"\n{len(post_ids)} post IDs")
        return
    download_videos_concurrently(
        session,
        post_ids,
        _prompt_resolution(),
        output_dir,
        filename_config,
        max_workers=thread_count(config),
        headers=headers,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(0)
