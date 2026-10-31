"""Download HLS streams via ffmpeg, with a Python segment fallback."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import m3u8
import requests

from scripts.api import thread_session
from scripts.utils import join_url, url_dirname, verify_video_file

logger = logging.getLogger("myfans_downloader")
ProgressCb = Optional[Callable[[str], None]]
FFMPEG_DOWNLOAD_TIMEOUT = 4 * 60 * 60
FFMPEG_REMUX_TIMEOUT = 30 * 60


def _cancelled(cancel_event) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _remove_partial(path: str) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _popen_until(cmd: List[str], cancel_event, poll_seconds: float = 0.3):
    """Run a process until it exits. Return (-15, "") when cancel_event is set."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        return None, ""
    chunks: List[str] = []

    def _read() -> None:
        if proc.stderr is None:
            return
        chunks.append(proc.stderr.read())

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    cancelled = False
    while proc.poll() is None:
        if _cancelled(cancel_event):
            cancelled = True
            proc.kill()
            break
        time.sleep(poll_seconds)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    reader.join(timeout=2)
    if proc.stderr is not None:
        try:
            proc.stderr.close()
        except OSError:
            pass
    if cancelled:
        return -15, ""
    return proc.returncode, "".join(chunks)


def _notify(progress: ProgressCb, message: str) -> None:
    logger.info(message)
    if progress:
        progress(message)


def _ffmpeg_headers(headers: Dict[str, str]) -> str:
    parts = []
    for key, value in headers.items():
        if not value:
            continue
        parts.append(f"{key}: {value}")
    return "\r\n".join(parts) + "\r\n"


def ffmpeg_download(url: str, output_file: str, headers: Dict[str, str], cancel_event=None) -> bool:
    os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
    header_arg = _ffmpeg_headers(headers)
    user_agent = headers.get("user-agent") or headers.get("User-Agent") or "Mozilla/5.0"
    commands = [
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-headers",
            header_arg,
            "-user_agent",
            user_agent,
            "-i",
            url,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            output_file,
        ],
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-headers",
            header_arg,
            "-user_agent",
            user_agent,
            "-i",
            url,
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            output_file,
        ],
    ]
    for cmd in commands:
        if _cancelled(cancel_event):
            _remove_partial(output_file)
            return False
        code, stderr = _popen_until(cmd, cancel_event)
        if code is None:
            logger.error("ffmpeg is not installed or not on PATH")
            return False
        if code == -15:
            _remove_partial(output_file)
            return False
        if code == 0 and verify_video_file(output_file):
            return True
        if stderr:
            logger.warning("ffmpeg failed: %s", stderr.strip()[:500])
        _remove_partial(output_file)
    return False


def _playlist_is_encrypted(playlist: m3u8.M3U8) -> bool:
    keys = list(playlist.keys or [])
    for segment in playlist.segments or []:
        if getattr(segment, "key", None):
            keys.append(segment.key)
    for key in keys:
        if key is None:
            continue
        method = (key.method or "").upper()
        if method and method != "NONE":
            return True
    return False


def pick_master_variant(playlist: m3u8.M3U8, requested_height: Optional[int] = None) -> Optional[m3u8.Playlist]:
    variants = [item for item in playlist.playlists or [] if item.uri]
    if not variants:
        return None

    def height_of(item: m3u8.Playlist) -> int:
        info = item.stream_info
        if info and info.resolution:
            return int(info.resolution[1])
        return 0

    def bandwidth_of(item: m3u8.Playlist) -> int:
        info = item.stream_info
        return int(info.bandwidth or 0) if info else 0

    if requested_height:
        exact = [item for item in variants if height_of(item) == requested_height]
        if exact:
            return sorted(exact, key=bandwidth_of, reverse=True)[0]
        below = [item for item in variants if 0 < height_of(item) <= requested_height]
        if below:
            return sorted(below, key=lambda item: (height_of(item), bandwidth_of(item)), reverse=True)[0]
    return sorted(variants, key=lambda item: (height_of(item), bandwidth_of(item)), reverse=True)[0]


def resolve_media_playlist(
    session: requests.Session,
    url: str,
    headers: Dict[str, str],
    requested_height: Optional[int] = None,
) -> Tuple[m3u8.M3U8, str]:
    response = session.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    playlist = m3u8.loads(response.text)
    playlist.base_uri = url_dirname(url)
    if playlist.playlists:
        variant = pick_master_variant(playlist, requested_height)
        if variant is None:
            raise ValueError("Master playlist has no usable variants")
        variant_url = join_url(playlist.base_uri, variant.uri)
        response = session.get(variant_url, headers=headers, timeout=30)
        response.raise_for_status()
        media = m3u8.loads(response.text)
        media.base_uri = url_dirname(variant_url)
        return media, variant_url
    if playlist.segments:
        return playlist, url
    raise ValueError("Playlist contains neither variants nor segments")


def _segment_uri(playlist: m3u8.M3U8, uri: str) -> str:
    return uri if uri.lower().startswith(("http://", "https://")) else urljoin(playlist.base_uri, uri)


def python_hls_download(
    url: str,
    output_file: str,
    headers: Dict[str, str],
    post_id: str,
    requested_height: Optional[int] = None,
    progress: ProgressCb = None,
    max_workers: int = 8,
    cancel_event=None,
) -> bool:
    import concurrent.futures

    session = thread_session(headers)
    playlist, playlist_url = resolve_media_playlist(session, url, headers, requested_height)
    if _playlist_is_encrypted(playlist):
        _notify(progress, "Playlist is encrypted; using ffmpeg")
        return ffmpeg_download(playlist_url, output_file, headers, cancel_event=cancel_event)

    output_folder = os.path.dirname(output_file) or "."
    temp_root = tempfile.mkdtemp(prefix=f"m3u8_{uuid.uuid4().hex[:8]}_", dir=output_folder)
    try:
        init_path = None
        init_section = None
        if playlist.segment_map:
            init_section = playlist.segment_map[0]
        elif playlist.segments and getattr(playlist.segments[0], "init_section", None):
            init_section = playlist.segments[0].init_section
        if init_section and init_section.uri:
            init_url = _segment_uri(playlist, init_section.uri)
            init_path = os.path.join(temp_root, "init.mp4")
            resp = session.get(init_url, headers=headers, timeout=30)
            resp.raise_for_status()
            with open(init_path, "wb") as handle:
                handle.write(resp.content)

        total = len(playlist.segments)
        _notify(progress, f"Downloading {total} segments for {post_id}")
        results: List[Optional[str]] = [None] * total

        def download_one(index: int, segment: m3u8.Segment) -> Tuple[int, Optional[str]]:
            if _cancelled(cancel_event) or not segment.uri:
                return index, None
            dest = os.path.join(temp_root, f"segment_{index:05d}.bin")
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                return index, dest
            seg_url = _segment_uri(playlist, segment.uri)
            last_error = None
            worker = thread_session(headers)
            for attempt in range(3):
                try:
                    resp = worker.get(seg_url, headers=headers, timeout=30)
                    resp.raise_for_status()
                    with open(dest, "wb") as handle:
                        handle.write(resp.content)
                    if os.path.getsize(dest) > 0:
                        return index, dest
                except Exception as exc:
                    last_error = exc
                    time.sleep(1 + attempt)
            logger.error("Segment %s failed: %s", index, last_error)
            return index, None

        workers = max(1, min(max_workers, total or 1))
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        try:
            futures = [executor.submit(download_one, i, seg) for i, seg in enumerate(playlist.segments)]
            done = 0
            for future in concurrent.futures.as_completed(futures):
                if _cancelled(cancel_event):
                    break
                index, path = future.result()
                results[index] = path
                done += 1
                if done % 50 == 0 or done == total:
                    ok = len([item for item in results if item])
                    _notify(progress, f"Progress: {done}/{total} segments ({ok} ok)")
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        if _cancelled(cancel_event):
            return False

        valid = [path for path in results if path]
        if len(valid) != total:
            raise RuntimeError(f"Incomplete HLS download: {len(valid)}/{total} segments")

        ts_path = os.path.join(temp_root, "joined.ts")
        with open(ts_path, "wb") as outfile:
            if init_path:
                with open(init_path, "rb") as handle:
                    outfile.write(handle.read())
            for path in valid:
                with open(path, "rb") as handle:
                    outfile.write(handle.read())

        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", ts_path, "-c", "copy", output_file],
                capture_output=True,
                text=True,
                timeout=FFMPEG_REMUX_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            logger.error("ffmpeg remux timed out after %s seconds", FFMPEG_REMUX_TIMEOUT)
            return False
        if result.returncode != 0:
            logger.error("ffmpeg remux failed: %s", (result.stderr or "")[:500])
            return False
        return verify_video_file(output_file)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def download_stream(
    url: str,
    output_file: str,
    headers: Dict[str, str],
    post_id: str,
    requested_height: Optional[int] = None,
    progress: ProgressCb = None,
    max_retries: int = 3,
    segment_threads: int = 8,
    cancel_event=None,
) -> bool:
    if not url:
        return False
    if os.path.exists(output_file) and verify_video_file(output_file):
        _notify(progress, f"Verified existing file: {os.path.basename(output_file)}")
        return True
    if os.path.exists(output_file):
        try:
            os.remove(output_file)
        except OSError:
            pass

    last_error = None
    for attempt in range(max_retries):
        if _cancelled(cancel_event):
            return False
        try:
            _notify(progress, f"Downloading video {post_id} (attempt {attempt + 1}/{max_retries})")
            if ffmpeg_download(url, output_file, headers, cancel_event=cancel_event):
                return True
            if _cancelled(cancel_event):
                return False
            _notify(progress, "ffmpeg download failed, trying Python HLS downloader")
            if python_hls_download(
                url,
                output_file,
                headers,
                post_id,
                requested_height=requested_height,
                progress=progress,
                max_workers=segment_threads,
                cancel_event=cancel_event,
            ):
                return True
            if _cancelled(cancel_event):
                return False
        except Exception as exc:
            last_error = exc
            logger.error("Download attempt %s failed: %s", attempt + 1, exc)
            if progress:
                progress(f"Download attempt {attempt + 1} failed: {exc}")
            time.sleep(2)
    if last_error:
        logger.error("Giving up on %s: %s", post_id, last_error)
    return False
