from __future__ import annotations

import calendar
import hashlib
import secrets
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import feedparser
import httpx
from dateutil import parser as date_parser

from . import __version__, config, db
from .paths import is_audio_filename, parse_duration

USER_AGENT = (
    f"Un-Interupted/{__version__} (personal podcast proxy; "
    "+https://github.com/uninterupted)"
)
MAX_FEED_BYTES = 40 * 1024 * 1024


class FeedError(RuntimeError):
    pass


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, time.struct_time):
        return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc).isoformat()
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = date_parser.parse(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


def _jsonable(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, time.struct_time):
        return _iso(value)
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, dict):
        return {
            str(k): _jsonable(v, depth + 1)
            for k, v in value.items()
            if not str(k).startswith("_")
        }
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, depth + 1) for v in value]
    return str(value)


def _first(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _image_url(entry: Any) -> str | None:
    image = entry.get("image")
    if isinstance(image, dict) and image.get("href"):
        return str(image["href"])
    itunes_image = entry.get("itunes_image")
    if isinstance(itunes_image, dict) and itunes_image.get("href"):
        return str(itunes_image["href"])
    if isinstance(itunes_image, str) and itunes_image:
        return itunes_image
    thumbs = entry.get("media_thumbnail")
    thumb = _first(thumbs)
    if isinstance(thumb, dict) and thumb.get("url"):
        return str(thumb["url"])
    return None


def _enclosure(entry: Any) -> dict[str, Any]:
    enclosures = entry.get("enclosures") or []
    candidates = []
    for enc in enclosures:
        if not isinstance(enc, dict):
            continue
        href = enc.get("href") or enc.get("url")
        if not href:
            continue
        ctype = str(enc.get("type") or "")
        candidates.append((href, ctype, enc.get("length")))
    # Prefer audio enclosures, then any known audio extension.
    for href, ctype, length in candidates:
        if ctype.lower().startswith("audio/"):
            return {"url": str(href), "type": ctype or None, "length": _int(length)}
    for href, ctype, length in candidates:
        if is_audio_filename(str(href)):
            return {"url": str(href), "type": ctype or None, "length": _int(length)}
    if candidates:
        href, ctype, length = candidates[0]
        return {"url": str(href), "type": ctype or None, "length": _int(length)}
    # media:content fallback
    media = _first(entry.get("media_content"))
    if isinstance(media, dict) and media.get("url"):
        return {
            "url": str(media["url"]),
            "type": media.get("type"),
            "length": _int(media.get("fileSize") or media.get("length")),
        }
    return {"url": "", "type": None, "length": None}


def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _category(entry: Any) -> str | None:
    itunes = entry.get("itunes_category")
    if isinstance(itunes, dict):
        text = itunes.get("text") or itunes.get("term")
        if text:
            return str(text)
    if isinstance(itunes, (list, tuple)) and itunes:
        first = _first(itunes)
        if isinstance(first, dict):
            return str(first.get("text") or first.get("term") or "") or None
    tags = entry.get("tags") or []
    for tag in tags:
        if isinstance(tag, dict) and tag.get("term"):
            return str(tag["term"])
    return None


def _explicit(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"yes", "true", "explicit", "1"}


def _guid(entry: Any) -> str:
    value = entry.get("id") or entry.get("guid") or entry.get("link")
    if value:
        return str(value)
    fingerprint = "|".join(
        [
            str(entry.get("title") or ""),
            str(entry.get("published") or ""),
            str(_enclosure(entry).get("url") or ""),
        ]
    )
    return "sha1:" + hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()


def _entry(item: Any) -> dict[str, Any]:
    enc = _enclosure(item)
    content = item.get("content") or []
    content_html = None
    if content and isinstance(content, list) and isinstance(content[0], dict):
        content_html = content[0].get("value")
    description = item.get("summary") or item.get("description") or content_html
    published = (
        item.get("published_parsed")
        or item.get("updated_parsed")
        or item.get("published")
        or item.get("updated")
    )
    url = str(enc.get("url") or "")
    title = str(item.get("title") or "Untitled episode").strip()
    return {
        "guid": _guid(item),
        "title": title,
        "description": str(description) if description else None,
        "content_html": str(content_html) if content_html else None,
        "link": str(item.get("link")) if item.get("link") else None,
        "comments_url": str(item.get("comments")) if item.get("comments") else None,
        "published_at": _iso(published),
        "original_url": url,
        "enclosure_type": enc.get("type")
        or ("audio/mpeg" if url else None),
        "enclosure_length": enc.get("length"),
        "duration": parse_duration(item.get("itunes_duration")),
        "artwork_url": _image_url(item),
        "author": str(item.get("author") or item.get("itunes_author") or "") or None,
        "explicit": _explicit(item.get("itunes_explicit")),
        "episode": _int(item.get("itunes_episode")),
        "season": _int(item.get("itunes_season")),
        "item_json": _jsonable(item),
    }


def fetch_feed(
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout: float = 45,
) -> dict[str, Any]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(connect=20, read=timeout, write=20, pool=20),
        ) as client:
            resp = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise FeedError(f"Could not fetch feed: {exc}") from exc

    if resp.status_code == 304:
        return {
            "status": 304,
            "content": None,
            "etag": etag,
            "last_modified": last_modified,
        }
    if resp.status_code >= 400:
        raise FeedError(f"Feed returned HTTP {resp.status_code}")
    if len(resp.content) > MAX_FEED_BYTES:
        raise FeedError("Feed is unexpectedly large")
    return {
        "status": resp.status_code,
        "content": resp.content,
        "etag": resp.headers.get("etag"),
        "last_modified": resp.headers.get("last-modified"),
    }


def parse_feed(content: bytes, source_url: str = "") -> dict[str, Any]:
    try:
        parsed = feedparser.parse(content)
    except Exception as exc:  # feedparser is very tolerant; this is a safety net.
        raise FeedError(f"Could not parse feed: {exc}") from exc

    feed = parsed.feed or {}
    if not feed and not parsed.entries:
        detail = ""
        if getattr(parsed, "bozo", False) and getattr(parsed, "bozo_exception", None):
            detail = f" ({parsed.bozo_exception})"
        raise FeedError(f"Not a valid RSS/Atom feed{detail}")

    channel_title = str(feed.get("title") or "Untitled podcast").strip()
    description = feed.get("description") or feed.get("subtitle")
    artwork = _image_url(feed)
    language = feed.get("language") or (feed.get("feed", {}) or {}).get("lang")
    if language:
        language = str(language).split("-")[0].lower()
    else:
        language = "auto"
    site_url = feed.get("link")
    if site_url:
        site_url = urljoin(source_url or "", str(site_url))
    channel = {
        "title": channel_title,
        "site_url": site_url,
        "description": str(description) if description else None,
        "author": str(feed.get("author") or feed.get("itunes_author") or "") or None,
        "owner_name": str(
            (feed.get("itunes_owner") or {}).get("name")
            if isinstance(feed.get("itunes_owner"), dict)
            else ""
        )
        or None,
        "language": language,
        "category": _category(feed) or "Society & Culture",
        "explicit": _explicit(feed.get("itunes_explicit")),
        "artwork_url": artwork,
        "copyright": str(feed.get("copyright") or "") or None,
        "link": site_url,
        "item_count": len(parsed.entries),
    }

    entries = [_entry(item) for item in parsed.entries]
    # De-duplicate by GUID, preserving order.
    seen: set[str] = set()
    unique = []
    for item in entries:
        if item["guid"] in seen:
            continue
        seen.add(item["guid"])
        unique.append(item)

    return {
        "channel": channel,
        "entries": unique,
        "bozo": bool(getattr(parsed, "bozo", False)),
        "bozo_exception": str(getattr(parsed, "bozo_exception", "") or ""),
        "version": parsed.version,
    }


def preview(url: str, limit: int = 8) -> dict[str, Any]:
    fetched = fetch_feed(url)
    parsed = parse_feed(fetched["content"] or b"", url)
    parsed["entries_preview"] = parsed["entries"][:limit]
    parsed["source_url"] = url
    return parsed


def parse_opml(content: bytes | str) -> list[dict[str, str]]:
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise FeedError(f"Invalid OPML: {exc}") from exc
    feeds = []
    seen = set()
    for outline in root.iter("outline"):
        xml_url = outline.get("xmlUrl") or outline.get("xmlurl")
        if not xml_url or xml_url in seen:
            continue
        seen.add(xml_url)
        feeds.append(
            {
                "title": outline.get("text") or outline.get("title") or xml_url,
                "xml_url": xml_url,
                "html_url": outline.get("htmlUrl") or outline.get("htmlurl") or "",
            }
        )
    return feeds


def _new_episode_should_auto_select(podcast: dict[str, Any]) -> bool:
    return bool(podcast.get("auto_process"))


def poll_podcast(podcast_id: int) -> dict[str, Any]:
    """Conditional GET, upsert all feed items, enqueue the new ones if enabled."""
    podcast = db.get_podcast(podcast_id)
    if not podcast:
        raise FeedError("Podcast no longer exists")

    fetched = fetch_feed(
        podcast["feed_url"],
        etag=podcast.get("last_etag"),
        last_modified=podcast.get("last_modified"),
    )
    if fetched["status"] == 304:
        db.update_podcast(
            podcast_id, last_polled_at=db.now_iso(), last_error=None
        )
        return {"status": "not-modified", "new": 0, "updated": 0}

    parsed = parse_feed(fetched["content"] or b"", podcast["feed_url"])
    channel = parsed["channel"]
    updates: dict[str, Any] = {
        "last_polled_at": db.now_iso(),
        "last_etag": fetched.get("etag"),
        "last_modified": fetched.get("last_modified"),
        "last_error": None,
    }
    # Fill missing channel metadata; never overwrite a deliberate title edit.
    for key in ("description", "author", "category", "artwork_url", "site_url"):
        if not podcast.get(key) and channel.get(key):
            updates[key] = channel[key]
    if podcast.get("language") in (None, "", "auto") and channel.get("language") not in (None, "auto"):
        updates["language"] = channel["language"]
    db.update_podcast(podcast_id, **updates)
    refreshed = db.get_podcast(podcast_id)
    if refreshed and not refreshed.get("artwork_path") and refreshed.get("artwork_url"):
        try:
            from . import artwork

            artwork.ensure_artwork(config.load_config(), refreshed)
        except Exception:
            pass

    new_items = []
    updated_items = 0
    for item in parsed["entries"]:
        episode, created = db.upsert_feed_episode(podcast_id, item)
        if created:
            new_items.append(episode)
        else:
            updated_items += 1

    enqueued = 0
    if _new_episode_should_auto_select(podcast):
        for episode in new_items:
            if not episode.get("original_url"):
                continue
            db.select_episode(episode["id"], "queued")
            db.enqueue_job(
                "download",
                podcast_id=podcast_id,
                episode_id=episode["id"],
                priority=100,
            )
            enqueued += 1

    return {
        "status": "ok",
        "new": len(new_items),
        "updated": updated_items,
        "enqueued": enqueued,
        "entries": len(parsed["entries"]),
    }


def add_podcast_from_feed(
    url: str,
    *,
    backfill: int | str = 3,
    auto_process: bool = True,
    auto_publish: bool = False,
    title_override: str | None = None,
    poll_interval_minutes: int = 30,
    retention_days: int | None = None,
) -> dict[str, Any]:
    """Create a podcast and seed its episode list. Downloads are enqueued here."""
    existing = db.get_podcast_by_feed_url(url)
    if existing:
        raise FeedError(f"This feed is already added ({existing['title']}).")

    parsed = preview(url, limit=0)
    channel = parsed["channel"]
    from . import artwork

    cfg = config.load_config()
    if retention_days is None:
        configured = cfg.get("default_retention_days", 30)
        retention_days = int(configured) if configured not in (None, "", "forever") else None
    title = (title_override or channel["title"]).strip() or channel["title"]
    podcast = db.create_podcast(
        {
            "title": title,
            "feed_url": url,
            "site_url": channel.get("site_url"),
            "description": channel.get("description"),
            "author": channel.get("author"),
            "language": channel.get("language") or "auto",
            "category": channel.get("category"),
            "explicit": channel.get("explicit"),
            "artwork_url": channel.get("artwork_url"),
            "labels_json": "[]",
            "ad_show_stop": "",
            "ad_lang": "auto",
            "auto_process": auto_process,
            "auto_publish": auto_publish,
            "backfill_limit": 3 if backfill == "all" else int(backfill or 0),
            "poll_interval_minutes": poll_interval_minutes,
            "retention_days": retention_days,
            "feed_token": secrets.token_urlsafe(18),
            "last_polled_at": db.now_iso(),
        }
    )
    config.ensure_episode_dirs(cfg, podcast["slug"])

    stored = []
    for item in parsed["entries"]:
        episode, _ = db.upsert_feed_episode(podcast["id"], item)
        stored.append(episode)
    stored.sort(key=lambda e: e.get("published_at") or "", reverse=True)

    if backfill == "all":
        selected = [e for e in stored if e.get("original_url")]
    else:
        selected = [e for e in stored if e.get("original_url")][: int(backfill or 0)]

    for episode in selected:
        db.select_episode(episode["id"], "queued")
        db.enqueue_job(
            "download",
            podcast_id=podcast["id"],
            episode_id=episode["id"],
            priority=100,
        )

    # Best-effort local artwork (never fatal).
    try:
        artwork.ensure_artwork(cfg, podcast)
    except Exception:
        pass

    podcast = db.get_podcast(podcast["id"]) or podcast
    podcast["_seeded"] = len(stored)
    podcast["_selected"] = len(selected)
    return podcast
