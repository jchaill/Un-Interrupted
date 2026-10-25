from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator

from . import config
from .paths import unique_slug

SCHEMA_VERSION = 1
_WRITE_LOCK = threading.RLock()

EPISODE_STATUSES = (
    "discovered",
    "queued",
    "downloading",
    "downloaded",
    "transcribing",
    "detecting",
    "awaiting_review",
    "approved",
    "cutting",
    "qa",
    "qa_failed",
    "ready_publish",
    "publishing",
    "published",
    "failed",
    "skipped",
)

ACTIVE_JOB_STATES = ("queued", "running")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    config.app_home().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.db_path()), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    config.ensure_app_dirs()
    with connect() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            _create_v1(conn)
            conn.execute("PRAGMA user_version = 1")
        # Future migrations go here, one block per version.
        _ensure_column(conn, "episodes", "clean_published_at", "TEXT")
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
            ("default_retention_days", str(config.load_config().get("default_retention_days", 30))),
        )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _create_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS podcasts (
            id INTEGER PRIMARY KEY,
            slug TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            feed_url TEXT UNIQUE NOT NULL,
            site_url TEXT,
            description TEXT,
            author TEXT,
            language TEXT DEFAULT 'auto',
            category TEXT,
            explicit INTEGER DEFAULT 0,
            artwork_url TEXT,
            artwork_path TEXT,
            labels_json TEXT DEFAULT '[]',
            ad_show_stop TEXT DEFAULT '',
            ad_lang TEXT DEFAULT 'auto',
            auto_process INTEGER DEFAULT 1,
            auto_publish INTEGER DEFAULT 0,
            backfill_limit INTEGER DEFAULT 3,
            poll_interval_minutes INTEGER DEFAULT 30,
            retention_days INTEGER,
            feed_token TEXT NOT NULL,
            last_polled_at TEXT,
            last_etag TEXT,
            last_modified TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY,
            podcast_id INTEGER NOT NULL REFERENCES podcasts(id) ON DELETE CASCADE,
            guid TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            content_html TEXT,
            link TEXT,
            comments_url TEXT,
            published_at TEXT,
            original_url TEXT NOT NULL,
            enclosure_type TEXT,
            enclosure_length INTEGER,
            duration_original REAL,
            item_json TEXT,
            status TEXT NOT NULL DEFAULT 'discovered',
            selected INTEGER DEFAULT 0,
            original_path TEXT,
            clean_filename TEXT,
            clean_duration REAL,
            clean_size INTEGER,
            clean_guid TEXT,
            confidence REAL,
            cuts_json TEXT,
            approved_cuts_json TEXT,
            review_json TEXT,
            engine_version TEXT,
            error_message TEXT,
            discovered_at TEXT NOT NULL,
            downloaded_at TEXT,
            clean_published_at TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(podcast_id, guid)
        );

        CREATE INDEX IF NOT EXISTS idx_episodes_podcast_status
            ON episodes(podcast_id, status);
        CREATE INDEX IF NOT EXISTS idx_episodes_podcast_published
            ON episodes(podcast_id, published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_episodes_guid
            ON episodes(guid);

        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY,
            type TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',
            podcast_id INTEGER REFERENCES podcasts(id) ON DELETE CASCADE,
            episode_id INTEGER REFERENCES episodes(id) ON DELETE CASCADE,
            payload_json TEXT,
            priority INTEGER DEFAULT 100,
            attempts INTEGER DEFAULT 0,
            max_attempts INTEGER DEFAULT 3,
            run_after TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            error TEXT,
            log_path TEXT,
            progress REAL DEFAULT 0,
            progress_total REAL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_jobs_state_run_after
            ON jobs(state, run_after, priority, id);
        CREATE INDEX IF NOT EXISTS idx_jobs_episode
            ON jobs(episode_id, type, state);

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )


# ---------------------------------------------------------------------------
# Settings


def get_setting(key: str, default: str | None = None) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: Any) -> None:
    with _WRITE_LOCK, connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, "" if value is None else str(value)),
        )


# ---------------------------------------------------------------------------
# Podcasts


def get_podcast(podcast_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM podcasts WHERE id=?", (podcast_id,)).fetchone())


def get_podcast_by_slug(slug: str) -> dict[str, Any] | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM podcasts WHERE slug=?", (slug,)).fetchone())


def get_podcast_by_feed_url(feed_url: str) -> dict[str, Any] | None:
    with connect() as conn:
        return _row(
            conn.execute("SELECT * FROM podcasts WHERE feed_url=?", (feed_url,)).fetchone()
        )


def list_podcasts() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id=p.id) AS episode_count,
                   (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id=p.id
                      AND e.status='published') AS published_count,
                   (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id=p.id
                      AND e.status='awaiting_review') AS review_count,
                   (SELECT COUNT(*) FROM episodes e WHERE e.podcast_id=p.id
                      AND e.status='failed') AS failed_count
            FROM podcasts p
            ORDER BY p.title COLLATE NOCASE
            """
        ).fetchall()
    return _rows(rows)


def create_podcast(fields: dict[str, Any]) -> dict[str, Any]:
    with _WRITE_LOCK, connect() as conn:
        def exists(slug: str) -> bool:
            return (
                conn.execute("SELECT 1 FROM podcasts WHERE slug=?", (slug,)).fetchone()
                is not None
            )

        slug = fields.get("slug") or unique_slug(fields["title"], exists)
        now = now_iso()
        cur = conn.execute(
            """
            INSERT INTO podcasts(
                slug, title, feed_url, site_url, description, author, language,
                category, explicit, artwork_url, artwork_path, labels_json,
                ad_show_stop, ad_lang, auto_process, auto_publish, backfill_limit,
                poll_interval_minutes, retention_days, feed_token, last_polled_at,
                last_etag, last_modified, last_error, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                slug,
                fields["title"],
                fields["feed_url"],
                fields.get("site_url"),
                fields.get("description"),
                fields.get("author"),
                fields.get("language") or "auto",
                fields.get("category"),
                1 if fields.get("explicit") else 0,
                fields.get("artwork_url"),
                fields.get("artwork_path"),
                fields.get("labels_json") or "[]",
                fields.get("ad_show_stop") or "",
                fields.get("ad_lang") or "auto",
                1 if fields.get("auto_process", True) else 0,
                1 if fields.get("auto_publish", False) else 0,
                int(fields.get("backfill_limit", 3) or 0),
                int(fields.get("poll_interval_minutes", 30) or 30),
                fields.get("retention_days"),
                fields["feed_token"],
                fields.get("last_polled_at") or now,
                fields.get("last_etag"),
                fields.get("last_modified"),
                None,
                now,
                now,
            ),
        )
        podcast_id = int(cur.lastrowid)
    return get_podcast(podcast_id)  # type: ignore[return-value]


PODCAST_EDITABLE = {
    "title",
    "site_url",
    "description",
    "author",
    "language",
    "category",
    "explicit",
    "artwork_url",
    "artwork_path",
    "labels_json",
    "ad_show_stop",
    "ad_lang",
    "auto_process",
    "auto_publish",
    "backfill_limit",
    "poll_interval_minutes",
    "retention_days",
    "last_polled_at",
    "last_etag",
    "last_modified",
    "last_error",
    # feed_url and slug are intentionally immutable.
}


def update_podcast(podcast_id: int, **fields: Any) -> dict[str, Any] | None:
    updates = {k: v for k, v in fields.items() if k in PODCAST_EDITABLE}
    if not updates:
        return get_podcast(podcast_id)
    updates["updated_at"] = now_iso()
    assignments = ", ".join(f"{key}=?" for key in updates)
    values = list(updates.values()) + [podcast_id]
    with _WRITE_LOCK, connect() as conn:
        conn.execute(f"UPDATE podcasts SET {assignments} WHERE id=?", values)
    return get_podcast(podcast_id)


def delete_podcast(podcast_id: int) -> None:
    with _WRITE_LOCK, connect() as conn:
        conn.execute("DELETE FROM podcasts WHERE id=?", (podcast_id,))


# ---------------------------------------------------------------------------
# Episodes


def get_episode(episode_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone())


def get_episode_by_guid(podcast_id: int, guid: str) -> dict[str, Any] | None:
    with connect() as conn:
        return _row(
            conn.execute(
                "SELECT * FROM episodes WHERE podcast_id=? AND guid=?",
                (podcast_id, guid),
            ).fetchone()
        )


def list_episodes(
    podcast_id: int,
    *,
    status: str | None = None,
    statuses: tuple[str, ...] | None = None,
    selected_only: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where = ["podcast_id=?"]
    params: list[Any] = [podcast_id]
    if statuses:
        where.append("status IN (" + ",".join("?" for _ in statuses) + ")")
        params.extend(statuses)
    elif status:
        where.append("status=?")
        params.append(status)
    if selected_only:
        where.append("selected=1")
    params.extend([limit, offset])
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM episodes
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(published_at, discovered_at) DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
    return _rows(rows)


def count_episodes(
    podcast_id: int,
    *,
    status: str | None = None,
    statuses: tuple[str, ...] | None = None,
    selected_only: bool = False,
) -> int:
    where = ["podcast_id=?"]
    params: list[Any] = [podcast_id]
    if statuses:
        where.append("status IN (" + ",".join("?" for _ in statuses) + ")")
        params.extend(statuses)
    elif status:
        where.append("status=?")
        params.append(status)
    if selected_only:
        where.append("selected=1")
    with connect() as conn:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM episodes WHERE {' AND '.join(where)}", params
            ).fetchone()[0]
        )


def episode_status_counts(podcast_id: int | None = None) -> dict[str, int]:
    with connect() as conn:
        if podcast_id is None:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM episodes GROUP BY status"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM episodes WHERE podcast_id=? GROUP BY status",
                (podcast_id,),
            ).fetchall()
    return {r["status"]: int(r["n"]) for r in rows}


def upsert_feed_episode(podcast_id: int, item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Insert/update an item discovered in a feed.

    Local columns (status, selected, paths, cuts, ...) are never overwritten.
    """
    now = now_iso()
    with _WRITE_LOCK, connect() as conn:
        existing = conn.execute(
            "SELECT * FROM episodes WHERE podcast_id=? AND guid=?",
            (podcast_id, item["guid"]),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE episodes SET
                    title=?, description=?, content_html=?, link=?, comments_url=?,
                    published_at=?, original_url=?, enclosure_type=?,
                    enclosure_length=?, duration_original=COALESCE(duration_original, ?),
                    item_json=?, updated_at=?
                WHERE id=?
                """,
                (
                    item.get("title") or existing["title"],
                    item.get("description"),
                    item.get("content_html"),
                    item.get("link"),
                    item.get("comments_url"),
                    item.get("published_at") or existing["published_at"],
                    item.get("original_url") or existing["original_url"],
                    item.get("enclosure_type"),
                    item.get("enclosure_length"),
                    item.get("duration"),
                    json.dumps(item.get("item_json") or {}, ensure_ascii=False),
                    now,
                    existing["id"],
                ),
            )
            return get_episode(int(existing["id"])), False  # type: ignore[return-value]

        cur = conn.execute(
            """
            INSERT INTO episodes(
                podcast_id, guid, title, description, content_html, link,
                comments_url, published_at, original_url, enclosure_type,
                enclosure_length, duration_original, item_json, status, selected,
                discovered_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                podcast_id,
                item["guid"],
                item.get("title") or "Untitled episode",
                item.get("description"),
                item.get("content_html"),
                item.get("link"),
                item.get("comments_url"),
                item.get("published_at"),
                item.get("original_url") or "",
                item.get("enclosure_type"),
                item.get("enclosure_length"),
                item.get("duration"),
                json.dumps(item.get("item_json") or {}, ensure_ascii=False),
                "discovered",
                0,
                now,
                now,
            ),
        )
        episode_id = int(cur.lastrowid)
    return get_episode(episode_id), True  # type: ignore[return-value]


EPISODE_EDITABLE = {
    "title",
    "description",
    "content_html",
    "link",
    "comments_url",
    "published_at",
    "original_url",
    "enclosure_type",
    "enclosure_length",
    "duration_original",
    "item_json",
    "status",
    "selected",
    "original_path",
    "clean_filename",
    "clean_duration",
    "clean_size",
    "clean_guid",
    "confidence",
    "cuts_json",
    "approved_cuts_json",
    "review_json",
    "engine_version",
    "error_message",
    "downloaded_at",
    "published_at",
    "clean_published_at",
}


def update_episode(episode_id: int, **fields: Any) -> dict[str, Any] | None:
    updates = {k: v for k, v in fields.items() if k in EPISODE_EDITABLE}
    if not updates:
        return get_episode(episode_id)
    updates["updated_at"] = now_iso()
    assignments = ", ".join(f"{key}=?" for key in updates)
    values = list(updates.values()) + [episode_id]
    with _WRITE_LOCK, connect() as conn:
        conn.execute(f"UPDATE episodes SET {assignments} WHERE id=?", values)
    return get_episode(episode_id)


def set_episode_status(episode_id: int, status: str, error: str | None = None) -> None:
    assert status in EPISODE_STATUSES, status
    update_episode(episode_id, status=status, error_message=error)


def select_episode(episode_id: int, status: str = "queued") -> None:
    update_episode(episode_id, selected=1, status=status, error_message=None)


def episode_feed_items(podcast_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM episodes
            WHERE podcast_id=? AND (clean_filename IS NOT NULL OR status='published')
            ORDER BY COALESCE(published_at, discovered_at) DESC
            """,
            (podcast_id,),
        ).fetchall()
    return _rows(rows)


def published_count(podcast_id: int) -> int:
    with connect() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM episodes WHERE podcast_id=? AND status='published'",
                (podcast_id,),
            ).fetchone()[0]
        )


# ---------------------------------------------------------------------------
# Jobs


def enqueue_job(
    job_type: str,
    *,
    podcast_id: int | None = None,
    episode_id: int | None = None,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
    max_attempts: int = 3,
    run_after: str | None = None,
    dedupe: bool = True,
) -> int:
    now = now_iso()
    with _WRITE_LOCK, connect() as conn:
        if dedupe:
            row = conn.execute(
                """
                SELECT id FROM jobs
                WHERE type=? AND state IN ('queued','running')
                  AND COALESCE(episode_id,-1)=COALESCE(?,-1)
                  AND COALESCE(podcast_id,-1)=COALESCE(?,-1)
                ORDER BY id LIMIT 1
                """,
                (job_type, episode_id, podcast_id),
            ).fetchone()
            if row:
                return int(row["id"])
        cur = conn.execute(
            """
            INSERT INTO jobs(type, state, podcast_id, episode_id, payload_json,
                             priority, attempts, max_attempts, run_after, created_at)
            VALUES(?, 'queued', ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                job_type,
                podcast_id,
                episode_id,
                json.dumps(payload or {}, ensure_ascii=False),
                priority,
                max_attempts,
                run_after or now,
                now,
            ),
        )
        return int(cur.lastrowid)


def get_job(job_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return _row(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def list_jobs(limit: int = 100, active_only: bool = False) -> list[dict[str, Any]]:
    where = "WHERE state IN ('queued','running')" if active_only else ""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT j.*, p.title AS podcast_title, e.title AS episode_title
            FROM jobs j
            LEFT JOIN podcasts p ON p.id=j.podcast_id
            LEFT JOIN episodes e ON e.id=j.episode_id
            {where}
            ORDER BY CASE j.state WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                     j.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return _rows(rows)


def job_counts() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state").fetchall()
    return {r["state"]: int(r["n"]) for r in rows}


def claim_job() -> dict[str, Any] | None:
    now = now_iso()
    with _WRITE_LOCK, connect() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE state='queued' AND run_after <= ?
                ORDER BY priority, id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if not row:
                conn.execute("COMMIT")
                return None
            conn.execute(
                "UPDATE jobs SET state='running', started_at=?, attempts=attempts+1 WHERE id=?",
                (now, row["id"]),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    job = dict(row)
    job["state"] = "running"
    job["started_at"] = now
    job["attempts"] = int(job.get("attempts", 0)) + 1
    return job


def update_job(job_id: int, **fields: Any) -> None:
    allowed = {
        "state",
        "error",
        "log_path",
        "progress",
        "progress_total",
        "run_after",
        "finished_at",
        "started_at",
        "attempts",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{key}=?" for key in updates)
    with _WRITE_LOCK, connect() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*updates.values(), job_id))


def finish_job(job_id: int) -> None:
    update_job(job_id, state="done", finished_at=now_iso(), error=None)


def fail_job(job_id: int, error: str, *, retry: bool = False, delay_seconds: float = 30) -> None:
    if retry:
        update_job(
            job_id,
            state="queued",
            error=error[:2000],
            run_after=iso_after(delay_seconds),
        )
    else:
        update_job(job_id, state="failed", error=error[:2000], finished_at=now_iso())


def cancel_job(job_id: int) -> bool:
    """Only queued jobs can be canceled; a running subprocess cannot be stopped
    safely from the web process, so it is left to finish."""
    with _WRITE_LOCK, connect() as conn:
        cur = conn.execute(
            "UPDATE jobs SET state='canceled', finished_at=?, error='canceled by user' "
            "WHERE id=? AND state='queued'",
            (now_iso(), job_id),
        )
        return cur.rowcount > 0


def retry_job(job_id: int) -> bool:
    with _WRITE_LOCK, connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["state"] not in ("failed", "canceled"):
            return False
        conn.execute(
            "UPDATE jobs SET state='queued', error=NULL, run_after=?, finished_at=NULL, "
            "attempts=0 WHERE id=?",
            (now_iso(), job_id),
        )
        return True


def reset_stale_jobs() -> int:
    """Jobs left 'running' by a crashed process are safe to retry."""
    with _WRITE_LOCK, connect() as conn:
        cur = conn.execute(
            "UPDATE jobs SET state='queued', error='requeued after restart', run_after=? "
            "WHERE state='running'",
            (now_iso(),),
        )
        return cur.rowcount


def latest_job_for_episode(episode_id: int, job_type: str | None = None) -> dict[str, Any] | None:
    where = "WHERE episode_id=?"
    params: list[Any] = [episode_id]
    if job_type:
        where += " AND type=?"
        params.append(job_type)
    with connect() as conn:
        row = conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY id DESC LIMIT 1", params
        ).fetchone()
    return _row(row)


def next_queued_eta() -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT MIN(run_after) FROM jobs WHERE state='queued'"
        ).fetchone()
    return row[0] if row else None


def latest_jobs_by_episode(podcast_id: int) -> dict[int, dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT j.* FROM jobs j
            JOIN (
                SELECT episode_id, MAX(id) AS mid
                FROM jobs WHERE podcast_id=? AND episode_id IS NOT NULL
                GROUP BY episode_id
            ) latest ON latest.mid=j.id
            """,
            (podcast_id,),
        ).fetchall()
    return {int(r["episode_id"]): dict(r) for r in rows}
