from __future__ import annotations

import email.utils
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import artwork, config, db
from .paths import (
    display_title_from_filename,
    human_duration,
    is_audio_filename,
)

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"

ET.register_namespace("itunes", ITUNES_NS)
ET.register_namespace("atom", ATOM_NS)
ET.register_namespace("content", CONTENT_NS)


def feed_path(podcast: dict[str, Any]) -> str:
    return f"/feed/{podcast['slug']}/{podcast['feed_token']}.xml"


def feed_url(base_url: str, podcast: dict[str, Any]) -> str:
    return base_url.rstrip("/") + feed_path(podcast)


def media_url(base_url: str, podcast: dict[str, Any], filename: str) -> str:
    return (
        base_url.rstrip("/")
        + f"/media/{podcast['slug']}/{podcast['feed_token']}/"
        + quote(filename, safe="")
    )


def artwork_url(base_url: str, podcast: dict[str, Any]) -> str | None:
    local = artwork.artwork_file(podcast)
    if local:
        return (
            base_url.rstrip("/")
            + f"/artwork/{podcast['slug']}/{podcast['feed_token']}{local.suffix}"
        )
    if podcast.get("artwork_url"):
        return str(podcast["artwork_url"])
    return None


def _rfc2822(value: str | None) -> str:
    if value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return email.utils.format_datetime(dt.astimezone(timezone.utc))
        except ValueError:
            pass
    return email.utils.format_datetime(datetime.now(timezone.utc))


def _iso_from_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _mp3_duration(path: Path) -> float | None:
    """Cheap MP3 duration estimate for hand-dropped files (no ffprobe needed)."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(65536)
    except OSError:
        return None
    if len(head) < 10:
        return None
    audio_start = 0
    if head[:3] == b"ID3":
        audio_start = 10 + (
            ((head[6] & 0x7F) << 21)
            | ((head[7] & 0x7F) << 14)
            | ((head[8] & 0x7F) << 7)
            | (head[9] & 0x7F)
        )
    rates_mpeg1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
    rates_mpeg2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]
    for i in range(max(0, audio_start), min(len(head) - 4, audio_start + 8192)):
        if head[i] == 0xFF and (head[i + 1] & 0xE0) == 0xE0:
            version = (head[i + 1] >> 3) & 0x03
            layer = (head[i + 1] >> 1) & 0x03
            bitrate_index = (head[i + 2] >> 4) & 0x0F
            if layer != 1 or bitrate_index in (0, 15):
                continue
            rates = rates_mpeg1 if version == 3 else rates_mpeg2
            bitrate = rates[bitrate_index] * 1000
            if bitrate:
                return max(0.0, (size - audio_start) * 8 / bitrate)
    return None


def _duration_for(path: Path, episode: dict[str, Any] | None) -> float | None:
    if episode and episode.get("clean_duration"):
        return float(episode["clean_duration"])
    if path.suffix.lower() == ".mp3":
        return _mp3_duration(path)
    return None


def _file_episode(
    cfg: dict[str, Any],
    podcast: dict[str, Any],
    path: Path,
    episode: dict[str, Any] | None,
) -> dict[str, Any]:
    stat = path.stat()
    if episode:
        published = episode.get("published_at") or _iso_from_timestamp(stat.st_mtime)
        return {
            "filename": path.name,
            "path": path,
            "size": stat.st_size,
            "duration": _duration_for(path, episode),
            "title": episode.get("title") or display_title_from_filename(path.name),
            "description": episode.get("description") or episode.get("content_html"),
            "content_html": episode.get("content_html"),
            "published_at": published,
            "link": episode.get("link") or podcast.get("site_url"),
            "comments_url": episode.get("comments_url"),
            "guid": episode.get("clean_guid")
            or f"uninterupted:{podcast['slug']}:{episode['guid']}:clean",
            "episode": _json_int(episode.get("item_json"), "itunes_episode"),
            "season": _json_int(episode.get("item_json"), "itunes_season"),
            "explicit": bool(episode.get("explicit")),
            "author": podcast.get("author"),
            "db_episode": episode,
        }
    return {
        "filename": path.name,
        "path": path,
        "size": stat.st_size,
        "duration": _duration_for(path, None),
        "title": display_title_from_filename(path.name),
        "description": None,
        "content_html": None,
        "published_at": _iso_from_timestamp(stat.st_mtime),
        "link": podcast.get("site_url"),
        "comments_url": None,
        "guid": f"uninterupted:{podcast['slug']}:file:{quote(path.name, safe='')}",
        "episode": None,
        "season": None,
        "explicit": bool(podcast.get("explicit")),
        "author": podcast.get("author"),
        "db_episode": None,
    }


def _json_int(item_json: str | None, key: str) -> int | None:
    if not item_json:
        return None
    try:
        import json

        data = json.loads(item_json)
        value = data.get(key)
        return int(value) if value is not None else None
    except (ValueError, TypeError, ImportError):
        return None


def collect_episodes(cfg: dict[str, Any], podcast: dict[str, Any]) -> list[dict[str, Any]]:
    dirs = config.ensure_episode_dirs(cfg, podcast["slug"])
    rows = {
        row["clean_filename"]: row
        for row in db.episode_feed_items(podcast["id"])
        if row.get("clean_filename")
    }
    files = [
        entry
        for entry in dirs["episodes"].iterdir()
        if entry.is_file() and is_audio_filename(entry.name) and not entry.name.startswith(".")
    ]
    records = []
    claimed = set()
    for path in files:
        row = rows.get(path.name)
        if row:
            claimed.add(path.name)
        records.append(_file_episode(cfg, podcast, path, row))
    # A DB row whose file is gone is not published.
    records.sort(key=lambda r: (r.get("published_at") or "", r["filename"]), reverse=True)
    return records


def _text(parent: ET.Element, tag: str, value: Any, **attrib: str) -> ET.Element | None:
    if value is None:
        return None
    el = ET.SubElement(parent, tag, attrib)
    el.text = str(value)
    return el


def build_feed_xml(
    cfg: dict[str, Any], podcast: dict[str, Any], base_url: str
) -> tuple[bytes, datetime | None]:
    records = collect_episodes(cfg, podcast)
    channel_title = podcast["title"]
    if not channel_title.rstrip().lower().endswith("(no ads)"):
        channel_title = channel_title.rstrip() + " (no ads)"
    art = artwork_url(base_url, podcast)

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    _text(channel, "title", channel_title)
    _text(channel, "link", podcast.get("site_url") or base_url)
    _text(channel, "description", podcast.get("description") or podcast["title"])
    _text(channel, "language", podcast.get("language") if podcast.get("language") in {"fr", "en", "es", "de", "it", "pt", "nl"} else "en")
    _text(channel, "generator", "Un-Interupted")
    _text(channel, "copyright", None)
    ET.SubElement(
        channel,
        f"{{{ATOM_NS}}}link",
        {
            "href": feed_url(base_url, podcast),
            "rel": "self",
            "type": "application/rss+xml",
        },
    )
    _text(channel, f"{{{ITUNES_NS}}}author", podcast.get("author") or podcast["title"])
    _text(channel, f"{{{ITUNES_NS}}}explicit", "true" if podcast.get("explicit") else "false")
    _text(channel, f"{{{ITUNES_NS}}}type", "episodic")
    if podcast.get("category"):
        ET.SubElement(channel, f"{{{ITUNES_NS}}}category", {"text": str(podcast["category"])})
    owner = ET.SubElement(channel, f"{{{ITUNES_NS}}}owner")
    _text(owner, f"{{{ITUNES_NS}}}name", podcast.get("author") or podcast["title"])

    if art:
        ET.SubElement(channel, f"{{{ITUNES_NS}}}image", {"href": art})
        image = ET.SubElement(channel, "image")
        _text(image, "url", art)
        _text(image, "title", channel_title)
        _text(image, "link", podcast.get("site_url") or base_url)

    newest = None
    for rec in records:
        try:
            dt = datetime.fromisoformat(rec["published_at"])
            if newest is None or dt > newest:
                newest = dt
        except (ValueError, TypeError):
            pass
    _text(channel, "lastBuildDate", _rfc2822(newest.isoformat() if newest else None))

    for rec in records:
        item = ET.SubElement(channel, "item")
        _text(item, "title", rec["title"])
        _text(item, f"{{{ITUNES_NS}}}title", rec["title"])
        description = rec.get("description") or rec.get("content_html") or rec["title"]
        _text(item, "description", description)
        if rec.get("content_html"):
            _text(item, f"{{{CONTENT_NS}}}encoded", rec["content_html"])
        if rec.get("link"):
            _text(item, "link", rec["link"])
        if rec.get("comments_url"):
            _text(item, "comments", rec["comments_url"])
        _text(item, "pubDate", _rfc2822(rec["published_at"]))
        _text(item, "guid", rec["guid"], isPermaLink="false")
        ET.SubElement(
            item,
            "enclosure",
            {
                "url": media_url(base_url, podcast, rec["filename"]),
                "length": str(rec["size"]),
                "type": _content_type(rec["filename"]),
            },
        )
        _text(item, f"{{{ITUNES_NS}}}author", rec.get("author") or podcast.get("author") or podcast["title"])
        _text(item, f"{{{ITUNES_NS}}}explicit", "true" if rec.get("explicit") else "false")
        if rec.get("duration"):
            _text(item, f"{{{ITUNES_NS}}}duration", human_duration(rec["duration"]))
        if rec.get("episode"):
            _text(item, f"{{{ITUNES_NS}}}episode", rec["episode"])
        if rec.get("season"):
            _text(item, f"{{{ITUNES_NS}}}season", rec["season"])
        if art:
            ET.SubElement(item, f"{{{ITUNES_NS}}}image", {"href": art})

    xml = ET.tostring(rss, encoding="utf-8", xml_declaration=True)
    return xml, newest


def _content_type(filename: str) -> str:
    from .paths import audio_type_for

    return audio_type_for(filename)


def feed_etag(xml: bytes) -> str:
    return '"' + hashlib.sha1(xml).hexdigest() + '"'


def build_opml(cfg: dict[str, Any], base_url: str) -> bytes:
    opml = ET.Element("opml", {"version": "2.0"})
    head = ET.SubElement(opml, "head")
    _text(head, "title", "Un-Interupted podcasts")
    _text(head, "dateCreated", email.utils.format_datetime(datetime.now(timezone.utc)))
    body = ET.SubElement(opml, "body")
    group = ET.SubElement(body, "outline", {"text": "Un-Interupted"})
    for podcast in db.list_podcasts():
        ET.SubElement(
            group,
            "outline",
            {
                "type": "rss",
                "text": podcast["title"] + " (no ads)",
                "title": podcast["title"] + " (no ads)",
                "xmlUrl": feed_url(base_url, podcast),
                "htmlUrl": podcast.get("site_url") or "",
            },
        )
    return ET.tostring(opml, encoding="utf-8", xml_declaration=True)
