from __future__ import annotations

import subprocess
from pathlib import Path

from .adapter import EngineError, tool_env, which_tool

_CODEC_ARGS = {
    ".m4a": ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"],
    ".m4b": ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"],
    ".mp4": ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"],
    ".aac": ["-c:a", "aac", "-b:a", "192k"],
    ".flac": ["-c:a", "flac"],
    ".ogg": ["-c:a", "libvorbis", "-q:a", "5"],
    ".oga": ["-c:a", "libvorbis", "-q:a", "5"],
    ".opus": ["-c:a", "libopus", "-b:a", "128k"],
    ".wav": ["-c:a", "pcm_s16le"],
}


def keep_ranges(cuts: list[list[float]], total: float) -> list[tuple[float, float]]:
    normalized = sorted(
        (max(0.0, float(s)), min(total, float(e) if e is not None else total))
        for s, e in cuts
    )
    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in normalized:
        if start > cursor + 0.2:
            keeps.append((cursor, start))
        cursor = max(cursor, end)
    if total - cursor > 0.2:
        keeps.append((cursor, total))
    return keeps


def cut_generic(
    in_path: Path,
    out_path: Path,
    cuts: list[list[float]],
    total: float,
    *,
    log_path: Path,
    timeout: float = 3600,
) -> None:
    """Same-container cut for non-MP3 files, re-encoding the kept ranges.

    The upstream cutter's MP3 stream-copy path is preferred for MP3. For other
    containers, filter-based re-encoding in the original container/codec is the
    most reliable way to keep the source format.
    """
    ffmpeg = which_tool("ffmpeg")
    if not ffmpeg:
        raise EngineError("ffmpeg is not installed")
    ext = in_path.suffix.lower()
    codec_args = _CODEC_ARGS.get(ext)
    if not codec_args:
        raise EngineError(f"Unsupported output format for cutting: {ext}")

    keeps = keep_ranges(cuts, total)
    if not keeps:
        raise EngineError("nothing left after cuts")

    filters = []
    for i in range(len(keeps)):
        start, end = keeps[i]
        filters.append(
            f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{i}]"
        )
    concat_inputs = "".join(f"[a{i}]" for i in range(len(keeps)))
    filters.append(f"{concat_inputs}concat=n={len(keeps)}:v=0:a=1[out]")

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(in_path),
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[out]",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        *codec_args,
        str(out_path),
    ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log:
        log.write("$ " + " ".join(cmd) + "\n")
        log.flush()
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=tool_env(),
            timeout=timeout,
        )
        if proc.stdout:
            log.write(proc.stdout)
        if proc.stderr:
            log.write(proc.stderr)
        log.flush()
    if proc.returncode != 0:
        raise EngineError(
            "ffmpeg cut failed: " + (proc.stderr or proc.stdout or "").strip()[:1500]
        )
    if not out_path.is_file() or out_path.stat().st_size < 1000:
        raise EngineError("ffmpeg produced an empty output file")
