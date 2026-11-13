from __future__ import annotations

import json
from pathlib import Path

from app import pipeline


def test_auto_publish_accepts_a_no_ad_episode(tmp_path, monkeypatch):
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        pipeline.db,
        "update_episode",
        lambda episode_id, **fields: calls.setdefault("episode", (episode_id, fields)),
    )
    monkeypatch.setattr(
        pipeline.db,
        "enqueue_job",
        lambda *args, **kwargs: calls.setdefault("job", (args, kwargs)),
    )
    review = {
        "warnings": ["No ad blocks were detected."],
        "confidence": 0.0,
        "llm": {"error": None},
        "no_ads": True,
    }
    assert pipeline._maybe_auto_publish(
        {"id": 7, "auto_publish": 1},
        42,
        [],
        review,
        tmp_path / "detect.log",
    )
    episode_id, fields = calls["episode"]  # type: ignore[misc]
    assert episode_id == 42
    assert fields["approved_cuts_json"] == "[]"
    assert fields["status"] == "approved"
    args, kwargs = calls["job"]  # type: ignore[misc]
    assert args[0] == "cut"
    assert kwargs["episode_id"] == 42


def test_auto_publish_publishes_no_ad_episode_even_when_llm_failed(tmp_path, monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(
        pipeline.db,
        "update_episode",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        pipeline.db,
        "enqueue_job",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    review = {
        "warnings": ["LLM ad detection failed: offline"],
        "confidence": 0.0,
        "llm": {"error": "offline"},
    }
    assert pipeline._maybe_auto_publish(
        {"id": 7, "auto_publish": 1},
        42,
        [],
        review,
        tmp_path / "detect.log",
    )
    assert len(calls) == 2


def test_auto_publish_ignores_confidence_warnings_and_llm_only_ranges(tmp_path, monkeypatch):
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        pipeline.db,
        "update_episode",
        lambda episode_id, **fields: calls.setdefault("episode", (episode_id, fields)),
    )
    monkeypatch.setattr(
        pipeline.db,
        "enqueue_job",
        lambda *args, **kwargs: calls.setdefault("job", (args, kwargs)),
    )
    cuts = [{"start": 10.0, "end": 40.0, "title": "Sponsor"}]
    review = {
        "warnings": ["Low confidence cut.", "Another warning."],
        "confidence": 0.12,
        "llm_only": True,
        "llm": {"error": None},
    }
    assert pipeline._maybe_auto_publish(
        {"id": 7, "auto_publish": 1},
        42,
        cuts,
        review,
        tmp_path / "detect.log",
    )
    episode_id, fields = calls["episode"]  # type: ignore[misc]
    assert episode_id == 42
    assert json.loads(fields["approved_cuts_json"]) == cuts
    assert fields["status"] == "approved"
    args, kwargs = calls["job"]  # type: ignore[misc]
    assert args[0] == "cut"
    assert kwargs["episode_id"] == 42


def test_auto_publish_does_nothing_when_disabled(tmp_path, monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(
        pipeline.db,
        "update_episode",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        pipeline.db,
        "enqueue_job",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert not pipeline._maybe_auto_publish(
        {"id": 7, "auto_publish": 0},
        42,
        [{"start": 10.0, "end": 40.0}],
        {"warnings": [], "confidence": 1.0, "llm": {"error": None}},
        tmp_path / "detect.log",
    )
    assert calls == []


def test_copy_as_clean_preserves_the_original(tmp_path):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"ID3" + b"a" * 5000)
    clean = tmp_path / "work" / "clean.mp3"
    pipeline._copy_as_clean(source, clean, tmp_path / "cut.log")
    assert clean.read_bytes() == source.read_bytes()
    assert clean.stat().st_size == source.stat().st_size


def _folder_podcast(tmp_path, *, source: Path | None):
    from app import config, db

    cfg = config.load_config()
    podcast = db.create_podcast(
        {
            "title": "Folder show",
            "feed_url": f"folder://{tmp_path}",
            "language": "fr",
            "labels_json": "[]",
            "feed_token": "folder-token",
            "source_kind": "folder",
            "source_path": str(tmp_path),
        }
    )
    episode, _ = db.upsert_feed_episode(
        podcast["id"],
        {
            "guid": "file:episode.mp3",
            "title": "Episode",
            "published_at": "2026-11-01T00:00:00+00:00",
            "original_url": (source.as_uri() if source else "file:///gone.mp3"),
            "enclosure_type": "audio/mpeg",
            "enclosure_length": 1000,
            "item_json": {"source_rel": "episode.mp3"},
        },
    )
    db.update_episode(
        episode["id"],
        status="queued",
        selected=1,
        # The row always points at a source file; for the failure test that
        # file simply does not exist on disk any more.
        source_path=str(source or (tmp_path / "episode.mp3")),
    )
    return cfg, db.get_podcast(podcast["id"]), db.get_episode(episode["id"])


def test_download_stage_uses_the_source_file_in_place(tmp_path, app_env):
    from app import db, pipeline

    source = tmp_path / "episode.mp3"
    source.write_bytes(b"ID3" + b"a" * 4000)
    _cfg, podcast, episode = _folder_podcast(tmp_path, source=source)
    log = tmp_path / "download.log"

    pipeline.handle_download(
        {"id": 1, "type": "download", "episode_id": episode["id"], "podcast_id": podcast["id"]},
        log,
    )

    saved = db.get_episode(episode["id"])
    assert saved["status"] == "downloaded"
    assert saved["original_path"] is None  # nothing copied into the library
    assert source.is_file() and source.stat().st_size == 4003
    job = db.latest_job_for_episode(episode["id"], "transcribe")
    assert job is not None and job["state"] == "queued"


def test_download_stage_reports_a_missing_source_file(tmp_path, app_env):
    import pytest

    from app import pipeline

    _cfg, podcast, episode = _folder_podcast(tmp_path, source=None)
    with pytest.raises(pipeline.JobFatal) as excinfo:
        pipeline.handle_download(
            {"id": 1, "type": "download", "episode_id": episode["id"], "podcast_id": podcast["id"]},
            tmp_path / "download.log",
        )
    assert "Source file is missing" in str(excinfo.value)
    assert excinfo.value.episode_status == "failed"


def test_worker_schedules_a_scan_for_folder_podcasts(monkeypatch):
    from app import db, worker

    calls: list[tuple] = []

    monkeypatch.setattr(
        db,
        "list_podcasts",
        lambda: [
            {"id": 1, "source_kind": "folder", "poll_interval_minutes": 30, "last_polled_at": None},
            {"id": 2, "source_kind": "rss", "poll_interval_minutes": 30, "last_polled_at": None},
        ],
    )
    monkeypatch.setattr(db, "enqueue_job", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(db, "get_setting", lambda *args, **kwargs: db.now_iso())

    worker.Worker().schedule()

    jobs = {(args[0], kwargs.get("podcast_id")) for args, kwargs in calls if args}
    assert ("scan", 1) in jobs
    assert ("poll", 2) in jobs
