"""Tests for the OPS/GP response file cache (patent_checker/cache.py)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from patent_checker import cache as cache_mod
from patent_checker import config
from patent_checker.cache import (
    DEFAULT_TTLS,
    Cache,
    content_suffix,
    default_cache,
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
def test_is_fresh_false_for_offset_aware_timestamp(kind: str) -> None:
    """Cache timestamps are naive local time; an offset-aware one is not trusted."""
    now = datetime(2026, 9, 2, 12, 0, 0)
    assert is_fresh(kind, "2026-09-02T11:59:00+09:00", now) is False


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
    assert hit.fetched_at == "2026-09-02T10:30:00"
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
    assert meta["fetched_at"] == "2026-09-02T10:30:00"
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
