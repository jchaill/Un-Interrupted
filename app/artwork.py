from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from . import config, db

MAX_ARTWORK_BYTES = 20 * 1024 * 1024
_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def artwork_file(podcast: dict[str, Any]) -> Path | None:
    rel = (podcast or {}).get("artwork_path")
    if not rel:
        return None
    path = config.artwork_dir() / os.path.basename(rel)
    return path if path.is_file() else None


def ensure_artwork(cfg: dict[str, Any], podcast: dict[str, Any]) -> str | None:
    existing = artwork_file(podcast)
    if existing:
        return existing.name

    url = (podcast or {}).get("artwork_url")
    if not url:
        return None

    ext = Path(unquote(urlparse(url).path)).suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        ext = ""

    config.artwork_dir().mkdir(parents=True, exist_ok=True)
    slug = podcast["slug"]
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=20, read=40, write=20, pool=20),
            headers={"User-Agent": "Un-Interupted/0.1"},
        ) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                ctype = (resp.headers.get("content-type") or "").split(";")[0].lower()
                if ctype and not (ctype.startswith("image/") or ctype == "application/octet-stream"):
                    return None
                if not ext:
                    ext = _EXT_BY_TYPE.get(ctype) or ".jpg"
                dest = config.artwork_dir() / f"{slug}{ext}"
                tmp = dest.with_suffix(dest.suffix + ".part")
                total = 0
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_bytes(64 * 1024):
                        total += len(chunk)
                        if total > MAX_ARTWORK_BYTES:
                            raise ValueError("artwork is too large")
                        fh.write(chunk)
                if total < 256:
                    tmp.unlink(missing_ok=True)
                    return None
                with open(tmp, "rb") as check:
                    magic = check.read(16)
                if not (
                    magic.startswith(b"\xff\xd8")
                    or magic.startswith(b"\x89PNG")
                    or magic.startswith(b"GIF8")
                    or (magic[:4] == b"RIFF" and magic[8:12] == b"WEBP")
                ):
                    tmp.unlink(missing_ok=True)
                    return None
                os.replace(tmp, dest)
    except (httpx.HTTPError, OSError, ValueError):
        return None

    # Remove a previous artwork file with a different extension.
    for old in config.artwork_dir().glob(f"{slug}.*"):
        if old != dest and old.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            old.unlink(missing_ok=True)

    db.update_podcast(podcast["id"], artwork_path=dest.name)
    return dest.name


def content_type_for(path: Path) -> str:
    ctype = mimetypes.guess_type(str(path))[0]
    return ctype or "application/octet-stream"


def placeholder_svg(title: str, size: int = 600) -> str:
    import html

    safe = html.escape((title or "Podcast")[:40])
    initials = "".join(part[0] for part in (title or "P").split()[:2]).upper() or "P"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#5b7cfa"/><stop offset="1" stop-color="#9b5cff"/>
  </linearGradient></defs>
  <rect width="{size}" height="{size}" rx="72" fill="url(#g)"/>
  <text x="50%" y="47%" text-anchor="middle" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif"
        font-size="{size * 0.28:.0f}" font-weight="700" fill="white">{html.escape(initials)}</text>
  <text x="50%" y="67%" text-anchor="middle" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif"
        font-size="{size * 0.045:.0f}" fill="rgba(255,255,255,.85)">{safe}</text>
</svg>"""
