"""Minimal file cache for EPO OPS responses (v0.3, no TTL settings yet).

Entries live under ``<base>/<kind>/``: the raw XML as ``<key>.xml`` next to a
JSON sidecar ``<key>.meta.json`` carrying when it was fetched. A cache hit
serves the stored bytes without touching the network, so it is never logged
to ``headers.jsonl`` (that log stays an accurate record of real OPS usage).

Two families of keys are used, matching the two shapes of OPS request this
covers:

- Publication-keyed kinds (``biblio``, ``claims``, ``legal``, ``family``) are
  keyed by the DOCDB spelling of the publication number (:func:`pub_key`).
- Search-keyed kinds (``search``, ``searchbib``) are keyed by a hash of the
  CQL query and paging window (:func:`search_key`); the CQL text is not
  normalized, so whitespace differences are deliberately different entries.

Freshness is decided by :func:`is_fresh`: publication-keyed kinds never
expire (a granted publication's biblio/claims/family do not change), while
search results and legal-status events are only served from cache within the
same local calendar day they were fetched. A later version is expected to
replace this with configurable TTLs; keeping the policy in one function makes
that a local change.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from patent_checker import config
from patent_checker.pubnum import parse_pubnum

# Kinds keyed by publication number (DOCDB spelling).
PUB_KINDS: tuple[str, ...] = ("biblio", "claims", "legal", "family")

# Kinds keyed by a hash of the CQL query and paging window.
SEARCH_KINDS: tuple[str, ...] = ("search", "searchbib")

KINDS: tuple[str, ...] = PUB_KINDS + SEARCH_KINDS

# Publication-keyed kinds whose content never goes stale.
NO_EXPIRY_KINDS: frozenset[str] = frozenset({"biblio", "claims", "family"})

# Kinds only fresh within the local calendar day they were fetched.
SAME_DAY_KINDS: frozenset[str] = frozenset({"legal", "search", "searchbib"})


@dataclass(frozen=True)
class CacheHit:
    """One cache entry read back by :meth:`Cache.get`.

    Attributes:
        content: The cached response body.
        fetched_at: ISO 8601 local timestamp recorded when the entry was
            written (seconds precision).
        ident: The human-readable identifier (CQL query or publication
            number) the entry was written with.
        raw_path: Path of the original raw response, as a string.
        path: The cache content file this entry was read from.
    """

    content: bytes
    fetched_at: str
    ident: str
    raw_path: str
    path: Path


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


def is_fresh(kind: str, fetched_at: str, now: datetime) -> bool:
    """Return True if an entry of *kind* fetched at *fetched_at* is still usable.

    Publication-keyed kinds (:data:`NO_EXPIRY_KINDS`) never go stale. The
    remaining kinds (:data:`SAME_DAY_KINDS`) are only fresh while *now* falls
    on the same local calendar day as *fetched_at*.

    Args:
        kind: One of :data:`KINDS`.
        fetched_at: ISO 8601 timestamp the entry was written with.
        now: The moment freshness is evaluated at.

    Raises:
        ValueError: If *kind* is not one of :data:`KINDS`.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown cache kind: {kind!r}")
    if kind in NO_EXPIRY_KINDS:
        return True
    try:
        fetched_at_dt = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    return fetched_at_dt.date() == now.date()


class Cache:
    """File cache rooted at *base*, one subdirectory per kind.

    Nothing is created on disk until the first :meth:`put`.
    """

    def __init__(self, base: Path, *, clock: Callable[[], datetime] = datetime.now) -> None:
        """Initialize the cache.

        Args:
            base: Cache root directory (e.g. ``<data_base>/cache/ops``).
            clock: Source of "now", injectable for tests.
        """
        self._base = base
        self._clock = clock

    @property
    def base(self) -> Path:
        """Return the cache root directory."""
        return self._base

    def _content_path(self, kind: str, key: str) -> Path:
        """Return the content-file path for *kind*/*key*, without validating *kind*."""
        return self._base / kind / f"{key}.xml"

    def _meta_path(self, kind: str, key: str) -> Path:
        """Return the sidecar path for *kind*/*key*, without validating *kind*."""
        return self._base / kind / f"{key}.meta.json"

    def get(self, kind: str, key: str) -> CacheHit | None:
        """Return the cached entry for *kind*/*key*, or ``None`` on a miss.

        A missing content file, a missing or unreadable sidecar, and a stale
        entry are all reported as a plain miss rather than raising.

        Args:
            kind: One of :data:`KINDS`.
            key: Cache key, as produced by :func:`pub_key` or
                :func:`search_key`.

        Raises:
            ValueError: If *kind* is not one of :data:`KINDS`.
        """
        if kind not in KINDS:
            raise ValueError(f"unknown cache kind: {kind!r}")

        content_path = self._content_path(kind, key)
        meta_path = self._meta_path(kind, key)
        if not content_path.is_file() or not meta_path.is_file():
            return None

        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(meta, dict):
            return None

        try:
            fetched_at = meta["fetched_at"]
            ident = meta["ident"]
            raw_path = meta["raw_path"]
        except KeyError:
            return None

        if not is_fresh(kind, fetched_at, self._clock()):
            return None

        try:
            content = content_path.read_bytes()
        except OSError:
            return None

        return CacheHit(
            content=content,
            fetched_at=fetched_at,
            ident=ident,
            raw_path=raw_path,
            path=content_path,
        )

    def put(self, kind: str, key: str, content: bytes, *, ident: str, raw_path: Path | str) -> Path:
        """Write *content* and its sidecar for *kind*/*key*, overwriting any prior entry.

        The sidecar is written to a temp file and moved into place with
        ``os.replace`` so a crash mid-write never leaves a truncated sidecar
        next to a complete content file.

        Args:
            kind: One of :data:`KINDS`.
            key: Cache key, as produced by :func:`pub_key` or
                :func:`search_key`.
            content: The response body to cache.
            ident: Human-readable identifier (CQL query or publication
                number) recorded in the sidecar.
            raw_path: Path of the original raw response.

        Returns:
            The path of the written content file.

        Raises:
            ValueError: If *kind* is not one of :data:`KINDS`.
        """
        if kind not in KINDS:
            raise ValueError(f"unknown cache kind: {kind!r}")

        kind_dir = self._base / kind
        kind_dir.mkdir(parents=True, exist_ok=True)

        content_path = self._content_path(kind, key)
        content_path.write_bytes(content)

        meta = {
            "kind": kind,
            "key": key,
            "ident": ident,
            "fetched_at": self._clock().isoformat(timespec="seconds"),
            "raw_path": str(raw_path),
        }
        meta_path = self._meta_path(kind, key)
        tmp_path = meta_path.with_name(meta_path.name + ".tmp")
        tmp_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, meta_path)

        return content_path


def default_cache() -> Cache:
    """Return the file cache rooted in the current data base directory."""
    return Cache(config.data_base() / "cache" / "ops")
