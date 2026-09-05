"""Enumerate and remove what patent-checker leaves on a machine.

Under a data base (``patent_checker.config.data_base``) the package itself
only ever writes three entries -- ``cache/``, ``raw/`` and ``consent.json``
(:data:`CODE_OWNED_NAMES`). Everything else found there was written by the
agent running the Skill (reports, exploration notes), which is why the
default cleanup never touches it: deleting a file the code did not create is
the one mistake that cannot be undone by re-fetching.

What the default scope does cover is "the traces of this project":

===============  ===============================================================
category         what it is
===============  ===============================================================
search-cache     ``<data_base>/cache/ops/{search,searchbib}/`` -- cached search
                 responses, keyed by the CQL query
request-log      ``<data_base>/raw/ops/headers.jsonl`` -- the upstream request
                 log, which records the queries that were run
legacy-raw       the v0.3 response bodies (``<data_base>/raw/ops/*.xml``,
                 ``<data_base>/raw/gp/``), which nothing writes any more
legacy-cache     the v0.3 per-project publication cache
                 (``<data_base>/cache/ops/{biblio,claims,legal,family}/``)
===============  ===============================================================

The last one has an exception that matters: when ``<data_base>/cache`` *is*
the shared cache root (the layout of a default-configured MCP server, whose
data base is the per-user directory), those directories are live shared data
rather than leftovers, and are neither reported by :func:`legacy_summary` nor
planned for deletion.

Three scopes stay opt-in, because each of them is shared with something
outside the project being cleaned:

===============  ===============================================================
category         opt-in flag
===============  ===============================================================
shared-cache     ``shared`` -- the per-user cache root, reusable by every
                 other project
server-data      ``shared`` -- the MCP server's own data directory
artifact         ``include_artifacts`` -- the data base entries the package
                 does not own (reports, exploration output, ...)
consent          ``include_consent`` -- the recorded agreement to the legal
                 notice (the per-user record needs ``shared`` as well)
===============  ===============================================================

Safety
------

Planning and deleting are separate steps: :func:`plan_cleanup` only reads,
and the CLI prints its result as a dry run unless the caller asks for the
deletion explicitly. :func:`execute` then applies three rules:

- every path it deletes must sit under one of the plan's :attr:`roots`
  (checked with the path's parent resolved, so ``..`` and a symlinked parent
  cannot escape), and is otherwise reported in ``errors`` and left alone;
- a symlink is deleted as a link, never followed -- including a symlink to a
  directory, which is why no tree is ever removed with ``shutil.rmtree``;
- directories are removed with ``rmdir`` after their files, so a directory
  still holding something unplanned survives.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from patent_checker.cache import SEARCH_KINDS, Cache, kind_subdir

# Entries the package itself writes under a data base. Anything else found
# there belongs to the agent that ran the exploration, so it is only removed
# when the caller opts in.
CODE_OWNED_NAMES: frozenset[str] = frozenset({"cache", "raw", "consent.json"})

# The upstream request log, relative to a data base. It records the CQL
# queries that were sent, so it counts as exploration residue.
REQUEST_LOG_RELATIVE: Path = Path("raw") / "ops" / "headers.jsonl"

# Publication-keyed kinds that v0.3 stored under the project's own cache and
# v0.4 stores under the shared root.
LEGACY_PUB_KINDS: tuple[str, ...] = ("biblio", "claims", "legal", "family")

# Reported for a path that would take a deletion outside the plan's roots.
_OUTSIDE_ROOTS = "outside the cleanup roots"


@dataclass(frozen=True)
class CleanupItem:
    """One thing a cleanup would delete.

    Attributes:
        category: Why it is in the plan; one of ``"search-cache"``,
            ``"request-log"``, ``"legacy-raw"``, ``"legacy-cache"``,
            ``"shared-cache"``, ``"server-data"``, ``"artifact"`` or
            ``"consent"``.
        scope: Whose data it is: ``"project"``, ``"shared"`` or ``"user"``.
        path: A file, or a directory whose whole tree is meant.
        files: Number of regular files below *path* (a symlink counts as
            one file and is not followed).
        bytes: Their total size on disk (a symlink contributes nothing).
    """

    category: str
    scope: str
    path: Path
    files: int
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        """Return the item as a JSON-serializable dict."""
        return {
            "category": self.category,
            "scope": self.scope,
            "path": str(self.path),
            "files": self.files,
            "bytes": self.bytes,
        }


@dataclass(frozen=True)
class CleanupPlan:
    """What a cleanup would delete, and the directories it may not leave.

    Attributes:
        items: The planned deletions. No item is nested inside another, so
            the totals below never count a file twice.
        roots: The directories every deletion must stay under.
    """

    items: tuple[CleanupItem, ...]
    roots: tuple[Path, ...]

    @property
    def total_files(self) -> int:
        """Return the number of files the whole plan covers."""
        return sum(item.files for item in self.items)

    @property
    def total_bytes(self) -> int:
        """Return the total size of the files the whole plan covers."""
        return sum(item.bytes for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        """Return the plan as a JSON-serializable dict."""
        return {
            "items": [item.to_dict() for item in self.items],
            "total_files": self.total_files,
            "total_bytes": self.total_bytes,
        }


def legacy_summary(data_base: Path, cache: Cache) -> dict[str, Any]:
    """Report what the pre-v0.4 layout still holds under *data_base*.

    This is the "you can reclaim this" part of ``cache status``: the three
    locations nothing writes any more, each with a file count and a byte
    total. A location that does not exist contributes zeros.

    Args:
        data_base: The project data base to inspect.
        cache: The cache in use, used to tell a v0.3 leftover from the live
            shared cache of a default-configured MCP server (whose
            ``<data_base>/cache`` *is* the shared root).

    Returns:
        ``{"raw_ops_bodies": {"dir", "files", "bytes"}, "raw_gp": {...},
        "cache_pub_kinds": {...}, "total_files": int, "total_bytes": int}``.
    """
    raw_ops = data_base / "raw" / "ops"
    bodies_files = 0
    bodies_bytes = 0
    for body in _legacy_raw_bodies(raw_ops):
        files, size = _count_tree(body)
        bodies_files += files
        bodies_bytes += size

    gp_files, gp_bytes = _count_tree(data_base / "raw" / "gp")

    project_cache = data_base / "cache"
    pub_files = 0
    pub_bytes = 0
    # Same directory as the shared root means these are live entries, not
    # something left behind by an older version.
    if not _same_dir(project_cache, cache.shared):
        for kind in LEGACY_PUB_KINDS:
            files, size = _count_tree(project_cache / kind_subdir(kind))
            pub_files += files
            pub_bytes += size

    sections = {
        "raw_ops_bodies": {"dir": str(raw_ops), "files": bodies_files, "bytes": bodies_bytes},
        "raw_gp": {"dir": str(data_base / "raw" / "gp"), "files": gp_files, "bytes": gp_bytes},
        "cache_pub_kinds": {
            "dir": str(project_cache / "ops"),
            "files": pub_files,
            "bytes": pub_bytes,
        },
    }
    return {
        **sections,
        "total_files": sum(section["files"] for section in sections.values()),
        "total_bytes": sum(section["bytes"] for section in sections.values()),
    }


def plan_cleanup(
    *,
    data_base: Path,
    cache: Cache,
    shared: bool = False,
    include_artifacts: bool = False,
    include_consent: bool = False,
    user_data_dir: Path | None = None,
    user_consent_path: Path | None = None,
) -> CleanupPlan:
    """Enumerate what a cleanup would delete, without deleting anything.

    See the module docstring for what each scope covers. Paths that are not
    on disk are left out of the plan, and an item nested inside another one
    (which happens when the shared root and the project cache are the same
    directory) is dropped so the totals stay honest.

    Args:
        data_base: The project data base to clean.
        cache: The cache in use, for its shared and local roots.
        shared: Also target the shared cache root and, when
            *user_data_dir* names a different directory, the MCP server's
            data directory.
        include_artifacts: Also target the *data_base* entries the package
            does not write (see :data:`CODE_OWNED_NAMES`).
        include_consent: Also target the project's consent record, plus the
            per-user one when *shared* is set.
        user_data_dir: The MCP server's per-user data directory, or ``None``
            when the caller does not want it considered.
        user_consent_path: The per-user consent record, or ``None``.

    Returns:
        The plan, ready to print or to hand to :func:`execute`.
    """
    items: list[CleanupItem] = []

    def _add(category: str, scope: str, path: Path) -> None:
        """Append *path* as an item of *category*, unless it is not on disk."""
        if not _exists(path):
            return
        files, size = _count_tree(path)
        items.append(
            CleanupItem(category=category, scope=scope, path=path, files=files, bytes=size)
        )

    project_cache = data_base / "cache"

    for kind in SEARCH_KINDS:
        _add("search-cache", "project", project_cache / kind_subdir(kind))

    _add("request-log", "project", data_base / REQUEST_LOG_RELATIVE)

    for body in _legacy_raw_bodies(data_base / "raw" / "ops"):
        _add("legacy-raw", "project", body)
    _add("legacy-raw", "project", data_base / "raw" / "gp")

    if not _same_dir(project_cache, cache.shared):
        for kind in LEGACY_PUB_KINDS:
            _add("legacy-cache", "project", project_cache / kind_subdir(kind))

    if shared:
        for source in ("ops", "gp"):
            _add("shared-cache", "shared", cache.shared / source)
        if user_data_dir is not None and not _same_dir(user_data_dir, data_base):
            _add_server_data(_add, user_data_dir=user_data_dir, cache=cache)

    if include_artifacts:
        for child in _sorted_children(data_base):
            if child.name not in CODE_OWNED_NAMES:
                _add("artifact", "project", child)

    if include_consent:
        _add("consent", "project", data_base / "consent.json")
        if shared and user_consent_path is not None:
            _add("consent", "user", user_consent_path)

    roots = [data_base, cache.shared, cache.local]
    if user_data_dir is not None:
        roots.append(user_data_dir)
    if user_consent_path is not None:
        roots.append(user_consent_path.parent)

    return CleanupPlan(items=_drop_covered(items), roots=_unique_paths(roots))


def execute(plan: CleanupPlan) -> dict[str, Any]:
    """Delete what *plan* lists, staying inside its roots.

    Files are unlinked first and the emptied directories removed afterwards,
    bottom-up; a directory that still holds something is left in place. A
    symlink is unlinked as a link, so its target is never touched. A path
    that is missing is skipped silently, and a path that either escapes the
    plan's roots or cannot be removed is reported without stopping the rest
    of the run.

    Args:
        plan: The plan to apply, as built by :func:`plan_cleanup`.

    Returns:
        ``{"removed_files": int, "bytes": int, "paths": [str, ...],
        "errors": [{"path": str, "error": str}, ...]}``.
    """
    roots = [root.resolve() for root in plan.roots]
    removed_paths: list[str] = []
    errors: list[dict[str, str]] = []
    freed_bytes = 0

    def _inside_roots(path: Path) -> bool:
        """Return True if deleting *path* stays under one of the plan's roots."""
        resolved = _resolved_leaf(path)
        if resolved is None:
            return False
        return any(resolved.is_relative_to(root) for root in roots)

    def _remove_file(path: Path) -> None:
        """Unlink one file or symlink, recording the outcome."""
        nonlocal freed_bytes
        if not _inside_roots(path):
            errors.append({"path": str(path), "error": _OUTSIDE_ROOTS})
            return
        if not _exists(path):
            return
        # A symlink is deleted as a link, so its target's size is not freed.
        size = 0 if path.is_symlink() else _size_of(path)
        try:
            path.unlink()
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})
            return
        freed_bytes += size
        removed_paths.append(str(path))

    def _remove_dir(path: Path) -> None:
        """Remove *path* if it is now empty; a directory still in use is kept."""
        if not _inside_roots(path):
            errors.append({"path": str(path), "error": _OUTSIDE_ROOTS})
            return
        try:
            path.rmdir()
        except OSError:
            # Not empty (something unplanned lives there, or a file above
            # could not be removed) or already gone: both mean "leave it".
            pass

    def _remove_item(path: Path) -> None:
        """Remove one planned path: a single file, or a whole directory tree."""
        if path.is_symlink() or not path.is_dir():
            _remove_file(path)
            return
        for dirpath, dirnames, filenames in os.walk(path, topdown=False, followlinks=False):
            base = Path(dirpath)
            for name in sorted(filenames):
                _remove_file(base / name)
            for name in sorted(dirnames):
                child = base / name
                # os.walk never descends into a symlinked directory, so the
                # link is removed here and its target stays untouched.
                if child.is_symlink():
                    _remove_file(child)
            _remove_dir(base)

    for item in plan.items:
        _remove_item(item.path)

    return {
        "removed_files": len(removed_paths),
        "bytes": freed_bytes,
        "paths": removed_paths,
        "errors": errors,
    }


def _add_server_data(
    add: Callable[[str, str, Path], None],
    *,
    user_data_dir: Path,
    cache: Cache,
) -> None:
    """Add the MCP server's own leftovers to a plan being built.

    Args:
        add: The ``_add(category, scope, path)`` callback of
            :func:`plan_cleanup`.
        user_data_dir: The server's data directory (already known to differ
            from the data base being cleaned).
        cache: The cache in use, to detect a server cache that is the shared
            root: only its search kinds are project-shaped residue, the rest
            is the shared cache and needs the ``shared`` scope of its own.
    """
    server_cache = user_data_dir / "cache"
    if _same_dir(server_cache, cache.shared):
        for kind in SEARCH_KINDS:
            add("server-data", "user", server_cache / kind_subdir(kind))
    else:
        add("server-data", "user", server_cache)
    add("server-data", "user", user_data_dir / "raw")


def _legacy_raw_bodies(raw_ops: Path) -> list[Path]:
    """Return the v0.3 response bodies in *raw_ops*, sorted, excluding the request log."""
    try:
        # ``*.xml`` matches the bodies only: headers.jsonl is still written.
        return sorted(raw_ops.glob("*.xml"))
    except OSError:
        return []


def _sorted_children(directory: Path) -> list[Path]:
    """Return the direct children of *directory*, sorted, or an empty list if unreadable."""
    try:
        return sorted(directory.iterdir())
    except OSError:
        return []


def _exists(path: Path) -> bool:
    """Return True if *path* is on disk, counting a broken symlink as present."""
    return path.exists() or path.is_symlink()


def _size_of(path: Path) -> int:
    """Return *path*'s size in bytes, or 0 when it cannot be measured."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _count_tree(path: Path) -> tuple[int, int]:
    """Return ``(file count, total bytes)`` for a file or a whole directory tree.

    A symlink counts as one zero-byte file and is never followed, so a link
    pointing outside the tree cannot drag its target into the total.
    """
    if path.is_symlink():
        return 1, 0
    if not path.is_dir():
        return (1, _size_of(path)) if path.exists() else (0, 0)

    files = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        base = Path(dirpath)
        for name in filenames:
            files += 1
            child = base / name
            if not child.is_symlink():
                total += _size_of(child)
        # A symlink to a directory is listed here and not descended into;
        # count the link itself, exactly like any other symlink.
        files += sum(1 for name in dirnames if (base / name).is_symlink())
    return files, total


def _same_dir(left: Path, right: Path) -> bool:
    """Return True if both paths name the same directory once resolved."""
    return left.resolve() == right.resolve()


def _resolved_leaf(path: Path) -> Path | None:
    """Return *path* with its parent resolved and its own name left as-is.

    Resolving the parent defeats ``..`` segments and a symlinked parent,
    while leaving the last component alone keeps a symlink judged (and
    deleted) as the link itself rather than as its target.

    Returns:
        The resolved path, or ``None`` when the parent cannot be resolved.
    """
    try:
        return path.parent.resolve() / path.name
    except OSError:
        return None


def _drop_covered(items: Sequence[CleanupItem]) -> tuple[CleanupItem, ...]:
    """Return *items* without those already covered by another item.

    Two roots can be the same directory (a default-configured MCP server has
    one), which would otherwise put the same files in the plan twice -- once
    on their own and once inside an enclosing directory -- and double the
    reported totals.
    """
    resolved = [_resolved_leaf(item.path) for item in items]
    kept: list[CleanupItem] = []
    for index, path in enumerate(resolved):
        if path is None:
            kept.append(items[index])
            continue
        covered = False
        for other_index, other in enumerate(resolved):
            if other_index == index or other is None:
                continue
            if path == other:
                # Keep the first of two identical paths, drop the rest.
                covered = other_index < index
            else:
                covered = path.is_relative_to(other)
            if covered:
                break
        if not covered:
            kept.append(items[index])
    return tuple(kept)


def _unique_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    """Return *paths* without repeats, in first-seen order."""
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return tuple(unique)
