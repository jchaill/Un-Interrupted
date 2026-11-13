import json
from pathlib import Path

from fastapi.testclient import TestClient

import app.web as web
from app import config, db, review


def _client(app_env):
    client = TestClient(web.app)
    client.post(
        "/login",
        data={"password": "password123", "next": "/"},
        follow_redirects=False,
    )
    return client


def _stub_llm_discovery(monkeypatch, models=None):
    models = models or []

    def fake_discover(cfg, *, timeout=0):
        return {
            "ok": True,
            "installed": True,
            "binary": "/opt/homebrew/bin/ollama",
            "daemon_ok": True,
            "endpoint": "http://127.0.0.1:11434/api/tags",
            "current": "test:cloud",
            "models": models,
            "error": None,
            "hints": [],
            "cloud": False,
        }

    monkeypatch.setattr(web.llm, "discover", fake_discover)


def test_setup_is_required_without_config(tmp_path, monkeypatch):
    monkeypatch.setenv("UNINTERUPTED_HOME", str(tmp_path / "fresh-home"))
    monkeypatch.setenv("UNINTERUPTED_BASE_URL", "http://testserver:8090")
    with TestClient(web.app) as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/setup"
        response = client.get("/setup")
        assert response.status_code == 200
        assert "Library folder" in response.text


def test_setup_post_creates_config(tmp_path, monkeypatch, app_env):
    # Start from an empty home so /setup is available.
    monkeypatch.setenv("UNINTERUPTED_HOME", str(tmp_path / "another-home"))
    library = tmp_path / "another-library"
    library.mkdir()
    monkeypatch.setattr(web.toolchain, "start_install", lambda: True)
    with TestClient(web.app) as client:
        response = client.post(
            "/setup",
            data={
                "storage_root": str(library),
                "base_url": "http://testserver:8090",
                "password": "password123",
                "password2": "password123",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/setup/toolchain")
        assert "ui_session" in client.cookies
        assert config.load_config()["setup_complete"] is True


def test_admin_pages_render(app_env, monkeypatch):
    _stub_llm_discovery(monkeypatch)
    client = _client(app_env)
    for path in ["/", "/podcasts/add", "/podcasts/import", "/settings", "/queue"]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "Un-Interupted" in response.text
        # Assets are stamped so a deploy cannot serve a stale app.js/app.css.
        assert "/static/app.js?v=" in response.text
        assert "/static/app.css?v=" in response.text


def test_podcast_and_review_pages_render(app_env):
    cfg = config.load_config()
    podcast = db.create_podcast(
        {
            "title": "Page Show",
            "feed_url": "https://example.com/page.xml",
            "language": "en",
            "labels_json": '[["(?i)acme","Acme"]]',
            "feed_token": "page-token",
            "last_polled_at": db.now_iso(),
        }
    )
    episode, _ = db.upsert_feed_episode(
        podcast["id"],
        {
            "guid": "page-guid",
            "title": "A page episode",
            "description": "Description",
            "published_at": "2026-10-20T12:00:00+00:00",
            "original_url": "https://example.com/page.mp3",
            "enclosure_type": "audio/mpeg",
            "duration": 30.0,
            "item_json": {},
        },
    )
    dirs = config.ensure_episode_dirs(cfg, podcast["slug"])
    original = dirs["originals"] / "page.mp3"
    original.write_bytes(b"ID3" + b"a" * 1000)
    transcript = dirs["work"] / f"tr_ep{episode['id']}.json"
    transcript.write_text(
        json.dumps(
            {
                "transcription": [
                    {"offsets": {"from": 0, "to": 3000}, "text": "Welcome."},
                    {"offsets": {"from": 3000, "to": 9000}, "text": "Brought to you by Acme."},
                    {"offsets": {"from": 9000, "to": 20000}, "text": "Back to the show."},
                ]
            }
        )
    )
    cuts = [[3, 9]]
    llm_result = {
        "ok": True,
        "enabled": True,
        "model": "test:cloud",
        "ads": [
            {
                "start": 3.0,
                "end": 9.0,
                "brand": "Acme",
                "kind": "host-read",
                "confidence": 0.91,
                "evidence": "Brought to you by Acme.",
            }
        ],
        "chunks": 1,
    }
    review_data = review.build_review(
        transcript,
        cuts,
        labels_json=podcast["labels_json"],
        total_duration=30.0,
        llm_result=llm_result,
    )
    db.update_episode(
        episode["id"],
        status="awaiting_review",
        original_path=config.rel_to_storage(cfg, original),
        duration_original=30.0,
        cuts_json=json.dumps(cuts),
        review_json=json.dumps(review_data),
        confidence=review_data["confidence"],
    )
    client = _client(app_env)
    response = client.get(f"/podcasts/{podcast['slug']}")
    assert response.status_code == 200
    assert "Page Show" in response.text
    response = client.get(f"/podcasts/{podcast['slug']}/episodes/{episode['id']}/review")
    assert response.status_code == 200
    assert "Cut ranges" in response.text
    assert "Acme" in response.text
    assert "LLM 91%" in response.text
    assert "Retry from scratch" in response.text
    response = client.post(
        f"/podcasts/{podcast['slug']}/episodes/{episode['id']}/approve",
        data={"start": "3", "end": "9"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/podcasts/{podcast['slug']}?filter=review")


def test_approve_without_cuts_queues_original_as_clean(app_env):
    cfg = config.load_config()
    podcast = db.create_podcast(
        {
            "title": "No Ads Show",
            "feed_url": "https://example.com/no-ads.xml",
            "language": "en",
            "labels_json": "[]",
            "feed_token": "no-ads-token",
            "last_polled_at": db.now_iso(),
        }
    )
    episode, _ = db.upsert_feed_episode(
        podcast["id"],
        {
            "guid": "no-ads-guid",
            "title": "An episode without ads",
            "description": "Description",
            "published_at": "2026-10-21T12:00:00+00:00",
            "original_url": "https://example.com/no-ads.mp3",
            "enclosure_type": "audio/mpeg",
            "duration": 30.0,
            "item_json": {},
        },
    )
    dirs = config.ensure_episode_dirs(cfg, podcast["slug"])
    original = dirs["originals"] / "no-ads.mp3"
    original.write_bytes(b"ID3" + b"a" * 1000)
    transcript = dirs["work"] / f"tr_ep{episode['id']}.json"
    transcript.write_text(
        json.dumps(
            {
                "transcription": [
                    {"offsets": {"from": 0, "to": 6000}, "text": "Welcome to the show."},
                    {"offsets": {"from": 6000, "to": 18000}, "text": "Here is the interview."},
                    {"offsets": {"from": 18000, "to": 30000}, "text": "Thanks for listening."},
                ]
            }
        )
    )
    review_data = review.build_review(
        transcript,
        [],
        labels_json="[]",
        total_duration=30.0,
    )
    db.update_episode(
        episode["id"],
        status="awaiting_review",
        original_path=config.rel_to_storage(cfg, original),
        duration_original=30.0,
        cuts_json="[]",
        review_json=json.dumps(review_data),
        confidence=review_data["confidence"],
    )
    client = _client(app_env)
    response = client.post(
        f"/podcasts/{podcast['slug']}/episodes/{episode['id']}/approve",
        data={},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/podcasts/{podcast['slug']}?filter=review")
    saved = db.get_episode(episode["id"])
    assert saved["status"] == "approved"
    assert json.loads(saved["approved_cuts_json"]) == []
    job = db.latest_job_for_episode(episode["id"], "cut")
    assert job is not None
    assert job["state"] == "queued"


def test_batch_process_queues_selected_episodes(app_env):
    podcast = db.create_podcast(
        {
            "title": "Batch Show",
            "feed_url": "https://example.com/batch.xml",
            "language": "en",
            "feed_token": "batch-token",
            "last_polled_at": db.now_iso(),
        }
    )
    episodes = []
    for index in range(3):
        episode, _ = db.upsert_feed_episode(
            podcast["id"],
            {
                "guid": f"batch-guid-{index}",
                "title": f"Batch episode {index}",
                "description": "Description",
                "published_at": f"2026-10-2{index}T12:00:00+00:00",
                "original_url": f"https://example.com/batch-{index}.mp3",
                "enclosure_type": "audio/mpeg",
                "duration": 30.0,
                "item_json": {},
            },
        )
        episodes.append(episode)
    db.update_episode(episodes[2]["id"], status="published")

    client = _client(app_env)
    page = client.get(f"/podcasts/{podcast['slug']}")
    assert page.status_code == 200
    assert "Download &amp; process" in page.text
    assert "Download &amp;amp; process" not in page.text
    assert 'id="batch-form"' in page.text

    response = client.post(
        f"/podcasts/{podcast['slug']}/episodes/batch-process",
        data={
            "episode_ids": [
                str(episodes[0]["id"]),
                str(episodes[1]["id"]),
                str(episodes[2]["id"]),
            ],
            "filter": "all",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Queued+2+episode" in response.headers["location"]
    for episode in episodes[:2]:
        saved = db.get_episode(episode["id"])
        assert saved["status"] == "queued"
        job = db.latest_job_for_episode(episode["id"], "download")
        assert job is not None
        assert job["state"] == "queued"
    assert db.get_episode(episodes[2]["id"])["status"] == "published"


def test_media_range_and_token(app_env):
    cfg = config.load_config()
    podcast = db.create_podcast(
        {
            "title": "Media Show",
            "feed_url": "https://example.com/media.xml",
            "language": "en",
            "labels_json": "[]",
            "feed_token": "media-token",
            "last_polled_at": db.now_iso(),
        }
    )
    dirs = config.ensure_episode_dirs(cfg, podcast["slug"])
    clean = dirs["episodes"] / "2026-01-01 - media - deadbeef (no ads).mp3"
    clean.write_bytes(b"ID3" + b"z" * 2000)
    client = _client(app_env)
    url = f"/media/{podcast['slug']}/media-token/{clean.name}"
    response = client.get(url, headers={"Range": "bytes=10-19"})
    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 10-19/{clean.stat().st_size}"
    assert len(response.content) == 10
    assert client.head(url).headers["accept-ranges"] == "bytes"
    assert client.get(f"/media/{podcast['slug']}/wrong/{clean.name}").status_code == 404
    feed = client.get(f"/feed/{podcast['slug']}/media-token.xml")
    assert feed.status_code == 200
    assert feed.headers["etag"]
    assert client.get(
        f"/feed/{podcast['slug']}/media-token.xml",
        headers={"If-None-Match": feed.headers["etag"]},
    ).status_code == 304


def test_settings_llm_saves_and_masks_key(app_env, monkeypatch):
    _stub_llm_discovery(
        monkeypatch,
        models=[
            {
                "name": "deepseek-v4.1-flash:cloud",
                "selected": True,
                "current": False,
                "listed": True,
                "cloud": True,
            }
        ],
    )
    client = _client(app_env)
    response = client.post(
        "/settings/llm",
        data={
            "llm_enabled": "on",
            "llm_model": "deepseek-v4.1-flash:cloud",
            "llm_base_url": "http://127.0.0.1:11434",
            "llm_min_confidence": "0.8",
            "llm_api_key": "secret-key",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    cfg = config.load_config()
    assert cfg["llm_enabled"] == 1
    assert cfg["llm_min_confidence"] == 0.8
    assert cfg["llm_api_key"] == "secret-key"

    page = client.get("/settings")
    assert "AI ad detection" in page.text
    assert "already set" in page.text
    assert "secret-key" not in page.text
    assert "Check LLM install" in page.text
    assert 'value="deepseek-v4.1-flash:cloud"' in page.text

    # Leaving the password blank keeps the stored key.
    client.post(
        "/settings/llm",
        data={
            "llm_enabled": "on",
            "llm_model": "deepseek-v4.1-flash:cloud",
            "llm_base_url": "http://127.0.0.1:11434",
            "llm_min_confidence": "0.7",
            "llm_api_key": "",
        },
        follow_redirects=False,
    )
    assert config.load_config()["llm_api_key"] == "secret-key"

    client.post(
        "/settings/llm",
        data={
            "llm_model": "deepseek-v4.1-flash:cloud",
            "llm_base_url": "http://127.0.0.1:11434",
            "llm_min_confidence": "0.7",
            "llm_api_key_clear": "on",
        },
        follow_redirects=False,
    )
    cfg = config.load_config()
    assert cfg["llm_enabled"] == 1
    assert cfg["llm_api_key"] == ""


def test_settings_llm_test_route(app_env, monkeypatch):
    _stub_llm_discovery(monkeypatch)
    client = _client(app_env)

    def fake_test(cfg):
        return {"model": "deepseek-v4.1-flash", "seconds": 0.42}

    monkeypatch.setattr(web.llm, "test_connection", fake_test)
    response = client.post("/settings/llm/test", follow_redirects=False)
    assert response.status_code == 303
    assert "deepseek-v4.1-flash" in response.headers["location"]


def test_settings_llm_check_route(app_env, monkeypatch):
    _stub_llm_discovery(
        monkeypatch,
        models=[
            {
                "name": "llama3.2",
                "selected": False,
                "current": False,
                "listed": True,
                "cloud": False,
            }
        ],
    )
    client = _client(app_env)
    response = client.post("/settings/llm/check", follow_redirects=False)
    assert response.status_code == 303
    assert "Ollama+is+ready" in response.headers["location"]


def test_settings_llm_install_hints(app_env, monkeypatch):
    def fake_discover(cfg, *, timeout=0):
        return {
            "ok": False,
            "installed": False,
            "binary": None,
            "daemon_ok": False,
            "endpoint": "http://127.0.0.1:11434/api/tags",
            "current": "test:cloud",
            "models": [],
            "error": None,
            "hints": [
                "Ollama is not installed. Install the Ollama app, or run `brew install ollama` in Terminal."
            ],
            "cloud": False,
        }

    monkeypatch.setattr(web.llm, "discover", fake_discover)
    client = _client(app_env)
    page = client.get("/settings")
    assert "Ollama is not installed" in page.text
    assert "brew install ollama" in page.text


def _episode_with_artifacts(app_env, slug: str = "reprocess-show"):
    cfg = config.load_config()
    podcast = db.create_podcast(
        {
            "title": "Reprocess Show",
            "feed_url": f"https://example.com/{slug}.xml",
            "language": "fr",
            "feed_token": f"{slug}-token",
            "last_polled_at": db.now_iso(),
        }
    )
    episode, _ = db.upsert_feed_episode(
        podcast["id"],
        {
            "guid": f"{slug}-guid",
            "title": "Les grosses têtes",
            "description": "Description",
            "published_at": "2026-11-01T12:00:00+00:00",
            "original_url": f"https://example.com/{slug}.mp3",
            "enclosure_type": "audio/mpeg",
            "duration": 120.0,
            "item_json": {},
        },
    )
    dirs = config.ensure_episode_dirs(cfg, podcast["slug"])
    original = dirs["originals"] / "episode.mp3"
    original.write_bytes(b"ID3" + b"a" * 2000)
    transcript = dirs["work"] / f"tr_ep{episode['id']}.json"
    transcript.write_text('{"transcription": []}', encoding="utf-8")
    cuts = dirs["work"] / f"cuts_ep{episode['id']}.json"
    cuts.write_text("[]", encoding="utf-8")
    clean = dirs["episodes"] / "2026-11-01 - les grosses tetes - deadbeef (no ads).mp3"
    clean.write_bytes(b"ID3" + b"b" * 2000)
    pre = dirs["work"] / f"pre_ep{episode['id']}.wav"
    pre.write_bytes(b"RIFF" + b"c" * 500)
    index = dirs["work"] / "index.json"
    index.write_text(
        json.dumps(
            {
                f"ep{episode['id']}": {"mp3": "episode.mp3", "duration": 120.0},
                "ep999999": {"mp3": "other.mp3", "duration": 60.0},
            }
        ),
        encoding="utf-8",
    )
    db.update_episode(
        episode["id"],
        status="failed",
        selected=1,
        original_path=config.rel_to_storage(cfg, original),
        clean_filename=clean.name,
        cuts_json="[]",
        approved_cuts_json="[]",
        review_json="{}",
        error_message="machine rebooted during processing",
        duration_original=120.0,
    )
    return podcast, db.get_episode(episode["id"]), dirs, original, transcript, cuts, clean, pre, index


def test_batch_reprocess_restarts_from_a_fresh_download(app_env):
    (
        podcast,
        episode,
        _dirs,
        original,
        transcript,
        cuts,
        clean,
        pre,
        index,
    ) = _episode_with_artifacts(app_env)
    client = _client(app_env)

    page = client.get(f"/podcasts/{podcast['slug']}?filter=failed")
    assert page.status_code == 200
    assert 'id="batch-reprocess"' in page.text
    assert 'id="batch-delete"' in page.text
    assert 'data-processable="1"' in page.text

    response = client.post(
        f"/podcasts/{podcast['slug']}/episodes/batch-reprocess",
        data={"episode_ids": [str(episode["id"])], "filter": "failed"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "full+reprocess" in response.headers["location"]

    saved = db.get_episode(episode["id"])
    assert saved["status"] == "queued"
    assert saved["error_message"] is None
    assert saved["original_path"] is None
    assert saved["clean_filename"] is None
    assert saved["cuts_json"] is None
    assert saved["approved_cuts_json"] is None
    assert saved["review_json"] is None
    assert saved["selected"] == 1

    for path in (original, transcript, cuts, clean, pre):
        assert not path.exists(), path
    # transcribe.sh bookkeeping is pruned too, other tags are kept.
    assert f"ep{episode['id']}" not in json.loads(index.read_text())
    assert "ep999999" in json.loads(index.read_text())

    job = db.latest_job_for_episode(episode["id"], "download")
    assert job is not None and job["state"] == "queued"


def test_batch_reprocess_skips_the_running_episode(app_env):
    podcast, episode, _dirs, original, _t, _c, _cl, _pre, _idx = _episode_with_artifacts(
        app_env, slug="busy-show"
    )
    job_id = db.enqueue_job("cut", podcast_id=podcast["id"], episode_id=episode["id"])
    db.update_job(job_id, state="running", started_at=db.now_iso())
    client = _client(app_env)
    response = client.post(
        f"/podcasts/{podcast['slug']}/episodes/batch-reprocess",
        data={"episode_ids": [str(episode["id"])], "filter": "all"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]
    assert db.get_episode(episode["id"])["status"] == "failed"
    assert original.exists()


def test_batch_delete_removes_episode_files_and_jobs(app_env):
    (
        podcast,
        episode,
        _dirs,
        original,
        transcript,
        cuts,
        clean,
        pre,
        index,
    ) = _episode_with_artifacts(app_env, slug="delete-show")
    job_id = db.enqueue_job("cut", podcast_id=podcast["id"], episode_id=episode["id"])
    client = _client(app_env)

    response = client.post(
        f"/podcasts/{podcast['slug']}/episodes/batch-delete",
        data={"episode_ids": [str(episode["id"])], "filter": "all"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Deleted+1+episode" in response.headers["location"]

    # The row is kept as a tombstone so polling cannot resurrect the episode.
    assert db.get_episode(episode["id"])["status"] == "deleted"
    assert db.list_episodes(podcast["id"]) == []
    assert db.count_episodes(podcast["id"]) == 0
    assert "deleted" not in db.episode_status_counts(podcast["id"])
    assert db.get_job(job_id) is None
    for path in (original, transcript, cuts, clean, pre):
        assert not path.exists(), path
    assert f"ep{episode['id']}" not in json.loads(index.read_text())
    assert "ep999999" in json.loads(index.read_text())

    feed = client.get(f"/feed/{podcast['slug']}/delete-show-token.xml")
    assert feed.status_code == 200
    assert "Les grosses" not in feed.text

    # A later poll sees the same guid: no new row, no re-download.
    again, created = db.upsert_feed_episode(
        podcast["id"],
        {
            "guid": "delete-show-guid",
            "title": "Les grosses têtes",
            "original_url": "https://example.com/delete-show.mp3",
            "published_at": "2026-11-01T12:00:00+00:00",
            "item_json": {},
        },
    )
    assert created is False
    assert again["status"] == "deleted"
    assert db.count_episodes(podcast["id"]) == 0

    # Hidden from "All", but reachable from the Deleted tab and restorable.
    assert "Les grosses" not in client.get(f"/podcasts/{podcast['slug']}").text
    deleted_page = client.get(f"/podcasts/{podcast['slug']}?filter=deleted")
    assert "Les grosses" in deleted_page.text
    assert "Restore" in deleted_page.text
    response = client.post(
        f"/podcasts/{podcast['slug']}/episodes/{episode['id']}/restore",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.get_episode(episode["id"])["status"] == "discovered"
    assert db.count_episodes(podcast["id"]) == 1


def test_delete_selected_with_nothing_checked_reports_an_error(app_env):
    podcast = db.create_podcast(
        {
            "title": "Empty Delete Show",
            "feed_url": "https://example.com/empty-delete.xml",
            "language": "en",
            "feed_token": "empty-delete-token",
            "last_polled_at": db.now_iso(),
        }
    )
    client = _client(app_env)
    response = client.post(
        f"/podcasts/{podcast['slug']}/episodes/batch-delete",
        data={"filter": "all"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]


def test_add_folder_podcast_preview_create_and_page(app_env, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "2026-11-01 - one.mp3").write_bytes(b"ID3" + b"a" * 200)
    (inbox / "2026-11-02 - two.mp3").write_bytes(b"ID3" + b"b" * 200)
    (inbox / "notes.txt").write_text("ignore me")
    client = _client(app_env)

    page = client.get("/podcasts/add?tab=folder")
    assert page.status_code == 200
    assert "From a folder" in page.text
    assert 'name="folder_path"' in page.text

    preview = client.post(
        "/podcasts/add-folder",
        data={"action": "preview", "folder_path": str(inbox), "title": "Archive", "language": "fr"},
    )
    assert preview.status_code == 200
    assert "2 audio file(s)" in preview.text
    assert "one.mp3" in preview.text
    assert "notes.txt" not in preview.text

    created = client.post(
        "/podcasts/add-folder",
        data={
            "action": "add",
            "folder_path": str(inbox),
            "title": "Archive",
            "language": "fr",
            "scan_interval": "15",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    slug = created.headers["location"].split("?")[0].rstrip("/").rsplit("/", 1)[-1]

    podcast = db.get_podcast_by_slug(slug)
    assert podcast["source_kind"] == "folder"
    assert podcast["source_path"] == str(inbox)
    assert podcast["feed_url"] == f"folder://{inbox}"
    assert podcast["poll_interval_minutes"] == 15
    assert db.count_episodes(podcast["id"]) == 2
    for episode in db.list_episodes(podcast["id"]):
        assert episode["original_path"] is None
        assert episode["source_path"]
        assert Path(episode["source_path"]).is_file()

    detail = client.get(f"/podcasts/{slug}")
    assert "Scan folder now" in detail.text
    assert "Check feed now" not in detail.text
    assert "Scan interval (minutes)" in detail.text
    assert str(inbox) in detail.text

    # Duplicate folder is rejected.
    duplicate = client.post(
        "/podcasts/add-folder",
        data={"action": "preview", "folder_path": str(inbox), "title": "Again"},
    )
    assert duplicate.status_code == 200
    assert "already added" in duplicate.text


def test_folder_podcast_scan_route_and_settings(app_env, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "2026-11-01 - one.mp3").write_bytes(b"ID3" + b"a" * 200)
    client = _client(app_env)
    response = client.post(
        "/podcasts/add-folder",
        data={"action": "add", "folder_path": str(inbox), "title": "Archive", "scan_interval": "10"},
        follow_redirects=False,
    )
    slug = response.headers["location"].split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    podcast = db.get_podcast_by_slug(slug)

    scan = client.post(f"/podcasts/{slug}/scan", follow_redirects=False)
    assert scan.status_code == 303
    job = db.list_jobs(limit=5)[0]
    assert job["type"] == "scan" and job["state"] == "queued"

    moved = tmp_path / "moved"
    moved.mkdir()
    (moved / "2026-11-01 - one.mp3").write_bytes(b"ID3" + b"a" * 200)
    saved = client.post(
        f"/podcasts/{slug}/settings",
        data={
            "title": podcast["title"],
            "language": "fr",
            "poll_interval_minutes": "45",
            "source_path": str(moved),
            "labels_json": "[]",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    updated = db.get_podcast(podcast["id"])
    assert updated["source_path"] == str(moved)
    assert updated["poll_interval_minutes"] == 45
    episode = db.list_episodes(podcast["id"])[0]
    assert Path(episode["source_path"]).parent == moved


def test_folder_picker_api_lists_directories(app_env, tmp_path):
    root = tmp_path / "media"
    (root / "Show A").mkdir(parents=True)
    (root / "Show A" / "one.mp3").write_bytes(b"x")
    (root / ".hidden").mkdir()
    client = _client(app_env)

    roots = client.get("/api/fs/roots")
    assert roots.status_code == 200
    assert roots.json()["roots"][0]["name"] == "Home"

    listing = client.get("/api/fs/list", params={"path": str(root)})
    assert listing.status_code == 200
    payload = listing.json()
    assert payload["path"] == str(root.resolve())
    assert [row["name"] for row in payload["dirs"]] == ["Show A"]
    assert payload["dirs"][0]["audio"] == 1

    hidden = client.get("/api/fs/list", params={"path": str(root), "hidden": 1}).json()
    assert [row["name"] for row in hidden["dirs"]] == [".hidden", "Show A"]

    bad = client.get("/api/fs/list", params={"path": str(tmp_path / "nope")})
    assert bad.status_code == 400
    assert "not found" in bad.json()["detail"].lower()


def test_folder_picker_api_requires_admin(app_env):
    client = TestClient(web.app)  # no login
    for url in ("/api/fs/roots", "/api/fs/list?path=/"):
        response = client.get(url, follow_redirects=False)
        assert response.status_code == 401


def test_folder_forms_expose_the_browse_button(app_env):
    client = _client(app_env)
    page = client.get("/podcasts/add?tab=folder")
    assert 'data-path-picker="#folder-path"' in page.text
    assert 'id="folder-picker"' in page.text

    podcast = db.create_podcast(
        {
            "title": "Archive",
            "feed_url": "folder:///tmp/nowhere",
            "labels_json": "[]",
            "feed_token": "tok",
            "source_kind": "folder",
            "source_path": "/tmp/nowhere",
        }
    )
    detail = client.get(f"/podcasts/{podcast['slug']}")
    assert 'data-path-picker="#source-path"' in detail.text
