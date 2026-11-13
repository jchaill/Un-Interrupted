from __future__ import annotations

import email.utils
import html
import io
from contextlib import asynccontextmanager
import json
import os
import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, artwork, config, db, feeds, folders, pipeline, rss, security, toolchain
from .engine import adapter, llm
from .media import serve_audio, serve_file
from .paths import (
    human_duration,
    human_size,
    is_audio_filename,
    safe_join,
)
from .review import build_review
from .worker import worker_status

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.ensure_app_dirs()
    db.init_db()
    yield


app = FastAPI(
    title="Un-Interupted",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ---------------------------------------------------------------------------
# Template helpers


def _fmt_dt(value: str | None, fmt: str = "%b %d, %Y · %H:%M") -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime(fmt)
    except ValueError:
        return value


def _strip_html(value: str | None, limit: int | None = None) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


_STATUS_CLASS = {
    "published": "ok",
    "ready_publish": "ok",
    "awaiting_review": "warn",
    "qa_failed": "bad",
    "failed": "bad",
    "skipped": "muted",
    "discovered": "muted",
    "queued": "info",
    "downloading": "info",
    "downloaded": "info",
    "transcribing": "info",
    "detecting": "info",
    "approved": "info",
    "cutting": "info",
    "qa": "info",
    "publishing": "info",
}


def _status_class(value: str | None) -> str:
    return _STATUS_CLASS.get(value or "", "muted")


def _status_label(value: str | None) -> str:
    labels = {
        "ready_publish": "ready to publish",
        "qa_failed": "QA failed",
        "awaiting_review": "awaiting review",
    }
    return labels.get(value or "", (value or "").replace("_", " "))


templates.env.filters["human_size"] = human_size
templates.env.filters["duration"] = human_duration
templates.env.filters["fmt_dt"] = _fmt_dt
templates.env.filters["strip_html"] = _strip_html
templates.env.filters["status_class"] = _status_class
templates.env.filters["status_label"] = _status_label
templates.env.globals["app_version"] = __version__


def _static_url(name: str) -> str:
    """Static asset URL with an mtime stamp so a deploy never serves stale JS/CSS."""
    try:
        stamp = int((BASE_DIR / "static" / name).stat().st_mtime)
    except OSError:
        stamp = 0
    return f"/static/{name}?v={stamp}"


templates.env.globals["static_url"] = _static_url


def _base_url(cfg: dict[str, Any]) -> str:
    value = (
        os.environ.get("UNINTERUPTED_BASE_URL")
        or cfg.get("base_url")
        or config.default_base_url(config.effective_port(cfg))
    )
    return str(value).rstrip("/")


def _redirect(path: str, *, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    if msg or err:
        joiner = "&" if "?" in path else "?"
        params = {}
        if msg:
            params["msg"] = msg
        if err:
            params["err"] = err
        path = path + joiner + urlencode(params)
    return RedirectResponse(path, status_code=303)


def _render(request: Request, name: str, **context: Any):
    cfg = context.pop("cfg", None) or config.load_config()
    context.setdefault("cfg", cfg)
    context.setdefault("base_url", _base_url(cfg))
    context.setdefault("version", __version__)
    context.setdefault("worker", worker_status())
    context.setdefault("job_counts", db.job_counts())
    context.setdefault("tools", adapter.check_tools())
    context.setdefault("msg", request.query_params.get("msg"))
    context.setdefault("err", request.query_params.get("err"))
    context.setdefault("path", request.url.path)
    context.setdefault("is_admin", security.is_admin(request, cfg))
    return templates.TemplateResponse(request, name, context)


def _tools_ready() -> bool:
    return bool(adapter.check_tools().get("ready"))


def _dir_size(path: Path, *, limit_files: int = 20000) -> int:
    total = 0
    count = 0
    if not path.exists():
        return 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
            count += 1
            if count >= limit_files:
                return total
    return total


def _podcast_or_404(slug: str) -> dict[str, Any]:
    podcast = db.get_podcast_by_slug(slug)
    if not podcast:
        raise HTTPException(status_code=404, detail="Podcast not found")
    return podcast


def _episode_for_podcast(podcast: dict[str, Any], episode_id: int) -> dict[str, Any]:
    episode = db.get_episode(episode_id)
    if not episode or int(episode["podcast_id"]) != int(podcast["id"]):
        raise HTTPException(status_code=404, detail="Episode not found")
    return episode


def _require_public_token(podcast: dict[str, Any] | None, token: str) -> dict[str, Any]:
    if not podcast or not security.token_matches(podcast.get("feed_token") or "", token):
        raise HTTPException(status_code=404, detail="Not found")
    return podcast


def _review_for_episode(
    cfg: dict[str, Any], podcast: dict[str, Any], episode: dict[str, Any]
) -> dict[str, Any] | None:
    if episode.get("review_json"):
        try:
            return json.loads(episode["review_json"])
        except json.JSONDecodeError:
            pass
    dirs = config.episode_dirs(cfg, podcast["slug"])
    transcript = dirs["work"] / f"tr_{pipeline.episode_tag(episode)}.json"
    if not transcript.is_file():
        return None
    try:
        cuts = json.loads(episode.get("cuts_json") or "[]")
    except json.JSONDecodeError:
        cuts = []
    try:
        llm_result = json.loads(episode.get("llm_json") or "null")
    except json.JSONDecodeError:
        llm_result = None
    return build_review(
        transcript,
        cuts,
        labels_json=podcast.get("labels_json"),
        total_duration=episode.get("duration_original"),
        llm_result=llm_result,
    )


# ---------------------------------------------------------------------------
# Setup and auth


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    cfg = config.load_config()
    if config.is_configured(cfg):
        return _redirect("/")
    return _render(
        request,
        "setup.html",
        cfg=cfg,
        storage_default=str(config.default_storage_root()),
        base_url_default=_base_url(cfg),
    )


@app.post("/setup")
def setup_submit(
    request: Request,
    storage_root: str = Form(...),
    base_url: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
):
    cfg = config.load_config()
    if config.is_configured(cfg):
        return _redirect("/")
    errors = []
    root = Path(storage_root).expanduser()
    if not root.is_absolute():
        errors.append("Storage folder must be an absolute path.")
    if password != password2:
        errors.append("Passwords do not match.")
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    base = base_url.strip().rstrip("/")
    if not re.match(r"^https?://[^\s]+$", base):
        errors.append("Base URL must start with http:// or https://")
    if errors:
        return _render(
            request,
            "setup.html",
            cfg=cfg,
            err=" ".join(errors),
            storage_default=storage_root,
            base_url_default=base_url,
        )
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".uninterupted-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        return _render(
            request,
            "setup.html",
            cfg=cfg,
            err=f"Cannot write to {root}: {exc}",
            storage_default=storage_root,
            base_url_default=base_url,
        )

    cfg.update(
        {
            "storage_root": str(root.resolve()),
            "base_url": base,
            "password_hash": security.hash_password(password),
            "setup_complete": True,
        }
    )
    config.save_config(cfg)
    config.ensure_storage_dirs(cfg)
    db.init_db()
    toolchain.start_install()
    response = _redirect("/setup/toolchain", msg="Setup saved. Installing the processing toolchain…")
    security.set_session(response, cfg, {"admin": True})
    return response


@app.get("/setup/toolchain", response_class=HTMLResponse)
def setup_toolchain_page(request: Request):
    cfg = config.load_config()
    if not config.is_configured(cfg):
        return _redirect("/setup")
    if not security.is_admin(request, cfg):
        return _redirect("/login?next=/setup/toolchain")
    state = toolchain.status()
    if not state["tools"]["ready"] and not state["state"].get("running"):
        toolchain.start_install()
        state = toolchain.status()
    return _render(request, "setup_toolchain.html", toolchain=state)


@app.get("/setup/toolchain/status")
def setup_toolchain_status(request: Request):
    cfg = config.load_config()
    if not config.is_configured(cfg):
        raise HTTPException(403)
    if not security.is_admin(request, cfg):
        raise HTTPException(403)
    return JSONResponse(toolchain.status())


@app.get("/setup/toolchain/log", response_class=PlainTextResponse)
def setup_toolchain_log(request: Request):
    cfg = config.load_config()
    if not security.is_admin(request, cfg):
        raise HTTPException(403)
    return PlainTextResponse(toolchain.log_tail(60_000))


@app.post("/setup/toolchain/install")
def setup_toolchain_install(request: Request):
    cfg = config.load_config()
    if not security.is_admin(request, cfg):
        return _redirect("/login?next=/setup/toolchain")
    toolchain.start_install()
    return _redirect("/setup/toolchain", msg="Toolchain installation running…")


@app.post("/setup/toolchain/recheck")
def setup_toolchain_recheck(request: Request):
    cfg = config.load_config()
    if not security.is_admin(request, cfg):
        return _redirect("/login?next=/setup/toolchain")
    state = toolchain.recheck()
    msg = "Toolchain ready." if state["tools"]["ready"] else "Toolchain is still incomplete."
    return _redirect("/setup/toolchain", msg=msg)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    cfg = config.load_config()
    if not config.is_configured(cfg):
        return _redirect("/setup")
    if security.is_admin(request, cfg):
        return _redirect("/")
    return _render(request, "login.html", next=request.query_params.get("next") or "/")


@app.post("/login")
def login_submit(
    request: Request,
    password: str = Form(...),
    next: str = Form("/"),
):
    cfg = config.load_config()
    if not config.is_configured(cfg):
        return _redirect("/setup")
    if not security.verify_password(password, cfg.get("password_hash", "")):
        return _render(request, "login.html", err="Wrong password.", next=next)
    if not next.startswith("/") or next.startswith("//"):
        next = "/"
    response = _redirect(next)
    security.set_session(response, cfg, {"admin": True})
    return response


@app.post("/logout")
def logout(request: Request):
    response = _redirect("/login")
    security.clear_session(response)
    return response


# ---------------------------------------------------------------------------
# Dashboard


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    cfg, _session = security.require_admin(request)
    podcasts = db.list_podcasts()
    counts = db.episode_status_counts()
    root = config.storage_root(cfg)
    try:
        disk = shutil.disk_usage(root if root.exists() else Path.home())
    except OSError:
        disk = None
    library_bytes = _dir_size(config.podcasts_root(cfg), limit_files=50000)
    return _render(
        request,
        "dashboard.html",
        podcasts=podcasts,
        counts=counts,
        disk=disk,
        library_bytes=library_bytes,
        tools_ready=_tools_ready(),
        recent_jobs=db.list_jobs(limit=8),
        queue_total=db.job_counts().get("queued", 0) + db.job_counts().get("running", 0),
    )


# ---------------------------------------------------------------------------
# Add podcast / OPML


@app.get("/podcasts/add", response_class=HTMLResponse)
def add_podcast_page(request: Request):
    cfg, _session = security.require_admin(request)
    tab = "folder" if request.query_params.get("tab") == "folder" else "rss"
    return _render(
        request,
        "add_podcast.html",
        tab=tab,
        feed_url=request.query_params.get("url", ""),
        preview=None,
        backfill="3",
        folder_path=request.query_params.get("path", ""),
        folder_title="",
        folder_language="auto",
        folder_preview=None,
    )


def _add_page_context(**overrides: Any) -> dict[str, Any]:
    context: dict[str, Any] = {
        "tab": "rss",
        "feed_url": "",
        "preview": None,
        "backfill": "3",
        "folder_path": "",
        "folder_title": "",
        "folder_language": "auto",
        "folder_preview": None,
    }
    context.update(overrides)
    return context


@app.post("/podcasts/add", response_class=HTMLResponse)
async def add_podcast_submit(request: Request):
    cfg, _session = security.require_admin(request)
    form = await request.form()
    action = str(form.get("action") or "preview")
    feed_url = str(form.get("feed_url") or "").strip()
    backfill = str(form.get("backfill") or "3")
    if backfill not in {"0", "3", "5", "10", "all"}:
        backfill = "3"
    auto_process = form.get("auto_process") is not None

    if not feed_url:
        return _render(
            request,
            "add_podcast.html",
            **_add_page_context(feed_url=feed_url, backfill=backfill),
            err="Paste a feed URL.",
        )

    if action == "add":
        try:
            podcast = feeds.add_podcast_from_feed(
                feed_url,
                backfill=backfill,
                auto_process=auto_process,
                title_override=str(form.get("title") or "").strip() or None,
                retention_days=_retention_from_config(),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            return _render(
                request,
                "add_podcast.html",
                **_add_page_context(feed_url=feed_url, backfill=backfill),
                err=str(exc),
            )
        return _redirect(
            f"/podcasts/{podcast['slug']}",
            msg=f"Added {podcast['title']}. {podcast.get('_selected', 0)} episode(s) queued.",
        )

    try:
        preview = feeds.preview(feed_url, limit=8)
    except Exception as exc:  # noqa: BLE001
        return _render(
            request,
            "add_podcast.html",
            **_add_page_context(feed_url=feed_url, backfill=backfill),
            err=str(exc),
        )
    return _render(
        request,
        "add_podcast.html",
        **_add_page_context(feed_url=feed_url, preview=preview, backfill=backfill),
    )


def _api_admin(request: Request) -> dict[str, Any]:
    """Admin guard for JSON endpoints: 401 instead of a login redirect."""
    try:
        cfg, _session = security.require_admin(request)
    except HTTPException as exc:
        if exc.status_code in {303, 401}:
            raise HTTPException(status_code=401, detail="Sign in again.") from exc
        raise
    return cfg


@app.get("/api/fs/roots")
def api_fs_roots(request: Request):
    cfg = _api_admin(request)
    return {"roots": folders.roots(cfg)}


@app.get("/api/fs/list")
def api_fs_list(request: Request, path: str = "", hidden: int = 0):
    _cfg = _api_admin(request)
    try:
        return folders.browse(path, hidden=bool(hidden))
    except folders.FolderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


COVER_NAMES = ("cover", "folder", "artwork", "poster", "front")


def _folder_cover(root: Path) -> Path | None:
    for stem in COVER_NAMES:
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            candidate = root / f"{stem}{ext}"
            if candidate.is_file():
                return candidate
    return None


def _folder_preview(root: Path) -> dict[str, Any]:
    files = folders.list_audio_files(root)
    rows = []
    total = 0
    for path in files[:60]:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        total += size
        rows.append(
            {
                "name": folders.relative_tag(root, path),
                "size": size,
                "published_at": folders.published_at_for(path),
            }
        )
    for path in files[60:]:
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return {
        "path": str(root),
        "count": len(files),
        "files": rows,
        "total_bytes": total,
        "truncated": len(files) > len(rows),
        "cover": _folder_cover(root),
    }


@app.post("/podcasts/add-folder", response_class=HTMLResponse)
async def add_folder_submit(request: Request):
    cfg, _session = security.require_admin(request)
    form = await request.form()
    action = str(form.get("action") or "preview")
    raw_path = str(form.get("folder_path") or "").strip()
    title = str(form.get("title") or "").strip()
    language = str(form.get("language") or "auto")
    context = _add_page_context(
        tab="folder",
        folder_path=raw_path,
        folder_title=title,
        folder_language=language,
    )

    try:
        root = folders.normalize_path(raw_path, cfg)
    except folders.FolderError as exc:
        return _render(request, "add_podcast.html", **context, err=str(exc))

    existing = db.get_podcast_by_feed_url(folders.feed_url_for(root))
    if existing:
        return _render(
            request,
            "add_podcast.html",
            **context,
            err=f"That folder is already added as “{existing['title']}”.",
        )

    try:
        preview = _folder_preview(root)
    except OSError as exc:
        return _render(request, "add_podcast.html", **context, err=f"Cannot read folder: {exc}")

    if not preview["count"]:
        return _render(
            request,
            "add_podcast.html",
            **context,
            err=f"No audio files found in {root}.",
        )

    if action != "add":
        context["folder_path"] = str(root)
        context["folder_preview"] = preview
        return _render(request, "add_podcast.html", **context)

    try:
        interval = max(5, int(str(form.get("scan_interval") or "30")))
    except ValueError:
        interval = 30
    auto_process = form.get("auto_process") is not None
    auto_publish = form.get("auto_publish") is not None

    podcast = db.create_podcast(
        {
            "title": title or root.name or "Local folder",
            "feed_url": folders.feed_url_for(root),
            "description": f"Local folder: {root}",
            "language": language,
            "labels_json": "[]",
            "auto_process": auto_process,
            "auto_publish": auto_publish,
            "backfill_limit": 0,
            "poll_interval_minutes": interval,
            "retention_days": None,
            "feed_token": secrets.token_urlsafe(18),
            "source_kind": "folder",
            "source_path": str(root),
        }
    )
    config.ensure_episode_dirs(cfg, podcast["slug"])

    cover = preview.get("cover")
    if cover:
        try:
            ext = cover.suffix.lower()
            artwork_dir = config.artwork_dir()
            artwork_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cover, artwork_dir / f"{podcast['slug']}{ext}")
            db.update_podcast(podcast["id"], artwork_path=f"{podcast['slug']}{ext}")
            podcast = db.get_podcast(podcast["id"]) or podcast
        except OSError:
            pass

    try:
        result = folders.scan(podcast)
    except folders.FolderError as exc:
        return _redirect(
            f"/podcasts/{podcast['slug']}",
            err=f"Folder added, but the first scan failed: {exc}",
        )
    message = (
        f"Added {podcast['title']}. {result['files']} audio file(s) found, "
        f"{result['queued']} queued for processing."
    )
    return _redirect(f"/podcasts/{podcast['slug']}", msg=message)


@app.post("/podcasts/{slug}/scan")
def podcast_scan_now(request: Request, slug: str):
    cfg, _session = security.require_admin(request)
    podcast = _podcast_or_404(slug)
    if (podcast.get("source_kind") or "rss") != "folder":
        return _redirect(f"/podcasts/{slug}", err="This podcast is not a folder podcast.")
    db.enqueue_job("scan", podcast_id=podcast["id"], priority=5, max_attempts=2)
    return _redirect(f"/podcasts/{slug}", msg="Folder scan queued.")


def _retention_from_config() -> int | None:
    cfg = config.load_config()
    value = cfg.get("default_retention_days", 30)
    if value in (None, "", "forever"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return 30


@app.get("/podcasts/import", response_class=HTMLResponse)
def opml_page(request: Request):
    cfg, _session = security.require_admin(request)
    return _render(request, "import_opml.html", parsed=None, backfill="3")


@app.post("/podcasts/import", response_class=HTMLResponse)
async def opml_submit(request: Request):
    cfg, _session = security.require_admin(request)
    form = await request.form()
    action = str(form.get("action") or "preview")
    backfill = str(form.get("backfill") or "3")
    text = str(form.get("opml_text") or "")
    upload = form.get("file")
    if upload is not None and hasattr(upload, "read"):
        content = await upload.read()
        if content:
            text = content.decode("utf-8", errors="replace")
    if action == "import":
        chosen = [str(url) for url in form.getlist("feed") if str(url).strip()]
        if not chosen:
            return _render(request, "import_opml.html", parsed=None, backfill=backfill, err="Select at least one feed.")
        added = []
        errors = []
        for feed_url in chosen:
            try:
                podcast = feeds.add_podcast_from_feed(
                    feed_url,
                    backfill=backfill,
                    auto_process=True,
                    retention_days=_retention_from_config(),
                )
                added.append(podcast["title"])
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{feed_url}: {exc}")
        if errors:
            return _render(
                request,
                "import_opml.html",
                parsed=None,
                backfill=backfill,
                msg=f"Imported {len(added)} feed(s).",
                err=" | ".join(errors[:4]),
            )
        return _redirect("/", msg=f"Imported {len(added)} podcast(s).")

    if not text.strip():
        return _render(request, "import_opml.html", parsed=None, backfill=backfill, err="Paste OPML or choose a file.")

    try:
        parsed = feeds.parse_opml(text)
    except Exception as exc:  # noqa: BLE001
        return _render(request, "import_opml.html", parsed=None, backfill=backfill, err=str(exc))

    return _render(request, "import_opml.html", parsed=parsed, backfill=backfill)


# ---------------------------------------------------------------------------
# Podcast detail


@app.get("/podcasts/{slug}", response_class=HTMLResponse)
def podcast_page(request: Request, slug: str):
    cfg, _session = security.require_admin(request)
    podcast = _podcast_or_404(slug)
    filter_name = request.query_params.get("filter", "all")
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    per_page = 100

    status = None
    statuses = None
    selected_only = False
    include_deleted = False
    if filter_name == "review":
        statuses = ("awaiting_review", "qa_failed")
    elif filter_name == "failed":
        status = "failed"
    elif filter_name == "published":
        status = "published"
    elif filter_name == "selected":
        selected_only = True
    elif filter_name == "deleted":
        status = "deleted"
        include_deleted = True

    episodes = db.list_episodes(
        podcast["id"],
        status=status,
        statuses=statuses,
        selected_only=selected_only,
        include_deleted=include_deleted,
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    total = db.count_episodes(
        podcast["id"],
        status=status,
        statuses=statuses,
        selected_only=selected_only,
        include_deleted=include_deleted,
    )
    total_all = db.count_episodes(podcast["id"])
    selected_count = db.count_episodes(podcast["id"], selected_only=True)
    deleted_count = db.count_episodes(podcast["id"], status="deleted", include_deleted=True)
    counts = db.episode_status_counts(podcast["id"])
    latest_jobs = db.latest_jobs_by_episode(podcast["id"])
    dirs = config.episode_dirs(cfg, slug)
    clean_files = {
        p.name
        for p in dirs["episodes"].iterdir()
        if p.is_file() and is_audio_filename(p.name)
    } if dirs["episodes"].exists() else set()
    original_names = {
        Path(e["original_path"]).name
        for e in db.list_episodes(podcast["id"], limit=1000)
        if e.get("original_path")
    }

    for episode in episodes:
        episode["_clean_present"] = episode.get("clean_filename") in clean_files
        if episode.get("source_path"):
            # Folder podcast: the audio lives in the user's folder, never here.
            episode["_original_present"] = Path(episode["source_path"]).is_file()
        else:
            episode["_original_present"] = (
                bool(episode.get("original_path"))
                and Path(episode["original_path"]).name in original_names
            )
        episode["_latest_job"] = latest_jobs.get(int(episode["id"]))
        episode["_can_review"] = episode["status"] in {"awaiting_review", "qa_failed"} or bool(episode.get("review_json"))
        episode["_busy"] = bool(
            episode["_latest_job"]
            and episode["_latest_job"]["state"] in {"queued", "running"}
        )
        episode["_batch_processable"] = (
            episode["status"] in BATCH_PROCESSABLE and not episode["_busy"]
        )

    art_url = rss.artwork_url(_base_url(cfg), podcast)
    if not art_url:
        art_url = f"/podcast-art/{podcast['slug']}.svg"
    retention = podcast.get("retention_days")
    return _render(
        request,
        "podcast.html",
        podcast=podcast,
        is_folder=(podcast.get("source_kind") or "rss") == "folder",
        source_path=podcast.get("source_path") or "",
        episodes=episodes,
        counts=counts,
        total=total,
        total_all=total_all,
        selected_count=selected_count,
        deleted_count=deleted_count,
        page=page,
        pages=max(1, (total + per_page - 1) // per_page),
        filter=filter_name,
        feed_url=rss.feed_url(_base_url(cfg), podcast),
        feed_path=rss.feed_path(podcast),
        art_url=art_url,
        qr_url=f"/qr/{podcast['slug']}.svg",
        retention=("" if retention is None else str(retention)),
        retention_options=["", "0", "7", "14", "30", "60", "90", "180", "365"],
        library_bytes=_dir_size(dirs["episodes"]),
        original_bytes=_dir_size(dirs["originals"]),
        tools_ready=_tools_ready(),
        labels_json=podcast.get("labels_json") or "[]",
        jobs=latest_jobs,
    )


@app.post("/podcasts/{slug}/settings")
async def podcast_settings(request: Request, slug: str):
    cfg, _session = security.require_admin(request)
    podcast = _podcast_or_404(slug)
    form = await request.form()
    labels_json = str(form.get("labels_json") or "[]").strip() or "[]"
    try:
        _validate_labels(labels_json)
    except ValueError as exc:
        return _redirect(f"/podcasts/{slug}", err=f"Labels: {exc}")
    try:
        poll_interval = max(5, int(str(form.get("poll_interval_minutes") or "30")))
    except ValueError:
        poll_interval = 30
    retention_value = str(form.get("retention_days") or "").strip()
    retention = None
    if retention_value:
        try:
            retention = max(0, int(retention_value))
        except ValueError:
            retention = None

    folder_notice = ""
    if (podcast.get("source_kind") or "rss") == "folder":
        raw_folder = str(form.get("source_path") or "").strip()
        if raw_folder and raw_folder != (podcast.get("source_path") or ""):
            try:
                new_root = folders.normalize_path(raw_folder, cfg)
            except folders.FolderError as exc:
                return _redirect(f"/podcasts/{slug}", err=f"Folder: {exc}")
            db.update_podcast(podcast["id"], source_path=str(new_root))
            podcast = db.get_podcast(podcast["id"]) or podcast
            moved = folders.remap_episodes(podcast, new_root)
            folder_notice = f" {moved} episode(s) re-pointed to the new folder."

    db.update_podcast(
        podcast["id"],
        title=str(form.get("title") or podcast["title"]).strip() or podcast["title"],
        description=str(form.get("description") or "") or None,
        author=str(form.get("author") or "") or None,
        language=str(form.get("language") or "auto"),
        category=str(form.get("category") or "") or None,
        ad_lang=str(form.get("ad_lang") or "auto"),
        ad_show_stop=str(form.get("ad_show_stop") or ""),
        labels_json=labels_json,
        poll_interval_minutes=poll_interval,
        retention_days=retention,
        auto_process=1 if form.get("auto_process") is not None else 0,
        auto_publish=1 if form.get("auto_publish") is not None else 0,
    )
    return _redirect(f"/podcasts/{slug}", msg="Settings saved." + folder_notice)


def _validate_labels(labels_json: str) -> None:
    try:
        rows = json.loads(labels_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON ({exc})") from exc
    if not isinstance(rows, list):
        raise ValueError("must be a JSON array of [regex, title] pairs")
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ValueError("each row must be [regex, title]")
        try:
            re.compile(str(row[0]))
        except re.error as exc:
            raise ValueError(f"bad regex {row[0]!r}: {exc}") from exc


@app.post("/podcasts/{slug}/poll")
def podcast_poll_now(request: Request, slug: str):
    cfg, _session = security.require_admin(request)
    podcast = _podcast_or_404(slug)
    db.enqueue_job("poll", podcast_id=podcast["id"], priority=5, max_attempts=2)
    return _redirect(f"/podcasts/{slug}", msg="Feed check queued.")


@app.post("/podcasts/{slug}/episodes/batch-process")
async def episodes_batch_process(request: Request, slug: str):
    cfg, _session = security.require_admin(request)
    podcast = _podcast_or_404(slug)
    form = await request.form()
    filter_name = _filter_name(form)
    accepted = 0
    skipped = 0
    for episode, latest in _selected_episodes(podcast, form):
        if episode is None:
            skipped += 1
            continue
        if (
            episode["status"] not in BATCH_PROCESSABLE
            or (latest and latest["state"] in {"queued", "running"})
        ):
            skipped += 1
            continue
        _resume_episode(cfg, podcast, episode)
        accepted += 1
    if not accepted:
        return _redirect(
            f"/podcasts/{slug}?filter={filter_name}",
            err="No selected episodes were eligible for processing.",
        )
    message = f"Queued {accepted} episode(s) for processing."
    if skipped:
        message += f" Skipped {skipped} ineligible episode(s)."
    return _redirect(f"/podcasts/{slug}?filter={filter_name}", msg=message)


@app.post("/podcasts/{slug}/episodes/{episode_id}/process")
def episode_process(request: Request, slug: str, episode_id: int):
    cfg, _session = security.require_admin(request)
    podcast = _podcast_or_404(slug)
    episode = _episode_for_podcast(podcast, episode_id)
    next_stage = _resume_episode(cfg, podcast, episode)
    return _redirect(f"/podcasts/{slug}", msg=f"Queued {next_stage}.")


def _selected_episodes(podcast: dict[str, Any], form: Any):
    """Yield (episode, latest_job) for the checked ids that belong to the show.

    Unknown ids and ids from another podcast yield (None, None) so the caller
    can count them as skipped.
    """
    for raw in form.getlist("episode_ids"):
        try:
            episode_id = int(str(raw))
        except (TypeError, ValueError):
            yield None, None
            continue
        try:
            episode = _episode_for_podcast(podcast, episode_id)
        except HTTPException:
            yield None, None
            continue
        yield episode, db.latest_job_for_episode(episode["id"])


def _batch_feedback(slug: str, filter_name: str, accepted: int, skipped: int, verb: str):
    if not accepted:
        return _redirect(
            f"/podcasts/{slug}?filter={filter_name}",
            err="No selected episodes were eligible or nothing was selected.",
        )
    message = f"{verb} {accepted} episode(s)."
    if skipped:
        message += f" Skipped {skipped} busy or invalid episode(s)."
    return _redirect(f"/podcasts/{slug}?filter={filter_name}", msg=message)


def _filter_name(form: Any) -> str:
    filter_name = str(form.get("filter") or "all")
    if filter_name not in {"all", "selected", "review", "failed", "published", "deleted"}:
        return "all"
    return filter_name


@app.post("/podcasts/{slug}/episodes/batch-reprocess")
async def episodes_batch_reprocess(request: Request, slug: str):
    """Restart the full pipeline (download → process → publish) for a selection."""
    cfg, _session = security.require_admin(request)
    podcast = _podcast_or_404(slug)
    form = await request.form()
    filter_name = _filter_name(form)
    accepted = skipped = 0
    for episode, latest in _selected_episodes(podcast, form):
        if episode is None:
            skipped += 1
            continue
        if latest and latest["state"] == "running":
            skipped += 1
            continue
        pipeline.reset_episode_for_reprocess(cfg, podcast, episode)
        accepted += 1
    return _batch_feedback(
        slug, filter_name, accepted, skipped, "Queued a full reprocess (download → process → publish) for"
    )


@app.post("/podcasts/{slug}/episodes/batch-delete")
async def episodes_batch_delete(request: Request, slug: str):
    """Delete a selection of episodes, their files and their queue entries."""
    cfg, _session = security.require_admin(request)
    podcast = _podcast_or_404(slug)
    form = await request.form()
    filter_name = _filter_name(form)
    accepted = skipped = 0
    for episode, latest in _selected_episodes(podcast, form):
        if episode is None:
            skipped += 1
            continue
        if latest and latest["state"] == "running":
            skipped += 1
            continue
        pipeline.delete_episode(cfg, podcast, episode)
        accepted += 1
    return _batch_feedback(slug, filter_name, accepted, skipped, "Deleted")


@app.post("/podcasts/{slug}/episodes/{episode_id}/restore")
def episode_restore(request: Request, slug: str, episode_id: int):
    cfg, _session = security.require_admin(request)
    podcast = _podcast_or_404(slug)
    episode = _episode_for_podcast(podcast, episode_id)
    if episode["status"] != "deleted":
        return _redirect(f"/podcasts/{slug}", err="This episode is not deleted.")
    db.update_episode(episode["id"], status="discovered", selected=0, error_message=None)
    return _redirect(
        f"/podcasts/{slug}",
        msg="Restored. Use Download and process to fetch and rebuild it.",
    )


@app.post("/podcasts/{slug}/episodes/{episode_id}/reprocess")
def episode_reprocess(request: Request, slug: str, episode_id: int):
    cfg, _session = security.require_admin(request)
    podcast = _podcast_or_404(slug)
    episode = _episode_for_podcast(podcast, episode_id)
    latest = db.latest_job_for_episode(episode_id)
    if latest and latest["state"] == "running":
        return _redirect(
            f"/podcasts/{slug}",
            err="This episode is being processed right now; try again when the job finishes.",
        )
    pipeline.reset_episode_for_reprocess(cfg, podcast, episode)
    return _redirect(f"/podcasts/{slug}", msg="Queued a full reprocess (download → process → publish).")


BATCH_PROCESSABLE = {"discovered", "downloaded", "skipped", "failed"}


def _resume_episode(cfg: dict[str, Any], podcast: dict[str, Any], episode: dict[str, Any]) -> str:
    dirs = config.episode_dirs(cfg, podcast["slug"])
    tag = pipeline.episode_tag(episode)
    ext = pipeline.original_extension(episode)
    original = pipeline.original_path(cfg, episode)
    clean = dirs["work"] / f"clean_{tag}{ext}"
    db.update_episode(episode["id"], selected=1, error_message=None)

    if not original or not original.is_file():
        db.update_episode(episode["id"], status="queued")
        db.enqueue_job("download", podcast_id=podcast["id"], episode_id=episode["id"], priority=100)
        return "download"
    transcript = dirs["work"] / f"tr_{tag}.json"
    if not transcript.is_file():
        db.update_episode(episode["id"], status="queued")
        db.enqueue_job("transcribe", podcast_id=podcast["id"], episode_id=episode["id"], priority=110, max_attempts=2)
        return "transcription"
    if not episode.get("cuts_json") and not episode.get("review_json"):
        db.update_episode(episode["id"], status="queued", llm_json=None)
        db.enqueue_job("detect", podcast_id=podcast["id"], episode_id=episode["id"], priority=90)
        return "detection"
    if episode.get("status") == "published":
        return "nothing (already published)"
    if episode.get("approved_cuts_json"):
        if clean.is_file():
            db.update_episode(episode["id"], status="qa")
            db.enqueue_job("qa", podcast_id=podcast["id"], episode_id=episode["id"], priority=88)
            return "QA"
        db.update_episode(episode["id"], status="approved")
        db.enqueue_job("cut", podcast_id=podcast["id"], episode_id=episode["id"], priority=85)
        return "cutting"
    db.update_episode(episode["id"], status="detecting", llm_json=None)
    db.enqueue_job("detect", podcast_id=podcast["id"], episode_id=episode["id"], priority=90)
    return "detection"


@app.get("/podcasts/{slug}/episodes/{episode_id}/review", response_class=HTMLResponse)
def review_page(request: Request, slug: str, episode_id: int):
    cfg, _session = security.require_admin(request)
    podcast = _podcast_or_404(slug)
    episode = _episode_for_podcast(podcast, episode_id)
    review = _review_for_episode(cfg, podcast, episode)
    dirs = config.episode_dirs(cfg, podcast["slug"])
    tag = pipeline.episode_tag(episode)
    ext = pipeline.original_extension(episode)
    original = pipeline.original_path(cfg, episode)
    clean = dirs["work"] / f"clean_{tag}{ext}"
    transcript = dirs["work"] / f"tr_{tag}.json"
    latest = db.latest_job_for_episode(episode_id)
    return _render(
        request,
        "review.html",
        podcast=podcast,
        episode=episode,
        review=review,
        cuts=(review or {}).get("cuts") or [],
        has_original=bool(original and original.is_file()),
        original_label=(str(original) if original else "not downloaded"),
        original_in_place=bool(episode.get("source_path")),
        has_clean_preview=clean.is_file(),
        has_transcript=transcript.is_file(),
        latest_job=latest,
        tools_ready=_tools_ready(),
    )


@app.post("/podcasts/{slug}/episodes/{episode_id}/approve")
async def review_approve(request: Request, slug: str, episode_id: int):
    cfg, _session = security.require_admin(request)
    podcast = _podcast_or_404(slug)
    episode = _episode_for_podcast(podcast, episode_id)
    form = await request.form()
    starts = form.getlist("start")
    ends = form.getlist("end")
    total = episode.get("duration_original")
    if not total:
        original = pipeline.original_path(cfg, episode)
        total = adapter.probe_duration(original) if original else None
    cuts: list[list[float | None]] = []
    for start_s, end_s in zip(starts, ends):
        start_s = str(start_s).strip()
        if not start_s:
            continue
        try:
            start = max(0.0, float(start_s))
            end = float(str(end_s).strip()) if str(end_s).strip() else None
        except ValueError:
            return _redirect(
                f"/podcasts/{slug}/episodes/{episode_id}/review",
                err="Cut ranges must be numbers.",
            )
        if total:
            start = min(start, float(total))
            end = min(end, float(total)) if end is not None else None
        if end is not None and end <= start:
            return _redirect(
                f"/podcasts/{slug}/episodes/{episode_id}/review",
                err="Each cut must end after it starts.",
            )
        cuts.append([round(start, 2), round(end, 2) if end is not None else None])
    transcript = config.episode_dirs(cfg, slug)["work"] / f"tr_{pipeline.episode_tag(episode)}.json"
    if not cuts and not episode.get("review_json") and not transcript.is_file():
        return _redirect(
            f"/podcasts/{slug}/episodes/{episode_id}/review",
            err="Run detection first, or add at least one cut range.",
        )
    cuts.sort(key=lambda c: c[0])
    try:
        llm_result = json.loads(episode.get("llm_json") or "null")
    except json.JSONDecodeError:
        llm_result = None
    review = build_review(
        transcript,
        cuts,
        labels_json=podcast.get("labels_json"),
        total_duration=total,
        llm_result=llm_result,
    )
    db.update_episode(
        episode_id,
        cuts_json=json.dumps(cuts),
        approved_cuts_json=json.dumps(cuts),
        review_json=json.dumps(review, ensure_ascii=False),
        confidence=review["confidence"],
        status="approved",
        error_message=None,
    )
    db.enqueue_job("cut", podcast_id=podcast["id"], episode_id=episode_id, priority=85)
    message = (
        "Episode approved; the original audio will be published unchanged because no ads were cut."
        if not cuts
        else "Cut approved and queued."
    )
    return _redirect(f"/podcasts/{slug}?filter=review", msg=message)


@app.post("/podcasts/{slug}/episodes/{episode_id}/skip")
def review_skip(request: Request, slug: str, episode_id: int):
    cfg, _session = security.require_admin(request)
    podcast = _podcast_or_404(slug)
    _episode_for_podcast(podcast, episode_id)
    db.update_episode(episode_id, status="skipped", approved_cuts_json=None, error_message="Skipped by user.")
    return _redirect(f"/podcasts/{slug}", msg="Episode skipped; it will not be published.")


@app.post("/podcasts/{slug}/episodes/{episode_id}/redetect")
def review_redetect(request: Request, slug: str, episode_id: int):
    cfg, _session = security.require_admin(request)
    podcast = _podcast_or_404(slug)
    episode = _episode_for_podcast(podcast, episode_id)
    dirs = config.episode_dirs(cfg, podcast["slug"])
    tag = pipeline.episode_tag(episode)
    if (dirs["work"] / f"tr_{tag}.json").is_file():
        db.update_episode(
            episode_id,
            status="detecting",
            approved_cuts_json=None,
            llm_json=None,
            error_message=None,
        )
        db.enqueue_job("detect", podcast_id=podcast["id"], episode_id=episode_id, priority=90)
    else:
        db.update_episode(
            episode_id,
            status="queued",
            approved_cuts_json=None,
            llm_json=None,
            error_message=None,
        )
        db.enqueue_job("transcribe", podcast_id=podcast["id"], episode_id=episode_id, priority=110, max_attempts=2)
    return _redirect(f"/podcasts/{slug}/episodes/{episode_id}/review", msg="Detection re-queued.")


@app.post("/podcasts/{slug}/episodes/{episode_id}/recut")
def review_recut(request: Request, slug: str, episode_id: int):
    cfg, _session = security.require_admin(request)
    podcast = _podcast_or_404(slug)
    episode = _episode_for_podcast(podcast, episode_id)
    if not episode.get("approved_cuts_json"):
        return _redirect(f"/podcasts/{slug}/episodes/{episode_id}/review", err="Approve cut ranges first.")
    db.update_episode(episode_id, status="approved", error_message=None)
    db.enqueue_job("cut", podcast_id=podcast["id"], episode_id=episode_id, priority=85)
    return _redirect(f"/podcasts/{slug}/episodes/{episode_id}/review", msg="Re-cut queued.")


@app.get("/admin/episodes/{episode_id}/transcript", response_class=PlainTextResponse)
def transcript_download(request: Request, episode_id: int):
    cfg, _session = security.require_admin(request)
    episode = db.get_episode(episode_id)
    if not episode:
        raise HTTPException(status_code=404)
    podcast = db.get_podcast(episode["podcast_id"])
    if not podcast:
        raise HTTPException(status_code=404)
    dirs = config.episode_dirs(cfg, podcast["slug"])
    path = dirs["work"] / f"tr_{pipeline.episode_tag(episode)}.json"
    if not path.is_file():
        return PlainTextResponse("Transcript not available yet.", status_code=404)
    return PlainTextResponse(adapter.transcript_text(path))


@app.api_route("/admin/media/episode/{episode_id}/original", methods=["GET", "HEAD"])
def admin_original(request: Request, episode_id: int):
    cfg, _session = security.require_admin(request)
    episode = db.get_episode(episode_id)
    if not episode:
        raise HTTPException(status_code=404)
    path = pipeline.original_path(cfg, episode)
    if not path or not path.is_file():
        raise HTTPException(status_code=404)
    return serve_audio(request, path, filename=path.name)


@app.api_route("/admin/media/episode/{episode_id}/clean", methods=["GET", "HEAD"])
def admin_clean(request: Request, episode_id: int):
    cfg, _session = security.require_admin(request)
    episode = db.get_episode(episode_id)
    if not episode:
        raise HTTPException(status_code=404)
    podcast = db.get_podcast(episode["podcast_id"])
    if not podcast:
        raise HTTPException(status_code=404)
    dirs = config.episode_dirs(cfg, podcast["slug"])
    path = dirs["work"] / f"clean_{pipeline.episode_tag(episode)}{pipeline.original_extension(episode)}"
    if not path.is_file():
        raise HTTPException(status_code=404)
    return serve_audio(request, path, filename=path.name)


# ---------------------------------------------------------------------------
# Public feeds / media / artwork


@app.api_route("/feed/{slug}/{token}.xml", methods=["GET", "HEAD"])
def public_feed(request: Request, slug: str, token: str):
    cfg = config.load_config()
    podcast = _require_public_token(db.get_podcast_by_slug(slug), token)
    xml, newest = rss.build_feed_xml(cfg, podcast, _base_url(cfg))
    etag = rss.feed_etag(xml)
    headers = {
        "ETag": etag,
        "Cache-Control": "private, max-age=0, must-revalidate",
    }
    if newest:
        headers["Last-Modified"] = email.utils.format_datetime(newest.astimezone(timezone.utc))
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(
        content=xml,
        media_type="application/rss+xml; charset=utf-8",
        headers=headers,
    )


@app.api_route("/media/{slug}/{token}/{filename:path}", methods=["GET", "HEAD"])
def public_media(request: Request, slug: str, token: str, filename: str):
    cfg = config.load_config()
    podcast = _require_public_token(db.get_podcast_by_slug(slug), token)
    dirs = config.episode_dirs(cfg, podcast["slug"])
    try:
        path = safe_join(dirs["episodes"], filename)
    except ValueError:
        raise HTTPException(status_code=404)
    if not path.is_file() or not is_audio_filename(path.name):
        raise HTTPException(status_code=404)
    return serve_audio(request, path, filename=path.name)


@app.api_route("/artwork/{slug}/{file}", methods=["GET", "HEAD"])
def public_artwork(request: Request, slug: str, file: str):
    token, dot, ext = file.rpartition(".")
    if not dot:
        raise HTTPException(status_code=404)
    podcast = _require_public_token(db.get_podcast_by_slug(slug), token)
    ext = "." + ext.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}:
        raise HTTPException(status_code=404)
    path = config.artwork_dir() / f"{slug}{ext}"
    if path.is_file():
        return serve_file(
            request,
            path,
            content_type=artwork.content_type_for(path),
            cache_control="private, max-age=86400",
        )
    svg = artwork.placeholder_svg(podcast["title"]).encode("utf-8")
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/podcast-art/{slug}.svg")
def podcast_art_placeholder(request: Request, slug: str):
    cfg, _session = security.require_admin(request)
    podcast = _podcast_or_404(slug)
    local = artwork.artwork_file(podcast)
    if local:
        return serve_file(
            request,
            local,
            content_type=artwork.content_type_for(local),
            cache_control="private, max-age=86400",
        )
    return Response(artwork.placeholder_svg(podcast["title"]), media_type="image/svg+xml")


@app.get("/qr/{slug}.svg")
def podcast_qr(request: Request, slug: str):
    cfg, _session = security.require_admin(request)
    podcast = _podcast_or_404(slug)
    import qrcode
    import qrcode.image.svg

    url = rss.feed_url(_base_url(cfg), podcast)
    image = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    return Response(
        buffer.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "private, max-age=300"},
    )


@app.get("/opml.xml")
def opml_export(request: Request):
    cfg, _session = security.require_admin(request)
    data = rss.build_opml(cfg, _base_url(cfg))
    return Response(
        data,
        media_type="text/x-opml; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="uninterupted.opml"'},
    )


# ---------------------------------------------------------------------------
# Queue and jobs


@app.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request):
    cfg, _session = security.require_admin(request)
    return _render(
        request,
        "queue.html",
        jobs=db.list_jobs(limit=200),
        counts=db.job_counts(),
        worker=worker_status(),
    )


@app.get("/queue/partial", response_class=HTMLResponse)
def queue_partial(request: Request):
    cfg, _session = security.require_admin(request)
    return _render(
        request,
        "partials/queue_table.html",
        jobs=db.list_jobs(limit=200),
        counts=db.job_counts(),
        worker=worker_status(),
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: int):
    cfg, _session = security.require_admin(request)
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404)
    log_text = ""
    if job.get("log_path"):
        path = Path(job["log_path"])
        if path.is_file():
            try:
                size = path.stat().st_size
                with open(path, "rb") as fh:
                    fh.seek(max(0, size - 150_000))
                    log_text = fh.read().decode("utf-8", errors="replace")
            except OSError:
                pass
    podcast = db.get_podcast(job["podcast_id"]) if job.get("podcast_id") else None
    episode = db.get_episode(job["episode_id"]) if job.get("episode_id") else None
    return _render(
        request,
        "job.html",
        job=job,
        podcast=podcast,
        episode=episode,
        log_text=log_text,
    )


@app.post("/jobs/{job_id}/retry")
def job_retry(request: Request, job_id: int):
    cfg, _session = security.require_admin(request)
    db.retry_job(job_id)
    return _redirect(f"/jobs/{job_id}", msg="Job re-queued.")


@app.post("/jobs/{job_id}/cancel")
def job_cancel(request: Request, job_id: int):
    cfg, _session = security.require_admin(request)
    db.cancel_job(job_id)
    return _redirect(f"/jobs/{job_id}", msg="Job canceled.")


# ---------------------------------------------------------------------------
# Settings


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    cfg, _session = security.require_admin(request)
    root = config.storage_root(cfg)
    try:
        disk = shutil.disk_usage(root if root.exists() else Path.home())
    except OSError:
        disk = None
    llm_opts = llm.settings(cfg)
    try:
        llm_discovery = llm.discover(cfg, timeout=3.0)
    except Exception as exc:  # The settings page must still render.
        llm_discovery = {
            "ok": False,
            "installed": False,
            "binary": None,
            "daemon_ok": False,
            "endpoint": llm.tags_endpoint(llm_opts["base_url"]),
            "current": llm_opts["model"],
            "models": [],
            "error": str(exc),
            "hints": [],
            "cloud": "ollama.com" in llm_opts["base_url"],
        }
    return _render(
        request,
        "settings.html",
        toolchain=toolchain.status(),
        storage_root=str(root),
        app_home=str(config.app_home()),
        disk=disk,
        library_bytes=_dir_size(config.podcasts_root(cfg), limit_files=50000),
        podcast_count=len(db.list_podcasts()),
        llm=llm_opts,
        llm_discovery=llm_discovery,
        llm_api_key_set=bool(llm_opts["api_key"]),
        relocate_pending=any(
            job["type"] == "relocate" and job["state"] in {"queued", "running"}
            for job in db.list_jobs(limit=50, active_only=True)
        ),
    )


@app.post("/settings/general")
async def settings_general(request: Request):
    cfg, _session = security.require_admin(request)
    form = await request.form()
    base_url = str(form.get("base_url") or "").strip().rstrip("/")
    if not re.match(r"^https?://[^\s]+$", base_url):
        return _redirect("/settings", err="Base URL must start with http:// or https://")
    try:
        port = int(str(form.get("port") or cfg.get("port") or 8090))
        min_free_gb = max(1, int(str(form.get("min_free_gb") or cfg.get("min_free_gb") or 5)))
        default_retention = str(form.get("default_retention_days") or "30").strip()
        default_retention_value: int | None = int(default_retention) if default_retention else None
    except ValueError:
        return _redirect("/settings", err="Numbers are invalid.")
    config.save_config(
        {
            **cfg,
            "base_url": base_url,
            "port": port,
            "min_free_gb": min_free_gb,
            "default_retention_days": default_retention_value,
        }
    )
    return _redirect("/settings", msg="Settings saved. A port change needs a restart.")


@app.post("/settings/llm/check")
def settings_llm_check(request: Request):
    cfg, _session = security.require_admin(request)
    try:
        found = llm.discover(cfg, timeout=3.0)
    except Exception as exc:
        return _redirect("/settings", err=f"Ollama check failed: {exc}")
    if found["ok"]:
        return _redirect(
            "/settings",
            msg=f"Ollama is ready; {len(found['models'])} model(s) available.",
        )
    reason = found.get("error") or "the Ollama daemon did not answer"
    return _redirect("/settings", err=f"Ollama check failed: {reason}")


@app.post("/settings/llm")
async def settings_llm(request: Request):
    cfg, _session = security.require_admin(request)
    form = await request.form()
    model = str(form.get("llm_model") or "").strip()
    base_url = str(form.get("llm_base_url") or "").strip().rstrip("/")
    if not model:
        return _redirect("/settings", err="Ollama model name is required.")
    if not re.match(r"^https?://[^\s]+$", base_url):
        return _redirect("/settings", err="Ollama base URL must start with http:// or https://")
    try:
        min_confidence = float(str(form.get("llm_min_confidence") or "0.72"))
    except ValueError:
        return _redirect("/settings", err="LLM confidence must be a number.")
    min_confidence = max(0.1, min(0.99, min_confidence))
    api_key = str(form.get("llm_api_key") or "").strip()
    if form.get("llm_api_key_clear") is not None:
        api_key = ""
    elif not api_key:
        api_key = str(cfg.get("llm_api_key") or "")
    config.update_config(
        llm_enabled=1,
        llm_model=model,
        llm_base_url=base_url,
        llm_api_key=api_key,
        llm_min_confidence=min_confidence,
    )
    return _redirect("/settings", msg="AI ad detection settings saved.")


@app.post("/settings/llm/test")
def settings_llm_test(request: Request):
    cfg, _session = security.require_admin(request)
    try:
        meta = llm.test_connection(cfg)
    except llm.LLMError as exc:
        return _redirect("/settings", err=f"Ollama test failed: {exc}")
    return _redirect(
        "/settings",
        msg=f"Ollama replied with {meta.get('model')} in {meta.get('seconds')}s.",
    )


@app.post("/settings/password")
async def settings_password(request: Request):
    cfg, _session = security.require_admin(request)
    form = await request.form()
    current = str(form.get("current_password") or "")
    new = str(form.get("new_password") or "")
    confirm = str(form.get("confirm_password") or "")
    if not security.verify_password(current, cfg.get("password_hash", "")):
        return _redirect("/settings", err="Current password is wrong.")
    if len(new) < 8:
        return _redirect("/settings", err="New password must be at least 8 characters.")
    if new != confirm:
        return _redirect("/settings", err="New passwords do not match.")
    config.update_config(password_hash=security.hash_password(new))
    return _redirect("/settings", msg="Password changed.")


@app.post("/settings/storage")
async def settings_storage(request: Request):
    cfg, _session = security.require_admin(request)
    form = await request.form()
    new_root_s = str(form.get("storage_root") or "").strip()
    delete_old = form.get("delete_old") is not None
    if not new_root_s:
        return _redirect("/settings", err="Enter a storage folder.")
    new_root = Path(new_root_s).expanduser()
    if not new_root.is_absolute():
        return _redirect("/settings", err="Storage folder must be an absolute path.")
    new_root = new_root.resolve()
    old_root = config.storage_root(cfg).resolve()
    if new_root == old_root:
        return _redirect("/settings", msg="Storage folder is unchanged.")
    if new_root.is_relative_to(old_root) or old_root.is_relative_to(new_root):
        return _redirect("/settings", err="The new folder must not be inside the current library (or contain it).")
    if any(
        job["type"] == "relocate" and job["state"] in {"queued", "running"}
        for job in db.list_jobs(limit=50, active_only=True)
    ):
        return _redirect("/settings", err="A library move is already queued or running.")
    try:
        new_root.mkdir(parents=True, exist_ok=True)
        probe = new_root / ".uninterupted-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        return _redirect("/settings", err=f"Cannot write to {new_root}: {exc}")

    podcasts_dir = config.podcasts_root(cfg)
    has_library = podcasts_dir.exists() and any(podcasts_dir.iterdir())
    if not has_library:
        config.update_config(storage_root=str(new_root))
        config.ensure_storage_dirs()
        return _redirect("/settings", msg=f"Storage folder changed to {new_root}.")

    db.enqueue_job(
        "relocate",
        payload={"old_root": str(old_root), "new_root": str(new_root), "delete_old": delete_old},
        priority=5,
        max_attempts=1,
        dedupe=True,
    )
    return _redirect("/queue", msg="Library move queued. The old folder stays until the copy finishes.")


@app.post("/settings/toolchain/install")
def settings_toolchain_install(request: Request):
    cfg, _session = security.require_admin(request)
    toolchain.start_install()
    return _redirect("/settings", msg="Toolchain installation running…")


@app.post("/settings/toolchain/recheck")
def settings_toolchain_recheck(request: Request):
    cfg, _session = security.require_admin(request)
    state = toolchain.recheck()
    msg = "Toolchain ready." if state["tools"]["ready"] else "Toolchain is still incomplete."
    return _redirect("/settings", msg=msg)


@app.get("/settings/toolchain/status")
def settings_toolchain_status(request: Request):
    cfg, _session = security.require_admin(request)
    return JSONResponse(toolchain.status())


# ---------------------------------------------------------------------------
# Health


@app.get("/health")
def health():
    cfg = config.load_config()
    worker = worker_status()
    return {
        "status": "ok",
        "version": __version__,
        "setup_complete": bool(cfg.get("setup_complete")),
        "worker_alive": bool(worker.get("alive")),
        "worker_state": worker.get("state"),
        "jobs": db.job_counts(),
        "toolchain_ready": _tools_ready(),
    }


@app.get("/favicon.ico")
def favicon():
    return RedirectResponse("/static/favicon.svg", status_code=307)
