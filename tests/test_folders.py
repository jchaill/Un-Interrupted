from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import config, db, folders


def _make_folder(tmp_path: Path, names, *, subdir: str | None = None) -> Path:
    root = tmp_path / "archive"
    root.mkdir(exist_ok=True)
    target = root / subdir if subdir else root
    target.mkdir(parents=True, exist_ok=True)
    for name in names:
        (target / name).write_bytes(b"ID3" + b"x" * 64)
    return root


def _podcast(root: Path, *, auto_process: bool = False) -> dict:
    return db.create_podcast(
        {
            "title": "Folder show",
            "feed_url": folders.feed_url_for(root),
            "language": "fr",
            "labels_json": "[]",
            "feed_token": "folder-token",
            "auto_process": auto_process,
            "source_kind": "folder",
            "source_path": str(root),
        }
    )


def test_normalize_path_accepts_a_folder(tmp_path, app_env):
    root = _make_folder(tmp_path, ["a.mp3"])
    assert folders.normalize_path(str(root), config.load_config()) == root.resolve()


def test_normalize_path_rejects_bad_paths(tmp_path, app_env):
    cfg = config.load_config()
    with pytest.raises(folders.FolderError):
        folders.normalize_path("", cfg)
    with pytest.raises(folders.FolderError):
        folders.normalize_path(str(tmp_path / "nope"), cfg)
    afile = tmp_path / "a.mp3"
    afile.write_bytes(b"x")
    with pytest.raises(folders.FolderError):
        folders.normalize_path(str(afile), cfg)
    with pytest.raises(folders.FolderError):
        folders.normalize_path(str(config.podcasts_root(cfg) / "x"), cfg)
    with pytest.raises(folders.FolderError):
        folders.normalize_path(str(config.app_home() / "cache"), cfg)


def test_list_audio_files_is_recursive_and_skips_junk(tmp_path):
    root = _make_folder(tmp_path, ["b.mp3", "notes.txt", ".hidden.mp3"])
    nested = root / "2026"
    nested.mkdir()
    (nested / "a.m4a").write_bytes(b"x")
    names = [folders.relative_tag(root, p) for p in folders.list_audio_files(root)]
    assert names == ["b.mp3", "2026/a.m4a"]


def test_guid_and_dates_are_derived_from_the_name(tmp_path):
    root = _make_folder(tmp_path, ["2026-11-09 - emission du lundi.mp3", "no-date.mp3"])
    files = sorted(folders.list_audio_files(root))
    dated = next(p for p in files if p.name.startswith("2026-"))
    undated = next(p for p in files if not p.name.startswith("2026-"))
    assert folders.guid_for(folders.relative_tag(root, dated)) == (
        "file:2026-11-09 - emission du lundi.mp3"
    )
    assert folders.published_at_for(dated).startswith("2026-11-09T00:00:00")
    # Falls back to the file mtime, which is recent.
    assert folders.published_at_for(undated) >= "2020-01-01"


def test_scan_creates_one_episode_per_file_and_keeps_sources(tmp_path, app_env):
    root = _make_folder(tmp_path, ["2026-11-01 - one.mp3", "2026-11-02 - two.mp3"])
    podcast = _podcast(root)

    result = folders.scan(podcast)

    assert result["files"] == 2
    assert result["new"] == 2
    assert result["queued"] == 0  # auto_process is off by default here
    episodes = db.list_episodes(podcast["id"])
    assert len(episodes) == 2
    titles = {e["title"] for e in episodes}
    assert titles == {"one", "two"}
    for episode in episodes:
        assert episode["source_path"] and Path(episode["source_path"]).is_file()
        assert episode["original_path"] is None  # never copied into the library
        assert Path(episode["source_path"]).parent == root
        assert json.loads(episode["item_json"])["source_rel"]
    # sources untouched
    assert len(list(root.iterdir())) == 2


def test_rescan_adds_only_new_files(tmp_path, app_env):
    root = _make_folder(tmp_path, ["2026-11-01 - one.mp3"])
    podcast = _podcast(root)
    folders.scan(podcast)
    (root / "2026-11-03 - three.mp3").write_bytes(b"x")

    again = folders.scan(podcast)

    assert again["new"] == 1
    assert again["known"] == 1
    assert db.count_episodes(podcast["id"]) == 2


def test_scan_queues_downloads_when_auto_process_is_on(tmp_path, app_env):
    root = _make_folder(tmp_path, ["2026-11-01 - one.mp3"])
    podcast = _podcast(root, auto_process=True)
    folders.scan(db.get_podcast(podcast["id"]))

    episode = db.list_episodes(podcast["id"])[0]
    assert episode["status"] == "queued"
    assert episode["selected"] == 1
    job = db.latest_job_for_episode(episode["id"], "download")
    assert job is not None and job["state"] == "queued"


def test_scan_reports_a_missing_folder(app_env, tmp_path):
    podcast = _podcast(tmp_path / "gone")
    with pytest.raises(folders.FolderError):
        folders.scan(podcast)


def test_remap_episodes_follows_a_moved_folder(tmp_path, app_env):
    root = _make_folder(tmp_path, ["2026-11-01 - one.mp3"])
    podcast = _podcast(root)
    folders.scan(podcast)
    moved = tmp_path / "moved"
    moved.mkdir()
    (moved / "2026-11-01 - one.mp3").write_bytes(b"x")

    assert folders.remap_episodes(podcast, moved) == 1
    assert db.list_episodes(podcast["id"])[0]["source_path"] == str(
        moved / "2026-11-01 - one.mp3"
    )


def test_deleted_folder_episode_is_not_reimported(tmp_path, app_env):
    from app import pipeline

    root = _make_folder(tmp_path, ["2026-11-01 - one.mp3"])
    podcast = _podcast(root)
    folders.scan(podcast)
    episode = db.list_episodes(podcast["id"])[0]

    pipeline.delete_episode(config.load_config(), podcast, episode)

    # The source file is never touched by the delete...
    assert (root / "2026-11-01 - one.mp3").is_file()
    # ...and the tombstone keeps the next scan from importing it again.
    result = folders.scan(db.get_podcast(podcast["id"]))
    assert result["new"] == 0
    assert db.count_episodes(podcast["id"]) == 0


def test_browse_lists_only_folders_with_audio_hints(tmp_path):
    root = tmp_path / "media"
    (root / "Show A").mkdir(parents=True)
    (root / "Show A" / "one.mp3").write_bytes(b"x")
    (root / "Show A" / "two.mp3").write_bytes(b"x")
    (root / "Show B").mkdir()
    (root / ".hidden").mkdir()
    (root / "stray.mp3").write_bytes(b"x")

    payload = folders.browse(root)

    assert payload["path"] == str(root.resolve())
    assert payload["parent"] == str(root.resolve().parent)
    assert [row["name"] for row in payload["dirs"]] == ["Show A", "Show B"]
    assert payload["dirs"][0]["audio"] == 2
    assert payload["dirs"][1]["audio"] == 0
    assert payload["truncated"] is False

    with_hidden = folders.browse(root, hidden=True)
    assert [row["name"] for row in with_hidden["dirs"]] == [".hidden", "Show A", "Show B"]


def test_browse_defaults_to_home_and_reports_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    payload = folders.browse("")
    assert payload["path"] == str(tmp_path.resolve())

    with pytest.raises(folders.FolderError):
        folders.browse(tmp_path / "missing")
    afile = tmp_path / "a.mp3"
    afile.write_bytes(b"x")
    with pytest.raises(folders.FolderError):
        folders.browse(afile)


def test_browse_reports_permission_errors(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o000)
    try:
        with pytest.raises(folders.FolderError) as excinfo:
            folders.browse(blocked)
        assert "Permission denied" in str(excinfo.value)
    finally:
        blocked.chmod(0o700)


def test_roots_include_home_and_existing_shortcuts(app_env, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Desktop").mkdir()
    roots = folders.roots(config.load_config())
    names = [row["name"] for row in roots]
    assert names[0] == "Home"
    assert "Desktop" in names
    assert all(row["path"] for row in roots)
    assert roots[0]["path"] == str(tmp_path)
