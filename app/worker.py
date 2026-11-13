from __future__ import annotations

import fcntl
import json
import os
import signal
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any

from . import config, db
from .pipeline import JobFatal, JobQAFailed, JobRetry, execute_job


def _heartbeat(state: str, current: dict[str, Any] | None = None) -> None:
    path = config.app_home() / "worker.json"
    payload = {
        "pid": os.getpid(),
        "state": state,
        "last_seen": db.now_iso(),
        "current_job": (
            {
                "id": current.get("id"),
                "type": current.get("type"),
                "episode_id": current.get("episode_id"),
                "podcast_id": current.get("podcast_id"),
            }
            if current
            else None
        ),
    }
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def worker_status() -> dict[str, Any]:
    path = config.app_home() / "worker.json"
    if not path.is_file():
        return {"alive": False, "state": "not started", "last_seen": None, "current_job": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"alive": False, "state": "unreadable", "last_seen": None, "current_job": None}
    last = data.get("last_seen")
    alive = False
    if last:
        try:
            dt = datetime.fromisoformat(last)
            alive = (datetime.now(timezone.utc) - dt).total_seconds() < 120
        except ValueError:
            pass
    data["alive"] = alive
    return data


def _worker_log(text: str) -> None:
    path = config.logs_dir() / "worker.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", errors="replace") as fh:
        fh.write(f"[{db.now_iso()}] {text}\n")


def _due(last: str | None, interval_minutes: int) -> bool:
    if not last:
        return True
    try:
        dt = datetime.fromisoformat(last)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() >= interval_minutes * 60
    except ValueError:
        return True


class Worker:
    def __init__(self, stop_event: threading.Event | None = None):
        self.stop = stop_event or threading.Event()
        self.thread: threading.Thread | None = None
        self.last_schedule = 0.0
        self.current_job: dict[str, Any] | None = None
        self._lock_handle = None
        self._beat_stop = threading.Event()

    def start(self) -> threading.Thread:
        self.thread = threading.Thread(target=self.run, name="worker", daemon=True)
        self.thread.start()
        return self.thread

    def schedule(self) -> None:
        for podcast in db.list_podcasts():
            interval = int(podcast.get("poll_interval_minutes") or 30)
            if not _due(podcast.get("last_polled_at"), interval):
                continue
            job_type = "scan" if (podcast.get("source_kind") or "rss") == "folder" else "poll"
            db.enqueue_job(
                job_type,
                podcast_id=podcast["id"],
                priority=10,
                max_attempts=2,
                dedupe=True,
            )
        last_cleanup = db.get_setting("last_cleanup_at")
        if _due(last_cleanup, 24 * 60):
            db.enqueue_job("cleanup", priority=200, max_attempts=1)
        self.last_schedule = time.time()

    def _acquire_lock(self) -> bool:
        path = config.app_home() / "worker.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._lock_handle = handle
        return True

    def _release_lock(self) -> None:
        if self._lock_handle is not None:
            try:
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_handle.close()
                self._lock_handle = None

    def _beat_loop(self) -> None:
        while not self.stop.is_set() and not self._beat_stop.is_set():
            _heartbeat("running" if self.current_job else "idle", self.current_job)
            self._beat_stop.wait(10)

    def _finish(self, job: dict[str, Any]) -> None:
        db.finish_job(job["id"])

    def _fail(self, job: dict[str, Any], exc: Exception) -> None:
        episode_id = job.get("episode_id")
        max_attempts = int(job.get("max_attempts") or 3)
        attempts = int(job.get("attempts") or 1)
        message = str(exc) or exc.__class__.__name__
        _worker_log(
            f"job {job['id']} {job['type']} failed "
            f"(attempt {attempts}/{max_attempts}): {message}"
        )
        if isinstance(exc, JobQAFailed):
            db.fail_job(job["id"], message, retry=False)
            if episode_id:
                db.update_episode(episode_id, status="qa_failed", error_message=message[:2000])
            return
        if isinstance(exc, JobFatal):
            db.fail_job(job["id"], message, retry=False)
            if episode_id:
                status = exc.episode_status or "failed"
                db.update_episode(episode_id, status=status, error_message=message[:2000])
            return
        if isinstance(exc, JobRetry) and attempts < max_attempts:
            delay = min(300, 20 * (2 ** max(0, attempts - 1)))
            db.fail_job(job["id"], message, retry=True, delay_seconds=delay)
            if episode_id:
                db.update_episode(episode_id, error_message=message[:2000])
            return
        if not isinstance(exc, JobRetry) and attempts < max_attempts:
            # Unexpected failure: retry a couple of times before giving up.
            delay = min(300, 30 * (2 ** max(0, attempts - 1)))
            db.fail_job(job["id"], message, retry=True, delay_seconds=delay)
            if episode_id:
                db.update_episode(episode_id, error_message=message[:2000])
            return
        db.fail_job(job["id"], message, retry=False)
        if episode_id:
            db.update_episode(episode_id, status="failed", error_message=message[:2000])

    def run(self) -> None:
        db.init_db()
        if not self._acquire_lock():
            _worker_log("another worker already holds the lock; exiting")
            return
        try:
            reset = db.reset_stale_jobs()
            if reset:
                _worker_log(f"Re-queued {reset} job(s) left running by a previous process")
            _heartbeat("starting")
            beat = threading.Thread(target=self._beat_loop, name="worker-heartbeat", daemon=True)
            beat.start()
            self.schedule()
            while not self.stop.is_set():
                if time.time() - self.last_schedule > 60:
                    self.schedule()
                job = db.claim_job()
                if not job:
                    self.stop.wait(2.0)
                    continue
                self.current_job = job
                try:
                    execute_job(job)
                except Exception as exc:  # noqa: BLE001 - the worker must never die
                    if not isinstance(exc, (JobRetry, JobFatal)):
                        _worker_log(traceback.format_exc())
                    self._fail(job, exc)
                else:
                    self._finish(job)
                finally:
                    self.current_job = None
                # Keep the loop responsive to Ctrl-C / app shutdown.
                self.stop.wait(0.2)
            self._beat_stop.set()
            beat.join(timeout=2)
            _heartbeat("stopped")
            _worker_log("worker stopped")
        finally:
            self._beat_stop.set()
            self._release_lock()


def main() -> None:
    config.ensure_app_dirs()
    db.init_db()
    stop = threading.Event()

    def _signal(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, _signal)
    signal.signal(signal.SIGTERM, _signal)
    Worker(stop).run()


if __name__ == "__main__":
    main()
