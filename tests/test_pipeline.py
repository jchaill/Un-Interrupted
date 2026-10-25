from __future__ import annotations

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


def test_auto_publish_ignores_no_ad_episode_when_llm_failed(tmp_path, monkeypatch):
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
    assert not pipeline._maybe_auto_publish(
        {"id": 7, "auto_publish": 1},
        42,
        [],
        review,
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
