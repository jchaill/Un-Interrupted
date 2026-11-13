"""Local-folder podcasts.

A folder podcast keeps its audio where the user put it: the app never copies,
moves or deletes those files. A periodic ``scan`` job lists the folder, turns
every audio file into an episode (stable guid derived from the relative path)
and lets the normal pipeline take over from the ``download`` stage, which for
these episodes simply verifies the source file is still there.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config, db
from .paths import audio_type_for, display_title_from_filename, is_audio_filename

MAX_FILES = 5000
MAX_DEPTH = 6
MAX_BROWSE_ENTRIES = 2000
BROWSE_COUNT_LIMIT = 250

_DATE_PREFIX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?!\d)")


class FolderError(RuntimeError):
    pass


def normalize_path(raw: str | Path, cfg: dict[str, Any] | None = None) -> Path:
    """Validate a user-supplied folder path. Returns the resolved directory."""
    text = str(raw or "").strip()
    if not text:
        raise FolderError("Enter a folder path.")
    path = Path(os.path.expandvars(text)).expanduser()
    try:
        path = path.resolve()
    except OSError as exc:
        raise FolderError(f"Cannot resolve {text}: {exc}") from exc
    if not path.exists():
        raise FolderError(f"Folder not found: {path}")
    if not path.is_dir():
        raise FolderError(f"Not a folder: {path}")
    if not os.access(path, os.R_OK | os.X_OK):
        raise FolderError(f"Folder is not readable: {path}")

    cfg = cfg or config.load_config()
    home = config.app_home().resolve()
    if path == home or home in path.parents:
        raise FolderError("That folder is inside the app's own data folder.")
    library = config.podcasts_root(cfg).resolve()
    if path == library or library in path.parents:
        raise FolderError("That folder is inside the podcast library; pick another one.")
    return path


def feed_url_for(path: Path) -> str:
    """Synthetic unique key stored in podcasts.feed_url (never fetched)."""
    return f"folder://{path}"


def list_audio_files(root: Path, *, limit: int = MAX_FILES) -> list[Path]:
    """Audio files under ``root`` (recursive, hidden entries skipped)."""
    found: list[Path] = []
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        depth = len(current.parts) - root_depth
        if depth >= MAX_DEPTH:
            dirnames[:] = []
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and not (current / d).is_symlink()
        )
        for name in sorted(filenames):
            if name.startswith(".") or not is_audio_filename(name):
                continue
            found.append(current / name)
            if len(found) >= limit:
                return found
    return found


def relative_tag(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def guid_for(rel: str) -> str:
    return f"file:{rel}"


def published_at_for(path: Path) -> str:
    """Leading ``YYYY-MM-DD`` in the name wins, else the file's mtime."""
    match = _DATE_PREFIX.match(path.name)
    if match:
        try:
            moment = datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)),
                tzinfo=UTC,
            )
            return moment.isoformat()
        except ValueError:
            pass
    try:
        stamp = path.stat().st_mtime
    except OSError:
        stamp = datetime.now(UTC).timestamp()
    return datetime.fromtimestamp(stamp, UTC).isoformat()


def scan(podcast: dict[str, Any], *, auto_process: bool | None = None) -> dict[str, Any]:
    """Upsert one episode per audio file. Returns a summary for the job log."""
    raw_root = podcast.get("source_path") or ""
    root = Path(raw_root)
    if not raw_root or not root.is_dir():
        raise FolderError(f"Source folder is missing: {raw_root or '(none)'}")

    files = list_audio_files(root)
    auto = bool(podcast.get("auto_process")) if auto_process is None else auto_process
    created = refreshed = selected = skipped = 0
    errors: list[str] = []

    for path in files:
        try:
            stat = path.stat()
        except OSError as exc:
            skipped += 1
            errors.append(f"{path.name}: {exc}")
            continue
        rel = relative_tag(root, path)
        item = {
            "guid": guid_for(rel),
            "title": display_title_from_filename(path.name),
            "published_at": published_at_for(path),
            "original_url": path.as_uri(),
            "enclosure_type": audio_type_for(path),
            "enclosure_length": stat.st_size,
            "item_json": {"source_rel": rel},
        }
        episode, was_created = db.upsert_feed_episode(podcast["id"], item)
        # The absolute location is refreshed on every scan so a remounted
        # volume or a moved folder is picked up without touching the DB.
        if episode.get("source_path") != str(path):
            db.update_episode(episode["id"], source_path=str(path))
        if was_created:
            created += 1
            if auto:
                db.select_episode(episode["id"], "queued")
                db.enqueue_job(
                    "download",
                    podcast_id=podcast["id"],
                    episode_id=episode["id"],
                    priority=100,
                )
                selected += 1
        else:
            refreshed += 1

    db.update_podcast(podcast["id"], last_polled_at=db.now_iso(), last_error=None)
    return {
        "status": "ok",
        "folder": str(root),
        "files": len(files),
        "new": created,
        "known": refreshed,
        "queued": selected,
        "skipped": skipped,
        "errors": errors[:5],
    }


def _count_audio(path: Path, limit: int = 999) -> int:
    """How many audio files sit directly in ``path`` (for the picker hint)."""
    count = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir():
                        continue
                except OSError:
                    continue
                if is_audio_filename(entry.name):
                    count += 1
                    if count >= limit:
                        break
    except OSError:
        return 0
    return count


def browse(raw: str | Path, *, hidden: bool = False) -> dict[str, Any]:
    """List the sub-folders of ``raw`` for the in-app folder picker.

    Read-only: it never creates, moves or inspects file contents, and it only
    reports directories. Selection is still validated by ``normalize_path``.
    """
    text = str(raw or "").strip()
    base = Path(os.path.expandvars(text)).expanduser() if text else Path.home()
    try:
        path = base.resolve()
    except OSError as exc:
        raise FolderError(f"Cannot resolve {base}: {exc}") from exc
    if not path.exists():
        raise FolderError(f"Folder not found: {path}")
    if not path.is_dir():
        raise FolderError(f"Not a folder: {path}")

    entries: list[dict[str, Any]] = []
    truncated = False
    try:
        with os.scandir(path) as scan:
            for entry in scan:
                if not hidden and entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                entries.append({"name": entry.name, "path": str(path / entry.name)})
                if len(entries) >= MAX_BROWSE_ENTRIES:
                    truncated = True
                    break
    except PermissionError as exc:
        raise FolderError(f"Permission denied: {path}") from exc
    except OSError as exc:
        raise FolderError(f"Cannot read {path}: {exc}") from exc

    entries.sort(key=lambda row: row["name"].lower())
    if len(entries) <= BROWSE_COUNT_LIMIT:
        for row in entries:
            row["audio"] = _count_audio(Path(row["path"]))
    else:
        for row in entries:
            row["audio"] = None
    parent = path.parent
    return {
        "path": str(path),
        "parent": str(parent) if parent != path else None,
        "dirs": entries,
        "hidden": hidden,
        "truncated": truncated,
    }


def roots(cfg: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Shortcuts shown at the top of the folder picker."""
    cfg = cfg or config.load_config()
    home = Path.home()
    found: list[dict[str, str]] = [{"name": "Home", "path": str(home), "kind": "home"}]
    for label in ("Desktop", "Documents", "Downloads", "Movies", "Music"):
        candidate = home / label
        if candidate.is_dir():
            found.append({"name": label, "path": str(candidate), "kind": "folder"})
    icloud = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    if icloud.is_dir():
        found.append({"name": "iCloud Drive", "path": str(icloud), "kind": "folder"})
    volumes = Path("/Volumes")
    if volumes.is_dir():
        try:
            for entry in sorted(volumes.iterdir(), key=lambda item: item.name.lower()):
                if entry.is_dir():
                    found.append({"name": entry.name, "path": str(entry), "kind": "volume"})
        except OSError:
            pass
    try:
        storage = config.storage_root(cfg)
    except (OSError, ValueError):
        storage = None
    if storage and storage.is_dir():
        found.append({"name": "Podcast storage", "path": str(storage), "kind": "storage"})
    return found


def remap_episodes(podcast: dict[str, Any], new_root: Path) -> int:
    """Point existing episodes at a new folder, keeping their relative paths."""
    moved = 0
    for episode in db.list_episodes(podcast["id"], limit=100000):
        try:
            rel = (json.loads(episode.get("item_json") or "{}") or {}).get("source_rel")
        except (TypeError, ValueError):
            rel = None
        if not rel:
            continue
        candidate = new_root / rel
        if candidate.is_file():
            db.update_episode(episode["id"], source_path=str(candidate))
            moved += 1
    return moved
