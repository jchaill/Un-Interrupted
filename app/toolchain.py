from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from . import config
from .engine.adapter import check_tools, tool_env, which_tool

MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
    "ggml-large-v3-turbo-q5_0.bin"
)
MODEL_MIN_BYTES = 100_000_000

_LOCK = threading.RLock()
_THREAD: threading.Thread | None = None


def _default_state() -> dict[str, Any]:
    return {
        "running": False,
        "pid": None,
        "step": "idle",
        "message": "Not installed yet",
        "error": None,
        "started_at": None,
        "updated_at": None,
        "downloaded": 0,
        "total": 0,
    }


def read_state() -> dict[str, Any]:
    path = config.setup_state_path()
    state = _default_state()
    if path.is_file():
        try:
            state.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    if state.get("running") and state.get("pid"):
        try:
            os.kill(int(state["pid"]), 0)
        except (OSError, ValueError, ProcessLookupError):
            state.update(
                {
                    "running": False,
                    "error": "The previous toolchain install was interrupted.",
                    "message": "Interrupted",
                }
            )
            _write_state(state)
    return state


def _write_state(state: dict[str, Any]) -> None:
    path = config.setup_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _update(**fields: Any) -> None:
    with _LOCK:
        state = read_state()
        state.update(fields)
        _write_state(state)


def _log(line: str) -> None:
    path = config.setup_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", errors="replace") as fh:
        fh.write(line)
        if not line.endswith("\n"):
            fh.write("\n")
        fh.flush()


def log_tail(max_bytes: int = 30_000) -> str:
    path = config.setup_log_path()
    if not path.is_file():
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def status() -> dict[str, Any]:
    tools = check_tools()
    state = read_state()
    return {"tools": tools, "state": state, "log_tail": log_tail(8000)}


def _run_streaming(cmd: list[str], *, env_extra: dict[str, str] | None = None) -> int:
    env = tool_env()
    env.update(
        {
            "HOMEBREW_NO_AUTO_UPDATE": "1",
            "HOMEBREW_NO_INSTALL_CLEANUP": "1",
            "NONINTERACTIVE": "1",
        }
    )
    if env_extra:
        env.update(env_extra)
    _log("\n$ " + " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    assert proc.stdout is not None
    last_update = 0.0
    for line in proc.stdout:
        _log(line.rstrip("\n"))
        now = time.time()
        if now - last_update > 0.4:
            _update(message=line.strip()[:300] or "Working…")
            last_update = now
    return proc.wait()


def _download_model() -> None:
    model = config.model_path()
    config.models_dir().mkdir(parents=True, exist_ok=True)
    part = model.with_suffix(model.suffix + ".part")
    offset = part.stat().st_size if part.is_file() else 0

    free = shutil.disk_usage(config.models_dir()).free
    if free < 1.2 * 1024**3:
        raise RuntimeError(
            f"Not enough free disk space for the Whisper model "
            f"({free / 1024**3:.1f} GB free, need ~0.6 GB)"
        )

    headers = {"User-Agent": "Un-Interupted/0.1", "Accept": "*/*"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    _log(f"Downloading Whisper model to {model} (resume offset {offset})")
    with httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=30, read=120, write=30, pool=30),
    ) as client:
        with client.stream("GET", MODEL_URL, headers=headers) as resp:
            if resp.status_code == 416 and offset:
                # The partial file is already at least the full size.
                os.replace(part, model)
                return
            resp.raise_for_status()
            if resp.status_code == 206:
                total = None
                content_range = resp.headers.get("content-range") or ""
                if "/" in content_range:
                    try:
                        total = int(content_range.rsplit("/", 1)[1])
                    except ValueError:
                        total = None
                mode = "ab"
            else:
                offset = 0
                total = int(resp.headers.get("content-length") or 0) or None
                mode = "wb"
            _update(step="download-model", total=total or 0, downloaded=offset)
            done = offset
            last = 0.0
            with open(part, mode) as fh:
                for chunk in resp.iter_bytes(1024 * 512):
                    fh.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last > 0.5:
                        pct = (done / total * 100) if total else 0
                        _update(
                            step="download-model",
                            downloaded=done,
                            total=total or 0,
                            message=f"Downloading Whisper model… {pct:.0f}%",
                        )
                        last = now
                fh.flush()
                os.fsync(fh.fileno())
    if not part.is_file() or part.stat().st_size < MODEL_MIN_BYTES:
        size = part.stat().st_size if part.is_file() else 0
        raise RuntimeError(f"Whisper model download is incomplete ({size} bytes)")
    os.replace(part, model)
    _update(
        step="download-model",
        downloaded=model.stat().st_size,
        total=model.stat().st_size,
        message="Whisper model downloaded",
    )
    _log(f"Model ready: {model} ({model.stat().st_size} bytes)")


def _install_worker() -> None:
    try:
        _log(f"\n=== Toolchain install started {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
        tools = check_tools()
        brew = which_tool("brew")
        formulas: list[str] = []
        if not tools["ffmpeg"] or not tools["ffprobe"]:
            formulas.append("ffmpeg")
        if not tools["whisper_cli"]:
            formulas.append("whisper-cpp")

        if formulas:
            if not brew:
                raise RuntimeError(
                    "Homebrew is required to install ffmpeg and whisper.cpp. "
                    "Install it from https://brew.sh, then retry."
                )
            _update(step="brew", message="Installing " + ", ".join(formulas) + "…")
            rc = _run_streaming([brew, "install", *formulas])
            if rc != 0:
                raise RuntimeError(
                    "Homebrew install failed. On a fresh Mac this usually means the "
                    "Xcode license has not been accepted: run "
                    "`sudo xcodebuild -license accept` and retry. See the log below."
                )
            _update(step="recheck", message="Verifying tools…")

        tools = check_tools()
        if not tools["model"]:
            _update(step="download-model", message="Downloading Whisper model…")
            _download_model()

        tools = check_tools()
        if not tools["ready"]:
            missing = []
            if not tools["ffmpeg"]:
                missing.append("ffmpeg")
            if not tools["ffprobe"]:
                missing.append("ffprobe")
            if not tools["whisper_cli"]:
                missing.append("whisper-cli")
            if not tools["model"]:
                missing.append("model")
            if tools["missing_scripts"]:
                missing.append("scripts")
            raise RuntimeError("Toolchain still incomplete: " + ", ".join(missing) + ". See the log.")

        config.update_config(toolchain_ready=True)
        _log("=== Toolchain ready ===")
        _update(
            running=False,
            step="done",
            message="Toolchain ready",
            error=None,
            pid=None,
            downloaded=tools.get("model_size") or 0,
            total=tools.get("model_size") or 0,
        )
    except Exception as exc:  # noqa: BLE001 - the UI must always get a readable error
        message = str(exc)
        _log(f"ERROR: {message}")
        _update(running=False, step="error", message="Install failed", error=message, pid=None)


def start_install() -> bool:
    """Start the install thread if tools are missing. Returns True if started."""
    global _THREAD
    with _LOCK:
        state = read_state()
        if state.get("running") and _THREAD is not None and _THREAD.is_alive():
            return False
        tools = check_tools()
        if tools["ready"]:
            config.update_config(toolchain_ready=True)
            _update(running=False, step="done", message="Toolchain ready", error=None, pid=None)
            return False
        config.ensure_app_dirs()
        _update(
            running=True,
            pid=os.getpid(),
            step="starting",
            message="Starting toolchain installation…",
            error=None,
            downloaded=0,
            total=0,
        )
        _THREAD = threading.Thread(target=_install_worker, name="toolchain-install", daemon=True)
        _THREAD.start()
        return True


def recheck() -> dict[str, Any]:
    tools = check_tools()
    if tools["ready"]:
        config.update_config(toolchain_ready=True)
        _update(running=False, step="done", message="Toolchain ready", error=None, pid=None)
    return status()
