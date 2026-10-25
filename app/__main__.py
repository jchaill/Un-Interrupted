from __future__ import annotations

import os
import threading

import uvicorn

from . import config, db
from .worker import Worker


def main() -> None:
    config.ensure_app_dirs()
    db.init_db()
    cfg = config.load_config()
    port = config.effective_port(cfg)
    host = os.environ.get("UNINTERUPTED_HOST", "0.0.0.0")
    stop = threading.Event()
    worker: Worker | None = None
    if os.environ.get("UNINTERUPTED_NO_WORKER", "").lower() not in {"1", "true", "yes"}:
        worker = Worker(stop)
        worker.start()
        print("[uninterupted] worker started (pid thread)", flush=True)
    print(f"[uninterupted] web UI: http://127.0.0.1:{port}/", flush=True)
    if cfg.get("setup_complete"):
        print(f"[uninterupted] base URL: {cfg.get('base_url')}", flush=True)
    else:
        print(f"[uninterupted] first run: open http://127.0.0.1:{port}/setup", flush=True)
    try:
        uvicorn.run(
            "app.web:app",
            host=host,
            port=port,
            log_level=os.environ.get("UNINTERUPTED_LOG_LEVEL", "info"),
            access_log=False,
        )
    finally:
        stop.set()
        if worker and worker.thread:
            worker.thread.join(timeout=10)


if __name__ == "__main__":
    main()
