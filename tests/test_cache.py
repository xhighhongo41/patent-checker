"""Tests for the OPS response file cache (patent_checker/cache.py)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from patent_checker import cache as cache_mod
from patent_checker import config
from patent_checker.cache import Cache, default_cache, is_fresh, pub_key, search_key

# --- pub_key ---------------------------------------------------------------


def test_pub_key_normalizes_across_spellings() -> None:
    """pub_key returns the DOCDB spelling regardless of the input spelling."""
    assert pub_key("EP1234567A1") == pub_key("ep.1234567.a1") == "EP.1234567.A1"


def test_pub_key_unparseable_input_raises_value_error() -> None:
    """An unparseable publication number propagates parse_pubnum's ValueError."""
    with pytest.raises(ValueError):
        pub_key("nonsense")


# --- search_key --------------------------------------------------------------


def test_search_key_is_stable_and_16_hex_chars() -> None:
    """search_key is deterministic and returns 16 lowercase hex characters."""
    key = search_key("ti=drone", 1, 25)

    assert key == search_key("ti=drone", 1, 25)
    assert len(key) == 16
    assert all(c in "0123456789abcdef" for c in key)


def test_search_key_differs_for_whitespace_variants() -> None:
    """CQL text is not normalized: whitespace differences yield different keys."""
    assert search_key("ti=a", 1, 25) != search_key("ti=a ", 1, 25)


def test_search_key_differs_for_different_ranges() -> None:
    """Different paging windows for the same query yield different keys."""
    assert search_key("ti=a", 1, 25) != search_key("ti=a", 26, 50)


# --- is_fresh ----------------------------------------------------------------


@pytest.mark.parametrize("kind", cache_mod.NO_EXPIRY_KINDS)
def test_is_fresh_no_expiry_kinds_ignore_date(kind: str) -> None:
    """No-expiry kinds are always fresh, even with a stale-looking timestamp."""
    now = datetime(2026, 9, 2, 12, 0, 0)
    assert is_fresh(kind, "2020-01-01T00:00:00", now) is True


@pytest.mark.parametrize("kind", cache_mod.NO_EXPIRY_KINDS)
def test_is_fresh_no_expiry_kinds_ignore_unparseable_timestamp(kind: str) -> None:
    """No-expiry kinds are fresh even when fetched_at cannot be parsed at all."""
    now = datetime(2026, 9, 2, 12, 0, 0)
    assert is_fresh(kind, "not a timestamp", now) is True


@pytest.mark.parametrize("kind", cache_mod.SAME_DAY_KINDS)
def test_is_fresh_same_day_kinds_true_within_the_same_calendar_day(kind: str) -> None:
    """Same-day kinds are fresh when fetched_at falls on now's calendar day."""
    fetched_at = "2026-09-02T00:01:00"
    now = datetime(2026, 9, 2, 23, 59, 0)
    assert is_fresh(kind, fetched_at, now) is True


@pytest.mark.parametrize("kind", cache_mod.SAME_DAY_KINDS)
def test_is_fresh_same_day_kinds_false_on_the_next_day(kind: str) -> None:
    """Same-day kinds go stale as soon as the calendar day changes."""
    fetched_at = "2026-09-01T23:59:00"
    now = datetime(2026, 9, 2, 0, 1, 0)
    assert is_fresh(kind, fetched_at, now) is False


@pytest.mark.parametrize("kind", cache_mod.SAME_DAY_KINDS)
def test_is_fresh_same_day_kinds_false_for_unparseable_timestamp(kind: str) -> None:
    """An unparseable fetched_at is treated as stale for same-day kinds."""
    now = datetime(2026, 9, 2, 12, 0, 0)
    assert is_fresh(kind, "not a timestamp", now) is False


def test_is_fresh_unknown_kind_raises_value_error() -> None:
    """An unknown kind is rejected rather than silently treated as fresh/stale."""
    with pytest.raises(ValueError):
        is_fresh("bogus", "2026-09-02T00:00:00", datetime(2026, 9, 2))


# --- Cache.get / Cache.put -----------------------------------------------


def test_get_before_any_put_is_a_miss_and_creates_nothing(tmp_path: Path) -> None:
    """get() on an empty cache returns None and does not create any files."""
    cache = Cache(tmp_path / "cache")

    result = cache.get("biblio", "EP.1.A1")

    assert result is None
    assert not (tmp_path / "cache").exists()


def test_get_unknown_kind_raises_value_error(tmp_path: Path) -> None:
    """get() rejects an unknown kind rather than treating it as a miss."""
    cache = Cache(tmp_path / "cache")

    with pytest.raises(ValueError):
        cache.get("bogus", "key")


def test_put_returns_content_path_under_base_kind_key(tmp_path: Path) -> None:
    """put() writes the content file at <base>/<kind>/<key>.xml and returns its path."""
    cache = Cache(tmp_path / "cache")

    content_path = cache.put(
        "biblio", "EP.1.A1", b"<xml/>", ident="EP1A1", raw_path=Path("/tmp/raw.xml")
    )

    assert content_path == tmp_path / "cache" / "biblio" / "EP.1.A1.xml"
    assert content_path.read_bytes() == b"<xml/>"


def test_put_then_get_roundtrips_content_and_metadata(tmp_path: Path) -> None:
    """A put() followed by get() returns the content, ident, raw_path and fetched_at."""
    fixed_now = datetime(2026, 9, 2, 10, 30, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: fixed_now)

    cache.put("biblio", "EP.1.A1", b"<xml/>", ident="EP1A1", raw_path=Path("/tmp/raw.xml"))
    hit = cache.get("biblio", "EP.1.A1")

    assert hit is not None
    assert hit.content == b"<xml/>"
    assert hit.ident == "EP1A1"
    assert hit.raw_path == "/tmp/raw.xml"
    assert isinstance(hit.raw_path, str)
    assert hit.fetched_at == "2026-09-02T10:30:00"
    assert hit.path == tmp_path / "cache" / "biblio" / "EP.1.A1.xml"


def test_put_writes_a_sidecar_with_the_five_expected_keys(tmp_path: Path) -> None:
    """The sidecar JSON carries kind, key, ident, fetched_at and raw_path."""
    cache = Cache(tmp_path / "cache", clock=lambda: datetime(2026, 9, 2, 10, 30, 0))

    cache.put("legal", "EP.1.A1", b"<xml/>", ident="EP1A1", raw_path=Path("/tmp/raw.xml"))

    meta_path = tmp_path / "cache" / "legal" / "EP.1.A1.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert set(meta.keys()) == {"kind", "key", "ident", "fetched_at", "raw_path"}
    assert meta["kind"] == "legal"
    assert meta["key"] == "EP.1.A1"
    assert meta["ident"] == "EP1A1"
    assert meta["fetched_at"] == "2026-09-02T10:30:00"
    assert meta["raw_path"] == "/tmp/raw.xml"


def test_get_missing_sidecar_is_a_miss(tmp_path: Path) -> None:
    """A content file with no matching sidecar is reported as a miss."""
    cache = Cache(tmp_path / "cache")
    content_dir = tmp_path / "cache" / "biblio"
    content_dir.mkdir(parents=True)
    (content_dir / "EP.1.A1.xml").write_bytes(b"<xml/>")

    assert cache.get("biblio", "EP.1.A1") is None


def test_get_corrupt_sidecar_is_a_miss(tmp_path: Path) -> None:
    """A sidecar that is not valid JSON is reported as a miss."""
    cache = Cache(tmp_path / "cache")
    content_dir = tmp_path / "cache" / "biblio"
    content_dir.mkdir(parents=True)
    (content_dir / "EP.1.A1.xml").write_bytes(b"<xml/>")
    (content_dir / "EP.1.A1.meta.json").write_text("not json", encoding="utf-8")

    assert cache.get("biblio", "EP.1.A1") is None


def test_get_sidecar_missing_a_key_is_a_miss(tmp_path: Path) -> None:
    """A sidecar missing one of the required keys is reported as a miss."""
    cache = Cache(tmp_path / "cache")
    content_dir = tmp_path / "cache" / "biblio"
    content_dir.mkdir(parents=True)
    (content_dir / "EP.1.A1.xml").write_bytes(b"<xml/>")
    incomplete = {"kind": "biblio", "key": "EP.1.A1", "ident": "EP1A1"}
    (content_dir / "EP.1.A1.meta.json").write_text(json.dumps(incomplete), encoding="utf-8")

    assert cache.get("biblio", "EP.1.A1") is None


def test_legal_entry_goes_stale_across_a_calendar_day_while_biblio_stays_fresh(
    tmp_path: Path,
) -> None:
    """Same-day kinds go stale the next day; no-expiry kinds stay a hit."""
    clock_value = datetime(2026, 9, 1, 23, 59, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: clock_value)
    cache.put("legal", "EP.1.A1", b"<legal/>", ident="EP1A1", raw_path=Path("/tmp/l.xml"))
    cache.put("biblio", "EP.1.A1", b"<biblio/>", ident="EP1A1", raw_path=Path("/tmp/b.xml"))

    clock_value = datetime(2026, 9, 2, 0, 1, 0)

    assert cache.get("legal", "EP.1.A1") is None
    biblio_hit = cache.get("biblio", "EP.1.A1")
    assert biblio_hit is not None
    assert biblio_hit.content == b"<biblio/>"


def test_put_twice_overwrites_the_entry(tmp_path: Path) -> None:
    """A second put() for the same kind/key replaces the previous entry."""
    cache = Cache(tmp_path / "cache")

    cache.put("biblio", "EP.1.A1", b"<old/>", ident="old", raw_path=Path("/tmp/old.xml"))
    cache.put("biblio", "EP.1.A1", b"<new/>", ident="new", raw_path=Path("/tmp/new.xml"))

    hit = cache.get("biblio", "EP.1.A1")
    assert hit is not None
    assert hit.content == b"<new/>"
    assert hit.ident == "new"
    assert hit.raw_path == "/tmp/new.xml"


# --- default_cache -------------------------------------------------------


def test_default_cache_is_rooted_under_the_configured_data_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """default_cache() resolves its root from config.data_base()."""
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(tmp_path))

    assert default_cache().base == config.data_base() / "cache" / "ops"
