from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable

import httpx

from . import config

ProgressFn = Callable[[int, int | None], None]


class DownloadError(RuntimeError):
    pass


def _free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


def check_disk_space(path: Path, needed_bytes: int, min_free_gb: int | None = None) -> None:
    if min_free_gb is None:
        min_free_gb = int(config.load_config().get("min_free_gb", 5))
    reserve = int(min_free_gb) * 1024**3
    free = _free_bytes(path)
    if free - needed_bytes < reserve:
        raise DownloadError(
            f"Not enough disk space: need {needed_bytes / 1024**3:.1f} GB plus "
            f"{min_free_gb} GB reserve, only {free / 1024**3:.1f} GB free"
        )


def download_file(
    url: str,
    dest: Path,
    *,
    expected_size: int | None = None,
    progress: ProgressFn | None = None,
    timeout: float = 120,
) -> dict[str, Any]:
    """Download with resume, temp file, and atomic rename.

    Servers that ignore Range cause a clean restart. Existing completed files are
    returned untouched, which makes retries after a crash cheap.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        return {"path": dest, "size": dest.stat().st_size, "resumed": True, "skipped": True}

    part = dest.with_suffix(dest.suffix + ".part")
    offset = part.stat().st_size if part.is_file() else 0
    headers = {
        "User-Agent": "Un-Interupted/0.1",
        "Accept": "*/*",
        "Range": f"bytes={offset}-",
    } if offset else {"User-Agent": "Un-Interupted/0.1", "Accept": "*/*"}

    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=20, read=timeout, write=30, pool=20),
        ) as client:
            with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 416 and offset:
                    # The partial file is at least as large as the remote file.
                    # Let the audio probe decide whether it is valid.
                    os.replace(part, dest)
                    return {"path": dest, "size": offset, "resumed": True, "skipped": False}
                if resp.status_code >= 400:
                    raise DownloadError(f"Download failed with HTTP {resp.status_code}")

                if resp.status_code == 206:
                    mode = "ab"
                    total = None
                    if offset and resp.headers.get("content-range"):
                        try:
                            total = int(resp.headers["content-range"].split("/")[-1])
                        except (ValueError, IndexError):
                            total = None
                else:
                    mode = "wb"
                    offset = 0
                    total = int(resp.headers["content-length"]) if resp.headers.get("content-length") else None

                hint = total or expected_size
                if hint:
                    check_disk_space(dest.parent, hint)

                done = offset
                if progress:
                    progress(done, total)
                with open(part, mode) as fh:
                    for chunk in resp.iter_bytes(1024 * 256):
                        fh.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
                    fh.flush()
                    os.fsync(fh.fileno())
    except httpx.HTTPError as exc:
        raise DownloadError(f"Download failed: {exc}") from exc

    if not part.is_file() or part.stat().st_size == 0:
        raise DownloadError("Download produced an empty file")

    size = part.stat().st_size
    if expected_size and abs(size - expected_size) > 1024 * 1024:
        # Some CDNs report a stale length; the audio probe is authoritative.
        pass
    os.replace(part, dest)
    return {"path": dest, "size": size, "resumed": bool(offset), "skipped": False}
