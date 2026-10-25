from __future__ import annotations

import json
import os
import secrets
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__

APP_NAME = "Un-Interupted"
DEFAULT_PORT = 8090
DEFAULT_STORAGE_NAME = "Un-Interupted"

_CONFIG_LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def app_home() -> Path:
    override = os.environ.get("UNINTERUPTED_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / "Library" / "Application Support" / APP_NAME


def config_path() -> Path:
    return app_home() / "config.json"


def db_path() -> Path:
    return app_home() / "db.sqlite3"


def models_dir() -> Path:
    return app_home() / "models"


def model_path() -> Path:
    override = os.environ.get("WHISPER_MODEL") or os.environ.get("UNINTERUPTED_MODEL")
    if override:
        return Path(override).expanduser().resolve()
    return models_dir() / "ggml-large-v3-turbo-q5_0.bin"


def cache_dir() -> Path:
    return app_home() / "cache"


def artwork_dir() -> Path:
    return cache_dir() / "artwork"


def logs_dir() -> Path:
    return app_home() / "logs"


def setup_log_path() -> Path:
    return logs_dir() / "setup.log"


def setup_state_path() -> Path:
    return app_home() / "setup_state.json"


def default_storage_root() -> Path:
    return Path.home() / "Podcasts" / DEFAULT_STORAGE_NAME


def lan_ip() -> str:
    """Best-effort LAN address; never contacts the network (UDP connect only)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def default_base_url(port: int = DEFAULT_PORT) -> str:
    return f"http://{lan_ip()}:{port}"


def default_config() -> dict[str, Any]:
    return {
        "version": 1,
        "app_version": __version__,
        "setup_complete": False,
        "storage_root": str(default_storage_root()),
        "base_url": default_base_url(),
        "password_hash": "",
        "secret_key": secrets.token_urlsafe(48),
        "port": DEFAULT_PORT,
        "min_free_gb": 5,
        "default_retention_days": 30,
        "toolchain_ready": False,
        # Semantic ad detection through Ollama. Always runs on every detect
        # job; the model/endpoint are configurable, the pass is not optional.
        "llm_enabled": True,
        "llm_model": "deepseek-v4.1-flash:cloud",
        "llm_base_url": "http://127.0.0.1:11434",
        "llm_api_key": "",
        "llm_min_confidence": 0.72,
        "llm_max_chars": 24000,
        "llm_timeout": 180,
        "created_at": now_iso(),
    }


def _read_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    return data


def load_config() -> dict[str, Any]:
    """Load config, creating a default one on first run.

    A stable secret key is generated and persisted immediately so setup sessions
    survive process restarts.
    """
    with _CONFIG_LOCK:
        home = app_home()
        home.mkdir(parents=True, exist_ok=True)
        path = config_path()
        if not path.exists():
            cfg = default_config()
            save_config(cfg)
            return cfg

        try:
            cfg = _read_json(path)
        except Exception:
            # Do not silently destroy a possibly recoverable config.
            backup = path.with_suffix(f".json.bad-{int(datetime.now().timestamp())}")
            try:
                path.replace(backup)
            except OSError:
                pass
            cfg = default_config()
            save_config(cfg)
            return cfg

        defaults = default_config()
        changed = False
        for key, value in defaults.items():
            if key not in cfg:
                # Never regenerate a secret key or lose a password hash.
                cfg[key] = value
                changed = True
        # Keep the app version current, but preserve the stable secret.
        if cfg.get("app_version") != defaults["app_version"]:
            cfg["app_version"] = defaults["app_version"]
            changed = True
        if changed:
            save_config(cfg)
        return cfg


def save_config(cfg: dict[str, Any]) -> None:
    with _CONFIG_LOCK:
        home = app_home()
        home.mkdir(parents=True, exist_ok=True)
        path = config_path()
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def update_config(**values: Any) -> dict[str, Any]:
    with _CONFIG_LOCK:
        cfg = load_config()
        cfg.update(values)
        save_config(cfg)
        return cfg


def ensure_app_dirs() -> None:
    for path in (app_home(), models_dir(), artwork_dir(), logs_dir()):
        path.mkdir(parents=True, exist_ok=True)


def is_configured(cfg: dict[str, Any] | None = None) -> bool:
    cfg = cfg or load_config()
    return bool(cfg.get("setup_complete") and cfg.get("password_hash"))


def storage_root(cfg: dict[str, Any] | None = None) -> Path:
    cfg = cfg or load_config()
    return Path(cfg["storage_root"]).expanduser()


def podcasts_root(cfg: dict[str, Any] | None = None) -> Path:
    return storage_root(cfg) / "podcasts"


def ensure_storage_dirs(cfg: dict[str, Any] | None = None) -> None:
    root = podcasts_root(cfg)
    root.mkdir(parents=True, exist_ok=True)


def podcast_dir(cfg: dict[str, Any], slug: str) -> Path:
    return podcasts_root(cfg) / slug


def episode_dirs(cfg: dict[str, Any], slug: str) -> dict[str, Path]:
    base = podcast_dir(cfg, slug)
    return {
        "base": base,
        "episodes": base / "episodes",
        "originals": base / "originals",
        "work": base / "work",
        "logs": base / "work" / "logs",
    }


def ensure_episode_dirs(cfg: dict[str, Any], slug: str) -> dict[str, Path]:
    dirs = episode_dirs(cfg, slug)
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def rel_to_storage(cfg: dict[str, Any], path: Path | str) -> str:
    p = Path(path)
    try:
        return str(p.resolve().relative_to(storage_root(cfg).resolve()))
    except (ValueError, OSError):
        return str(p)


def abs_from_storage(cfg: dict[str, Any], rel: str) -> Path:
    p = Path(rel)
    if p.is_absolute():
        return p
    return storage_root(cfg) / p


def effective_port(cfg: dict[str, Any] | None = None) -> int:
    cfg = cfg or load_config()
    return int(os.environ.get("UNINTERUPTED_PORT") or cfg.get("port") or DEFAULT_PORT)
