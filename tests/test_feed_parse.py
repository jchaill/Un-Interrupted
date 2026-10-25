from pathlib import Path

from app import feeds


FIXTURE = Path(__file__).parent / "fixtures" / "simple_feed.xml"


def test_parse_feed_channel_and_entries():
    parsed = feeds.parse_feed(FIXTURE.read_bytes(), "https://example.com/feed.xml")
    channel = parsed["channel"]
    assert channel["title"] == "Test Show"
    assert channel["language"] == "en"
    assert channel["author"] == "Test Author"
    assert channel["category"] == "Comedy"
    assert channel["explicit"] is False
    assert channel["artwork_url"] == "https://example.com/art.jpg"
    assert len(parsed["entries"]) == 2

    first = parsed["entries"][0]
    assert first["guid"] == "guid-one"
    assert first["title"] == "Episode One"
    assert first["duration"] == 3723
    assert first["published_at"].startswith("2026-10-20T12:00:00")
    assert first["original_url"].endswith("/audio/one.mp3")
    assert first["enclosure_length"] == 123456
    assert first["episode"] == 7
    assert first["season"] == 2
    assert "<p>First episode" in first["description"]


def test_parse_opml(tmp_path):
    opml = b"""<?xml version="1.0"?>
    <opml version="1.0"><body><outline text="feeds">
      <outline type="rss" text="Show A" xmlUrl="https://example.com/a.xml"/>
      <outline type="rss" text="Show B" xmlUrl="https://example.com/b.xml"/>
    </outline></body></opml>"""
    feeds = __import__("app.feeds", fromlist=["parse_opml"]).parse_opml(opml)
    assert [f["title"] for f in feeds] == ["Show A", "Show B"]
    assert feeds[1]["xml_url"] == "https://example.com/b.xml"


def test_invalid_feed_raises():
    try:
        feeds.parse_feed(b"not xml", "https://example.com")
    except feeds.FeedError:
        pass
    else:
        raise AssertionError("invalid feed should raise FeedError")
