from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .. import config

ENGINE_API_VERSION = 1
ENGINE_ROOT = Path(__file__).resolve().parent / "upstream"
DEFAULT_SCRIPTS = ENGINE_ROOT / "scripts"


class EngineError(RuntimeError):
    pass


class EngineMissing(EngineError):
    pass


class QAFailure(EngineError):
    pass


def scripts_dir() -> Path:
    override = os.environ.get("UNINTERUPTED_ENGINE_DIR")
    if override:
        root = Path(override).expanduser().resolve()
        return root / "scripts" if (root / "scripts").is_dir() else root
    return DEFAULT_SCRIPTS


def engine_version() -> str:
    version_file = scripts_dir().parent / "VERSION"
    if version_file.is_file():
        text = version_file.read_text(encoding="utf-8", errors="replace")
        vendored = re.search(r"^vendored:\s*(\S+)", text, re.M)
        api = re.search(r"^api:\s*(\S+)", text, re.M)
        if vendored:
            return f"ad-remover@{vendored.group(1)}" + (f"+api{api.group(1)}" if api else "")
    return "ad-remover@unknown"


def tool_env() -> dict[str, str]:
    env = os.environ.copy()
    parts = [
        str(Path(sys.executable).parent),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        env.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
    ]
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    env["PATH"] = ":".join(seen)
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def which_tool(name: str) -> str | None:
    return shutil.which(name, path=tool_env()["PATH"])


def model_path() -> Path:
    return config.model_path()


def check_tools() -> dict[str, Any]:
    scripts = scripts_dir()
    expected = [
        "transcribe.sh",
        "refine.sh",
        "detect_cuts.py",
        "cut.py",
        "qa_join.py",
        "verify_audio.sh",
        "find_repeats.py",
        "analyze.py",
    ]
    missing_scripts = [name for name in expected if not (scripts / name).is_file()]
    model = model_path()
    model_ok = model.is_file() and model.stat().st_size > 100_000_000
    result = {
        "ffmpeg": which_tool("ffmpeg"),
        "ffprobe": which_tool("ffprobe"),
        "whisper_cli": which_tool("whisper-cli"),
        "model": str(model) if model_ok else None,
        "model_path": str(model),
        "model_size": model.stat().st_size if model.is_file() else 0,
        "scripts_dir": str(scripts),
        "missing_scripts": missing_scripts,
        "engine_version": engine_version(),
        "api_version": ENGINE_API_VERSION,
    }
    result["ready"] = bool(
        result["ffmpeg"]
        and result["ffprobe"]
        and result["whisper_cli"]
        and result["model"]
        and not missing_scripts
    )
    return result


def require_tools() -> dict[str, Any]:
    status = check_tools()
    missing = []
    if not status["ffmpeg"]:
        missing.append("ffmpeg")
    if not status["ffprobe"]:
        missing.append("ffprobe")
    if not status["whisper_cli"]:
        missing.append("whisper-cli")
    if not status["model"]:
        missing.append("Whisper model")
    if status["missing_scripts"]:
        missing.append("engine scripts: " + ", ".join(status["missing_scripts"]))
    if missing:
        raise EngineMissing(
            "Missing processing toolchain: "
            + ", ".join(missing)
            + ". Open Settings → Toolchain and install it."
        )
    return status


def _run(
    cmd: list[str],
    *,
    cwd: Path,
    log_path: Path,
    timeout: float,
    env_extra: dict[str, str] | None = None,
    allow_fail_codes: Iterable[int] = (),
) -> tuple[int, str]:
    env = tool_env()
    if env_extra:
        env.update(env_extra)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    output: list[str] = []
    with open(log_path, "a", encoding="utf-8", errors="replace") as log:
        log.write(
            f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n$ "
            + " ".join(shlex.quote(str(part)) for part in cmd)
            + f"\n(cwd: {cwd})\n"
        )
        log.flush()
        try:
            proc = subprocess.Popen(
                [str(part) for part in cmd],
                cwd=str(cwd),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise EngineError(f"Command not found: {cmd[0]}") from exc

        def reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                log.flush()
                if len(output) < 6000:
                    output.append(line)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait()
            thread.join(timeout=5)
            raise EngineError(f"Processing timed out after {int(timeout)}s: {cmd[0]}")
        thread.join(timeout=10)
    rc = int(rc)
    if rc != 0 and rc not in set(allow_fail_codes):
        tail = "".join(output[-12:]).strip()
        raise EngineError(f"{Path(cmd[0]).name} failed (exit {rc}).\n{tail}")
    return rc, "".join(output)


def _read_labels(labels_json: str | None) -> list[list[str]]:
    if not labels_json or not str(labels_json).strip():
        return []
    try:
        data = json.loads(labels_json)
    except json.JSONDecodeError as exc:
        raise EngineError(f"Labels are not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise EngineError("Labels must be a JSON array of [regex, title] pairs")
    cleaned: list[list[str]] = []
    for row in data:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise EngineError("Each label must be a [regex, title] pair")
        regex, title = str(row[0]), str(row[1])
        try:
            re.compile(regex)
        except re.error as exc:
            raise EngineError(f"Invalid label regex {regex!r}: {exc}") from exc
        cleaned.append([regex, title])
    return cleaned


def write_labels(podcast_dir: Path, podcast: dict[str, Any]) -> Path:
    labels = _read_labels(podcast.get("labels_json"))
    work = podcast_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    path = work / "labels.json"
    path.write_text(json.dumps(labels, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def language_for(podcast: dict[str, Any]) -> str:
    value = (podcast.get("language") or "auto").strip().lower()
    return value if value in {"fr", "en", "es", "de", "it", "pt", "nl"} else "auto"


def transcribe(
    *,
    podcast: dict[str, Any],
    podcast_dir: Path,
    original: Path,
    tag: str,
    log_path: Path,
    timeout: float = 3 * 3600,
) -> Path:
    require_tools()
    script = scripts_dir() / "transcribe.sh"
    _run(
        ["bash", str(script), str(original), tag],
        cwd=podcast_dir,
        log_path=log_path,
        timeout=timeout,
        env_extra={
            "WHISPER_MODEL": str(model_path()),
            "WHISPER_LANG": language_for(podcast),
        },
    )
    tr = podcast_dir / "work" / f"tr_{tag}.json"
    if not tr.is_file():
        raise EngineError(f"Transcription did not produce {tr.name}")
    try:
        payload = json.loads(tr.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EngineError(f"Transcription file is unreadable: {exc}") from exc
    if not payload.get("transcription"):
        raise EngineError("Transcription is empty")
    return tr


def detect(
    *,
    podcast: dict[str, Any],
    podcast_dir: Path,
    original: Path,
    transcript: Path,
    cuts_path: Path,
    labels_path: Path,
    log_path: Path,
    timeout: float = 1800,
) -> tuple[list[list[float | None]], str]:
    require_tools()
    script = scripts_dir() / "detect_cuts.py"
    cuts_path.unlink(missing_ok=True)
    env = {
        "WHISPER_MODEL": str(model_path()),
        "AD_LANG": (podcast.get("ad_lang") or "auto"),
        "AD_SHOW_STOP": (podcast.get("ad_show_stop") or ""),
    }
    _rc, output = _run(
        [
            sys.executable,
            str(script),
            str(original),
            str(transcript),
            str(cuts_path),
            "--labels",
            str(labels_path),
        ],
        cwd=podcast_dir,
        log_path=log_path,
        timeout=timeout,
        env_extra=env,
    )
    if not cuts_path.is_file():
        raise EngineError("Detection did not produce a cut plan")
    try:
        raw = json.loads(cuts_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EngineError(f"Cut plan is unreadable: {exc}") from exc
    cuts: list[list[float | None]] = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            start = float(row[0])
            end = float(row[1]) if row[1] is not None else None
        except (TypeError, ValueError):
            continue
        cuts.append([round(max(0.0, start), 2), round(end, 2) if end is not None else None])
    cuts.sort(key=lambda c: c[0])
    return cuts, output


def cut(
    *,
    podcast_dir: Path,
    original: Path,
    out_path: Path,
    cuts_path: Path,
    log_path: Path,
    transcript: Path,
    timeout: float = 3600,
) -> Path:
    require_tools()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)
    if original.suffix.lower() == ".mp3":
        script = scripts_dir() / "cut.py"
        _run(
            [
                sys.executable,
                str(script),
                str(original),
                str(out_path),
                str(cuts_path),
            ],
            cwd=podcast_dir,
            log_path=log_path,
            timeout=timeout,
            env_extra={"WHISPER_MODEL": str(model_path())},
        )
    else:
        from .cut_generic import cut_generic
        from .verify import probe_duration

        duration = probe_duration(original)
        if not duration:
            raise EngineError("Could not read the original duration")
        cuts = json.loads(cuts_path.read_text(encoding="utf-8"))
        cut_generic(
            original,
            out_path,
            cuts,
            float(duration),
            log_path=log_path,
            timeout=timeout,
        )
    if not out_path.is_file() or out_path.stat().st_size < 1000:
        raise EngineError("Cutting produced an empty output file")
    return out_path


def qa(
    *,
    podcast: dict[str, Any],
    podcast_dir: Path,
    clean: Path,
    cuts_path: Path,
    log_path: Path,
    expected_duration: float | None,
    source_title: str | None,
    timeout: float = 2 * 3600,
) -> tuple[bool, list[str]]:
    """Run the engine's seam QA plus a format-agnostic integrity check."""
    require_tools()
    script = scripts_dir() / "qa_join.py"
    lines: list[str] = []
    ok = True
    try:
        rc, output = _run(
            [sys.executable, str(script), str(clean), str(cuts_path)],
            cwd=podcast_dir,
            log_path=log_path,
            timeout=timeout,
            env_extra={
                "WHISPER_MODEL": str(model_path()),
                "WHISPER_LANG": language_for(podcast),
            },
            allow_fail_codes=(1,),
        )
        lines.extend(output.strip().splitlines())
        if rc != 0:
            ok = False
            lines.append("FAIL: seam QA found an ad signature (or could not verify a seam)")
    except EngineError as exc:
        ok = False
        lines.append(f"FAIL: seam QA crashed: {exc}")

    # Upstream's verify script is MP3-specific; always run our generic verifier.
    if clean.suffix.lower() == ".mp3":
        verify_script = scripts_dir() / "verify_audio.sh"
        if verify_script.is_file():
            try:
                rc, output = _run(
                    ["bash", str(verify_script), str(clean)]
                    + ([f"{expected_duration:.3f}"] if expected_duration else []),
                    cwd=podcast_dir,
                    log_path=log_path,
                    timeout=1800,
                    env_extra={"WHISPER_MODEL": str(model_path())},
                    allow_fail_codes=(1,),
                )
                lines.extend(output.strip().splitlines())
                if rc != 0:
                    ok = False
            except EngineError as exc:
                ok = False
                lines.append(f"FAIL: verify_audio.sh crashed: {exc}")

    from .verify import verify_audio

    try:
        generic_ok, generic_lines = verify_audio(
            clean,
            expected_duration=expected_duration,
            source_title=source_title,
            extension=clean.suffix,
        )
        lines.extend(generic_lines)
        ok = ok and generic_ok
    except Exception as exc:
        ok = False
        lines.append(f"FAIL: integrity verification crashed: {exc}")

    return ok, lines


def transcript_segments(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for seg in payload.get("transcription") or []:
        try:
            start = float(seg["offsets"]["from"]) / 1000.0
            end = float(seg["offsets"]["to"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            continue
        out.append({"start": start, "end": end, "text": str(seg.get("text") or "").strip()})
    return out


def transcript_text(path: Path) -> str:
    segs = transcript_segments(path)
    return "\n".join(f"[{s['start']:9.2f} -> {s['end']:9.2f}] {s['text']}" for s in segs)


def probe_duration(path: Path | str) -> float | None:
    from .verify import probe_duration as _probe_duration

    return _probe_duration(path)
