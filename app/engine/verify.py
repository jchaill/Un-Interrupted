from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .adapter import EngineMissing, tool_env, which_tool


def _require(name: str) -> str:
    path = which_tool(name)
    if not path:
        raise EngineMissing(
            f"{name} is not installed. Open Settings → Toolchain and install it."
        )
    return path


def probe(path: Path | str) -> dict[str, Any]:
    ffprobe = _require("ffprobe")
    proc = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,size,format_name:stream=codec_name,sample_rate,channels,bit_rate",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        env=tool_env(),
        timeout=120,
    )
    if proc.returncode != 0:
        raise ValueError((proc.stderr or "ffprobe failed").strip()[:500])
    data = json.loads(proc.stdout or "{}")
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    audio = next((s for s in streams if s.get("codec_name")), streams[0] if streams else {})
    duration = None
    try:
        duration = float(fmt.get("duration")) if fmt.get("duration") is not None else None
    except (TypeError, ValueError):
        duration = None
    return {
        "duration": duration,
        "size": int(fmt.get("size") or 0),
        "format_name": fmt.get("format_name"),
        "codec": audio.get("codec_name"),
        "sample_rate": audio.get("sample_rate"),
        "channels": audio.get("channels"),
        "bit_rate": audio.get("bit_rate"),
    }


def probe_duration(path: Path | str) -> float | None:
    try:
        return probe(path).get("duration")
    except Exception:
        return None


def verify_audio(
    path: Path | str,
    *,
    expected_duration: float | None = None,
    source_title: str | None = None,
    extension: str | None = None,
) -> tuple[bool, list[str]]:
    """Format-agnostic integrity checks. Returns (ok, report lines)."""
    path = Path(path)
    lines: list[str] = []
    failures = 0

    def note(text: str) -> None:
        lines.append(text)

    def fail(text: str) -> None:
        nonlocal failures
        failures += 1
        lines.append("FAIL: " + text)

    if not path.is_file():
        return False, [f"FAIL: file not found: {path}"]
    size = path.stat().st_size
    if size < 10_000:
        fail(f"file suspiciously small: {size} bytes")

    try:
        info = probe(path)
    except Exception as exc:
        return False, [f"FAIL: ffprobe failed: {exc}"]

    note(
        "stream: {codec} {rate} Hz {channels} ch {br} bps, {fmt}".format(
            codec=info.get("codec"),
            rate=info.get("sample_rate"),
            channels=info.get("channels"),
            br=info.get("bit_rate"),
            fmt=info.get("format_name"),
        )
    )
    if not info.get("codec"):
        fail("no audio stream")
    br = info.get("bit_rate")
    try:
        if br and int(br) < 24_000:
            fail(f"bitrate absurdly low: {br}")
    except (TypeError, ValueError):
        pass

    ffmpeg = _require("ffmpeg")
    try:
        scan = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True,
            text=True,
            env=tool_env(),
            timeout=1800,
        )
        if scan.returncode != 0 or (scan.stderr or "").strip():
            fail("decode errors:\n" + (scan.stderr or "").strip()[:1500])
        else:
            note("decode: clean (full file)")
    except subprocess.TimeoutExpired:
        fail("full decode timed out")

    duration = info.get("duration")
    if duration:
        try:
            tail_start = max(0.0, float(duration) - 5.0)
            tail = subprocess.run(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-ss",
                    f"{tail_start:.3f}",
                    "-i",
                    str(path),
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                env=tool_env(),
                timeout=120,
            )
            if tail.returncode != 0 or (tail.stderr or "").strip():
                fail("tail seek/decode errors:\n" + (tail.stderr or "").strip()[:800])
            else:
                note("tail seek+decode: clean")
        except subprocess.TimeoutExpired:
            fail("tail decode timed out")

    if expected_duration is not None and duration is not None:
        delta = abs(float(duration) - float(expected_duration))
        tolerance = 1.0 if (extension or path.suffix).lower() == ".mp3" else 2.0
        if delta <= tolerance:
            note(
                f"duration: {float(duration):.2f}s (expected {float(expected_duration):.2f}, "
                f"delta {delta:.2f}s) OK"
            )
        else:
            fail(
                f"duration {float(duration):.2f}s differs from expected "
                f"{float(expected_duration):.2f}s by {delta:.2f}s"
            )

    if source_title:
        try:
            tags = subprocess.run(
                [
                    _require("ffprobe"),
                    "-v",
                    "error",
                    "-show_entries",
                    "format_tags=title",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                env=tool_env(),
                timeout=60,
            ).stdout.strip()
            note(f"title tag: {tags}" if tags else "WARN: title tag missing")
        except Exception:
            pass

    ok = failures == 0
    note("VERIFY PASS" if ok else f"VERIFY FAILED ({failures} issue(s))")
    return ok, lines
