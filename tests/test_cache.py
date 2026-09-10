"""Tests for the OPS/GP response file cache (patent_checker/cache.py)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from patent_checker import cache as cache_mod
from patent_checker import config
from patent_checker.cache import (
    DEFAULT_TTLS,
    Cache,
    CacheEntry,
    content_suffix,
    default_cache,
    format_ttl,
    is_fresh,
    kind_subdir,
    pub_key,
    search_key,
)

# Kinds that do expire under the default policy, with their TTL.
EXPIRING_KINDS: tuple[str, ...] = tuple(
    kind for kind, ttl in DEFAULT_TTLS.items() if ttl is not None
)

# Every kind stored under ``ops/``.
OPS_KINDS: tuple[str, ...] = tuple(kind for kind in cache_mod.KINDS if kind != "gp")


def _ttl(kind: str) -> timedelta:
    """Return the default TTL of an expiring *kind*."""
    ttl = DEFAULT_TTLS[kind]
    assert ttl is not None
    return ttl


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


# --- kind_subdir / content_suffix -------------------------------------------


@pytest.mark.parametrize("kind", OPS_KINDS)
def test_kind_subdir_places_ops_kinds_under_ops(kind: str) -> None:
    """Every OPS kind lives in ``ops/<kind>/`` relative to its root."""
    assert kind_subdir(kind) == Path("ops") / kind


def test_kind_subdir_places_gp_in_its_own_directory() -> None:
    """The Google Patents kind lives in ``gp/``, apart from the OPS kinds."""
    assert kind_subdir("gp") == Path("gp")


def test_kind_subdir_unknown_kind_raises_value_error() -> None:
    """An unknown kind has no directory rather than a guessed one."""
    with pytest.raises(ValueError):
        kind_subdir("bogus")


@pytest.mark.parametrize("kind", OPS_KINDS)
def test_content_suffix_is_xml_for_ops_kinds(kind: str) -> None:
    """OPS bodies are XML."""
    assert content_suffix(kind) == ".xml"


def test_content_suffix_is_html_for_gp() -> None:
    """Google Patents bodies are HTML."""
    assert content_suffix("gp") == ".html"


def test_content_suffix_unknown_kind_raises_value_error() -> None:
    """An unknown kind has no body suffix rather than a guessed one."""
    with pytest.raises(ValueError):
        content_suffix("bogus")


# --- is_fresh ----------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(cache_mod.NO_EXPIRY_KINDS))
def test_is_fresh_no_expiry_kinds_ignore_date(kind: str) -> None:
    """No-expiry kinds are always fresh, even with a stale-looking timestamp."""
    now = datetime(2026, 9, 2, 12, 0, 0)
    assert is_fresh(kind, "2020-01-01T00:00:00", now) is True


@pytest.mark.parametrize("kind", sorted(cache_mod.NO_EXPIRY_KINDS))
def test_is_fresh_no_expiry_kinds_ignore_unparseable_timestamp(kind: str) -> None:
    """No-expiry kinds are fresh even when fetched_at cannot be parsed at all."""
    now = datetime(2026, 9, 2, 12, 0, 0)
    assert is_fresh(kind, "not a timestamp", now) is True


@pytest.mark.parametrize("kind", EXPIRING_KINDS)
def test_is_fresh_true_one_second_before_the_ttl_deadline(kind: str) -> None:
    """An expiring kind is fresh right up to (but not including) its deadline."""
    now = datetime(2026, 9, 2, 12, 0, 0)
    fetched_at = (now - _ttl(kind) + timedelta(seconds=1)).isoformat(timespec="seconds")

    assert is_fresh(kind, fetched_at, now) is True


@pytest.mark.parametrize("kind", EXPIRING_KINDS)
def test_is_fresh_false_exactly_at_the_ttl_deadline(kind: str) -> None:
    """An entry whose age is exactly its TTL has expired."""
    now = datetime(2026, 9, 2, 12, 0, 0)
    fetched_at = (now - _ttl(kind)).isoformat(timespec="seconds")

    assert is_fresh(kind, fetched_at, now) is False


@pytest.mark.parametrize("kind", EXPIRING_KINDS)
def test_is_fresh_false_one_second_past_the_ttl_deadline(kind: str) -> None:
    """An entry older than its TTL has expired."""
    now = datetime(2026, 9, 2, 12, 0, 0)
    fetched_at = (now - _ttl(kind) - timedelta(seconds=1)).isoformat(timespec="seconds")

    assert is_fresh(kind, fetched_at, now) is False


@pytest.mark.parametrize("kind", EXPIRING_KINDS)
def test_is_fresh_false_for_unparseable_timestamp(kind: str) -> None:
    """An unparseable fetched_at is treated as expired for expiring kinds."""
    now = datetime(2026, 9, 2, 12, 0, 0)
    assert is_fresh(kind, "not a timestamp", now) is False


@pytest.mark.parametrize("kind", EXPIRING_KINDS)
def test_is_fresh_compares_an_offset_aware_timestamp_by_instant(kind: str) -> None:
    """An offset-aware fetched_at (written since v1.0) is compared by instant."""
    now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)
    fresh = (now - _ttl(kind) + timedelta(seconds=1)).isoformat(timespec="seconds")
    stale = (now - _ttl(kind)).isoformat(timespec="seconds")

    assert is_fresh(kind, fresh, now) is True
    assert is_fresh(kind, stale, now) is False


@pytest.mark.parametrize("kind", EXPIRING_KINDS)
def test_is_fresh_mixes_an_aware_timestamp_with_a_naive_now(kind: str) -> None:
    """A naive "now" is read as local time, so it can be compared with an aware entry."""
    now = datetime(2026, 9, 2, 12, 0, 0)
    fresh = (now.astimezone(UTC) - _ttl(kind) + timedelta(seconds=1)).isoformat(timespec="seconds")
    stale = (now.astimezone(UTC) - _ttl(kind)).isoformat(timespec="seconds")

    assert is_fresh(kind, fresh, now) is True
    assert is_fresh(kind, stale, now) is False


@pytest.mark.parametrize("kind", EXPIRING_KINDS)
def test_is_fresh_false_for_a_non_string_timestamp(kind: str) -> None:
    """A sidecar whose fetched_at is a number is not trusted."""
    now = datetime(2026, 9, 2, 12, 0, 0)
    assert is_fresh(kind, 1756800000, now) is False  # type: ignore[arg-type]


def test_is_fresh_ttls_override_can_disable_expiry() -> None:
    """A None override makes an otherwise expiring kind never go stale."""
    now = datetime(2026, 9, 2, 12, 0, 0)

    assert is_fresh("legal", "2020-01-01T00:00:00", now, ttls={"legal": None}) is True


def test_is_fresh_ttls_override_can_shorten_a_ttl() -> None:
    """A shorter override expires an entry the default TTL would still serve."""
    now = datetime(2026, 9, 2, 12, 0, 0)
    ttls = {"biblio": timedelta(hours=1)}

    assert is_fresh("biblio", "2026-09-02T11:30:00", now, ttls=ttls) is True
    assert is_fresh("biblio", "2026-09-02T10:30:00", now, ttls=ttls) is False


def test_is_fresh_kinds_absent_from_ttls_fall_back_to_the_defaults() -> None:
    """A partial override mapping leaves the other kinds on their default TTL."""
    now = datetime(2026, 9, 2, 12, 0, 0)
    ttls = {"biblio": timedelta(hours=1)}

    assert is_fresh("legal", "2026-09-01T12:00:00", now, ttls=ttls) is True
    assert is_fresh("legal", "2026-08-01T12:00:00", now, ttls=ttls) is False


def test_is_fresh_unknown_kind_raises_value_error() -> None:
    """An unknown kind is rejected rather than silently treated as fresh/stale."""
    with pytest.raises(ValueError):
        is_fresh("bogus", "2026-09-02T00:00:00", datetime(2026, 9, 2))


# --- roots and paths ---------------------------------------------------------


def test_a_single_root_holds_every_kind(tmp_path: Path) -> None:
    """Omitting the local root puts shared and search kinds under the same tree."""
    cache = Cache(tmp_path / "cache")

    assert cache.shared == cache.local == cache.base == tmp_path / "cache"
    assert cache.root_for("biblio") == cache.root_for("search") == tmp_path / "cache"


def test_two_roots_split_publication_kinds_from_search_kinds(tmp_path: Path) -> None:
    """Publication-keyed kinds go to the shared root, search-keyed ones stay local."""
    cache = Cache(tmp_path / "shared", tmp_path / "local")

    cache.put("biblio", "EP.1.A1", b"<biblio/>", ident="EP1A1")
    cache.put("search", "abc123", b"<search/>", ident="ti=drone")

    assert (tmp_path / "shared" / "ops" / "biblio" / "EP.1.A1.xml").is_file()
    assert (tmp_path / "local" / "ops" / "search" / "abc123.xml").is_file()
    assert not (tmp_path / "shared" / "ops" / "search").exists()
    assert not (tmp_path / "local" / "ops" / "biblio").exists()
    assert cache.get("biblio", "EP.1.A1") is not None
    assert cache.get("search", "abc123") is not None


def test_base_stays_the_shared_root(tmp_path: Path) -> None:
    """``base`` keeps reporting the shared root for callers written against v0.3."""
    cache = Cache(tmp_path / "shared", tmp_path / "local")

    assert cache.base == tmp_path / "shared"


def test_root_for_unknown_kind_raises_value_error(tmp_path: Path) -> None:
    """An unknown kind has no root rather than defaulting to the shared one."""
    with pytest.raises(ValueError):
        Cache(tmp_path / "cache").root_for("bogus")


def test_content_path_and_meta_path_sit_next_to_each_other(tmp_path: Path) -> None:
    """The body and its sidecar share a directory and a key."""
    cache = Cache(tmp_path / "cache")

    assert cache.content_path("biblio", "EP.1.A1") == (
        tmp_path / "cache" / "ops" / "biblio" / "EP.1.A1.xml"
    )
    assert cache.meta_path("biblio", "EP.1.A1") == (
        tmp_path / "cache" / "ops" / "biblio" / "EP.1.A1.meta.json"
    )


def test_content_path_unknown_kind_raises_value_error(tmp_path: Path) -> None:
    """content_path() rejects an unknown kind rather than inventing a directory."""
    with pytest.raises(ValueError):
        Cache(tmp_path / "cache").content_path("bogus", "key")


def test_constructor_rejects_ttl_overrides_for_unknown_kinds(tmp_path: Path) -> None:
    """A TTL override naming an unknown kind is a configuration error."""
    with pytest.raises(ValueError):
        Cache(tmp_path / "cache", ttls={"bogus": timedelta(days=1)})


def test_ttls_layers_overrides_on_top_of_the_defaults(tmp_path: Path) -> None:
    """Overridden kinds take the given TTL; the others keep their default."""
    cache = Cache(tmp_path / "cache", ttls={"legal": None})

    assert cache.ttls["legal"] is None
    assert cache.ttls["biblio"] == DEFAULT_TTLS["biblio"]


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


def test_put_returns_content_path_under_root_ops_kind_key(tmp_path: Path) -> None:
    """put() writes the body at <root>/ops/<kind>/<key>.xml and returns its path."""
    cache = Cache(tmp_path / "cache")

    content_path = cache.put("biblio", "EP.1.A1", b"<xml/>", ident="EP1A1")

    assert content_path == tmp_path / "cache" / "ops" / "biblio" / "EP.1.A1.xml"
    assert content_path.read_bytes() == b"<xml/>"


def test_put_stores_a_gp_page_as_html_outside_the_ops_tree(tmp_path: Path) -> None:
    """A Google Patents page is cached as ``gp/<key>.html`` and read back."""
    cache = Cache(tmp_path / "cache")

    content_path = cache.put("gp", "US.1.A1", b"<html></html>", ident="US1A1")

    assert content_path == tmp_path / "cache" / "gp" / "US.1.A1.html"
    hit = cache.get("gp", "US.1.A1")
    assert hit is not None
    assert hit.content == b"<html></html>"


def test_put_then_get_roundtrips_content_and_metadata(tmp_path: Path) -> None:
    """A put() followed by get() returns the content, ident, path and fetched_at."""
    fixed_now = datetime(2026, 9, 2, 10, 30, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: fixed_now)

    cache.put("biblio", "EP.1.A1", b"<xml/>", ident="EP1A1")
    hit = cache.get("biblio", "EP.1.A1")

    assert hit is not None
    assert hit.content == b"<xml/>"
    assert hit.ident == "EP1A1"
    assert datetime.fromisoformat(hit.fetched_at) == fixed_now.astimezone(UTC)
    assert hit.path == tmp_path / "cache" / "ops" / "biblio" / "EP.1.A1.xml"
    assert hit.raw_path == str(hit.path)
    assert isinstance(hit.raw_path, str)


def test_put_writes_a_sidecar_with_the_five_expected_keys(tmp_path: Path) -> None:
    """The sidecar JSON carries kind, key, ident, fetched_at and size."""
    cache = Cache(tmp_path / "cache", clock=lambda: datetime(2026, 9, 2, 10, 30, 0))

    cache.put("legal", "EP.1.A1", b"<xml/>", ident="EP1A1")

    meta_path = tmp_path / "cache" / "ops" / "legal" / "EP.1.A1.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert set(meta.keys()) == {"kind", "key", "ident", "fetched_at", "size"}
    assert meta["kind"] == "legal"
    assert meta["key"] == "EP.1.A1"
    assert meta["ident"] == "EP1A1"
    assert datetime.fromisoformat(meta["fetched_at"]) == datetime(2026, 9, 2, 10, 30, 0).astimezone(
        UTC
    )
    assert meta["size"] == len(b"<xml/>")


def test_put_leaves_no_temporary_files_behind(tmp_path: Path) -> None:
    """A completed put() moves both temp files into place."""
    cache = Cache(tmp_path / "cache")

    cache.put("biblio", "EP.1.A1", b"<xml/>", ident="EP1A1")

    assert list((tmp_path / "cache").rglob("*.tmp")) == []


def test_get_missing_sidecar_is_a_miss(tmp_path: Path) -> None:
    """A body file with no matching sidecar is reported as a miss."""
    cache = Cache(tmp_path / "cache")
    content_dir = tmp_path / "cache" / "ops" / "biblio"
    content_dir.mkdir(parents=True)
    (content_dir / "EP.1.A1.xml").write_bytes(b"<xml/>")

    assert cache.get("biblio", "EP.1.A1") is None


def test_get_missing_content_file_is_a_miss(tmp_path: Path) -> None:
    """A sidecar with no matching body file is reported as a miss."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "EP.1.A1", b"<xml/>", ident="EP1A1")
    cache.content_path("biblio", "EP.1.A1").unlink()

    assert cache.get("biblio", "EP.1.A1") is None


def test_get_corrupt_sidecar_is_a_miss(tmp_path: Path) -> None:
    """A sidecar that is not valid JSON is reported as a miss."""
    cache = Cache(tmp_path / "cache")
    content_dir = tmp_path / "cache" / "ops" / "biblio"
    content_dir.mkdir(parents=True)
    (content_dir / "EP.1.A1.xml").write_bytes(b"<xml/>")
    (content_dir / "EP.1.A1.meta.json").write_text("not json", encoding="utf-8")

    assert cache.get("biblio", "EP.1.A1") is None


def test_get_sidecar_that_is_not_an_object_is_a_miss(tmp_path: Path) -> None:
    """A sidecar holding valid JSON of the wrong shape is reported as a miss."""
    cache = Cache(tmp_path / "cache")
    content_dir = tmp_path / "cache" / "ops" / "biblio"
    content_dir.mkdir(parents=True)
    (content_dir / "EP.1.A1.xml").write_bytes(b"<xml/>")
    (content_dir / "EP.1.A1.meta.json").write_text("[1, 2, 3]", encoding="utf-8")

    assert cache.get("biblio", "EP.1.A1") is None


def test_get_sidecar_missing_a_key_is_a_miss(tmp_path: Path) -> None:
    """A sidecar missing one of the required keys is reported as a miss."""
    cache = Cache(tmp_path / "cache")
    content_dir = tmp_path / "cache" / "ops" / "biblio"
    content_dir.mkdir(parents=True)
    (content_dir / "EP.1.A1.xml").write_bytes(b"<xml/>")
    incomplete = {"kind": "biblio", "key": "EP.1.A1", "ident": "EP1A1"}
    (content_dir / "EP.1.A1.meta.json").write_text(json.dumps(incomplete), encoding="utf-8")

    assert cache.get("biblio", "EP.1.A1") is None


def test_get_reads_a_pre_v0_4_sidecar_without_a_size(tmp_path: Path) -> None:
    """A v0.3 sidecar (raw_path, no size) is still served, without the size check."""
    cache = Cache(tmp_path / "cache", clock=lambda: datetime(2026, 9, 2, 10, 0, 0))
    content_dir = tmp_path / "cache" / "ops" / "biblio"
    content_dir.mkdir(parents=True)
    (content_dir / "EP.1.A1.xml").write_bytes(b"<xml/>")
    legacy = {
        "kind": "biblio",
        "key": "EP.1.A1",
        "ident": "EP1A1",
        "fetched_at": "2026-09-01T10:00:00",
        "raw_path": "/tmp/raw.xml",
    }
    (content_dir / "EP.1.A1.meta.json").write_text(json.dumps(legacy), encoding="utf-8")

    hit = cache.get("biblio", "EP.1.A1")

    assert hit is not None
    assert hit.content == b"<xml/>"
    assert hit.ident == "EP1A1"
    assert hit.raw_path == str(content_dir / "EP.1.A1.xml")


def test_get_rejects_a_body_whose_size_does_not_match_the_sidecar(tmp_path: Path) -> None:
    """A body that no longer has the recorded size is reported as a miss."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "EP.1.A1", b"<xml/>", ident="EP1A1")
    meta_path = cache.meta_path("biblio", "EP.1.A1")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["size"] = 999
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    assert cache.get("biblio", "EP.1.A1") is None


def test_get_rejects_truncated_content_when_sidecar_is_intact(tmp_path: Path) -> None:
    """A body truncated after a successful put() is never served as a hit.

    If the content file is truncated after a successful put() (e.g. a
    process killed mid-write on a later overwrite), get() must not serve
    the partial body back as a hit even though the sidecar is intact.
    """
    cache = Cache(tmp_path / "cache")
    content = b"<xml>" + b"a" * 500 + b"</xml>"
    content_path = cache.put("biblio", "EP.1.A1", content, ident="EP1A1")

    content_path.write_bytes(content[: len(content) // 2])

    assert cache.get("biblio", "EP.1.A1") is None


def test_get_after_the_kind_directory_is_deleted_is_a_miss(tmp_path: Path) -> None:
    """Wiping a kind's directory by hand leaves get() a plain miss, not an error."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "EP.1.A1", b"<xml/>", ident="EP1A1")
    for path in (tmp_path / "cache" / "ops" / "biblio").iterdir():
        path.unlink()
    (tmp_path / "cache" / "ops" / "biblio").rmdir()

    assert cache.get("biblio", "EP.1.A1") is None


def test_leftover_temp_files_do_not_disturb_get_or_put(tmp_path: Path) -> None:
    """Temp files from a crashed write are ignored by both get() and put()."""
    cache = Cache(tmp_path / "cache", clock=lambda: datetime(2026, 9, 2, 10, 0, 0))
    cache.put("search", "abc123", b"<search/>", ident="ti=drone")
    search_dir = tmp_path / "cache" / "ops" / "search"
    (search_dir / "abc123.xml.tmp").write_bytes(b"<half")
    (search_dir / "abc123.meta.json.tmp").write_text('{"kind"', encoding="utf-8")
    (search_dir / "def456.meta.json.tmp").write_text('{"kind"', encoding="utf-8")

    hit = cache.get("search", "abc123")
    assert hit is not None
    assert hit.content == b"<search/>"

    cache.put("search", "def456", b"<other/>", ident="ti=other")
    other = cache.get("search", "def456")
    assert other is not None
    assert other.content == b"<other/>"
    assert cache.get("search", "abc123") is not None


def test_put_repairs_a_corrupt_entry(tmp_path: Path) -> None:
    """A damaged entry misses, then the next put() restores a normal hit."""
    cache = Cache(tmp_path / "cache", clock=lambda: datetime(2026, 9, 2, 10, 0, 0))
    cache.put("biblio", "EP.1.A1", b"<old/>", ident="EP1A1")
    cache.content_path("biblio", "EP.1.A1").write_bytes(b"<tru")
    cache.meta_path("biblio", "EP.1.A1").write_text("not json", encoding="utf-8")
    assert cache.get("biblio", "EP.1.A1") is None

    cache.put("biblio", "EP.1.A1", b"<new/>", ident="EP1A1")

    hit = cache.get("biblio", "EP.1.A1")
    assert hit is not None
    assert hit.content == b"<new/>"
    meta = json.loads(cache.meta_path("biblio", "EP.1.A1").read_text(encoding="utf-8"))
    assert meta["size"] == len(b"<new/>")


def test_legal_expires_after_its_ttl_while_biblio_stays_fresh(tmp_path: Path) -> None:
    """A legal entry expires one second past its 7-day TTL; biblio is still a hit."""
    clock_value = datetime(2026, 9, 1, 12, 0, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: clock_value)
    cache.put("legal", "EP.1.A1", b"<legal/>", ident="EP1A1")
    cache.put("biblio", "EP.1.A1", b"<biblio/>", ident="EP1A1")

    clock_value = datetime(2026, 9, 1, 12, 0, 0) + _ttl("legal") + timedelta(seconds=1)

    assert cache.get("legal", "EP.1.A1") is None
    biblio_hit = cache.get("biblio", "EP.1.A1")
    assert biblio_hit is not None
    assert biblio_hit.content == b"<biblio/>"


def test_ttl_overrides_are_applied_by_get(tmp_path: Path) -> None:
    """A cache built with per-kind overrides serves and expires entries by them."""
    clock_value = datetime(2026, 9, 1, 12, 0, 0)
    cache = Cache(
        tmp_path / "cache",
        ttls={"legal": None, "biblio": timedelta(hours=1)},
        clock=lambda: clock_value,
    )
    cache.put("legal", "EP.1.A1", b"<legal/>", ident="EP1A1")
    cache.put("biblio", "EP.1.A1", b"<biblio/>", ident="EP1A1")

    clock_value = datetime(2026, 9, 10, 12, 0, 0)

    assert cache.get("legal", "EP.1.A1") is not None
    assert cache.get("biblio", "EP.1.A1") is None


def test_put_twice_overwrites_the_entry(tmp_path: Path) -> None:
    """A second put() for the same kind/key replaces the previous entry."""
    cache = Cache(tmp_path / "cache")

    cache.put("biblio", "EP.1.A1", b"<old/>", ident="old")
    cache.put("biblio", "EP.1.A1", b"<new/>", ident="new")

    hit = cache.get("biblio", "EP.1.A1")
    assert hit is not None
    assert hit.content == b"<new/>"
    assert hit.ident == "new"


# --- eviction of expired search entries ------------------------------------


def test_putting_a_search_entry_removes_the_expired_ones(tmp_path: Path) -> None:
    """Search entries do not pile up: an expired one is deleted by the next put()."""
    clock_value = datetime(2026, 9, 2, 10, 0, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: clock_value)
    cache.put("search", "aaa111", b"<old/>", ident="ti=old")

    clock_value = datetime(2026, 9, 4, 10, 0, 0)
    cache.put("search", "bbb222", b"<new/>", ident="ti=new")

    search_dir = tmp_path / "cache" / "ops" / "search"
    assert not (search_dir / "aaa111.xml").exists()
    assert not (search_dir / "aaa111.meta.json").exists()
    assert (search_dir / "bbb222.xml").is_file()
    assert (search_dir / "bbb222.meta.json").is_file()


def test_putting_a_search_entry_keeps_the_fresh_ones(tmp_path: Path) -> None:
    """Eviction only touches expired entries of the same kind."""
    clock_value = datetime(2026, 9, 2, 10, 0, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: clock_value)
    cache.put("search", "aaa111", b"<first/>", ident="ti=first")
    cache.put("searchbib", "ccc333", b"<other-kind/>", ident="ti=first")

    clock_value = datetime(2026, 9, 2, 18, 0, 0)
    cache.put("search", "bbb222", b"<second/>", ident="ti=second")

    assert cache.get("search", "aaa111") is not None
    assert cache.get("searchbib", "ccc333") is not None


def test_eviction_leaves_entries_with_an_unreadable_sidecar_alone(tmp_path: Path) -> None:
    """A sidecar that cannot be read may be mid-write, so eviction skips it."""
    clock_value = datetime(2026, 9, 2, 10, 0, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: clock_value)
    cache.put("search", "aaa111", b"<old/>", ident="ti=old")
    cache.meta_path("search", "aaa111").write_text("not json", encoding="utf-8")

    clock_value = datetime(2026, 9, 4, 10, 0, 0)
    cache.put("search", "bbb222", b"<new/>", ident="ti=new")

    assert cache.content_path("search", "aaa111").is_file()
    assert cache.meta_path("search", "aaa111").is_file()


def test_expired_publication_entries_are_not_evicted(tmp_path: Path) -> None:
    """The shared kinds are never swept: an expired biblio entry stays on disk."""
    clock_value = datetime(2026, 9, 2, 10, 0, 0)
    cache = Cache(
        tmp_path / "cache", ttls={"biblio": timedelta(hours=1)}, clock=lambda: clock_value
    )
    cache.put("biblio", "EP.1.A1", b"<old/>", ident="EP1A1")

    clock_value = datetime(2026, 9, 4, 10, 0, 0)
    cache.put("biblio", "EP.2.A1", b"<new/>", ident="EP2A1")

    assert cache.get("biblio", "EP.1.A1") is None
    assert cache.content_path("biblio", "EP.1.A1").is_file()
    assert cache.meta_path("biblio", "EP.1.A1").is_file()


# --- default_cache -------------------------------------------------------


def test_default_cache_uses_one_root_when_a_data_dir_is_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With PATENT_CHECKER_DATA_DIR set, both roots collapse onto <data dir>/cache."""
    monkeypatch.setattr(config, "_data_base_override", None)
    monkeypatch.delenv("PATENT_CHECKER_CACHE_DIR", raising=False)
    monkeypatch.delenv("PATENT_CHECKER_CACHE_TTL", raising=False)
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(tmp_path))

    cache = default_cache()

    assert cache.shared == cache.local == tmp_path / "cache"


def test_default_cache_splits_the_shared_cache_from_the_project_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without a data dir, publication kinds go to the per-user shared cache."""
    monkeypatch.setattr(config, "_data_base_override", None)
    monkeypatch.delenv("PATENT_CHECKER_CACHE_DIR", raising=False)
    monkeypatch.delenv("PATENT_CHECKER_CACHE_TTL", raising=False)
    monkeypatch.delenv("PATENT_CHECKER_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    cache = default_cache()

    assert cache.shared == config.cache_base()
    assert cache.local == config.data_base() / "cache"
    assert cache.shared != cache.local


def test_default_cache_applies_the_configured_ttl_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TTL overrides from the environment reach the cache."""
    monkeypatch.setattr(config, "_data_base_override", None)
    monkeypatch.delenv("PATENT_CHECKER_CACHE_DIR", raising=False)
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PATENT_CHECKER_CACHE_TTL", "legal=0,search=2h")

    cache = default_cache()

    assert cache.ttls["legal"] is None
    assert cache.ttls["search"] == timedelta(hours=2)
    assert cache.ttls["biblio"] == DEFAULT_TTLS["biblio"]


def test_default_cache_propagates_a_malformed_ttl_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A broken TTL setting is reported rather than silently ignored."""
    monkeypatch.setattr(config, "_data_base_override", None)
    monkeypatch.delenv("PATENT_CHECKER_CACHE_DIR", raising=False)
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PATENT_CHECKER_CACHE_TTL", "legal=forever")

    with pytest.raises(config.ConfigError):
        default_cache()


# --- format_ttl --------------------------------------------------------------


def test_format_ttl_none_is_never() -> None:
    """A None TTL formats as "never"."""
    assert format_ttl(None) == "never"


def test_format_ttl_whole_days() -> None:
    """A whole number of days formats with a "d" suffix."""
    assert format_ttl(timedelta(days=90)) == "90d"


def test_format_ttl_whole_hours() -> None:
    """A duration that is a whole number of hours (but not days) uses "h"."""
    assert format_ttl(timedelta(hours=12)) == "12h"


def test_format_ttl_falls_back_to_seconds() -> None:
    """A duration that is neither whole days nor whole hours uses seconds."""
    assert format_ttl(timedelta(seconds=90)) == "90s"


# --- Cache.entries -------------------------------------------------------


def test_entries_on_an_empty_cache_is_empty(tmp_path: Path) -> None:
    """entries() on a cache with nothing written returns an empty list."""
    cache = Cache(tmp_path / "cache")

    assert cache.entries() == []


def test_entries_lists_one_entry_per_root(tmp_path: Path) -> None:
    """A shared-root entry and a local-root entry are both listed, with the right root label."""
    cache = Cache(tmp_path / "shared", tmp_path / "local")
    cache.put("biblio", "EP.1.A1", b"<biblio/>", ident="EP1A1")
    cache.put("search", "abc123", b"<search/>", ident="ti=drone")

    entries = cache.entries()

    assert [(e.kind, e.key, e.root, e.broken) for e in entries] == [
        ("biblio", "EP.1.A1", "shared", False),
        ("search", "abc123", "local", False),
    ]


def test_entries_kind_filter_returns_only_that_kind(tmp_path: Path) -> None:
    """Passing a kind restricts the result to that kind's entries."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "EP.1.A1", b"<biblio/>", ident="EP1A1")
    cache.put("legal", "EP.1.A1", b"<legal/>", ident="EP1A1")

    entries = cache.entries("legal")

    assert [e.kind for e in entries] == ["legal"]


def test_entries_unknown_kind_raises_value_error(tmp_path: Path) -> None:
    """entries() rejects an unknown kind rather than treating it as empty."""
    cache = Cache(tmp_path / "cache")

    with pytest.raises(ValueError):
        cache.entries("bogus")


def test_entries_missing_body_is_broken(tmp_path: Path) -> None:
    """A sidecar with no matching body is reported broken with problem "missing body"."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "EP.1.A1", b"<xml/>", ident="EP1A1")
    cache.content_path("biblio", "EP.1.A1").unlink()

    (entry,) = cache.entries("biblio")

    assert entry.broken is True
    assert entry.problem == "missing body"
    assert entry.size == 0


def test_entries_missing_sidecar_is_broken(tmp_path: Path) -> None:
    """A body with no matching sidecar is reported broken with problem "missing sidecar"."""
    cache = Cache(tmp_path / "cache")
    content_dir = tmp_path / "cache" / "ops" / "biblio"
    content_dir.mkdir(parents=True)
    (content_dir / "EP.1.A1.xml").write_bytes(b"<xml/>")

    (entry,) = cache.entries("biblio")

    assert entry.broken is True
    assert entry.problem == "missing sidecar"
    assert entry.size == len(b"<xml/>")
    assert entry.ident is None


def test_entries_unreadable_sidecar_is_broken(tmp_path: Path) -> None:
    """A body with a corrupt sidecar is reported broken with problem "unreadable sidecar"."""
    cache = Cache(tmp_path / "cache")
    content_dir = tmp_path / "cache" / "ops" / "biblio"
    content_dir.mkdir(parents=True)
    (content_dir / "EP.1.A1.xml").write_bytes(b"<xml/>")
    (content_dir / "EP.1.A1.meta.json").write_text("not json", encoding="utf-8")

    (entry,) = cache.entries("biblio")

    assert entry.broken is True
    assert entry.problem == "unreadable sidecar"
    assert entry.ident is None
    assert entry.fetched_at is None


def test_entries_sidecar_missing_a_required_key_is_broken(tmp_path: Path) -> None:
    """A sidecar missing one of the required fields is also "unreadable sidecar"."""
    cache = Cache(tmp_path / "cache")
    content_dir = tmp_path / "cache" / "ops" / "biblio"
    content_dir.mkdir(parents=True)
    (content_dir / "EP.1.A1.xml").write_bytes(b"<xml/>")
    incomplete = {"kind": "biblio", "key": "EP.1.A1", "ident": "EP1A1"}
    (content_dir / "EP.1.A1.meta.json").write_text(json.dumps(incomplete), encoding="utf-8")

    (entry,) = cache.entries("biblio")

    assert entry.broken is True
    assert entry.problem == "unreadable sidecar"


def test_entries_size_mismatch_is_broken(tmp_path: Path) -> None:
    """A body whose size no longer matches the sidecar is reported "size mismatch"."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "EP.1.A1", b"<xml/>", ident="EP1A1")
    meta_path = cache.meta_path("biblio", "EP.1.A1")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["size"] = 999
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    (entry,) = cache.entries("biblio")

    assert entry.broken is True
    assert entry.problem == "size mismatch"
    assert entry.ident == "EP1A1"


def test_entries_stray_tmp_file_only_is_broken(tmp_path: Path) -> None:
    """A leftover .tmp file with neither body nor sidecar is "stray tmp file"."""
    cache = Cache(tmp_path / "cache")
    content_dir = tmp_path / "cache" / "ops" / "biblio"
    content_dir.mkdir(parents=True)
    (content_dir / "EP.1.A1.xml.tmp").write_bytes(b"<half")

    (entry,) = cache.entries("biblio")

    assert entry.broken is True
    assert entry.problem == "stray tmp file"
    assert entry.path == content_dir / "EP.1.A1.xml"
    assert entry.meta_path == content_dir / "EP.1.A1.meta.json"
    assert entry.size == 0
    assert entry.ident is None


def test_entries_expired_entry_has_expired_true(tmp_path: Path) -> None:
    """An entry past its TTL is reported with expired=True."""
    clock_value = datetime(2026, 9, 1, 12, 0, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: clock_value)
    cache.put("legal", "EP.1.A1", b"<legal/>", ident="EP1A1")

    clock_value = datetime(2026, 9, 1, 12, 0, 0) + _ttl("legal") + timedelta(seconds=1)

    (entry,) = cache.entries("legal")

    assert entry.broken is False
    assert entry.expired is True


def test_entries_a_stray_tmp_beside_a_normal_entry_is_still_normal(tmp_path: Path) -> None:
    """A leftover .tmp next to a complete entry does not make that entry broken."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "EP.1.A1", b"<xml/>", ident="EP1A1")
    content_dir = tmp_path / "cache" / "ops" / "biblio"
    (content_dir / "EP.1.A1.xml.tmp").write_bytes(b"<half")
    (content_dir / "EP.1.A1.meta.json.tmp").write_text('{"kind"', encoding="utf-8")

    entries = cache.entries("biblio")

    assert len(entries) == 1
    assert entries[0].broken is False
    assert entries[0].problem is None


# --- Cache.stats ---------------------------------------------------------


def test_stats_reports_the_expected_top_level_keys(tmp_path: Path) -> None:
    """The stats dict carries every documented top-level key."""
    cache = Cache(tmp_path / "cache")

    stats = cache.stats()

    assert set(stats.keys()) == {
        "shared_dir",
        "local_dir",
        "same_root",
        "ttls",
        "kinds",
        "totals",
    }
    assert set(stats["kinds"].keys()) == set(cache_mod.KINDS)
    assert set(stats["ttls"].keys()) == set(cache_mod.KINDS)
    assert stats["totals"] == {"entries": 0, "bytes": 0, "expired": 0, "broken": 0}


def test_stats_same_root_true_for_a_single_root(tmp_path: Path) -> None:
    """same_root is True when the cache was built with one root."""
    cache = Cache(tmp_path / "cache")

    assert cache.stats()["same_root"] is True


def test_stats_same_root_false_for_two_roots(tmp_path: Path) -> None:
    """same_root is False when shared and local point at different directories."""
    cache = Cache(tmp_path / "shared", tmp_path / "local")

    assert cache.stats()["same_root"] is False


def test_stats_ttls_are_formatted(tmp_path: Path) -> None:
    """Every kind's TTL is rendered with format_ttl, overrides included."""
    cache = Cache(tmp_path / "cache", ttls={"legal": None})

    stats = cache.stats()

    assert stats["ttls"]["legal"] == "never"
    assert stats["ttls"]["biblio"] == format_ttl(DEFAULT_TTLS["biblio"])


def test_stats_counts_bytes_expired_and_broken_per_kind(tmp_path: Path) -> None:
    """Per-kind counters reflect fresh, expired and broken entries on disk."""
    clock_value = datetime(2026, 9, 1, 12, 0, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: clock_value)
    cache.put("legal", "EP.1.A1", b"<old/>", ident="EP1A1")

    clock_value = datetime(2026, 9, 1, 12, 0, 0) + timedelta(hours=1)
    cache.put("legal", "EP.2.A1", b"<fresh/>", ident="EP2A1")
    cache.put("biblio", "EP.3.A1", b"<broken/>", ident="EP3A1")
    cache.content_path("biblio", "EP.3.A1").unlink()

    clock_value = datetime(2026, 9, 1, 12, 0, 0) + _ttl("legal") + timedelta(seconds=1)

    stats = cache.stats()

    assert stats["kinds"]["legal"]["entries"] == 2
    assert stats["kinds"]["legal"]["expired"] == 1
    assert stats["kinds"]["legal"]["broken"] == 0
    assert stats["kinds"]["legal"]["bytes"] == len(b"<old/>") + len(b"<fresh/>")
    assert datetime.fromisoformat(stats["kinds"]["legal"]["oldest"]) == datetime(
        2026, 9, 1, 12, 0, 0
    ).astimezone(UTC)
    assert datetime.fromisoformat(stats["kinds"]["legal"]["newest"]) == datetime(
        2026, 9, 1, 13, 0, 0
    ).astimezone(UTC)
    assert stats["kinds"]["biblio"]["entries"] == 1
    assert stats["kinds"]["biblio"]["broken"] == 1
    assert stats["kinds"]["biblio"]["bytes"] == 0
    assert stats["totals"]["entries"] == 3
    assert stats["totals"]["expired"] == 1
    assert stats["totals"]["broken"] == 1
    assert stats["totals"]["bytes"] == len(b"<old/>") + len(b"<fresh/>")


# --- Cache.select --------------------------------------------------------


def test_select_no_filters_returns_every_entry(tmp_path: Path) -> None:
    """With no filters given, select() returns every entry on disk."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "EP.1.A1", b"<biblio/>", ident="EP1A1")
    cache.put("search", "abc123", b"<search/>", ident="ti=drone")

    assert len(cache.select()) == 2


def test_select_without_expired_or_broken_flags_does_not_filter_by_state(
    tmp_path: Path,
) -> None:
    """With expired and broken both left False, entries are not filtered by state."""
    clock_value = datetime(2026, 9, 1, 12, 0, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: clock_value)
    cache.put("legal", "EP.OLD.A1", b"<old/>", ident="old")
    cache.put("biblio", "EP.BROKEN.A1", b"<broken/>", ident="broken")
    cache.content_path("biblio", "EP.BROKEN.A1").unlink()

    clock_value = datetime(2026, 9, 1, 12, 0, 0) + _ttl("legal") + timedelta(seconds=1)

    selected = cache.select()

    assert {e.key for e in selected} == {"EP.OLD.A1", "EP.BROKEN.A1"}


def test_select_kinds_filters_to_the_given_kinds(tmp_path: Path) -> None:
    """kinds restricts the result to the named kinds."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "EP.1.A1", b"<biblio/>", ident="EP1A1")
    cache.put("legal", "EP.1.A1", b"<legal/>", ident="EP1A1")
    cache.put("search", "abc123", b"<search/>", ident="ti=drone")

    selected = cache.select(kinds=["legal", "search"])

    assert {e.kind for e in selected} == {"legal", "search"}


def test_select_unknown_kind_raises_value_error(tmp_path: Path) -> None:
    """select() rejects an unknown kind named in kinds."""
    cache = Cache(tmp_path / "cache")

    with pytest.raises(ValueError):
        cache.select(kinds=["bogus"])


def test_select_pub_matches_only_the_publication_keyed_entry(tmp_path: Path) -> None:
    """pub restricts to the entry keyed by that publication number, never a search entry."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", pub_key("EP1234567A1"), b"<biblio/>", ident="EP1234567A1")
    cache.put("biblio", pub_key("EP7654321A1"), b"<other/>", ident="EP7654321A1")
    cache.put("search", "abc123", b"<search/>", ident="ti=drone")

    selected = cache.select(pub="EP1234567A1")

    assert [e.key for e in selected] == [pub_key("EP1234567A1")]


def test_select_older_than_boundary_is_inclusive(tmp_path: Path) -> None:
    """older_than includes an entry whose age exactly equals it, excludes a younger one."""
    clock_value = datetime(2026, 9, 1, 12, 0, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: clock_value)
    cache.put("biblio", "EP.BOUNDARY.A1", b"<boundary/>", ident="boundary")

    clock_value = datetime(2026, 9, 1, 12, 0, 5)
    cache.put("biblio", "EP.TOONEW.A1", b"<toonew/>", ident="toonew")

    clock_value = datetime(2026, 9, 1, 12, 0, 10)

    selected = cache.select(kinds=["biblio"], older_than=timedelta(seconds=10))

    assert {e.key for e in selected} == {"EP.BOUNDARY.A1"}


def test_select_older_than_excludes_entries_without_a_readable_sidecar(
    tmp_path: Path,
) -> None:
    """An entry whose sidecar cannot be read never matches an older_than filter."""
    cache = Cache(tmp_path / "cache")
    content_dir = tmp_path / "cache" / "ops" / "biblio"
    content_dir.mkdir(parents=True)
    (content_dir / "EP.1.A1.xml").write_bytes(b"<xml/>")

    selected = cache.select(kinds=["biblio"], older_than=timedelta(seconds=0))

    assert selected == []


def test_select_expired_only_matches_expired_entries(tmp_path: Path) -> None:
    """expired=True keeps only the entries that are past their TTL."""
    clock_value = datetime(2026, 9, 1, 12, 0, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: clock_value)
    cache.put("legal", "EP.OLD.A1", b"<old/>", ident="old")

    clock_value = datetime(2026, 9, 1, 12, 0, 0) + timedelta(hours=1)
    cache.put("legal", "EP.NEW.A1", b"<new/>", ident="new")

    clock_value = datetime(2026, 9, 1, 12, 0, 0) + _ttl("legal") + timedelta(seconds=1)

    selected = cache.select(kinds=["legal"], expired=True)

    assert {e.key for e in selected} == {"EP.OLD.A1"}


def test_select_broken_only_matches_broken_entries(tmp_path: Path) -> None:
    """broken=True keeps only the entries that cannot be served."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "EP.OK.A1", b"<ok/>", ident="ok")
    cache.put("biblio", "EP.BROKEN.A1", b"<broken/>", ident="broken")
    cache.content_path("biblio", "EP.BROKEN.A1").unlink()

    selected = cache.select(kinds=["biblio"], broken=True)

    assert {e.key for e in selected} == {"EP.BROKEN.A1"}


def test_select_expired_and_broken_is_an_or(tmp_path: Path) -> None:
    """When both expired and broken are True, an entry matching either is kept."""
    clock_value = datetime(2026, 9, 1, 12, 0, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: clock_value)
    cache.put("legal", "EP.OLD.A1", b"<old/>", ident="old")
    cache.put("biblio", "EP.BROKEN.A1", b"<broken/>", ident="broken")
    cache.content_path("biblio", "EP.BROKEN.A1").unlink()
    cache.put("biblio", "EP.OK.A1", b"<ok/>", ident="ok")

    clock_value = datetime(2026, 9, 1, 12, 0, 0) + _ttl("legal") + timedelta(seconds=1)

    selected = cache.select(expired=True, broken=True)

    assert {e.key for e in selected} == {"EP.OLD.A1", "EP.BROKEN.A1"}


# --- Cache.remove --------------------------------------------------------


def test_remove_deletes_the_body_sidecar_and_tmp_leftovers(tmp_path: Path) -> None:
    """remove() deletes the body, sidecar and both kinds of stray .tmp files."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "EP.1.A1", b"<xml/>", ident="EP1A1")
    kind_dir = tmp_path / "cache" / "ops" / "biblio"
    (kind_dir / "EP.1.A1.xml.tmp").write_bytes(b"<half")
    (kind_dir / "EP.1.A1.meta.json.tmp").write_text('{"kind"', encoding="utf-8")
    (entry,) = cache.entries("biblio")

    result = cache.remove([entry])

    assert not (kind_dir / "EP.1.A1.xml").exists()
    assert not (kind_dir / "EP.1.A1.meta.json").exists()
    assert not (kind_dir / "EP.1.A1.xml.tmp").exists()
    assert not (kind_dir / "EP.1.A1.meta.json.tmp").exists()
    assert result["removed"] == 4
    assert result["bytes"] == len(b"<xml/>")
    assert result["errors"] == []
    assert set(result["paths"]) == {
        str(kind_dir / "EP.1.A1.xml"),
        str(kind_dir / "EP.1.A1.meta.json"),
        str(kind_dir / "EP.1.A1.xml.tmp"),
        str(kind_dir / "EP.1.A1.meta.json.tmp"),
    }


def test_remove_ignores_files_that_do_not_exist(tmp_path: Path) -> None:
    """remove() silently skips paths that are already gone, without an error."""
    cache = Cache(tmp_path / "cache")
    kind_dir = tmp_path / "cache" / "ops" / "biblio"
    kind_dir.mkdir(parents=True)
    entry = CacheEntry(
        kind="biblio",
        key="EP.1.A1",
        root="shared",
        path=kind_dir / "EP.1.A1.xml",
        meta_path=kind_dir / "EP.1.A1.meta.json",
        ident=None,
        fetched_at=None,
        size=0,
        expired=False,
        broken=True,
        problem="missing body",
    )

    result = cache.remove([entry])

    assert result == {"removed": 0, "bytes": 0, "paths": [], "errors": []}


def test_remove_refuses_a_path_outside_the_cache_roots(tmp_path: Path) -> None:
    """A hand-built entry pointing outside the cache roots is not deleted."""
    cache = Cache(tmp_path / "cache")
    outside = tmp_path / "escape.xml"
    outside.write_bytes(b"do not touch")
    entry = CacheEntry(
        kind="biblio",
        key="escape",
        root="shared",
        path=outside,
        meta_path=tmp_path / "escape.meta.json",
        ident="escape",
        fetched_at="2026-09-01T00:00:00",
        size=len(b"do not touch"),
        expired=False,
        broken=False,
        problem=None,
    )

    result = cache.remove([entry])

    assert outside.read_bytes() == b"do not touch"
    assert result["removed"] == 0
    assert any(e["path"] == str(outside) for e in result["errors"])


def test_remove_deletes_a_symlink_without_touching_its_target(tmp_path: Path) -> None:
    """A symlink living under a cache root is removed; its target outside tmp_path is untouched."""
    cache = Cache(tmp_path / "cache")
    fd, raw_name = tempfile.mkstemp()
    outside_target = Path(raw_name)
    try:
        with open(fd, "wb") as handle:
            handle.write(b"external content")
        kind_dir = tmp_path / "cache" / "ops" / "biblio"
        kind_dir.mkdir(parents=True)
        link_path = kind_dir / "EP.1.A1.xml"
        link_path.symlink_to(outside_target)
        entry = CacheEntry(
            kind="biblio",
            key="EP.1.A1",
            root="shared",
            path=link_path,
            meta_path=kind_dir / "EP.1.A1.meta.json",
            ident=None,
            fetched_at=None,
            size=0,
            expired=False,
            broken=True,
            problem="missing sidecar",
        )

        result = cache.remove([entry])

        assert not link_path.is_symlink()
        assert outside_target.read_bytes() == b"external content"
        assert result["errors"] == []
        assert str(link_path) in result["paths"]
    finally:
        outside_target.unlink(missing_ok=True)


def test_remove_never_deletes_a_directory(tmp_path: Path) -> None:
    """remove() leaves the kind directory itself in place."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "EP.1.A1", b"<xml/>", ident="EP1A1")
    kind_dir = tmp_path / "cache" / "ops" / "biblio"
    (entry,) = cache.entries("biblio")

    cache.remove([entry])

    assert kind_dir.is_dir()


def test_remove_returns_total_count_and_bytes_across_several_entries(tmp_path: Path) -> None:
    """remove() sums removed file counts and freed body bytes across all entries."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "EP.1.A1", b"<one/>", ident="one")
    cache.put("legal", "EP.1.A1", b"<two-x/>", ident="two")
    entries = cache.entries()

    result = cache.remove(entries)

    assert result["removed"] == 4  # 2 bodies + 2 sidecars
    assert result["bytes"] == len(b"<one/>") + len(b"<two-x/>")
    assert cache.entries() == []


# --- key validation ---------------------------------------------------------

# Keys that must never reach the filesystem: they escape the kind directory or
# carry characters the cache layout does not allow.
BAD_KEYS: tuple[str, ...] = (
    "",
    "..",
    "../escape",
    "sub/dir",
    "back\\slash",
    "EP.1.A1\x00",
    "with space",
    "EP.1.A1;rm",
)


@pytest.mark.parametrize("key", BAD_KEYS)
def test_put_rejects_a_key_outside_the_allowed_characters(tmp_path: Path, key: str) -> None:
    """put() refuses a key that is not made of [A-Za-z0-9._-] characters."""
    cache = Cache(tmp_path / "cache")

    with pytest.raises(ValueError):
        cache.put("biblio", key, b"<xml/>", ident="EP1A1")


@pytest.mark.parametrize("key", BAD_KEYS)
def test_get_rejects_a_key_outside_the_allowed_characters(tmp_path: Path, key: str) -> None:
    """get() refuses the same keys put() refuses, so a lookup cannot escape either."""
    cache = Cache(tmp_path / "cache")

    with pytest.raises(ValueError):
        cache.get("biblio", key)


def test_put_refusing_a_bad_key_writes_nothing(tmp_path: Path) -> None:
    """A rejected key leaves the cache tree untouched."""
    cache = Cache(tmp_path / "cache")

    with pytest.raises(ValueError):
        cache.put("biblio", "../escape", b"<xml/>", ident="EP1A1")

    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize(
    "pub", ["EP1234567A1", "US2024/0111636A1", "WO2020/123456A1", "JP2019123456A"]
)
def test_keys_produced_by_pub_key_are_accepted(tmp_path: Path, pub: str) -> None:
    """Every key pub_key() produces passes the key check."""
    cache = Cache(tmp_path / "cache")

    assert cache.put("biblio", pub_key(pub), b"<xml/>", ident=pub).is_file()


def test_keys_produced_by_search_key_are_accepted(tmp_path: Path) -> None:
    """Every key search_key() produces passes the key check."""
    cache = Cache(tmp_path / "cache")
    key = search_key("ti=drone and pd within 2020", 1, 25)

    assert cache.put("search", key, b"<xml/>", ident="ti=drone").is_file()


# --- temporary file names ----------------------------------------------------


def test_put_uses_a_unique_temporary_name_per_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The temp sibling carries the pid and a random part, so two writers cannot collide."""
    seen: list[str] = []
    real_write_bytes = Path.write_bytes

    def spy(self: Path, data: bytes) -> int:
        seen.append(self.name)
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", spy)
    cache = Cache(tmp_path / "cache")

    cache.put("biblio", "EP.1.A1", b"<xml/>", ident="EP1A1")
    cache.put("biblio", "EP.1.A1", b"<xml/>", ident="EP1A1")

    tmp_names = [name for name in seen if name.endswith(".tmp")]
    assert len(tmp_names) == 4  # two bodies and two sidecars
    assert len(set(tmp_names)) == 4
    assert all(str(os.getpid()) in name for name in tmp_names)
    assert all(name.startswith(("EP.1.A1.xml.", "EP.1.A1.meta.json.")) for name in tmp_names)


def test_concurrent_put_of_the_same_key_leaves_a_consistent_entry(tmp_path: Path) -> None:
    """Two threads writing the same key never fail and leave one readable entry."""
    cache = Cache(tmp_path / "cache")
    payloads = (b"A" * 64, b"B" * 64)
    rounds = 200
    start = threading.Barrier(len(payloads))
    failures: list[BaseException] = []

    def writer(payload: bytes) -> None:
        start.wait(timeout=30)
        try:
            for _ in range(rounds):
                cache.put("biblio", "EP.1.A1", payload, ident="EP1A1")
        except BaseException as exc:  # noqa: BLE001 - reported through the assertion below
            failures.append(exc)

    threads = [threading.Thread(target=writer, args=(payload,)) for payload in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert failures == []
    assert all(not thread.is_alive() for thread in threads)
    hit = cache.get("biblio", "EP.1.A1")
    assert hit is not None
    assert hit.content in payloads
    (entry,) = cache.entries("biblio")
    assert entry.broken is False
    assert list((tmp_path / "cache").rglob("*.tmp")) == []


def test_entries_reports_a_stray_tmp_file_written_with_a_unique_name(tmp_path: Path) -> None:
    """A leftover temp file with the new pid/random name is still a "stray tmp file"."""
    cache = Cache(tmp_path / "cache")
    content_dir = tmp_path / "cache" / "ops" / "biblio"
    content_dir.mkdir(parents=True)
    (content_dir / "EP.1.A1.xml.4321.0a1b2c3d.tmp").write_bytes(b"<half")

    (entry,) = cache.entries("biblio")

    assert entry.key == "EP.1.A1"
    assert entry.broken is True
    assert entry.problem == "stray tmp file"


def test_remove_deletes_a_stray_tmp_file_written_with_a_unique_name(tmp_path: Path) -> None:
    """remove() cleans up temp leftovers whatever their unique suffix is."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "EP.1.A1", b"<xml/>", ident="EP1A1")
    kind_dir = tmp_path / "cache" / "ops" / "biblio"
    body_tmp = kind_dir / "EP.1.A1.xml.4321.0a1b2c3d.tmp"
    meta_tmp = kind_dir / "EP.1.A1.meta.json.4321.0a1b2c3d.tmp"
    body_tmp.write_bytes(b"<half")
    meta_tmp.write_text('{"kind"', encoding="utf-8")

    result = cache.remove(cache.entries("biblio"))

    assert not body_tmp.exists()
    assert not meta_tmp.exists()
    assert result["errors"] == []
    assert list(kind_dir.iterdir()) == []


# --- listing races -----------------------------------------------------------


def test_entries_survives_a_body_deleted_between_listing_and_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body removed by another process after the listing is reported, not raised."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "EP.1.A1", b"<xml/>", ident="EP1A1")
    body = cache.content_path("biblio", "EP.1.A1")
    real_stat = Path.stat
    deleted: list[Path] = []

    def stat_after_deleting(self: Path, **kwargs: object) -> os.stat_result:
        # Delete the body on the first stat of it, reproducing a concurrent
        # ``clean`` between iterdir() and stat().
        if self == body and not deleted:
            deleted.append(body)
            body.unlink()
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_after_deleting)

    (entry,) = cache.entries("biblio")

    assert entry.size == 0
    assert entry.broken is True
    assert entry.problem == "missing body"


def test_stats_and_select_survive_a_body_deleted_between_listing_and_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same race does not break stats() or select() either."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "EP.1.A1", b"<xml/>", ident="EP1A1")
    body = cache.content_path("biblio", "EP.1.A1")
    real_stat = Path.stat
    deleted: list[Path] = []

    def stat_after_deleting(self: Path, **kwargs: object) -> os.stat_result:
        if self == body and not deleted:
            deleted.append(body)
            body.unlink()
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_after_deleting)

    stats = cache.stats()
    selected = cache.select(broken=True)

    assert stats["kinds"]["biblio"]["broken"] == 1
    assert stats["kinds"]["biblio"]["bytes"] == 0
    assert [entry.key for entry in selected] == ["EP.1.A1"]


# --- fetched_at is stored as UTC ---------------------------------------------


def test_put_records_fetched_at_as_an_offset_aware_utc_timestamp(tmp_path: Path) -> None:
    """A sidecar written since v1.0 carries UTC, so a TZ change cannot shift it."""
    fixed_now = datetime(2026, 9, 2, 10, 30, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: fixed_now)

    cache.put("legal", "EP.1.A1", b"<xml/>", ident="EP1A1")

    meta = json.loads(cache.meta_path("legal", "EP.1.A1").read_text(encoding="utf-8"))
    assert meta["fetched_at"].endswith("+00:00")
    assert datetime.fromisoformat(meta["fetched_at"]) == fixed_now.astimezone(UTC)


def test_put_keeps_an_already_aware_clock_in_utc(tmp_path: Path) -> None:
    """An offset-aware clock is converted to UTC rather than stored as given."""
    fixed_now = datetime(2026, 9, 2, 10, 30, 0, tzinfo=UTC)
    cache = Cache(tmp_path / "cache", clock=lambda: fixed_now)

    cache.put("legal", "EP.1.A1", b"<xml/>", ident="EP1A1")

    meta = json.loads(cache.meta_path("legal", "EP.1.A1").read_text(encoding="utf-8"))
    assert meta["fetched_at"] == "2026-09-02T10:30:00+00:00"


def test_get_still_reads_a_naive_sidecar_written_before_v1_0(tmp_path: Path) -> None:
    """A naive fetched_at is read as local time, so pre-v1.0 entries keep working."""
    clock_value = datetime(2026, 9, 2, 10, 0, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: clock_value)
    content_dir = tmp_path / "cache" / "ops" / "legal"
    content_dir.mkdir(parents=True)
    (content_dir / "EP.1.A1.xml").write_bytes(b"<xml/>")
    (content_dir / "EP.1.A1.meta.json").write_text(
        json.dumps(
            {
                "kind": "legal",
                "key": "EP.1.A1",
                "ident": "EP1A1",
                "fetched_at": "2026-09-01T10:00:00",
                "size": len(b"<xml/>"),
            }
        ),
        encoding="utf-8",
    )

    hit = cache.get("legal", "EP.1.A1")

    assert hit is not None
    assert hit.fetched_at == "2026-09-01T10:00:00"


def test_ttl_is_evaluated_the_same_for_naive_and_aware_sidecars(tmp_path: Path) -> None:
    """The naive (pre-v1.0) and the aware (v1.0) spellings expire at the same moment."""
    written_at = datetime(2026, 9, 2, 10, 0, 0)
    clock_value = written_at
    cache = Cache(tmp_path / "cache", clock=lambda: clock_value)
    cache.put("legal", "EP.AWARE.A1", b"<xml/>", ident="aware")
    naive_dir = tmp_path / "cache" / "ops" / "legal"
    (naive_dir / "EP.NAIVE.A1.xml").write_bytes(b"<xml/>")
    (naive_dir / "EP.NAIVE.A1.meta.json").write_text(
        json.dumps(
            {
                "kind": "legal",
                "key": "EP.NAIVE.A1",
                "ident": "naive",
                "fetched_at": written_at.isoformat(timespec="seconds"),
                "size": len(b"<xml/>"),
            }
        ),
        encoding="utf-8",
    )
    legal_ttl = _ttl("legal")

    clock_value = written_at + legal_ttl - timedelta(seconds=1)
    assert cache.get("legal", "EP.AWARE.A1") is not None
    assert cache.get("legal", "EP.NAIVE.A1") is not None

    clock_value = written_at + legal_ttl
    assert cache.get("legal", "EP.AWARE.A1") is None
    assert cache.get("legal", "EP.NAIVE.A1") is None


# --- select() time handling and shortcuts ------------------------------------


def test_select_older_than_handles_an_offset_aware_timestamp(tmp_path: Path) -> None:
    """An aware fetched_at is compared by instant instead of raising TypeError."""
    clock_value = datetime(2026, 9, 1, 12, 0, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: clock_value)
    content_dir = tmp_path / "cache" / "ops" / "biblio"
    content_dir.mkdir(parents=True)
    (content_dir / "EP.1.A1.xml").write_bytes(b"<xml/>")
    (content_dir / "EP.1.A1.meta.json").write_text(
        json.dumps(
            {
                "kind": "biblio",
                "key": "EP.1.A1",
                "ident": "EP1A1",
                "fetched_at": "2026-09-01T20:00:00+09:00",  # 11:00 UTC
                "size": len(b"<xml/>"),
            }
        ),
        encoding="utf-8",
    )

    clock_value = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
    selected = cache.select(kinds=["biblio"], older_than=timedelta(minutes=59))
    too_young = cache.select(kinds=["biblio"], older_than=timedelta(minutes=61))

    assert [entry.key for entry in selected] == ["EP.1.A1"]
    assert too_young == []


def test_select_older_than_ignores_a_non_string_timestamp(tmp_path: Path) -> None:
    """A numeric fetched_at is not trusted, and does not raise TypeError."""
    cache = Cache(tmp_path / "cache")
    content_dir = tmp_path / "cache" / "ops" / "biblio"
    content_dir.mkdir(parents=True)
    (content_dir / "EP.1.A1.xml").write_bytes(b"<xml/>")
    (content_dir / "EP.1.A1.meta.json").write_text(
        json.dumps(
            {
                "kind": "biblio",
                "key": "EP.1.A1",
                "ident": "EP1A1",
                "fetched_at": 1756800000,
                "size": len(b"<xml/>"),
            }
        ),
        encoding="utf-8",
    )

    assert cache.select(kinds=["biblio"], older_than=timedelta(seconds=0)) == []
    assert cache.entries("biblio")[0].expired is True


def _explode_for_kind(self: Cache, kind: str) -> list[CacheEntry]:
    """Stand-in for _entries_for_kind that must not be called."""
    raise AssertionError(f"_entries_for_kind called for {kind!r}")


def test_select_pub_does_not_list_every_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pub filter stats the candidate paths instead of walking every kind directory."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", pub_key("EP1234567A1"), b"<biblio/>", ident="EP1234567A1")
    cache.put("legal", pub_key("EP1234567A1"), b"<legal/>", ident="EP1234567A1")
    cache.put("search", "abc123", b"<search/>", ident="ti=drone")
    monkeypatch.setattr(Cache, "_entries_for_kind", _explode_for_kind)

    selected = cache.select(pub="EP1234567A1")

    assert {(entry.kind, entry.key) for entry in selected} == {
        ("biblio", "EP.1234567.A1"),
        ("legal", "EP.1234567.A1"),
    }


def test_select_pub_still_honours_the_other_filters(tmp_path: Path) -> None:
    """The pub shortcut applies the kinds, expired and broken filters as before."""
    clock_value = datetime(2026, 9, 1, 12, 0, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: clock_value)
    cache.put("biblio", pub_key("EP1234567A1"), b"<biblio/>", ident="EP1234567A1")
    cache.put("legal", pub_key("EP1234567A1"), b"<legal/>", ident="EP1234567A1")

    clock_value = datetime(2026, 9, 1, 12, 0, 0) + _ttl("legal") + timedelta(seconds=1)

    assert [e.kind for e in cache.select(pub="EP1234567A1", kinds=["legal"])] == ["legal"]
    assert [e.kind for e in cache.select(pub="EP1234567A1", expired=True)] == ["legal"]
    assert cache.select(pub="EP7654321A1") == []


def test_select_pub_reports_a_broken_entry_of_that_publication(tmp_path: Path) -> None:
    """The shortcut finds an entry whose body is gone, exactly as a full listing would."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", pub_key("EP1234567A1"), b"<biblio/>", ident="EP1234567A1")
    cache.content_path("biblio", pub_key("EP1234567A1")).unlink()

    (entry,) = cache.select(pub="EP1234567A1", broken=True)

    assert entry.problem == "missing body"
    assert entry.size == 0


# --- eviction throttling -----------------------------------------------------


def test_eviction_is_skipped_shortly_after_the_previous_sweep(tmp_path: Path) -> None:
    """A second put() within a minute does not walk the kind directory again."""
    clock_value = datetime(2026, 9, 2, 10, 0, 0)
    cache = Cache(
        tmp_path / "cache", ttls={"search": timedelta(seconds=1)}, clock=lambda: clock_value
    )
    cache.put("search", "aaa111", b"<first/>", ident="ti=first")

    clock_value = datetime(2026, 9, 2, 10, 0, 30)
    cache.put("search", "bbb222", b"<second/>", ident="ti=second")

    assert cache.content_path("search", "aaa111").is_file()


def test_eviction_runs_again_once_the_interval_has_passed(tmp_path: Path) -> None:
    """Once a minute has passed, the next put() sweeps the expired entries away."""
    clock_value = datetime(2026, 9, 2, 10, 0, 0)
    cache = Cache(
        tmp_path / "cache", ttls={"search": timedelta(seconds=1)}, clock=lambda: clock_value
    )
    cache.put("search", "aaa111", b"<first/>", ident="ti=first")

    clock_value = datetime(2026, 9, 2, 10, 0, 30)
    cache.put("search", "bbb222", b"<second/>", ident="ti=second")

    clock_value = datetime(2026, 9, 2, 10, 1, 30)
    cache.put("search", "ccc333", b"<third/>", ident="ti=third")

    assert not cache.content_path("search", "aaa111").exists()
    assert not cache.content_path("search", "bbb222").exists()
    assert cache.content_path("search", "ccc333").is_file()


def test_eviction_throttling_is_per_kind(tmp_path: Path) -> None:
    """A sweep of one search kind does not silence the other one."""
    clock_value = datetime(2026, 9, 2, 10, 0, 0)
    cache = Cache(
        tmp_path / "cache",
        ttls={"search": timedelta(seconds=1), "searchbib": timedelta(seconds=1)},
        clock=lambda: clock_value,
    )
    cache.put("searchbib", "aaa111", b"<first/>", ident="ti=first")

    clock_value = datetime(2026, 9, 2, 10, 1, 10)
    cache.put("search", "bbb222", b"<second/>", ident="ti=second")
    cache.put("searchbib", "ccc333", b"<third/>", ident="ti=third")

    assert not cache.content_path("searchbib", "aaa111").exists()


def test_first_put_of_a_new_cache_always_evicts(tmp_path: Path) -> None:
    """Throttling never suppresses the first sweep of a freshly built Cache."""
    clock_value = datetime(2026, 9, 2, 10, 0, 0)
    first = Cache(
        tmp_path / "cache", ttls={"search": timedelta(seconds=1)}, clock=lambda: clock_value
    )
    first.put("search", "aaa111", b"<first/>", ident="ti=first")

    clock_value = datetime(2026, 9, 2, 10, 0, 30)
    second = Cache(
        tmp_path / "cache", ttls={"search": timedelta(seconds=1)}, clock=lambda: clock_value
    )
    second.put("search", "bbb222", b"<second/>", ident="ti=second")

    assert not first.content_path("search", "aaa111").exists()


# --- quick_counts ------------------------------------------------------------


def test_quick_counts_reports_a_zero_for_every_kind_on_an_empty_cache(tmp_path: Path) -> None:
    """quick_counts() names every kind even when nothing has been written."""
    counts = Cache(tmp_path / "cache").quick_counts()

    assert counts == dict.fromkeys(cache_mod.KINDS, 0)


def test_quick_counts_counts_the_sidecars_of_each_kind(tmp_path: Path) -> None:
    """Each kind is counted in its own root and directory."""
    cache = Cache(tmp_path / "shared", tmp_path / "local")
    cache.put("biblio", "EP.1.A1", b"<biblio/>", ident="EP1A1")
    cache.put("biblio", "EP.2.A1", b"<biblio/>", ident="EP2A1")
    cache.put("gp", "US.1.A1", b"<html/>", ident="US1A1")
    cache.put("search", "abc123", b"<search/>", ident="ti=drone")

    counts = cache.quick_counts()

    assert counts["biblio"] == 2
    assert counts["gp"] == 1
    assert counts["search"] == 1
    assert counts["legal"] == 0


def test_quick_counts_ignores_bodies_and_temporary_files(tmp_path: Path) -> None:
    """Only sidecars are counted, so half-written entries do not inflate the total."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "EP.1.A1", b"<biblio/>", ident="EP1A1")
    content_dir = tmp_path / "cache" / "ops" / "biblio"
    (content_dir / "EP.2.A1.xml").write_bytes(b"<orphan/>")
    (content_dir / "EP.3.A1.meta.json.4321.0a1b2c3d.tmp").write_text("{", encoding="utf-8")

    assert cache.quick_counts()["biblio"] == 1


def test_quick_counts_does_not_read_any_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """quick_counts() only lists directory entries: no JSON is parsed."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "EP.1.A1", b"<biblio/>", ident="EP1A1")

    def no_reading(*args: object, **kwargs: object) -> object:
        raise AssertionError("quick_counts() must not open cache files")

    monkeypatch.setattr(Path, "read_text", no_reading)
    monkeypatch.setattr(Path, "read_bytes", no_reading)
    monkeypatch.setattr(Path, "open", no_reading)

    assert cache.quick_counts()["biblio"] == 1


# --- compatibility names -----------------------------------------------------


def test_v0_3_compatibility_names_say_so_in_their_docstrings() -> None:
    """The names kept only for v0.3 callers document that they are not used here."""
    note = "kept for v0.3 compatibility; not used by the package itself"
    docs = [cache_mod.Cache.base.__doc__, cache_mod.CacheHit.raw_path.__doc__]
    assert all(doc is not None and note in " ".join(doc.split()) for doc in docs)

    # A module-level constant has no runtime docstring, so its comment block
    # is checked in the source instead.
    source = Path(cache_mod.__file__).read_text(encoding="utf-8")
    declaration = source.index("NO_EXPIRY_KINDS: frozenset")
    comment_block = source[:declaration].rsplit("\n\n", 1)[-1]
    assert note in " ".join(comment_block.replace("#", " ").split())


def test_replace_with_retry_survives_a_transiently_busy_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename refused twice (Windows-style PermissionError) is retried and succeeds."""
    from patent_checker import cache as cache_module

    source = tmp_path / "payload.tmp"
    source.write_bytes(b"new")
    target = tmp_path / "payload"
    target.write_bytes(b"old")
    real_replace = os.replace
    refusals = {"left": 2}

    def flaky_replace(src: str | Path, dst: str | Path) -> None:
        if refusals["left"]:
            refusals["left"] -= 1
            raise PermissionError(13, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(cache_module.os, "replace", flaky_replace)
    monkeypatch.setattr(cache_module.time, "sleep", lambda _seconds: None)

    cache_module._replace_with_retry(source, target, attempts=3)

    assert target.read_bytes() == b"new"
    assert not source.exists()


def test_replace_with_retry_gives_up_after_the_last_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target that never frees up surfaces the PermissionError after the retries."""
    from patent_checker import cache as cache_module

    source = tmp_path / "payload.tmp"
    source.write_bytes(b"new")
    target = tmp_path / "payload"

    def refusing_replace(src: str | Path, dst: str | Path) -> None:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(cache_module.os, "replace", refusing_replace)
    monkeypatch.setattr(cache_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(PermissionError):
        cache_module._replace_with_retry(source, target, attempts=3)
