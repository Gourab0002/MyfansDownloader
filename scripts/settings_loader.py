"""Load and save config.ini without destroying unrelated sections or notes."""
from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from scripts.paths import downloads_dir, find_config_file, normalize_output_dir

IniItem = Tuple[str, str, str]


def load_config() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    path = find_config_file()
    if path.is_file():
        config.read(path, encoding="utf-8")
    if not config.has_section("Settings"):
        config.add_section("Settings")
    if not config.has_option("Settings", "output_dir"):
        config.set("Settings", "output_dir", str(downloads_dir()))
    if not config.has_section("Filename"):
        config.add_section("Filename")
        config.set("Filename", "pattern", os.getenv("FILENAME_PATTERN", "{creator}_{date}_{id}"))
        config.set("Filename", "separator", os.getenv("FILENAME_SEPARATOR", "_"))
        config.set("Filename", "numbers", "")
        config.set("Filename", "letters", "")
    if not config.has_section("Threads"):
        config.add_section("Threads")
        config.set("Threads", "threads", os.getenv("THREAD_COUNT", "3"))
    return config


def _explicit_items(config: configparser.ConfigParser) -> List[IniItem]:
    items: List[IniItem] = []
    for section in config.sections():
        bucket = config._sections.get(section, {})
        for key in bucket:
            if section.lower() == "settings" and str(key).lower() == "auth_token":
                continue
            items.append((section, str(key), config.get(section, str(key), raw=True)))
    return items


def _section_name(line: str) -> Optional[str]:
    stripped = line.strip()
    if len(stripped) < 3 or not stripped.startswith("[") or not stripped.endswith("]"):
        return None
    if stripped.count("[") != 1:
        return None
    name = stripped[1:-1].strip()
    if not name or name[0] in ";#":
        return None
    return name


def _newline_style(lines: Sequence[str]) -> str:
    return "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"


def _write_fresh(path: Path, items: Sequence[IniItem], newline: str) -> None:
    chunks: List[str] = []
    current = None
    for section, key, value in items:
        if section != current:
            if current is not None:
                chunks.append(newline)
            chunks.append(f"[{section}]{newline}")
            current = section
        chunks.append(f"{key} = {value}{newline}")
    path.write_text("".join(chunks), encoding="utf-8")


def _write_ini(path: Path, items: Sequence[IniItem]) -> None:
    """Update keys in place. Full-line notes and untouched lines stay as they are."""
    if not path.is_file():
        _write_fresh(path, items, "\n")
        return

    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    if not lines:
        _write_fresh(path, items, "\n")
        return
    newline = _newline_style(lines)
    pending = {(section.lower(), key.lower()): (section, key, value) for section, key, value in items}
    out: List[str] = []
    section = ""
    insert_at: Dict[str, int] = {}

    for line in lines:
        name = _section_name(line)
        if name is not None:
            section = name
            out.append(line if line.endswith(("\n", "\r\n")) else line + newline)
            insert_at[section.lower()] = len(out)
            continue

        body = line.rstrip("\r\n")
        stripped = body.lstrip()
        is_key = bool(section) and bool(stripped) and stripped[0] not in ";#" and "=" in body
        if is_key:
            indent_len = len(body) - len(body.lstrip(" \t"))
            left, right = body[indent_len:].split("=", 1)
            key = left.strip()
            ident = (section.lower(), key.lower())
            if ident[1] == "auth_token" and ident[0] == "settings":
                continue
            if ident in pending:
                _section_name_raw, _key, value = pending.pop(ident)
                if right.strip() == value:
                    out.append(line if line.endswith(("\n", "\r\n")) else line + newline)
                else:
                    ending = newline if line.endswith(("\n", "\r\n")) else newline
                    out.append(f"{body[:indent_len]}{key} = {value}{ending}")
                insert_at[section.lower()] = len(out)
                continue

        out.append(line if line.endswith(("\n", "\r\n")) else line + newline)
        if section and not is_key:
            pass

    extras: Dict[str, List[str]] = {}
    names: Dict[str, str] = {}
    for (section_key, _key_lower), (section_name, key, value) in pending.items():
        extras.setdefault(section_key, []).append(f"{key} = {value}{newline}")
        names[section_key] = section_name

    for section_key, new_lines in sorted(extras.items(), key=lambda item: insert_at.get(item[0], 10**9), reverse=True):
        if section_key in insert_at:
            index = insert_at[section_key]
            for offset, new_line in enumerate(new_lines):
                out.insert(index + offset, new_line)
            continue
        if out and out[-1].strip():
            out.append(newline)
        out.append(f"[{names[section_key]}]{newline}")
        out.extend(new_lines)

    path.write_text("".join(out), encoding="utf-8")


def save_config(config: configparser.ConfigParser) -> Path:
    path = find_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if config.has_section("Settings") and config.has_option("Settings", "auth_token"):
        config.remove_option("Settings", "auth_token")
    _write_ini(path, _explicit_items(config))
    return path


def migrate_token_out_of_config() -> None:
    """Move a token left in config.ini into header.txt, keeping the ini notes."""
    config = load_config()
    if not config.has_option("Settings", "auth_token"):
        return
    token = config.get("Settings", "auth_token", fallback="").strip()
    config.remove_option("Settings", "auth_token")
    if token:
        from scripts.api import has_auth_token, read_headers_from_file, write_auth_token

        present = False
        try:
            present = has_auth_token(read_headers_from_file("header.txt"))
        except FileNotFoundError:
            present = False
        if not present:
            write_auth_token(token)
    save_config(config)


def output_directory(config: configparser.ConfigParser) -> str:
    configured = config.get("Settings", "output_dir", fallback="downloads")
    env = os.getenv("DOWNLOADS_DIR")
    if env:
        return env
    return normalize_output_dir(configured)


def thread_count(config: configparser.ConfigParser) -> int:
    env = os.getenv("THREAD_COUNT")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    try:
        return max(1, config.getint("Threads", "threads", fallback=3))
    except ValueError:
        return 3


def segment_thread_count() -> int:
    try:
        return max(1, int(os.getenv("SEGMENT_DOWNLOAD_THREADS", "8")))
    except ValueError:
        return 8


def current_settings(config: configparser.ConfigParser) -> Dict[str, object]:
    return {
        "filename_pattern": os.getenv(
            "FILENAME_PATTERN", config.get("Filename", "pattern", fallback="{creator}_{date}_{id}")
        ),
        "filename_separator": os.getenv(
            "FILENAME_SEPARATOR", config.get("Filename", "separator", fallback="_")
        ),
        "auth_token": os.getenv("AUTH_TOKEN", config.get("Settings", "auth_token", fallback="")),
        "thread_count": thread_count(config),
        "write_metadata": os.getenv(
            "WRITE_METADATA", config.get("Settings", "write_metadata", fallback="0")
        ),
        "output_dir": output_directory(config),
    }
