import json

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


def test_admin_pages_render(app_env):
    client = _client(app_env)
    for path in ["/", "/podcasts/add", "/podcasts/import", "/settings", "/queue"]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "Un-Interupted" in response.text


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
    assert cfg["llm_enabled"] == 0
    assert cfg["llm_api_key"] == ""


def test_settings_llm_test_route(app_env, monkeypatch):
    client = _client(app_env)

    def fake_test(cfg):
        return {"model": "deepseek-v4.1-flash", "seconds": 0.42}

    monkeypatch.setattr(web.llm, "test_connection", fake_test)
    response = client.post("/settings/llm/test", follow_redirects=False)
    assert response.status_code == 303
    assert "deepseek-v4.1-flash" in response.headers["location"]
