from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import config, db, security


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setenv("UNINTERUPTED_HOME", str(home))
    monkeypatch.setenv("UNINTERUPTED_BASE_URL", "http://testserver:8090")
    cfg = config.load_config()
    cfg.update(
        {
            "storage_root": str(library),
            "base_url": "http://testserver:8090",
            "password_hash": security.hash_password("password123"),
            "setup_complete": True,
        }
    )
    config.save_config(cfg)
    db.init_db()
    return {"home": home, "library": library, "cfg": cfg}
