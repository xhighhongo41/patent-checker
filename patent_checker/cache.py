"""File cache for EPO OPS and Google Patents responses.

Two roots, one layout
---------------------

A :class:`Cache` is built from two roots and routes each kind to one of
them:

- ``shared`` holds the publication-keyed kinds (:data:`SHARED_KINDS`).
  Their content is tied to a published document, so one copy per user is
  worth keeping across projects and across the CLI and the MCP server.
- ``local`` holds the search-keyed kinds (:data:`LOCAL_KINDS`). Search
  results belong to the project that ran the query and go stale quickly,
  so they stay next to that project's data.

Passing a single root (``local=None``) puts everything under it; the MCP
server does this, since its data directory already is the per-user one.

On disk, an entry of *kind* keyed by *key* is a body file next to a JSON
sidecar ``<key>.meta.json``:

- ``<root>/ops/<kind>/<key>.xml`` for the OPS kinds, and
- ``<root>/gp/<key>.html`` for the Google Patents kind.

Two families of keys are used, matching the two shapes of request this
covers:

- Publication-keyed kinds (``biblio``, ``claims``, ``legal``, ``family``,
  ``gp``) are keyed by the DOCDB spelling of the publication number
  (:func:`pub_key`).
- Search-keyed kinds (``search``, ``searchbib``) are keyed by a hash of
  the CQL query and paging window (:func:`search_key`); the CQL text is
  not normalized, so whitespace differences are deliberately different
  entries.

A cache hit serves the stored bytes without touching the network, so it is
never logged to ``headers.jsonl`` (that log stays an accurate record of
real upstream usage).

Freshness
---------

Each kind has a time-to-live (:data:`DEFAULT_TTLS`); ``None`` means the
entry never expires:

===========  ==============================================================
kind         default TTL
===========  ==============================================================
biblio       90 days
claims       never expires (a published claim set does not change)
legal        7 days
family       30 days
gp           never expires
search       1 day
searchbib    1 day
===========  ==============================================================

A :class:`Cache` may be built with per-kind overrides (the CLI takes them
from ``$PATENT_CHECKER_CACHE_TTL`` via
:func:`patent_checker.config.cache_ttl_overrides`). An entry is fresh
while ``fetched_at + ttl`` is strictly after "now", so an entry exactly at
its deadline has expired.

Durability
----------

Both files are written to a ``.tmp`` sibling and moved into place with
``os.replace``, so a crash mid-write can leave a stray ``.tmp`` file but
never a truncated body or sidecar in place of a previously good entry.
:meth:`Cache.get` additionally verifies the body against the ``size``
recorded in the sidecar, so a body damaged after the fact is reported as a
miss rather than served as a short response; the next :meth:`Cache.put`
replaces both files and restores a normal hit.

The sidecar carries ``kind``, ``key``, ``ident``, ``fetched_at`` and
``size``. Sidecars written by v0.3 instead carry a ``raw_path`` and no
``size``; they are still read, simply without the size check.

Search entries are the only ones that accumulate without bound, so every
:meth:`Cache.put` of a search-keyed kind also removes the expired entries
sitting in that kind's directory.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

from patent_checker import config
from patent_checker.pubnum import parse_pubnum

# Kinds keyed by publication number (DOCDB spelling).
PUB_KINDS: tuple[str, ...] = ("biblio", "claims", "legal", "family", "gp")

# Kinds keyed by a hash of the CQL query and paging window.
SEARCH_KINDS: tuple[str, ...] = ("search", "searchbib")

KINDS: tuple[str, ...] = PUB_KINDS + SEARCH_KINDS

# Kinds stored under the shared root: one copy per user, reusable by every
# project, because their content belongs to a published document.
SHARED_KINDS: frozenset[str] = frozenset(PUB_KINDS)

# Kinds stored under the local (project) root: query-shaped and short-lived.
LOCAL_KINDS: frozenset[str] = frozenset(SEARCH_KINDS)

# Per-kind time-to-live; ``None`` means the entry never expires.
DEFAULT_TTLS: dict[str, timedelta | None] = {
    "biblio": timedelta(days=90),
    "claims": None,
    "legal": timedelta(days=7),
    "family": timedelta(days=30),
    "gp": None,
    "search": timedelta(days=1),
    "searchbib": timedelta(days=1),
}

# Kinds whose default policy is "never expires".
NO_EXPIRY_KINDS: frozenset[str] = frozenset(
    kind for kind, ttl in DEFAULT_TTLS.items() if ttl is None
)

# Sidecar fields an entry must carry to be served; ``size`` is optional so
# that sidecars written before v0.4 stay readable.
_REQUIRED_META_FIELDS: tuple[str, ...] = ("kind", "key", "ident", "fetched_at")

# Suffix of the sidecar file, relative to the key.
_META_SUFFIX = ".meta.json"

# Suffix of the half-written file a ``put`` moves into place.
_TMP_SUFFIX = ".tmp"


@dataclass(frozen=True)
class CacheHit:
    """One cache entry read back by :meth:`Cache.get`.

    Attributes:
        content: The cached response body.
        fetched_at: ISO 8601 local timestamp recorded when the entry was
            written (seconds precision).
        ident: The human-readable identifier (CQL query or publication
            number) the entry was written with.
        path: The cache body file this entry was read from.
    """

    content: bytes
    fetched_at: str
    ident: str
    path: Path

    @property
    def raw_path(self) -> str:
        """Return the body file as a string: the only copy kept on disk."""
        return str(self.path)


def pub_key(pub: str) -> str:
    """Return the cache key for a publication-keyed kind: its DOCDB spelling.

    Args:
        pub: Publication number in any spelling accepted by ``parse_pubnum``.

    Raises:
        ValueError: If *pub* is not a parseable publication number.
    """
    return parse_pubnum(pub).docdb()


def search_key(cql: str, begin: int, end: int) -> str:
    """Return the cache key for a search-keyed kind.

    The key is the first 16 hex characters of the SHA-256 digest of the CQL
    query and paging window; *cql* is hashed as given, without normalizing
    whitespace, so two queries that differ only in spacing are different
    entries by design.

    Args:
        cql: CQL query expression.
        begin: First hit of the requested page (1-based, inclusive).
        end: Last hit of the requested page (inclusive).
    """
    digest = hashlib.sha256(f"{cql}\n{begin}-{end}".encode())
    return digest.hexdigest()[:16]


def _check_kind(kind: str) -> None:
    """Raise ``ValueError`` unless *kind* is one of :data:`KINDS`."""
    if kind not in KINDS:
        raise ValueError(f"unknown cache kind: {kind!r}")


def kind_subdir(kind: str) -> Path:
    """Return the directory of *kind* relative to its root.

    The Google Patents kind sits directly under ``gp/``; every OPS kind
    sits under ``ops/<kind>/``, keeping upstream sources apart on disk.

    Args:
        kind: One of :data:`KINDS`.

    Raises:
        ValueError: If *kind* is not one of :data:`KINDS`.
    """
    _check_kind(kind)
    if kind == "gp":
        return Path("gp")
    return Path("ops") / kind


def content_suffix(kind: str) -> str:
    """Return the file suffix of *kind*'s body file.

    Args:
        kind: One of :data:`KINDS`.

    Raises:
        ValueError: If *kind* is not one of :data:`KINDS`.
    """
    _check_kind(kind)
    return ".html" if kind == "gp" else ".xml"


def is_fresh(
    kind: str,
    fetched_at: str,
    now: datetime,
    *,
    ttls: Mapping[str, timedelta | None] | None = None,
) -> bool:
    """Return True if an entry of *kind* fetched at *fetched_at* is still usable.

    The entry is fresh while ``fetched_at + ttl`` is strictly after *now*;
    an entry exactly at its deadline has expired. A ``None`` TTL means the
    kind never expires, in which case *fetched_at* is not even parsed.

    Args:
        kind: One of :data:`KINDS`.
        fetched_at: ISO 8601 timestamp the entry was written with. A value
            that cannot be parsed, or that carries a UTC offset (cache
            timestamps are naive local time), counts as expired.
        now: The moment freshness is evaluated at.
        ttls: Per-kind TTL overrides. Kinds missing from it, and every kind
            when it is ``None``, fall back to :data:`DEFAULT_TTLS`.

    Raises:
        ValueError: If *kind* is not one of :data:`KINDS`.
    """
    _check_kind(kind)
    effective = DEFAULT_TTLS if ttls is None else ttls
    ttl = effective.get(kind, DEFAULT_TTLS[kind])
    if ttl is None:
        return True
    try:
        fetched_at_dt = datetime.fromisoformat(fetched_at)
        return fetched_at_dt + ttl > now
    except (TypeError, ValueError):
        # A malformed timestamp, and an offset-aware one that cannot be
        # compared with the naive local *now*, both mean "do not trust it".
        return False


class Cache:
    """File cache spread over a shared root and a local (project) root.

    Nothing is created on disk until the first :meth:`put`.
    """

    def __init__(
        self,
        shared: Path,
        local: Path | None = None,
        *,
        ttls: Mapping[str, timedelta | None] | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        """Initialize the cache.

        Args:
            shared: Root of the publication-keyed kinds
                (e.g. ``<cache_base>``).
            local: Root of the search-keyed kinds (e.g.
                ``<data_base>/cache``). Defaults to *shared*, putting every
                kind under one root.
            ttls: Per-kind TTL overrides layered on :data:`DEFAULT_TTLS`;
                ``None`` as a value means the kind never expires.
            clock: Source of "now", injectable for tests.

        Raises:
            ValueError: If *ttls* names a kind that is not in :data:`KINDS`.
        """
        self._shared = shared
        self._local = shared if local is None else local
        merged: dict[str, timedelta | None] = dict(DEFAULT_TTLS)
        if ttls is not None:
            for kind, ttl in ttls.items():
                _check_kind(kind)
                merged[kind] = ttl
        self._ttls: Mapping[str, timedelta | None] = MappingProxyType(merged)
        self._clock = clock

    @property
    def shared(self) -> Path:
        """Return the root of the publication-keyed kinds."""
        return self._shared

    @property
    def local(self) -> Path:
        """Return the root of the search-keyed kinds."""
        return self._local

    @property
    def base(self) -> Path:
        """Return the shared root (kept for callers written against v0.3)."""
        return self._shared

    @property
    def ttls(self) -> Mapping[str, timedelta | None]:
        """Return the effective TTL of every kind, overrides included."""
        return self._ttls

    def root_for(self, kind: str) -> Path:
        """Return the root directory *kind* is stored under.

        Args:
            kind: One of :data:`KINDS`.

        Raises:
            ValueError: If *kind* is not one of :data:`KINDS`.
        """
        _check_kind(kind)
        return self._local if kind in LOCAL_KINDS else self._shared

    def content_path(self, kind: str, key: str) -> Path:
        """Return the body-file path for *kind*/*key*.

        Args:
            kind: One of :data:`KINDS`.
            key: Cache key, as produced by :func:`pub_key` or
                :func:`search_key`.

        Raises:
            ValueError: If *kind* is not one of :data:`KINDS`.
        """
        return self.root_for(kind) / kind_subdir(kind) / f"{key}{content_suffix(kind)}"

    def meta_path(self, kind: str, key: str) -> Path:
        """Return the sidecar path for *kind*/*key*.

        Args:
            kind: One of :data:`KINDS`.
            key: Cache key, as produced by :func:`pub_key` or
                :func:`search_key`.

        Raises:
            ValueError: If *kind* is not one of :data:`KINDS`.
        """
        return self.root_for(kind) / kind_subdir(kind) / f"{key}{_META_SUFFIX}"

    def get(self, kind: str, key: str) -> CacheHit | None:
        """Return the cached entry for *kind*/*key*, or ``None`` on a miss.

        A missing body file, a missing, unreadable or incomplete sidecar, a
        body whose size no longer matches the sidecar, and an expired entry
        are all reported as a plain miss rather than raising.

        Args:
            kind: One of :data:`KINDS`.
            key: Cache key, as produced by :func:`pub_key` or
                :func:`search_key`.

        Raises:
            ValueError: If *kind* is not one of :data:`KINDS`.
        """
        content_path = self.content_path(kind, key)
        meta = self._read_meta(self.meta_path(kind, key))
        if meta is None:
            return None

        if any(field not in meta for field in _REQUIRED_META_FIELDS):
            return None
        fetched_at = meta["fetched_at"]
        ident = meta["ident"]

        if not is_fresh(kind, fetched_at, self._clock(), ttls=self._ttls):
            return None

        try:
            content = content_path.read_bytes()
        except OSError:
            return None

        # Sidecars written before v0.4 carry no size; there is nothing to
        # check against, so they are served as they are.
        expected_size = meta.get("size")
        if expected_size is not None and expected_size != len(content):
            return None

        return CacheHit(
            content=content,
            fetched_at=fetched_at,
            ident=ident,
            path=content_path,
        )

    def put(self, kind: str, key: str, content: bytes, *, ident: str) -> Path:
        """Write *content* and its sidecar for *kind*/*key*, replacing any prior entry.

        Both files are written to a ``.tmp`` sibling and moved into place
        with ``os.replace``, so a crash mid-write cannot leave a good
        sidecar pointing at a half-written body. For search-keyed kinds,
        the expired entries of the same kind are removed afterwards.

        Args:
            kind: One of :data:`KINDS`.
            key: Cache key, as produced by :func:`pub_key` or
                :func:`search_key`.
            content: The response body to cache.
            ident: Human-readable identifier (CQL query or publication
                number) recorded in the sidecar.

        Returns:
            The path of the written body file.

        Raises:
            ValueError: If *kind* is not one of :data:`KINDS`.
            OSError: If the entry cannot be written.
        """
        content_path = self.content_path(kind, key)
        content_path.parent.mkdir(parents=True, exist_ok=True)
        _replace_atomically(content_path, content)

        now = self._clock()
        meta = {
            "kind": kind,
            "key": key,
            "ident": ident,
            "fetched_at": now.isoformat(timespec="seconds"),
            "size": len(content),
        }
        _replace_atomically(
            self.meta_path(kind, key),
            json.dumps(meta, ensure_ascii=False).encode("utf-8"),
        )

        if kind in LOCAL_KINDS:
            self._evict_expired(kind, now, keep=key)

        return content_path

    def _read_meta(self, meta_path: Path) -> dict[str, Any] | None:
        """Return the sidecar at *meta_path* as a dict, or ``None`` if unusable."""
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return meta if isinstance(meta, dict) else None

    def _evict_expired(self, kind: str, now: datetime, *, keep: str) -> None:
        """Delete the expired entries of *kind*, except the one keyed *keep*.

        Entries whose sidecar cannot be read are left alone: they may be
        mid-write, and :meth:`get` already treats them as a miss.
        """
        kind_dir = self.root_for(kind) / kind_subdir(kind)
        suffix = content_suffix(kind)
        try:
            meta_paths = sorted(kind_dir.glob(f"*{_META_SUFFIX}"))
        except OSError:
            return
        for meta_path in meta_paths:
            key = meta_path.name[: -len(_META_SUFFIX)]
            if key == keep:
                continue
            meta = self._read_meta(meta_path)
            if meta is None or "fetched_at" not in meta:
                continue
            if is_fresh(kind, meta["fetched_at"], now, ttls=self._ttls):
                continue
            _unlink_quietly(meta_path.with_name(f"{key}{suffix}"))
            _unlink_quietly(meta_path)


def _replace_atomically(path: Path, payload: bytes) -> None:
    """Write *payload* to a ``.tmp`` sibling of *path*, then move it onto *path*."""
    tmp_path = path.with_name(path.name + _TMP_SUFFIX)
    tmp_path.write_bytes(payload)
    os.replace(tmp_path, path)


def _unlink_quietly(path: Path) -> None:
    """Delete *path*, ignoring a missing file or any other filesystem refusal."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def default_cache() -> Cache:
    """Return the file cache configured for the current environment.

    Publication-keyed kinds live under :func:`patent_checker.config.cache_base`
    (shared by every project of the current user), search-keyed kinds under
    ``<data_base>/cache``, and TTL overrides come from
    ``$PATENT_CHECKER_CACHE_TTL``.

    Raises:
        patent_checker.config.ConfigError: If ``$PATENT_CHECKER_CACHE_TTL``
            is malformed; a broken cache policy is reported rather than
            silently replaced by the defaults.
    """
    return Cache(
        config.cache_base(),
        config.data_base() / "cache",
        ttls=config.cache_ttl_overrides(KINDS),
    )
