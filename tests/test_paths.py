
from app.paths import (
    ascii_slug,
    display_title_from_filename,
    episode_basename,
    extension_from_url,
    guid8,
    parse_duration,
    safe_join,
)


def test_slug_is_ascii_and_stable():
    assert ascii_slug("Les Grosses Têtes — L’INTÉGRALE") == "les-grosses-tetes-lintegrale"
    assert ascii_slug("  ???  ") == "podcast"


def test_episode_basename_is_ascii_and_identifier_based():
    name = episode_basename("Conan O’Brien Needs A Friend – Will Ferrell", "abc-123", "2026-10-22T04:05:00+00:00")
    assert name.startswith("2026-10-22 - conan-obrien-needs-a-friend")
    assert guid8("abc-123") in name
    assert name.isascii()


def test_extension_from_url_and_content_type():
    assert extension_from_url("https://x/y/ep.mp3?token=1", "audio/mpeg") == ".mp3"
    assert extension_from_url("https://x/y/ep", "audio/x-m4a") == ".m4a"
    assert extension_from_url("https://x/y/ep", "application/octet-stream") == ".mp3"


def test_safe_join_blocks_traversal(tmp_path):
    base = tmp_path / "episodes"
    base.mkdir()
    assert safe_join(base, "a.mp3") == (base / "a.mp3").resolve()
    try:
        safe_join(base, "../secret")
    except ValueError:
        pass
    else:
        raise AssertionError("traversal should be rejected")


def test_duration_parsing():
    assert parse_duration("01:02:03") == 3723
    assert parse_duration("45:10") == 2710
    assert parse_duration("90") == 90
    assert display_title_from_filename("2026-10-22 - hello-world - abc12345 (no ads).mp3") == "hello-world - abc12345"
