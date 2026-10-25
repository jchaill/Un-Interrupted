from __future__ import annotations

import email.utils
import os
import re
from pathlib import Path
from typing import Iterator

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from .paths import audio_type_for, is_audio_filename

CHUNK = 1024 * 1024
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    if not header or "," in header:
        return None
    match = _RANGE_RE.match(header.strip())
    if not match or (match.group(1) == "" and match.group(2) == ""):
        return None
    start_s, end_s = match.group(1), match.group(2)
    if start_s == "":
        suffix = int(end_s)
        if suffix <= 0:
            return None
        return max(0, size - suffix), size - 1
    start = int(start_s)
    if start >= size:
        return None
    end = size - 1 if end_s == "" else min(int(end_s), size - 1)
    if end < start:
        return None
    return start, end


def _etag(path: Path, size: int, mtime_ns: int) -> str:
    return f'"{size:x}-{mtime_ns:x}"'


def _iter_file(path: Path, start: int, length: int) -> Iterator[bytes]:
    with open(path, "rb") as fh:
        fh.seek(start)
        remaining = length
        while remaining > 0:
            chunk = fh.read(min(CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def serve_file(
    request: Request,
    path: Path,
    *,
    content_type: str | None = None,
    cache_control: str = "private, max-age=0, must-revalidate",
    filename: str | None = None,
) -> Response:
    path = Path(path)
    if not path.is_file():
        return Response("Not found", status_code=404)
    stat = path.stat()
    size = stat.st_size
    mtime_ns = stat.st_mtime_ns
    etag = _etag(path, size, mtime_ns)
    last_modified = email.utils.formatdate(stat.st_mtime, usegmt=True)
    ctype = content_type or audio_type_for(path)
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Last-Modified": last_modified,
        "Cache-Control": cache_control,
        "X-Content-Type-Options": "nosniff",
    }
    if filename:
        safe = filename.replace('"', "")
        headers["Content-Disposition"] = f"inline; filename=\"{safe}\""

    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={k: v for k, v in headers.items() if k in {"ETag", "Last-Modified", "Cache-Control"}})

    range_header = request.headers.get("range")
    if_range = request.headers.get("if-range")
    if range_header and if_range and if_range not in {etag, last_modified}:
        range_header = None

    requested = _parse_range(range_header, size) if range_header else None
    if range_header and requested is None:
        # Invalid/out-of-bounds range.
        return Response(
            status_code=416,
            headers={**headers, "Content-Range": f"bytes */{size}"},
        )

    if requested:
        start, end = requested
        length = end - start + 1
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(length)
        if request.method.upper() == "HEAD":
            return Response(status_code=206, headers=headers, media_type=ctype)
        return StreamingResponse(
            _iter_file(path, start, length),
            status_code=206,
            headers=headers,
            media_type=ctype,
        )

    headers["Content-Length"] = str(size)
    if request.method.upper() == "HEAD":
        return Response(status_code=200, headers=headers, media_type=ctype)
    return StreamingResponse(_iter_file(path, 0, size), status_code=200, headers=headers, media_type=ctype)


def serve_audio(request: Request, path: Path, *, filename: str | None = None) -> Response:
    if not is_audio_filename(path.name):
        return Response("Not found", status_code=404)
    return serve_file(
        request,
        path,
        content_type=audio_type_for(path),
        cache_control="private, max-age=3600",
        filename=filename or path.name,
    )
