"""Verifying a ledger against the format: the strict half of the reader.

:func:`check` walks a project's ledgers, reads each file once
(:mod:`patent_checker.ledger.reading`), applies the rules of every file
(:mod:`patent_checker.ledger.rules`) and hands back findings that name the
file, the line and the rule. This module holds what is left: the flow of
one target, and the two files that have no table of their own
(``ledger.json`` and ``translation.md``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from patent_checker import config
from patent_checker.ledger.findings import FileFindings, Findings, Ledger, target_report
from patent_checker.ledger.layout import (
    FAMILIES_FILENAME,
    FEATURES_FILENAME,
    LEDGER_DIRNAME,
    MANIFEST_FILENAME,
    QUERIES_FILENAME,
    RUNS_FILENAME,
    STATE_FILENAMES,
    SUPPORTED_FORMATS,
    TARGET_NAME_RE,
    TRANSLATION_FILENAME,
    WATCH_FILENAME,
)
from patent_checker.ledger.reading import (
    JsonLines,
    as_timestamp,
    checkable_targets,
    load_jsonl,
    read_text,
    shown,
)
from patent_checker.ledger.rules import (
    check_families,
    check_features,
    check_queries,
    check_runs,
    check_watch,
)
from patent_checker.ledger.values import require


def check(data_base: Path | None = None, *, target: str | None = None) -> dict[str, Any]:
    """Verify one ledger, or every ledger of a project, against the format.

    Every finding names the file, the line (1-based, counting blank lines)
    and the rule it breaks, and its message says what to write instead: the
    agent that wrote the ledger is the one that has to fix it. Files and
    keys this format does not define are ignored, so notes of the agent's
    own can sit next to the required ones.

    Args:
        data_base: The ``.patent-checker`` directory to read. Resolved from
            :func:`patent_checker.config.data_base` when ``None``.
        target: Check only this target's ledger. ``None`` checks every one.

    Returns:
        ``{"ok": bool, "path": str, "targets": [{"target", "ok", "errors",
        "warnings", "counts"}, ...]}``, where each finding is ``{"file":
        str, "line": int | None, "rule": str, "message": str}``. ``ok`` is
        True when no target reported an error; warnings do not affect it.
        A ledger that is not there is reported as a ``missing-ledger``
        error, not raised.

    Raises:
        OSError: If a file that is there cannot be read at all.
    """
    base = config.data_base() if data_base is None else data_base
    root = base / LEDGER_DIRNAME
    names = checkable_targets(root)
    wanted = names if target is None else [name for name in names if name == target]
    if not wanted:
        return {"ok": False, "path": str(root), "targets": [_missing_ledger_report(target or "")]}
    reports = [_check_target(base, root / name, name) for name in wanted]
    return {
        "ok": all(report["ok"] for report in reports),
        "path": str(root),
        "targets": reports,
    }


def _missing_ledger_report(name: str) -> dict[str, Any]:
    """Return the report of a ledger that is not there."""
    where = f"for target {shown(name)}" if name else "in this project"
    findings = Findings()
    findings.error(
        MANIFEST_FILENAME,
        None,
        "missing-ledger",
        f"there is no ledger directory {where}; the agent creates one on the first run "
        "(see references/ledger-format.md)",
    )
    return target_report(name, findings, {})


# --- Checking one target -----------------------------------------------------


def _check_target(base: Path, directory: Path, name: str) -> dict[str, Any]:
    """Check one ledger directory and return its report."""
    findings = Findings()
    _check_target_name(findings, name)
    _check_manifest(directory, name, findings)

    files = {
        filename: _load_for_check(directory, filename, findings) for filename in STATE_FILENAMES
    }
    runs = files[RUNS_FILENAME]
    features = files[FEATURES_FILENAME]
    families = files[FAMILIES_FILENAME]

    ctx = Ledger(
        base=base,
        findings=findings,
        run_ids=_identifiers(runs, "run_id"),
        run_modes=_run_modes(runs),
        feature_ids=_identifiers(features, "id"),
        family_ids=_identifiers(families, "family_id"),
    )
    check_runs(ctx, runs)
    check_features(ctx, features)
    check_queries(ctx, files[QUERIES_FILENAME])
    check_families(ctx, families)
    check_watch(ctx, files[WATCH_FILENAME])
    _check_translation(directory, findings)

    # "runs.jsonl" is counted under "runs", and so on for the other four.
    counts = {
        filename.removesuffix(".jsonl"): len(loaded.records) for filename, loaded in files.items()
    }
    return target_report(name, findings, counts)


def _check_target_name(findings: Findings, name: str) -> None:
    """Warn when the directory name is not a target name."""
    if TARGET_NAME_RE.match(name):
        return
    findings.warning(
        MANIFEST_FILENAME,
        None,
        "target-name",
        f"the ledger directory is named {shown(name)}; a target name uses lower-case "
        "letters, digits, '-' and '_', like the one in the report file name",
    )


def _load_for_check(directory: Path, filename: str, findings: Findings) -> JsonLines:
    """Read one required JSON Lines file, reporting what makes it unreadable."""
    ff = FileFindings(findings, filename)
    loaded = load_jsonl(directory / filename)
    if not loaded.present:
        ff.error(None, "missing-file", f"{filename} is missing; every ledger holds it, even empty")
    elif loaded.problem is not None:
        ff.error(None, "encoding", f"{filename} {loaded.problem}")
    else:
        for line in loaded.lines:
            if line.record is None:
                ff.error(line.number, "json", line.problem or "the line is not a JSON object")
    return loaded


def _identifiers(loaded: JsonLines, key: str) -> frozenset[str] | None:
    """Return the identifiers a file carries, or None when it could not be read."""
    if not loaded.readable:
        return None
    return frozenset(
        record[key] for _, record in loaded.records if isinstance(record.get(key), str)
    )


def _run_modes(runs: JsonLines) -> dict[str, str]:
    """Return the mode of every run that names one."""
    modes: dict[str, str] = {}
    for _, record in runs.records:
        run_id = record.get("run_id")
        mode = record.get("mode")
        if isinstance(run_id, str) and isinstance(mode, str):
            modes[run_id] = mode
    return modes


def _check_manifest(directory: Path, name: str, findings: Findings) -> None:
    """Check ``ledger.json``: it is what says the ledger is readable at all."""
    ff = FileFindings(findings, MANIFEST_FILENAME)
    path = directory / MANIFEST_FILENAME
    if not path.is_file():
        ff.error(None, "missing-file", f"{MANIFEST_FILENAME} is missing; it names the format")
        return
    text, problem = read_text(path)
    if text is None:
        ff.error(None, "encoding", f"{MANIFEST_FILENAME} {problem}")
        return
    try:
        record = json.loads(text)
    except json.JSONDecodeError as exc:
        ff.error(None, "json", f"{MANIFEST_FILENAME} is not valid JSON: {exc.msg}")
        return
    if not isinstance(record, dict):
        ff.error(None, "json", f"{MANIFEST_FILENAME} is JSON but not an object")
        return
    _check_manifest_values(ff, record, name)


def _check_manifest_values(ff: FileFindings, record: Mapping[str, Any], name: str) -> None:
    """Check the three things the manifest must say."""
    if require(ff, None, record, "format"):
        value = record["format"]
        if isinstance(value, bool) or value not in SUPPORTED_FORMATS:
            ff.error(
                None,
                "manifest",
                f'"format" is {shown(value)}; this release reads format '
                f"{', '.join(str(known) for known in SUPPORTED_FORMATS)}",
            )
    if require(ff, None, record, "target"):
        value = record["target"]
        if value != name:
            ff.error(
                None,
                "manifest",
                f'"target" is {shown(value)} but the directory is named {shown(name)}; '
                "the two must agree",
            )
    if require(ff, None, record, "created_at") and as_timestamp(record["created_at"]) is None:
        ff.error(
            None,
            "manifest",
            f'"created_at" is {shown(record["created_at"])}; expected an ISO 8601 '
            "timestamp such as 2026-03-01T09:30:00+00:00",
        )


def _check_translation(directory: Path, findings: Findings) -> None:
    """Warn when the cumulative vocabulary translation table is missing."""
    if (directory / TRANSLATION_FILENAME).is_file():
        return
    findings.warning(
        TRANSLATION_FILENAME,
        None,
        "translation-missing",
        f"{TRANSLATION_FILENAME} is missing; the vocabulary translation table is kept "
        "cumulative from run to run",
    )
