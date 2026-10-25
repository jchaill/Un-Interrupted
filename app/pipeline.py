from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from . import config, db, feeds
from .downloader import DownloadError, download_file
from .engine import adapter, llm
from .engine.adapter import EngineError, EngineMissing, probe_duration
from .paths import episode_basename, extension_from_url
from .review import build_review


class PipelineError(RuntimeError):
    pass


class JobRetry(PipelineError):
    pass


class JobFatal(PipelineError):
    def __init__(self, message: str, *, episode_status: str | None = None):
        super().__init__(message)
        self.episode_status = episode_status


class JobQAFailed(JobFatal):
    pass


def _json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def episode_tag(episode: dict[str, Any]) -> str:
    return f"ep{episode['id']}"


def original_extension(episode: dict[str, Any]) -> str:
    if episode.get("original_path"):
        ext = Path(episode["original_path"]).suffix.lower()
        if ext:
            return ext
    return extension_from_url(episode.get("original_url") or "", episode.get("enclosure_type"))


def original_path(cfg: dict[str, Any], episode: dict[str, Any]) -> Path | None:
    rel = episode.get("original_path")
    if rel:
        return config.abs_from_storage(cfg, rel)
    return None


def clean_filename_for(episode: dict[str, Any]) -> str:
    if episode.get("clean_filename"):
        return episode["clean_filename"]
    ext = original_extension(episode)
    return (
        episode_basename(
            episode.get("title") or "episode",
            episode.get("guid") or str(episode["id"]),
            episode.get("published_at"),
        )
        + " (no ads)"
        + ext
    )


def _dirs(cfg: dict[str, Any], podcast: dict[str, Any]) -> dict[str, Path]:
    return config.ensure_episode_dirs(cfg, podcast["slug"])


def _work_paths(cfg: dict[str, Any], podcast: dict[str, Any], episode: dict[str, Any]) -> dict[str, Path]:
    dirs = _dirs(cfg, podcast)
    tag = episode_tag(episode)
    ext = original_extension(episode)
    return {
        **dirs,
        "transcript": dirs["work"] / f"tr_{tag}.json",
        "cuts": dirs["work"] / f"cuts_{tag}.json",
        "clean": dirs["work"] / f"clean_{tag}{ext}",
        "final": dirs["episodes"] / clean_filename_for(episode),
    }


def job_log_path(cfg: dict[str, Any], job: dict[str, Any], podcast: dict[str, Any] | None) -> Path:
    if podcast:
        base = config.episode_dirs(cfg, podcast["slug"])["logs"]
    else:
        base = config.logs_dir() / "jobs"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{job['type']}_{job['id']}.log"


def _write_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", errors="replace") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")
        fh.flush()


def _progress_cb(job_id: int):
    last = [0.0]

    def cb(done: int, total: int | None) -> None:
        now = time.time()
        if now - last[0] >= 0.7:
            db.update_job(job_id, progress=done, progress_total=total or 0)
            last[0] = now

    return cb


def _require_podcast(podcast_id: int | None) -> dict[str, Any]:
    if not podcast_id:
        raise JobFatal("Job has no podcast")
    podcast = db.get_podcast(podcast_id)
    if not podcast:
        raise JobFatal("Podcast no longer exists")
    return podcast


def _require_episode(episode_id: int | None) -> dict[str, Any]:
    if not episode_id:
        raise JobFatal("Job has no episode")
    episode = db.get_episode(episode_id)
    if not episode:
        raise JobFatal("Episode no longer exists")
    return episode


def _set_status(episode_id: int, status: str, error: str | None = None) -> None:
    db.update_episode(episode_id, status=status, error_message=error)


# ---------------------------------------------------------------------------
# Handlers


def handle_poll(job: dict[str, Any], log: Path) -> None:
    podcast = _require_podcast(job.get("podcast_id"))
    _write_log(log, f"Polling {podcast['title']} <{podcast['feed_url']}>")
    try:
        result = feeds.poll_podcast(podcast["id"])
    except feeds.FeedError as exc:
        db.update_podcast(podcast["id"], last_error=str(exc)[:1000], last_polled_at=db.now_iso())
        raise JobRetry(str(exc)) from exc
    _write_log(
        log,
        f"Poll result: {result['status']}, new={result['new']}, "
        f"updated={result['updated']}, enqueued={result.get('enqueued', 0)}",
    )


def handle_download(job: dict[str, Any], log: Path) -> None:
    episode = _require_episode(job.get("episode_id"))
    podcast = _require_podcast(episode["podcast_id"])
    cfg = config.load_config()
    dirs = _dirs(cfg, podcast)

    existing = original_path(cfg, episode)
    if existing and existing.is_file() and existing.stat().st_size > 0:
        _write_log(log, f"Original already present: {existing}")
        _after_download(cfg, podcast, episode, existing, log)
        return

    if not episode.get("original_url"):
        raise JobFatal("Episode has no audio enclosure", episode_status="failed")

    ext = original_extension(episode)
    name = episode_basename(episode["title"], episode["guid"], episode.get("published_at")) + ext
    dest = dirs["originals"] / name
    _set_status(episode["id"], "downloading")
    _write_log(
        log,
        f"Downloading {episode['original_url']}\n  -> {dest}\n"
        f"  expected bytes: {episode.get('enclosure_length') or 'unknown'}",
    )
    try:
        result = download_file(
            episode["original_url"],
            dest,
            expected_size=episode.get("enclosure_length"),
            progress=_progress_cb(job["id"]),
        )
    except DownloadError as exc:
        message = str(exc)
        if "Not enough disk space" in message:
            raise JobFatal(message, episode_status="failed") from exc
        raise JobRetry(message) from exc

    _write_log(log, f"Downloaded {result['size']} bytes (skipped={result['skipped']})")
    _after_download(cfg, podcast, episode, dest, log)


def _after_download(
    cfg: dict[str, Any],
    podcast: dict[str, Any],
    episode: dict[str, Any],
    path: Path,
    log: Path,
) -> None:
    duration = probe_duration(path)
    db.update_episode(
        episode["id"],
        original_path=config.rel_to_storage(cfg, path),
        enclosure_type=episode.get("enclosure_type") or "audio/mpeg",
        duration_original=duration or episode.get("duration_original"),
        downloaded_at=db.now_iso(),
        selected=1,
        status="downloaded",
        error_message=None,
    )
    _write_log(log, f"Probed duration: {duration}")
    db.enqueue_job(
        "transcribe",
        podcast_id=podcast["id"],
        episode_id=episode["id"],
        priority=110,
        max_attempts=2,
    )


def handle_transcribe(job: dict[str, Any], log: Path) -> None:
    episode = _require_episode(job.get("episode_id"))
    podcast = _require_podcast(episode["podcast_id"])
    cfg = config.load_config()
    dirs = _dirs(cfg, podcast)
    paths = _work_paths(cfg, podcast, episode)
    source = original_path(cfg, episode)
    if not source or not source.is_file():
        db.enqueue_job(
            "download",
            podcast_id=podcast["id"],
            episode_id=episode["id"],
            priority=100,
        )
        raise JobFatal("Original audio is missing; download re-queued", episode_status="queued")

    transcript = paths["transcript"]
    if transcript.is_file():
        try:
            payload = json.loads(transcript.read_text(encoding="utf-8"))
            if payload.get("transcription"):
                _write_log(log, f"Reusing existing transcript {transcript}")
                db.update_episode(episode["id"], status="detecting", error_message=None)
                db.enqueue_job(
                    "detect",
                    podcast_id=podcast["id"],
                    episode_id=episode["id"],
                    priority=90,
                )
                return
        except (OSError, json.JSONDecodeError):
            pass

    _set_status(episode["id"], "transcribing")
    _write_log(log, f"Transcribing {source} with language={podcast.get('language') or 'auto'}")
    try:
        adapter.transcribe(
            podcast=podcast,
            podcast_dir=dirs["base"],
            original=source,
            tag=episode_tag(episode),
            log_path=log,
        )
    except EngineMissing as exc:
        raise JobFatal(str(exc), episode_status="failed") from exc
    except EngineError as exc:
        raise JobRetry(str(exc)) from exc

    duration = probe_duration(source)
    db.update_episode(
        episode["id"],
        duration_original=duration or episode.get("duration_original"),
        status="detecting",
        error_message=None,
    )
    db.enqueue_job(
        "detect",
        podcast_id=podcast["id"],
        episode_id=episode["id"],
        priority=90,
    )


def handle_detect(job: dict[str, Any], log: Path) -> None:
    episode = _require_episode(job.get("episode_id"))
    podcast = _require_podcast(episode["podcast_id"])
    cfg = config.load_config()
    dirs = _dirs(cfg, podcast)
    paths = _work_paths(cfg, podcast, episode)
    source = original_path(cfg, episode)
    if not source or not source.is_file():
        raise JobFatal("Original audio is missing", episode_status="failed")
    if not paths["transcript"].is_file():
        raise JobFatal("Transcript is missing", episode_status="downloaded")

    _set_status(episode["id"], "detecting")
    total = episode.get("duration_original") or probe_duration(source)
    try:
        labels_path = adapter.write_labels(dirs["base"], podcast)
    except EngineError as exc:
        raise JobFatal(str(exc), episode_status="failed") from exc
    try:
        cuts, output = adapter.detect(
            podcast=podcast,
            podcast_dir=dirs["base"],
            original=source,
            transcript=paths["transcript"],
            cuts_path=paths["cuts"],
            labels_path=labels_path,
            log_path=log,
        )
    except EngineMissing as exc:
        raise JobFatal(str(exc), episode_status="failed") from exc
    except EngineError as exc:
        raise JobRetry(str(exc)) from exc

    _write_log(log, "\n--- detect report ---\n" + output[-12000:])

    llm_result: dict[str, Any] | None = None
    if llm.is_enabled(cfg):
        opts = llm.settings(cfg)
        _write_log(
            log,
            f"\n--- LLM ad detection ({opts['model']} via {llm.endpoint(opts['base_url'])}) ---",
        )
        try:
            llm_result = llm.analyze(
                podcast=podcast,
                transcript=paths["transcript"],
                total_duration=total,
                cfg=cfg,
                known_cuts=[
                    {"start": row[0], "end": row[1]}
                    for row in cuts
                    if isinstance(row, (list, tuple)) and len(row) >= 2
                ],
            )
            before = len(cuts)
            cuts = llm.merge_cuts(cuts, llm_result.get("ads") or [], total_duration=total)
            _write_log(
                log,
                f"LLM proposed {len(llm_result.get('ads') or [])} ad(s) across "
                f"{llm_result.get('chunks', 0)} chunk(s); "
                f"{len(llm_result.get('rejected') or [])} rejected; "
                f"cut list {before} -> {len(cuts)}.",
            )
        except Exception as exc:  # The heuristic detector remains authoritative.
            llm_result = llm.failed_result(str(exc), cfg)
            _write_log(log, f"LLM detection failed; continuing with heuristic cuts: {exc}")
    else:
        _write_log(log, "LLM ad detection is disabled; using heuristic cuts only.")

    review = build_review(
        paths["transcript"],
        cuts,
        labels_json=podcast.get("labels_json"),
        total_duration=total,
        llm_result=llm_result,
    )
    db.update_episode(
        episode["id"],
        cuts_json=json.dumps(cuts, ensure_ascii=False),
        approved_cuts_json=None,
        review_json=json.dumps(review, ensure_ascii=False),
        llm_json=json.dumps(llm_result, ensure_ascii=False) if llm_result else None,
        confidence=review["confidence"],
        engine_version=adapter.engine_version(),
        status="awaiting_review",
        error_message=None,
    )
    _write_log(
        log,
        f"Proposed {len(cuts)} cut(s), confidence {review['confidence']:.2f}, "
        f"removed {review['removed_seconds']:.1f}s "
        f"({review['ratio'] * 100:.2f}%).",
    )

    if _maybe_auto_publish(podcast, episode["id"], cuts, review, log):
        return


def _maybe_auto_publish(
    podcast: dict[str, Any],
    episode_id: int,
    cuts: list[list[float | None]],
    review: dict[str, Any],
    log: Path,
) -> bool:
    if not podcast.get("auto_publish") or not cuts:
        return False
    if review.get("warnings") or float(review.get("confidence") or 0) < 0.80:
        return False
    if review.get("llm_only"):
        _write_log(log, "Auto-publish skipped: the cut list contains LLM-only ranges.")
        return False
    if db.published_count(podcast["id"]) < 3:
        _write_log(log, "Auto-publish skipped: fewer than 3 published episodes.")
        return False
    db.update_episode(
        episode_id,
        approved_cuts_json=json.dumps(cuts, ensure_ascii=False),
        status="approved",
        error_message=None,
    )
    db.enqueue_job("cut", podcast_id=podcast["id"], episode_id=episode_id, priority=85)
    _write_log(log, "Auto-publish approved this episode.")
    return True


def _normalized_cuts(episode: dict[str, Any]) -> list[list[float | None]]:
    raw = _json(episode.get("approved_cuts_json") or episode.get("cuts_json"), [])
    cuts: list[list[float | None]] = []
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            start = max(0.0, float(row[0]))
            end = float(row[1]) if row[1] is not None else None
        except (TypeError, ValueError):
            continue
        if end is not None and end <= start:
            continue
        cuts.append([round(start, 2), round(end, 2) if end is not None else None])
    cuts.sort(key=lambda c: c[0])
    return cuts


def handle_cut(job: dict[str, Any], log: Path) -> None:
    episode = _require_episode(job.get("episode_id"))
    podcast = _require_podcast(episode["podcast_id"])
    cfg = config.load_config()
    dirs = _dirs(cfg, podcast)
    paths = _work_paths(cfg, podcast, episode)
    source = original_path(cfg, episode)
    if not source or not source.is_file():
        raise JobFatal("Original audio is missing", episode_status="failed")
    cuts = _normalized_cuts(episode)
    if not cuts:
        raise JobFatal("No approved cut ranges", episode_status="awaiting_review")

    paths["cuts"].write_text(json.dumps(cuts, ensure_ascii=False), encoding="utf-8")
    try:
        adapter.write_labels(dirs["base"], podcast)
    except EngineError as exc:
        raise JobFatal(str(exc), episode_status="awaiting_review") from exc
    _set_status(episode["id"], "cutting")
    _write_log(log, f"Cutting {source} with {cuts}")
    try:
        adapter.cut(
            podcast_dir=dirs["base"],
            original=source,
            out_path=paths["clean"],
            cuts_path=paths["cuts"],
            log_path=log,
            transcript=paths["transcript"],
        )
    except EngineMissing as exc:
        raise JobFatal(str(exc), episode_status="failed") from exc
    except EngineError as exc:
        raise JobFatal(str(exc), episode_status="failed") from exc

    duration = probe_duration(paths["clean"])
    original_duration = episode.get("duration_original") or probe_duration(source)
    expected = None
    if original_duration:
        removed = 0.0
        for start, end in cuts:
            removed += (end if end is not None else original_duration) - start
        expected = max(0.0, original_duration - removed)
        if duration is not None and abs(duration - expected) > 1.5:
            raise JobFatal(
                f"Cut duration {duration:.2f}s differs from expected {expected:.2f}s",
                episode_status="qa_failed",
            )
    db.update_episode(
        episode["id"],
        clean_duration=duration,
        clean_size=paths["clean"].stat().st_size,
        clean_filename=clean_filename_for(episode),
        status="qa",
        error_message=None,
    )
    db.enqueue_job("qa", podcast_id=podcast["id"], episode_id=episode["id"], priority=88)


def handle_qa(job: dict[str, Any], log: Path) -> None:
    episode = _require_episode(job.get("episode_id"))
    podcast = _require_podcast(episode["podcast_id"])
    cfg = config.load_config()
    dirs = _dirs(cfg, podcast)
    paths = _work_paths(cfg, podcast, episode)
    clean = paths["clean"]
    if not clean.is_file():
        raise JobFatal("Clean audio file is missing; re-cut the episode", episode_status="approved")

    cuts = _normalized_cuts(episode)
    paths["cuts"].write_text(json.dumps(cuts, ensure_ascii=False), encoding="utf-8")
    try:
        adapter.write_labels(dirs["base"], podcast)
    except EngineError as exc:
        raise JobFatal(str(exc), episode_status="awaiting_review") from exc
    _set_status(episode["id"], "qa")

    original_duration = episode.get("duration_original")
    removed = 0.0
    if original_duration:
        for start, end in cuts:
            removed += (end if end is not None else original_duration) - start
    expected = max(0.0, original_duration - removed) if original_duration else None

    try:
        ok, report = adapter.qa(
            podcast=podcast,
            podcast_dir=dirs["base"],
            clean=clean,
            cuts_path=paths["cuts"],
            log_path=log,
            expected_duration=expected,
            source_title=episode.get("title"),
        )
    except EngineMissing as exc:
        raise JobFatal(str(exc), episode_status="failed") from exc
    except EngineError as exc:
        raise JobFatal(str(exc), episode_status="qa_failed") from exc

    _write_log(log, "\n--- QA report ---\n" + "\n".join(report[-400:]))
    if not ok:
        failure_lines = [line for line in report if line.startswith("FAIL") or "leaked" in line]
        message = "QA failed: " + ("; ".join(failure_lines[:4]) or "see job log")
        raise JobQAFailed(message, episode_status="qa_failed")

    db.update_episode(episode["id"], status="ready_publish", error_message=None)
    db.enqueue_job("publish", podcast_id=podcast["id"], episode_id=episode["id"], priority=80)


def handle_publish(job: dict[str, Any], log: Path) -> None:
    episode = _require_episode(job.get("episode_id"))
    podcast = _require_podcast(episode["podcast_id"])
    cfg = config.load_config()
    dirs = _dirs(cfg, podcast)
    paths = _work_paths(cfg, podcast, episode)
    clean = paths["clean"]
    if not clean.is_file():
        # Already published (idempotent retry).
        final = dirs["episodes"] / clean_filename_for(episode)
        if final.is_file():
            db.update_episode(
                episode["id"],
                clean_filename=final.name,
                clean_guid=_clean_guid(podcast, episode),
                status="published",
                clean_published_at=episode.get("clean_published_at") or db.now_iso(),
                error_message=None,
            )
            return
        raise JobFatal("Clean audio file is missing; re-cut the episode", episode_status="approved")

    _set_status(episode["id"], "publishing")
    dest = dirs["episodes"] / clean_filename_for(episode)
    if dest.exists():
        stem = dest.stem
        ext = dest.suffix
        for i in range(2, 1000):
            candidate = dest.with_name(f"{stem}-{i}{ext}")
            if not candidate.exists():
                dest = candidate
                break
    os.replace(clean, dest)
    db.update_episode(
        episode["id"],
        clean_filename=dest.name,
        clean_size=dest.stat().st_size,
        clean_duration=probe_duration(dest) or episode.get("clean_duration"),
        clean_guid=_clean_guid(podcast, episode),
        status="published",
        clean_published_at=db.now_iso(),
        error_message=None,
    )
    _write_log(log, f"Published {dest}")
    db.enqueue_job("cleanup", priority=200)


def _clean_guid(podcast: dict[str, Any], episode: dict[str, Any]) -> str:
    return f"uninterupted:{podcast['slug']}:{episode['guid']}:clean"


def handle_cleanup(job: dict[str, Any], log: Path) -> None:
    cfg = config.load_config()
    removed = 0
    for podcast in db.list_podcasts():
        retention = podcast.get("retention_days")
        if retention is None:
            continue
        try:
            days = int(retention)
        except (TypeError, ValueError):
            continue
        cutoff = db.iso_after(-days * 86400)
        for episode in db.list_episodes(podcast["id"], status="published", limit=1000):
            published = episode.get("clean_published_at") or episode.get("updated_at")
            if not published or published > cutoff:
                continue
            if not episode.get("original_path"):
                continue
            path = config.abs_from_storage(cfg, episode["original_path"])
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
                    _write_log(log, f"Deleted original after {days} days: {path}")
                db.update_episode(episode["id"], original_path=None)
            except OSError as exc:
                _write_log(log, f"Could not delete {path}: {exc}")
    db.set_setting("last_cleanup_at", db.now_iso())
    _write_log(log, f"Cleanup removed {removed} original file(s).")


def handle_relocate(job: dict[str, Any], log: Path) -> None:
    payload = _json(job.get("payload_json"), {})
    old_root = Path(payload.get("old_root") or config.storage_root()).resolve()
    new_root = Path(payload.get("new_root") or "").expanduser().resolve()
    delete_old = bool(payload.get("delete_old"))
    if not str(new_root):
        raise JobFatal("No destination storage folder given")

    current = config.storage_root().resolve()
    if current != old_root:
        raise JobFatal("Storage root changed since this job was queued; please retry from Settings.")
    old_podcasts = old_root / "podcasts"
    new_podcasts = new_root / "podcasts"

    if new_root == old_root:
        raise JobFatal("New storage folder is the same as the current one")
    if new_root.is_relative_to(old_root) or old_root.is_relative_to(new_root):
        raise JobFatal("The new storage folder overlaps the current library folder")

    if new_root.exists():
        extras = [p for p in new_root.iterdir() if p.name not in {".DS_Store", ".relocate-in-progress"}]
        if extras:
            raise JobFatal("The new storage folder must be empty")

    marker = new_root / ".relocate-in-progress"
    if new_podcasts.exists():
        if marker.exists():
            shutil.rmtree(new_podcasts, ignore_errors=True)
        else:
            raise JobFatal("The destination already contains a podcasts folder")
    new_root.mkdir(parents=True, exist_ok=True)
    marker.write_text(db.now_iso(), encoding="utf-8")

    files = 0
    total_bytes = 0
    if old_podcasts.exists():
        for root, _dirs, names in os.walk(old_podcasts):
            for name in names:
                files += 1
                try:
                    total_bytes += (Path(root) / name).stat().st_size
                except OSError:
                    pass
    _write_log(log, f"Moving {files} files ({total_bytes} bytes) to {new_root}")
    db.update_job(job["id"], progress_total=total_bytes)
    copied = 0
    last_update = 0.0
    for root, dirnames, names in os.walk(old_podcasts):
        rel = Path(root).relative_to(old_podcasts)
        dest_dir = new_podcasts / rel
        dest_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            src = Path(root) / name
            dst = dest_dir / name
            shutil.copy2(src, dst)
            try:
                copied += src.stat().st_size
            except OSError:
                pass
            now = time.time()
            if now - last_update > 0.7:
                db.update_job(job["id"], progress=copied)
                last_update = now

    if total_bytes and copied < total_bytes:
        raise JobFatal("Copy incomplete; the current storage folder was not changed")

    config.update_config(storage_root=str(new_root))
    config.ensure_storage_dirs()
    marker.unlink(missing_ok=True)
    _write_log(log, f"Storage root switched to {new_root}")
    if delete_old and old_podcasts.exists():
        shutil.rmtree(old_podcasts, ignore_errors=True)
        _write_log(log, f"Deleted old library folder {old_podcasts}")


HANDLERS = {
    "poll": handle_poll,
    "download": handle_download,
    "transcribe": handle_transcribe,
    "detect": handle_detect,
    "cut": handle_cut,
    "qa": handle_qa,
    "publish": handle_publish,
    "cleanup": handle_cleanup,
    "relocate": handle_relocate,
}


def execute_job(job: dict[str, Any]) -> None:
    handler = HANDLERS.get(job["type"])
    if not handler:
        raise JobFatal(f"Unknown job type: {job['type']}")
    cfg = config.load_config()
    podcast = db.get_podcast(job["podcast_id"]) if job.get("podcast_id") else None
    log = job_log_path(cfg, job, podcast)
    db.update_job(job["id"], log_path=str(log))
    _write_log(
        log,
        f"\n### job {job['id']} type={job['type']} attempt={job.get('attempts')} "
        f"podcast={podcast['slug'] if podcast else '-'}",
    )
    handler(job, log)
