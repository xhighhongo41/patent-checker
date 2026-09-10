"""Tests for the cleanup planner (patent_checker/cleanup.py).

Every test builds its own throwaway tree under ``tmp_path`` and points the
planner at it: no test may reach a real user directory, since these are the
only tests in the suite that delete files.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from patent_checker import cleanup
from patent_checker.cache import Cache
from patent_checker.cleanup import CleanupItem, CleanupPlan, execute, legacy_summary, plan_cleanup


@dataclass(frozen=True)
class _Tree:
    """The four roots a cleanup run can touch, as laid out by :func:`_build_tree`."""

    project: Path
    shared: Path
    user_data: Path
    user_consent: Path
    outside: Path


def _write(path: Path, text: str) -> Path:
    """Create *path*'s parents and write *text* to it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _build_tree(tmp_path: Path) -> _Tree:
    """Lay out a project data base, a shared cache, a server data dir and a user config.

    The project holds one file of every category the planner knows about, so
    a test can assert on the whole classification at once.
    """
    project = tmp_path / "project"
    shared = tmp_path / "shared"
    user_data = tmp_path / "userdata"
    user_consent = tmp_path / "userconfig" / "patent-checker" / "consent.json"
    outside = tmp_path / "outside"

    # Project: current search cache, request log, v0.3 leftovers, artifacts.
    _write(project / "cache" / "ops" / "search" / "aaaa.xml", "<search/>")
    _write(project / "cache" / "ops" / "search" / "aaaa.meta.json", "{}")
    _write(project / "cache" / "ops" / "searchbib" / "bbbb.xml", "<searchbib/>")
    _write(project / "raw" / "ops" / "headers.jsonl", '{"url": "https://ops.epo.org"}\n')
    _write(project / "raw" / "ops" / "20260101-120000_biblio_US1.xml", "<legacy-body/>")
    _write(project / "raw" / "gp" / "US11468338B2.html", "<html></html>")
    _write(project / "cache" / "ops" / "biblio" / "US.1.A1.xml", "<legacy-biblio/>")
    _write(project / "cache" / "ops" / "claims" / "US.1.A1.xml", "<legacy-claims/>")
    _write(project / "reports" / "report-x-20260101-1200.md", "# report")
    _write(project / "exploration-x" / "stage1.json", "[]")
    _write(project / "consent.json", '{"notice_version": "1"}')

    # Shared cache: another project's reusable resource.
    _write(shared / "ops" / "biblio" / "EP.2.A1.xml", "<shared-biblio/>")
    _write(shared / "gp" / "EP2A1.html", "<html></html>")

    # The MCP server's per-user data directory.
    _write(user_data / "cache" / "ops" / "search" / "cccc.xml", "<search/>")
    _write(user_data / "raw" / "ops" / "headers.jsonl", "{}\n")

    _write(user_consent, '{"notice_version": "1"}')
    _write(outside / "keep.txt", "not ours")

    return _Tree(
        project=project,
        shared=shared,
        user_data=user_data,
        user_consent=user_consent,
        outside=outside,
    )


def _cache_for(tree: _Tree) -> Cache:
    """Return the cache the CLI would build for *tree*: shared root plus project search cache."""
    return Cache(tree.shared, tree.project / "cache")


def _paths_by_category(plan: CleanupPlan) -> dict[str, set[Path]]:
    """Group the planned paths by category, for readable set comparisons."""
    grouped: dict[str, set[Path]] = {}
    for item in plan.items:
        grouped.setdefault(item.category, set()).add(item.path)
    return grouped


def _files_under(root: Path) -> set[Path]:
    """Return every regular file under *root* (symlinks included, never followed)."""
    if not root.exists():
        return set()
    return {path for path in root.rglob("*") if path.is_file() or path.is_symlink()}


# --- plan_cleanup: the default scope ---------------------------------------


def test_default_plan_targets_the_projects_own_leftovers(tmp_path: Path) -> None:
    """By default the plan holds the project's search cache, request log and v0.3 leftovers."""
    tree = _build_tree(tmp_path)

    plan = plan_cleanup(data_base=tree.project, cache=_cache_for(tree))

    assert _paths_by_category(plan) == {
        "search-cache": {
            tree.project / "cache" / "ops" / "search",
            tree.project / "cache" / "ops" / "searchbib",
        },
        "request-log": {tree.project / "raw" / "ops" / "headers.jsonl"},
        "legacy-raw": {
            tree.project / "raw" / "ops" / "20260101-120000_biblio_US1.xml",
            tree.project / "raw" / "gp",
        },
        "legacy-cache": {
            tree.project / "cache" / "ops" / "biblio",
            tree.project / "cache" / "ops" / "claims",
        },
    }
    assert {item.scope for item in plan.items} == {"project"}


def test_default_plan_leaves_artifacts_consent_and_the_shared_side_alone(tmp_path: Path) -> None:
    """Reports, exploration output, consent records and the shared roots are never planned."""
    tree = _build_tree(tmp_path)

    plan = plan_cleanup(
        data_base=tree.project,
        cache=_cache_for(tree),
        user_data_dir=tree.user_data,
        user_consent_path=tree.user_consent,
    )

    planned = {item.path for item in plan.items}
    for spared in (
        tree.project / "reports",
        tree.project / "exploration-x",
        tree.project / "consent.json",
        tree.shared,
        tree.user_data,
        tree.user_consent,
    ):
        assert not any(path == spared or path.is_relative_to(spared) for path in planned)


def test_plan_skips_paths_that_do_not_exist(tmp_path: Path) -> None:
    """A data base with nothing in it yields an empty plan rather than phantom items."""
    empty = tmp_path / "empty"
    empty.mkdir()

    plan = plan_cleanup(data_base=empty, cache=Cache(tmp_path / "shared", empty / "cache"))

    assert plan.items == ()
    assert plan.total_files == 0
    assert plan.total_bytes == 0


def test_legacy_cache_is_not_planned_when_the_project_cache_is_the_shared_root(
    tmp_path: Path,
) -> None:
    """The default-configured server's own cache is live data, not a v0.3 leftover."""
    tree = _build_tree(tmp_path)
    # The MCP server's data base: <data_base>/cache *is* the shared root.
    cache = Cache(tree.project / "cache")

    plan = plan_cleanup(data_base=tree.project, cache=cache)

    assert "legacy-cache" not in _paths_by_category(plan)
    assert _paths_by_category(plan)["search-cache"] == {
        tree.project / "cache" / "ops" / "search",
        tree.project / "cache" / "ops" / "searchbib",
    }


# --- plan_cleanup: the opt-in scopes ---------------------------------------


def test_shared_flag_adds_the_shared_cache_and_the_server_data_directory(tmp_path: Path) -> None:
    """--shared brings in the per-user cache root and the server's data directory."""
    tree = _build_tree(tmp_path)

    plan = plan_cleanup(
        data_base=tree.project,
        cache=_cache_for(tree),
        shared=True,
        user_data_dir=tree.user_data,
    )

    grouped = _paths_by_category(plan)
    assert grouped["shared-cache"] == {tree.shared / "ops", tree.shared / "gp"}
    assert grouped["server-data"] == {tree.user_data / "cache", tree.user_data / "raw"}
    assert {item.scope for item in plan.items if item.category == "shared-cache"} == {"shared"}
    assert {item.scope for item in plan.items if item.category == "server-data"} == {"user"}


def test_shared_flag_without_a_user_data_dir_plans_no_server_data(tmp_path: Path) -> None:
    """A caller that does not name a server data directory gets no server-data items."""
    tree = _build_tree(tmp_path)

    plan = plan_cleanup(data_base=tree.project, cache=_cache_for(tree), shared=True)

    assert "server-data" not in _paths_by_category(plan)


def test_shared_flag_skips_the_server_data_directory_when_it_is_the_project_root(
    tmp_path: Path,
) -> None:
    """A server data directory equal to the data base is already covered by the project items."""
    tree = _build_tree(tmp_path)

    plan = plan_cleanup(
        data_base=tree.project,
        cache=_cache_for(tree),
        shared=True,
        user_data_dir=tree.project,
    )

    assert "server-data" not in _paths_by_category(plan)


def test_shared_flag_narrows_the_server_cache_to_searches_when_it_is_the_shared_root(
    tmp_path: Path,
) -> None:
    """When the server cache *is* the shared root, only its search kinds are added again."""
    tree = _build_tree(tmp_path)
    _write(tree.user_data / "cache" / "ops" / "searchbib" / "dddd.xml", "<searchbib/>")
    _write(tree.user_data / "cache" / "ops" / "biblio" / "US.9.A1.xml", "<biblio/>")
    cache = Cache(tree.user_data / "cache", tree.project / "cache")

    plan = plan_cleanup(
        data_base=tree.project,
        cache=cache,
        shared=True,
        user_data_dir=tree.user_data,
    )

    grouped = _paths_by_category(plan)
    # The whole shared tree is already planned as shared-cache, so the
    # narrowed search directories are folded into it rather than repeated.
    assert grouped["shared-cache"] == {tree.user_data / "cache" / "ops"}
    assert grouped["server-data"] == {tree.user_data / "raw"}


def test_include_artifacts_adds_only_what_the_package_does_not_write(tmp_path: Path) -> None:
    """--include-artifacts targets the data base children outside CODE_OWNED_NAMES."""
    tree = _build_tree(tmp_path)

    plan = plan_cleanup(data_base=tree.project, cache=_cache_for(tree), include_artifacts=True)

    grouped = _paths_by_category(plan)
    assert grouped["artifact"] == {
        tree.project / "reports",
        tree.project / "exploration-x",
    }
    assert {item.scope for item in plan.items if item.category == "artifact"} == {"project"}


def test_include_consent_adds_the_project_record_only(tmp_path: Path) -> None:
    """Without --shared, only the project-scoped consent record is planned."""
    tree = _build_tree(tmp_path)

    plan = plan_cleanup(
        data_base=tree.project,
        cache=_cache_for(tree),
        include_consent=True,
        user_consent_path=tree.user_consent,
    )

    assert _paths_by_category(plan)["consent"] == {tree.project / "consent.json"}


def test_include_consent_with_shared_adds_the_user_record_too(tmp_path: Path) -> None:
    """The per-user consent record needs both --shared and --include-consent."""
    tree = _build_tree(tmp_path)

    plan = plan_cleanup(
        data_base=tree.project,
        cache=_cache_for(tree),
        shared=True,
        include_consent=True,
        user_consent_path=tree.user_consent,
    )

    consent_items = [item for item in plan.items if item.category == "consent"]
    assert {item.path for item in consent_items} == {
        tree.project / "consent.json",
        tree.user_consent,
    }
    assert {item.scope for item in consent_items} == {"project", "user"}


# --- plan_cleanup: roots, counting and serialization ------------------------


def test_plan_roots_cover_every_directory_a_deletion_may_touch(tmp_path: Path) -> None:
    """Every planned path sits under one of the plan's roots."""
    tree = _build_tree(tmp_path)

    plan = plan_cleanup(
        data_base=tree.project,
        cache=_cache_for(tree),
        shared=True,
        include_artifacts=True,
        include_consent=True,
        user_data_dir=tree.user_data,
        user_consent_path=tree.user_consent,
    )

    roots = [root.resolve() for root in plan.roots]
    for item in plan.items:
        resolved = item.path.parent.resolve() / item.path.name
        assert any(resolved.is_relative_to(root) for root in roots), item.path


def test_plan_counts_files_and_bytes_per_item_and_in_total(tmp_path: Path) -> None:
    """An item's counts cover its whole tree; the totals are their sum."""
    tree = _build_tree(tmp_path)

    plan = plan_cleanup(data_base=tree.project, cache=_cache_for(tree))

    by_path = {item.path: item for item in plan.items}
    search_dir = by_path[tree.project / "cache" / "ops" / "search"]
    assert search_dir.files == 2  # body + sidecar
    assert search_dir.bytes == len("<search/>") + len("{}")
    log = by_path[tree.project / "raw" / "ops" / "headers.jsonl"]
    assert log.files == 1
    assert plan.total_files == sum(item.files for item in plan.items)
    assert plan.total_bytes == sum(item.bytes for item in plan.items)


def test_plan_counts_a_symlink_as_one_file_without_following_it(tmp_path: Path) -> None:
    """A symlink inside a planned tree counts as one zero-byte file, target untouched."""
    tree = _build_tree(tmp_path)
    (tree.project / "raw" / "gp" / "link.html").symlink_to(tree.outside / "keep.txt")

    plan = plan_cleanup(data_base=tree.project, cache=_cache_for(tree))

    by_path = {item.path: item for item in plan.items}
    gp_dir = by_path[tree.project / "raw" / "gp"]
    assert gp_dir.files == 2
    assert gp_dir.bytes == len("<html></html>")


def test_plan_does_not_count_a_directory_twice_when_the_roots_coincide(tmp_path: Path) -> None:
    """With one root for everything, --shared must not count the search cache twice."""
    tree = _build_tree(tmp_path)
    cache = Cache(tree.project / "cache")  # shared root == project cache

    plan = plan_cleanup(data_base=tree.project, cache=cache, shared=True)

    assert plan.total_files == len(_files_under(tree.project / "cache")) + len(
        _files_under(tree.project / "raw")
    )


def test_plan_to_dict_is_json_serializable(tmp_path: Path) -> None:
    """to_dict renders paths as strings so the CLI can print the plan as JSON."""
    tree = _build_tree(tmp_path)

    plan = plan_cleanup(data_base=tree.project, cache=_cache_for(tree))
    payload = json.loads(json.dumps(plan.to_dict()))

    assert payload["total_files"] == plan.total_files
    assert payload["total_bytes"] == plan.total_bytes
    assert len(payload["items"]) == len(plan.items)
    first = payload["items"][0]
    assert set(first) == {"category", "scope", "path", "files", "bytes"}
    assert isinstance(first["path"], str)


# --- legacy_summary --------------------------------------------------------


def test_legacy_summary_counts_the_v03_layout(tmp_path: Path) -> None:
    """The three v0.3 locations are reported separately and as a total."""
    tree = _build_tree(tmp_path)

    summary = legacy_summary(tree.project, _cache_for(tree))

    assert summary["raw_ops_bodies"]["dir"] == str(tree.project / "raw" / "ops")
    assert summary["raw_ops_bodies"]["files"] == 1  # headers.jsonl is not a leftover
    assert summary["raw_ops_bodies"]["bytes"] == len("<legacy-body/>")
    assert summary["raw_gp"]["files"] == 1
    assert summary["cache_pub_kinds"]["files"] == 2  # biblio + claims
    assert summary["total_files"] == 4
    assert summary["total_bytes"] == sum(
        summary[key]["bytes"] for key in ("raw_ops_bodies", "raw_gp", "cache_pub_kinds")
    )


def test_legacy_summary_ignores_a_cache_that_is_the_shared_root(tmp_path: Path) -> None:
    """The default-configured server's publication cache is live, so it is not counted."""
    tree = _build_tree(tmp_path)

    summary = legacy_summary(tree.project, Cache(tree.project / "cache"))

    assert summary["cache_pub_kinds"]["files"] == 0
    assert summary["cache_pub_kinds"]["bytes"] == 0
    assert summary["total_files"] == 2  # only the raw/ leftovers


def test_legacy_summary_on_a_missing_data_base_is_all_zero(tmp_path: Path) -> None:
    """Nothing on disk means zero counts, not an error."""
    missing = tmp_path / "nowhere"

    summary = legacy_summary(missing, Cache(tmp_path / "shared", missing / "cache"))

    assert summary["total_files"] == 0
    assert summary["total_bytes"] == 0


# --- execute ---------------------------------------------------------------


def test_execute_removes_the_planned_files_and_keeps_everything_else(tmp_path: Path) -> None:
    """The default plan empties the cache/raw leftovers and spares reports and consent."""
    tree = _build_tree(tmp_path)
    plan = plan_cleanup(data_base=tree.project, cache=_cache_for(tree))

    result = execute(plan)

    assert result["errors"] == []
    assert result["removed_files"] == plan.total_files
    assert result["bytes"] == plan.total_bytes
    assert len(result["paths"]) == plan.total_files
    assert (tree.project / "reports" / "report-x-20260101-1200.md").exists()
    assert (tree.project / "exploration-x" / "stage1.json").exists()
    assert (tree.project / "consent.json").exists()
    assert _files_under(tree.shared)  # the shared cache is untouched
    assert _files_under(tree.user_data)
    assert _files_under(tree.project / "cache") == set()
    assert _files_under(tree.project / "raw") == set()


def test_execute_removes_the_emptied_directories(tmp_path: Path) -> None:
    """A directory whose whole tree was planned is removed once it is empty."""
    tree = _build_tree(tmp_path)
    plan = plan_cleanup(data_base=tree.project, cache=_cache_for(tree))

    execute(plan)

    assert not (tree.project / "raw" / "gp").exists()
    assert not (tree.project / "cache" / "ops" / "search").exists()
    assert tree.project.exists()


def test_execute_is_idempotent(tmp_path: Path) -> None:
    """Running the same plan twice removes nothing the second time and reports no error."""
    tree = _build_tree(tmp_path)
    plan = plan_cleanup(data_base=tree.project, cache=_cache_for(tree))

    execute(plan)
    second = execute(plan)

    assert second["removed_files"] == 0
    assert second["bytes"] == 0
    assert second["errors"] == []


def test_execute_removes_a_symlink_without_touching_its_target(tmp_path: Path) -> None:
    """Only the link is unlinked: neither a file nor a directory target is followed."""
    tree = _build_tree(tmp_path)
    file_link = tree.project / "raw" / "gp" / "link.html"
    dir_link = tree.project / "raw" / "gp" / "elsewhere"
    file_link.symlink_to(tree.outside / "keep.txt")
    dir_link.symlink_to(tree.outside, target_is_directory=True)
    plan = plan_cleanup(data_base=tree.project, cache=_cache_for(tree))

    result = execute(plan)

    assert result["errors"] == []
    assert not file_link.is_symlink()
    assert not dir_link.is_symlink()
    assert (tree.outside / "keep.txt").read_text(encoding="utf-8") == "not ours"


def test_execute_refuses_a_path_outside_the_roots(tmp_path: Path) -> None:
    """A path that escapes the plan's roots is reported and left on disk."""
    tree = _build_tree(tmp_path)
    item = CleanupItem(
        category="artifact",
        scope="project",
        path=tree.outside,
        files=1,
        bytes=len("not ours"),
    )
    plan = CleanupPlan(items=(item,), roots=(tree.project,))

    result = execute(plan)

    assert result["removed_files"] == 0
    assert [error["path"] for error in result["errors"]] == [
        str(tree.outside / "keep.txt"),
        str(tree.outside),
    ]
    assert all("outside" in error["error"] for error in result["errors"])
    assert (tree.outside / "keep.txt").exists()


def test_execute_keeps_a_directory_it_could_not_empty(tmp_path: Path) -> None:
    """A partially removable tree loses what it may and keeps the rest, directory included."""
    tree = _build_tree(tmp_path)
    item = CleanupItem(
        category="legacy-raw", scope="project", path=tree.project / "raw", files=0, bytes=0
    )
    # Roots deliberately narrower than the item, so only raw/ops is removable.
    plan = CleanupPlan(items=(item,), roots=(tree.project / "raw" / "ops",))

    result = execute(plan)

    assert not (tree.project / "raw" / "ops").exists()
    assert (tree.project / "raw" / "gp" / "US11468338B2.html").exists()
    assert (tree.project / "raw").is_dir()
    assert {error["path"] for error in result["errors"]} == {
        str(tree.project / "raw" / "gp" / "US11468338B2.html"),
        str(tree.project / "raw" / "gp"),
        str(tree.project / "raw"),
    }


def test_execute_reports_an_unremovable_file_and_carries_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError on one file is recorded while the rest of the plan still runs."""
    tree = _build_tree(tmp_path)
    plan = plan_cleanup(data_base=tree.project, cache=_cache_for(tree))
    doomed = tree.project / "raw" / "ops" / "headers.jsonl"
    real_unlink = Path.unlink

    def fake_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == doomed:
            raise OSError("device is busy")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fake_unlink)

    result = execute(plan)

    assert [error["path"] for error in result["errors"]] == [str(doomed)]
    assert result["errors"][0]["error"] == "device is busy"
    assert doomed.exists()
    assert result["removed_files"] == plan.total_files - 1
    assert _files_under(tree.project / "cache") == set()


def test_execute_on_an_empty_plan_does_nothing(tmp_path: Path) -> None:
    """An empty plan is a valid no-op."""
    result = execute(CleanupPlan(items=(), roots=(tmp_path,)))

    assert result == {"removed_files": 0, "bytes": 0, "paths": [], "errors": []}


# --- module constants ------------------------------------------------------


def test_code_owned_names_cover_exactly_what_the_package_writes() -> None:
    """The names the package itself writes under a data base are the ones spared by default."""
    assert cleanup.CODE_OWNED_NAMES == frozenset({"cache", "raw", "consent.json"})
    assert cleanup.REQUEST_LOG_RELATIVE == Path("raw") / "ops" / "headers.jsonl"
    assert cleanup.REQUEST_LOG_RELATIVE.parts[0] in cleanup.CODE_OWNED_NAMES
    assert os.sep not in "".join(cleanup.CODE_OWNED_NAMES)


def test_execute_reports_a_permission_error_on_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory that may not be removed is reported, not silently kept.

    "Not empty" is a normal outcome (something unplanned lives there) and stays
    silent, but a permission problem is something the operator has to see.
    """
    tree = _build_tree(tmp_path)
    plan = plan_cleanup(data_base=tree.project, cache=_cache_for(tree))
    doomed = tree.project / "raw" / "gp"
    real_rmdir = Path.rmdir

    def fake_rmdir(self: Path) -> None:
        if self == doomed:
            raise PermissionError("permission denied")
        real_rmdir(self)

    monkeypatch.setattr(Path, "rmdir", fake_rmdir)

    result = execute(plan)

    assert [error["path"] for error in result["errors"]] == [str(doomed)]
    assert "permission denied" in result["errors"][0]["error"]
    # The rest of the plan still ran: only the one directory is left behind.
    assert doomed.exists()
    assert not (tree.project / "cache" / "ops" / "search").exists()


def test_execute_keeps_quiet_about_a_directory_that_is_not_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory that is still in use is left alone, without an error entry."""
    tree = _build_tree(tmp_path)
    plan = plan_cleanup(data_base=tree.project, cache=_cache_for(tree))
    kept = tree.project / "raw" / "gp"
    real_rmdir = Path.rmdir

    def fake_rmdir(self: Path) -> None:
        if self == kept:
            raise OSError("Directory not empty")
        real_rmdir(self)

    monkeypatch.setattr(Path, "rmdir", fake_rmdir)

    result = execute(plan)

    assert result["errors"] == []
    assert kept.exists()
