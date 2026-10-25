import xml.etree.ElementTree as ET

from app import config, db, rss


ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
ATOM = "{http://www.w3.org/2005/Atom}"


def test_feed_is_valid_and_uses_stable_metadata(app_env):
    cfg = config.load_config()
    podcast = db.create_podcast(
        {
            "title": "Test Show",
            "feed_url": "https://example.com/feed.xml",
            "site_url": "https://example.com",
            "description": "A show & tests",
            "author": "Tester",
            "language": "en",
            "category": "Comedy",
            "explicit": False,
            "labels_json": "[]",
            "feed_token": "token-abc",
            "last_polled_at": db.now_iso(),
        }
    )
    episode, _ = db.upsert_feed_episode(
        podcast["id"],
        {
            "guid": "episode-guid",
            "title": "Episode One",
            "description": "<p>Hello &amp; welcome</p>",
            "content_html": "<p>Hello</p>",
            "published_at": "2026-10-20T12:00:00+00:00",
            "original_url": "https://example.com/one.mp3",
            "enclosure_type": "audio/mpeg",
            "enclosure_length": 10,
            "duration": 123.0,
            "item_json": {"itunes_episode": 7},
        },
    )
    dirs = config.ensure_episode_dirs(cfg, podcast["slug"])
    clean = dirs["episodes"] / "2026-10-20 - episode-one - abc12345 (no ads).mp3"
    clean.write_bytes(b"ID3" + b"x" * 5000)
    db.update_episode(
        episode["id"],
        status="published",
        clean_filename=clean.name,
        clean_guid=f"uninterupted:{podcast['slug']}:episode-guid:clean",
        clean_duration=123.4,
        clean_size=clean.stat().st_size,
    )

    podcast = db.get_podcast(podcast["id"])
    xml, newest = rss.build_feed_xml(cfg, podcast, "http://testserver:8090")
    root = ET.fromstring(xml)
    channel = root.find("channel")
    assert root.tag == "rss"
    assert channel.findtext("title") == "Test Show (no ads)"
    assert channel.findtext("language") == "en"
    assert channel.findtext(f"{ITUNES}explicit") == "false"
    assert channel.findtext(f"{ITUNES}type") == "episodic"
    self_link = channel.find(f"{ATOM}link")
    assert self_link is not None
    assert self_link.attrib["href"].startswith("http://testserver:8090/feed/")

    item = channel.find("item")
    assert item is not None
    assert item.findtext("title") == "Episode One"
    assert item.findtext("guid") == f"uninterupted:{podcast['slug']}:episode-guid:clean"
    assert item.findtext("guid").startswith("uninterupted:")
    assert item.findtext(f"{ITUNES}duration") == "2:03"
    assert item.findtext(f"{ITUNES}episode") == "7"
    enclosure = item.find("enclosure")
    assert enclosure is not None
    assert "token-abc" in enclosure.attrib["url"]
    assert enclosure.attrib["length"] == str(clean.stat().st_size)
    assert enclosure.attrib["type"] == "audio/mpeg"
    assert newest is not None


def test_feed_scans_manually_dropped_files(app_env):
    cfg = config.load_config()
    podcast = db.create_podcast(
        {
            "title": "Manual Show",
            "feed_url": "https://example.com/manual.xml",
            "language": "en",
            "labels_json": "[]",
            "feed_token": "token-manual",
            "last_polled_at": db.now_iso(),
        }
    )
    dirs = config.ensure_episode_dirs(cfg, podcast["slug"])
    (dirs["episodes"] / "2026-01-02 - dropped-in - deadbeef (no ads).mp3").write_bytes(b"ID3" + b"y" * 100)
    xml, _ = rss.build_feed_xml(cfg, podcast, "http://testserver:8090")
    root = ET.fromstring(xml)
    items = root.findall("channel/item")
    assert len(items) == 1
    assert "dropped-in" in items[0].findtext("title")
    assert items[0].findtext("guid").startswith("uninterupted:")


def test_opml_export(app_env):
    cfg = config.load_config()
    db.create_podcast(
        {
            "title": "OPML Show",
            "feed_url": "https://example.com/opml.xml",
            "language": "en",
            "labels_json": "[]",
            "feed_token": "token-opml",
            "last_polled_at": db.now_iso(),
        }
    )
    root = ET.fromstring(rss.build_opml(cfg, "http://testserver:8090"))
    outlines = root.findall(".//outline[@type='rss']")
    assert len(outlines) == 1
    assert "token-opml" in outlines[0].attrib["xmlUrl"]
    assert outlines[0].attrib["text"].endswith("(no ads)")
