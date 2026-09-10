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
never a truncated body or sidecar in place of a previously good entry. The
temporary name carries the writer's process id and a random part, so the
CLI and the server writing the same key at the same time cannot take each
other's file away; the body is moved into place before the sidecar, so a
sidecar is never newer than the body it describes.
:meth:`Cache.get` additionally verifies the body against the ``size``
recorded in the sidecar, so a body damaged after the fact is reported as a
miss rather than served as a short response; the next :meth:`Cache.put`
replaces both files and restores a normal hit.

The sidecar carries ``kind``, ``key``, ``ident``, ``fetched_at`` and
``size``. ``fetched_at`` is written as an offset-aware UTC timestamp since
v1.0; the naive local timestamps written before that are still read, as
local time. Sidecars written by v0.3 instead carry a ``raw_path`` and no
``size``; they are still read, simply without the size check.

Search entries are the only ones that accumulate without bound, so a
:meth:`Cache.put` of a search-keyed kind also removes the expired entries
sitting in that kind's directory, at most once a minute per kind.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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

# Kinds whose default policy is "never expires". This name is kept for v0.3
# compatibility; not used by the package itself, which reads DEFAULT_TTLS
# directly.
NO_EXPIRY_KINDS: frozenset[str] = frozenset(
    kind for kind, ttl in DEFAULT_TTLS.items() if ttl is None
)

# Sidecar fields an entry must carry to be served; ``size`` is optional so
# that sidecars written before v0.4 stay readable.
_REQUIRED_META_FIELDS: tuple[str, ...] = ("kind", "key", "ident", "fetched_at")

# Suffix of the sidecar file, relative to the key.
_META_SUFFIX = ".meta.json"

# Suffix of the half-written file a ``put`` moves into place. The name in
# front of it is ``<final name>.<pid>.<random>``, so two writers of the same
# key cannot pick the same temporary file.
_TMP_SUFFIX = ".tmp"

# The ``.<pid>.<random>`` part of a temporary name, stripped to recover the
# final name the file was being written for.
_TMP_INFIX_RE = re.compile(r"\.\d+\.[0-9a-f]+\Z")

# Characters a cache key may be made of. Keys become file names, so anything
# that could escape the kind directory (a separator, ``..``) is refused.
_KEY_RE = re.compile(r"[A-Za-z0-9._-]+\Z")

# Keys that are made of allowed characters but still name a directory.
_RESERVED_KEYS: frozenset[str] = frozenset({".", ".."})

# How long a search-kind ``put`` may skip the sweep for expired entries of
# that kind. Sweeping on every put costs one directory walk per upstream
# call; once a minute is enough to keep the directory bounded.
_EVICT_INTERVAL = timedelta(seconds=60)


@dataclass(frozen=True)
class CacheHit:
    """One cache entry read back by :meth:`Cache.get`.

    Attributes:
        content: The cached response body.
        fetched_at: ISO 8601 timestamp recorded when the entry was written
            (seconds precision). Written as UTC since v1.0; entries written
            earlier carry naive local time.
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
        """Return the body file as a string: the only copy kept on disk.

        This name is kept for v0.3 compatibility; not used by the package
        itself, which reads :attr:`path`.
        """
        return str(self.path)


@dataclass(frozen=True)
class CacheEntry:
    """One entry (or entry fragment) found on disk by :meth:`Cache.entries`.

    Attributes:
        kind: One of :data:`KINDS`.
        key: Cache key, as produced by :func:`pub_key` or :func:`search_key`.
        root: ``"shared"`` or ``"local"``, whichever root *kind* is stored
            under.
        path: The body file path (may not exist when *broken*).
        meta_path: The sidecar path (may not exist when *broken*).
        ident: The identifier recorded in the sidecar, or ``None`` when the
            sidecar is missing or unreadable.
        fetched_at: The timestamp recorded in the sidecar, or ``None`` under
            the same conditions as *ident*.
        size: Body bytes on disk; ``0`` when the body is missing.
        expired: ``True`` when the sidecar is readable and the entry is no
            longer fresh. Always ``False`` when *broken* is ``True``.
        broken: ``True`` when the entry cannot be served for a structural
            reason (see *problem*).
        problem: Why the entry is broken -- one of ``"missing body"``,
            ``"missing sidecar"``, ``"unreadable sidecar"``,
            ``"size mismatch"`` or ``"stray tmp file"``; ``None`` when the
            entry is not broken.
    """

    kind: str
    key: str
    root: str
    path: Path
    meta_path: Path
    ident: str | None
    fetched_at: str | None
    size: int
    expired: bool
    broken: bool
    problem: str | None


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


def _check_key(key: str) -> None:
    """Raise ``ValueError`` unless *key* is a safe file name.

    A key becomes a file name inside the kind directory, so only the
    characters :func:`pub_key` and :func:`search_key` produce are accepted
    (``A-Z``, ``a-z``, ``0-9``, ``.``, ``_`` and ``-``), and a key naming a
    directory (``.``, ``..``) is refused as well.
    """
    if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None or key in _RESERVED_KEYS:
        raise ValueError(f"invalid cache key: {key!r}")


def _as_aware(moment: datetime) -> datetime:
    """Return *moment* as an offset-aware datetime, reading a naive one as local time."""
    return moment if moment.tzinfo is not None else moment.astimezone()


def _parse_timestamp(value: Any) -> datetime | None:
    """Return *value* as an offset-aware datetime, or ``None`` if it is not a timestamp.

    Naive values (every sidecar written before v1.0) are read as local
    time, which is how they were written.
    """
    try:
        return _as_aware(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return None


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
        fetched_at: ISO 8601 timestamp the entry was written with. It may
            be offset-aware (written since v1.0) or naive (written earlier,
            and read as local time); a value that cannot be parsed counts
            as expired.
        now: The moment freshness is evaluated at. A naive value is read as
            local time, like a naive *fetched_at*.
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
    fetched_at_dt = _parse_timestamp(fetched_at)
    if fetched_at_dt is None:
        # A malformed timestamp means "do not trust it".
        return False
    return fetched_at_dt + ttl > _as_aware(now)


def format_ttl(ttl: timedelta | None) -> str:
    """Return a short human-readable rendering of a TTL.

    Args:
        ttl: A time-to-live, or ``None`` for "never expires".

    Returns:
        ``"never"`` for ``None``; ``"<n>d"`` for a whole number of days;
        otherwise ``"<n>h"`` for a whole number of hours; otherwise
        ``"<n>s"`` for the total number of seconds.
    """
    if ttl is None:
        return "never"
    total_seconds = int(ttl.total_seconds())
    if total_seconds % 86400 == 0:
        return f"{total_seconds // 86400}d"
    if total_seconds % 3600 == 0:
        return f"{total_seconds // 3600}h"
    return f"{total_seconds}s"


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
        # When each kind was last swept for expired entries, so that a burst
        # of puts does not walk the same directory over and over.
        self._last_evicted_at: dict[str, datetime] = {}

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
        """Return the shared root.

        This name is kept for v0.3 compatibility; not used by the package
        itself, which names the two roots explicitly through :attr:`shared`
        and :attr:`local`.
        """
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
            ValueError: If *kind* is not one of :data:`KINDS`, or *key* is
                not made of the characters a cache key may use.
        """
        _check_key(key)
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

        Both files are written to a uniquely named ``.tmp`` sibling and
        moved into place with ``os.replace``, body first, so a crash
        mid-write cannot leave a good sidecar pointing at a half-written
        body and two writers of the same key cannot collide. For
        search-keyed kinds, the expired entries of the same kind are
        removed afterwards, at most once a minute.

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
            ValueError: If *kind* is not one of :data:`KINDS`, or *key* is
                not made of the characters a cache key may use.
            OSError: If the entry cannot be written.
        """
        _check_key(key)
        content_path = self.content_path(kind, key)
        content_path.parent.mkdir(parents=True, exist_ok=True)
        # The body goes in first: a reader that sees the sidecar then always
        # sees a body at least as new as it.
        _replace_atomically(content_path, content)

        now = self._clock()
        meta = {
            "kind": kind,
            "key": key,
            "ident": ident,
            # Stored as UTC so that a change of time zone (or of DST) cannot
            # make an entry look younger or older than it is.
            "fetched_at": _as_aware(now).astimezone(UTC).isoformat(timespec="seconds"),
            "size": len(content),
        }
        _replace_atomically(
            self.meta_path(kind, key),
            json.dumps(meta, ensure_ascii=False).encode("utf-8"),
        )

        if kind in LOCAL_KINDS:
            self._evict_expired(kind, now, keep=key)

        return content_path

    def entries(self, kind: str | None = None) -> list[CacheEntry]:
        """Return every entry found on disk, across one or all kinds.

        A kind whose directory does not exist yet contributes no entries.
        Entries are built from the union of body files, sidecar files and
        stray ``.tmp`` files sitting in a kind's directory, one
        :class:`CacheEntry` per key.

        Args:
            kind: One of :data:`KINDS`, or ``None`` for every kind.

        Raises:
            ValueError: If *kind* is given and is not one of :data:`KINDS`.
        """
        if kind is not None:
            _check_kind(kind)
            kinds: tuple[str, ...] = (kind,)
        else:
            kinds = KINDS
        found: list[CacheEntry] = []
        for one_kind in kinds:
            found.extend(self._entries_for_kind(one_kind))
        found.sort(key=lambda entry: (entry.kind, entry.key))
        return found

    def stats(self) -> dict[str, Any]:
        """Return counts and byte totals of every kind, ready to serialize as JSON."""
        kinds_stats: dict[str, Any] = {}
        totals = {"entries": 0, "bytes": 0, "expired": 0, "broken": 0}
        for kind in KINDS:
            kind_entries = self.entries(kind)
            fetched_ats = [
                entry.fetched_at for entry in kind_entries if entry.fetched_at is not None
            ]
            n_bytes = sum(entry.size for entry in kind_entries)
            n_expired = sum(1 for entry in kind_entries if entry.expired)
            n_broken = sum(1 for entry in kind_entries if entry.broken)
            kinds_stats[kind] = {
                "root": "local" if kind in LOCAL_KINDS else "shared",
                "dir": str(self.root_for(kind) / kind_subdir(kind)),
                "entries": len(kind_entries),
                "bytes": n_bytes,
                "expired": n_expired,
                "broken": n_broken,
                "oldest": min(fetched_ats) if fetched_ats else None,
                "newest": max(fetched_ats) if fetched_ats else None,
            }
            totals["entries"] += len(kind_entries)
            totals["bytes"] += n_bytes
            totals["expired"] += n_expired
            totals["broken"] += n_broken
        return {
            "shared_dir": str(self._shared),
            "local_dir": str(self._local),
            "same_root": self._shared.resolve() == self._local.resolve(),
            "ttls": {kind: format_ttl(self._ttls[kind]) for kind in KINDS},
            "kinds": kinds_stats,
            "totals": totals,
        }

    def select(
        self,
        *,
        kinds: Collection[str] | None = None,
        older_than: timedelta | None = None,
        pub: str | None = None,
        expired: bool = False,
        broken: bool = False,
    ) -> list[CacheEntry]:
        """Return the entries matching every given filter (AND semantics).

        Args:
            kinds: Restrict to these kinds; ``None`` for every kind.
            older_than: Keep only entries whose sidecar can be read and
                whose age is at least *older_than* (``fetched_at +
                older_than <= now``, so an entry exactly *older_than* old is
                included). Entries with an unreadable sidecar never match.
            pub: Keep only the entries keyed by this publication number
                (:func:`pub_key`); search-keyed kinds never match, since
                their keys are not publication numbers. Filtering on *pub*
                looks the candidate paths up directly instead of listing
                every kind directory.
            expired: When ``True``, require the entry to be expired.
            broken: When ``True``, require the entry to be broken. When
                both *expired* and *broken* are ``True``, an entry matches
                if either is true (an OR of the two); when both are
                ``False``, freshness and brokenness are not filtered on.

        Raises:
            ValueError: If *kinds* names a kind that is not one of
                :data:`KINDS`, or *pub* is not a parseable publication
                number.
        """
        if kinds is not None:
            for one_kind in kinds:
                _check_kind(one_kind)
        kind_set = set(kinds) if kinds is not None else None
        target_key = pub_key(pub) if pub is not None else None
        now = _as_aware(self._clock())

        if target_key is not None:
            candidates = self._entries_for_key(target_key, kind_set)
        else:
            candidates = [
                entry for entry in self.entries() if kind_set is None or entry.kind in kind_set
            ]

        result: list[CacheEntry] = []
        for entry in candidates:
            if older_than is not None:
                if entry.fetched_at is None:
                    continue
                fetched_at_dt = _parse_timestamp(entry.fetched_at)
                if fetched_at_dt is None:
                    # A timestamp that cannot be read (malformed, or not a
                    # string at all) is not trusted, so it never matches an
                    # age filter; the entry is reported as broken instead.
                    continue
                if fetched_at_dt + older_than > now:
                    continue
            if (expired or broken) and not (
                (expired and entry.expired) or (broken and entry.broken)
            ):
                continue
            result.append(entry)
        return result

    def remove(self, entries: Iterable[CacheEntry]) -> dict[str, Any]:
        """Delete the body, sidecar and any stray ``.tmp`` files of *entries*.

        Every path removed is required to sit under :attr:`shared` or
        :attr:`local` (checked with the path's directory resolved, but
        without following a symlink at the path itself, so a symlink that
        lives under a cache root is removable even when it points
        elsewhere -- only the link is deleted, never its target). A path
        that fails this check is left alone and reported in ``errors``. A
        path that does not exist is silently skipped. Directories are
        never removed.

        Args:
            entries: Entries to delete, typically produced by
                :meth:`entries` or :meth:`select`.

        Returns:
            A dict with ``removed`` (number of files deleted), ``bytes``
            (body bytes freed, from the *entries*' recorded ``size``),
            ``paths`` (the deleted file paths, as strings) and ``errors``
            (a list of ``{"path": ..., "error": ...}`` for paths that were
            not removed).
        """
        shared_root = self._shared.resolve()
        local_root = self._local.resolve()
        removed = 0
        freed_bytes = 0
        removed_paths: list[str] = []
        errors: list[dict[str, str]] = []

        def _under_a_root(path: Path) -> bool:
            try:
                parent = path.parent.resolve()
            except OSError:
                return False
            candidate = parent / path.name
            return candidate.is_relative_to(shared_root) or candidate.is_relative_to(local_root)

        def _remove_one(path: Path) -> bool:
            if not _under_a_root(path):
                errors.append({"path": str(path), "error": "outside the cache roots"})
                return False
            if not path.exists() and not path.is_symlink():
                return False
            try:
                path.unlink()
            except OSError as exc:
                errors.append({"path": str(path), "error": str(exc)})
                return False
            removed_paths.append(str(path))
            return True

        for entry in entries:
            if _remove_one(entry.path):
                removed += 1
                freed_bytes += entry.size
            for extra in (
                entry.meta_path,
                *_tmp_siblings(entry.path),
                *_tmp_siblings(entry.meta_path),
            ):
                if _remove_one(extra):
                    removed += 1

        return {
            "removed": removed,
            "bytes": freed_bytes,
            "paths": removed_paths,
            "errors": errors,
        }

    def quick_counts(self) -> dict[str, int]:
        """Return the number of sidecars of every kind, without reading any of them.

        This is the cheap counterpart of :meth:`stats`: it lists each kind's
        directory once and counts sidecar names, so no JSON is parsed, no
        body is stat'ed and nothing is sorted. A kind whose directory does
        not exist yet counts ``0``.

        Returns:
            ``{kind: number of sidecars}`` for every kind of :data:`KINDS`.
        """
        counts: dict[str, int] = {}
        for kind in KINDS:
            kind_dir = self.root_for(kind) / kind_subdir(kind)
            found = 0
            try:
                with os.scandir(kind_dir) as scan:
                    for item in scan:
                        # A ".meta.json.<pid>.<random>.tmp" leftover does not
                        # end with the sidecar suffix, so it is not counted.
                        if item.name.endswith(_META_SUFFIX):
                            found += 1
            except OSError:
                found = 0
            counts[kind] = found
        return counts

    def _entries_for_key(self, key: str, kind_set: set[str] | None) -> list[CacheEntry]:
        """Return the entries stored under *key*, looked up path by path.

        Only the publication-keyed kinds are considered: a search key is a
        hash of a query, never a publication number.
        """
        found: list[CacheEntry] = []
        for kind in PUB_KINDS:
            if kind_set is not None and kind not in kind_set:
                continue
            kind_dir = self.root_for(kind) / kind_subdir(kind)
            suffix = content_suffix(kind)
            has_body = (kind_dir / f"{key}{suffix}").exists()
            has_meta = (kind_dir / f"{key}{_META_SUFFIX}").exists()
            if not has_body and not has_meta:
                continue
            root_name = "local" if kind in LOCAL_KINDS else "shared"
            found.append(
                self._build_entry(kind, key, kind_dir, suffix, root_name, has_body, has_meta)
            )
        found.sort(key=lambda entry: (entry.kind, entry.key))
        return found

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
        mid-write, and :meth:`get` already treats them as a miss. The sweep
        runs at most once per :data:`_EVICT_INTERVAL` per kind, so a burst
        of puts costs one directory walk rather than one per put.
        """
        last = self._last_evicted_at.get(kind)
        if last is not None and _as_aware(now) - _as_aware(last) < _EVICT_INTERVAL:
            return
        self._last_evicted_at[kind] = now
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

    def _entries_for_kind(self, kind: str) -> list[CacheEntry]:
        """Return the entries of *kind* found in its directory, one per key."""
        kind_dir = self.root_for(kind) / kind_subdir(kind)
        try:
            names = [child.name for child in kind_dir.iterdir()]
        except OSError:
            return []

        suffix = content_suffix(kind)
        bodies: set[str] = set()
        metas: set[str] = set()
        body_tmp: set[str] = set()
        meta_tmp: set[str] = set()
        for name in names:
            target = _tmp_target_name(name)
            if target is not None:
                if target.endswith(_META_SUFFIX):
                    meta_tmp.add(target[: -len(_META_SUFFIX)])
                elif target.endswith(suffix):
                    body_tmp.add(target[: -len(suffix)])
                continue
            if name.endswith(_META_SUFFIX):
                metas.add(name[: -len(_META_SUFFIX)])
            elif name.endswith(suffix):
                bodies.add(name[: -len(suffix)])

        root_name = "local" if kind in LOCAL_KINDS else "shared"
        keys = bodies | metas | body_tmp | meta_tmp
        return [
            self._build_entry(kind, key, kind_dir, suffix, root_name, key in bodies, key in metas)
            for key in keys
        ]

    def _build_entry(
        self,
        kind: str,
        key: str,
        kind_dir: Path,
        suffix: str,
        root_name: str,
        has_body: bool,
        has_meta: bool,
    ) -> CacheEntry:
        """Build the :class:`CacheEntry` for *kind*/*key* from what is on disk."""
        path = kind_dir / f"{key}{suffix}"
        meta_path = kind_dir / f"{key}{_META_SUFFIX}"

        meta: dict[str, Any] | None = None
        if has_meta:
            meta = self._read_meta(meta_path)
            if meta is not None and any(field not in meta for field in _REQUIRED_META_FIELDS):
                meta = None

        size = 0
        if has_body:
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                # The body was removed between the listing and this stat
                # (another process cleaning up, or a put replacing it):
                # report it as missing instead of failing the whole listing.
                has_body = False

        problem: str | None = None
        if not has_body:
            problem = "missing body" if has_meta else "stray tmp file"
        elif not has_meta:
            problem = "missing sidecar"
        elif meta is None:
            problem = "unreadable sidecar"
        else:
            expected_size = meta.get("size")
            if expected_size is not None and expected_size != size:
                problem = "size mismatch"

        broken = problem is not None
        ident = meta.get("ident") if meta is not None else None
        fetched_at = meta.get("fetched_at") if meta is not None else None
        expired = (
            False if broken else not is_fresh(kind, fetched_at, self._clock(), ttls=self._ttls)
        )

        return CacheEntry(
            kind=kind,
            key=key,
            root=root_name,
            path=path,
            meta_path=meta_path,
            ident=ident,
            fetched_at=fetched_at,
            size=size,
            expired=expired,
            broken=broken,
            problem=problem,
        )


def _replace_atomically(path: Path, payload: bytes) -> None:
    """Write *payload* to a private ``.tmp`` sibling of *path*, then move it onto *path*.

    The temporary name is ``<name>.<pid>.<random>.tmp``, so two processes
    (or two threads) writing the same key each own their temporary file and
    neither can have it replaced or removed under it.
    """
    unique = f"{os.getpid()}.{uuid.uuid4().hex[:8]}"
    tmp_path = path.with_name(f"{path.name}.{unique}{_TMP_SUFFIX}")
    tmp_path.write_bytes(payload)
    os.replace(tmp_path, path)


def _tmp_target_name(name: str) -> str | None:
    """Return the final name a temporary file was being written for.

    Returns ``None`` when *name* is not a temporary file. Names written
    before v1.0 are ``<final name>.tmp`` with nothing in between, so the
    ``.<pid>.<random>`` part is stripped only when it is there.
    """
    if not name.endswith(_TMP_SUFFIX):
        return None
    return _TMP_INFIX_RE.sub("", name[: -len(_TMP_SUFFIX)])


def _tmp_siblings(path: Path) -> list[Path]:
    """Return the temporary files left behind for *path*, oldest naming scheme first."""
    siblings = [path.with_name(path.name + _TMP_SUFFIX)]
    try:
        siblings.extend(sorted(path.parent.glob(f"{path.name}.*{_TMP_SUFFIX}")))
    except OSError:
        pass
    return siblings


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
