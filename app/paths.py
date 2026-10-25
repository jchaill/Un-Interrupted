from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

AUDIO_EXT_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/x-m4a",
    ".m4b": "audio/x-m4b",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
}

CONTENT_TYPE_EXT = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/x-m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "video/mp4": ".mp4",
}

CLEAN_SUFFIX = " (no ads)"


def ascii_slug(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text or "podcast"


def unique_slug(base: str, exists) -> str:
    slug = ascii_slug(base)
    if not exists(slug):
        return slug
    for i in range(2, 1000):
        candidate = f"{slug}-{i}"
        if not exists(candidate):
            return candidate
    raise RuntimeError("could not allocate a unique slug")


def guid8(guid: str) -> str:
    return hashlib.sha1((guid or "").encode("utf-8")).hexdigest()[:8]


def published_date(value: str | None) -> str:
    if value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def short_title(title: str, max_len: int = 70) -> str:
    base = ascii_slug(title, max_len=max_len)
    return base or "episode"


def episode_basename(title: str, guid: str, published_at: str | None) -> str:
    """Stable, ASCII-safe basename without extension.

    Example: ``2026-10-22 - olivia-harrison - 1a2b3c4d``
    """
    return f"{published_date(published_at)} - {short_title(title)} - {guid8(guid)}"


def extension_from_url(url: str, content_type: str | None = None) -> str:
    path = unquote(urlparse(url or "").path)
    ext = os.path.splitext(path)[1].lower()
    if ext in AUDIO_EXT_TYPES:
        return ext
    if content_type:
        ctype = content_type.split(";")[0].strip().lower()
        if ctype in CONTENT_TYPE_EXT:
            return CONTENT_TYPE_EXT[ctype]
    return ".mp3"


def audio_type_for(path: Path | str) -> str:
    ext = Path(path).suffix.lower()
    return AUDIO_EXT_TYPES.get(ext, "application/octet-stream")


def is_audio_filename(name: str) -> bool:
    return Path(name).suffix.lower() in AUDIO_EXT_TYPES


def safe_join(base: Path, untrusted: str) -> Path:
    """Join a relative path and refuse to escape ``base``."""
    base = base.resolve()
    candidate = (base / untrusted).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError("path escapes its base directory")
    return candidate


def safe_component(name: str, fallback: str = "file") -> str:
    name = (name or "").strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        return fallback
    return name


def human_size(num: int | float | None) -> str:
    if num is None:
        return "—"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def human_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    total = int(round(float(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def parse_duration(value) -> float | None:
    """Parse itunes:duration values: seconds, MM:SS, HH:MM:SS."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    parts = text.split(":")
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return None
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def display_title_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    if stem.endswith(CLEAN_SUFFIX):
        stem = stem[: -len(CLEAN_SUFFIX)]
    # Strip our generated "YYYY-MM-DD - " prefix.
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}\s*-\s*", "", stem)
    return stem.strip() or stem
