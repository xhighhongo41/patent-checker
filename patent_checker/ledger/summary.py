"""Summarizing a project's ledgers: the forgiving half of the reader.

What the Skill reads before it decides which kind of run to propose. A
damaged ledger never fails this: what can be counted is counted, and what
could not be read is described in ``problems``. The strict reading lives in
:mod:`patent_checker.ledger.checking`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from patent_checker import config
from patent_checker.ledger.layout import (
    FAMILIES_FILENAME,
    FEATURE_STATUSES,
    FEATURES_FILENAME,
    LAST_RUN_KEYS,
    LEDGER_DIRNAME,
    MANIFEST_FILENAME,
    QUERIES_FILENAME,
    QUERY_STATUSES,
    RUNS_FILENAME,
    STATE_FILENAMES,
    WATCH_FILENAME,
)
from patent_checker.ledger.reading import (
    JsonLines,
    as_date,
    checkable_targets,
    has_value,
    load_jsonl,
    read_text,
    shown,
    sorted_names,
)


def status(
    data_base: Path | None = None,
    *,
    target: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Summarize the exploration ledgers of a project.

    This is what the Skill reads before it decides which kind of run to
    propose: whether a ledger exists at all, what the last run did, and how
    many monitored publications are open and due. Nothing here fails on a
    damaged ledger -- what can be counted is counted and what could not be
    read is described in ``problems``; :func:`check` is the strict reading.
    A target directory with no manifest yet is listed too, rather than
    omitted, so that this function and :func:`check` agree on which targets
    exist; its ``"format"`` is ``None`` and ``"problems"`` names the missing
    manifest, same as any other file this summary could not read.

    Args:
        data_base: The ``.patent-checker`` directory to read. Resolved from
            :func:`patent_checker.config.data_base` when ``None``.
        target: Summarize only this target's ledger (and only the legacy
            artifacts whose name carries it). ``None`` covers every target.
        today: The day ``due`` is counted against. Defaults to the current
            local date.

    Returns:
        ``{"exists": bool, "path": str, "targets": [str, ...], "ledgers":
        [{"target", "format", "runs", "last_run", "features", "queries",
        "families", "watch", "problems"}, ...], "legacy": {"reports":
        [str, ...], "explorations": [str, ...]}}``.

    Raises:
        OSError: If a file that is there cannot be read at all (a permission
            problem, for instance); a missing file is not an error here.
    """
    base = config.data_base() if data_base is None else data_base
    root = base / LEDGER_DIRNAME
    names = checkable_targets(root)
    wanted = names if target is None else [name for name in names if name == target]
    day = date.today() if today is None else today
    return {
        "exists": bool(wanted),
        "path": str(root),
        "targets": names,
        "ledgers": [_summarize_target(root / name, name, day) for name in wanted],
        "legacy": _legacy_artifacts(base, target),
    }


# --- What one ledger's summary is made of ------------------------------------


def _summarize_target(directory: Path, name: str, today: date) -> dict[str, Any]:
    """Summarize one ledger, describing rather than raising on what it cannot read."""
    problems: list[str] = []
    files = {
        filename: _load_for_status(directory, filename, problems) for filename in STATE_FILENAMES
    }
    runs = files[RUNS_FILENAME]
    return {
        "target": name,
        "format": _manifest_format(directory, problems),
        "runs": len(runs.records),
        "last_run": _last_run(runs),
        "features": _count_values(files[FEATURES_FILENAME], "status", FEATURE_STATUSES),
        "queries": _count_values(files[QUERIES_FILENAME], "status", QUERY_STATUSES),
        "families": len(files[FAMILIES_FILENAME].records),
        "watch": _watch_summary(files[WATCH_FILENAME], today),
        "problems": problems,
    }


def _load_for_status(directory: Path, filename: str, problems: list[str]) -> JsonLines:
    """Read one JSON Lines file, recording what it could not read as a sentence."""
    loaded = load_jsonl(directory / filename)
    if not loaded.present:
        problems.append(f"{filename} is missing")
    elif loaded.problem is not None:
        problems.append(f"{filename} {loaded.problem}")
    else:
        problems.extend(
            f"{filename} line {line.number}: {line.problem}"
            for line in loaded.lines
            if line.record is None
        )
    return loaded


def _manifest_format(directory: Path, problems: list[str]) -> int | None:
    """Return the format the manifest names, or None when it does not name one."""
    path = directory / MANIFEST_FILENAME
    if not path.is_file():
        problems.append(f"{MANIFEST_FILENAME} is missing")
        return None
    text, problem = read_text(path)
    if text is None:
        problems.append(f"{MANIFEST_FILENAME} {problem}")
        return None
    try:
        record = json.loads(text)
    except json.JSONDecodeError as exc:
        problems.append(f"{MANIFEST_FILENAME} is not valid JSON: {exc.msg} (line {exc.lineno})")
        return None
    if not isinstance(record, dict):
        problems.append(f"{MANIFEST_FILENAME} is JSON but not an object")
        return None
    value = record.get("format")
    if isinstance(value, bool) or not isinstance(value, int):
        problems.append(f'{MANIFEST_FILENAME} names no format ("format" is {shown(value)})')
        return None
    return value


def _last_run(runs: JsonLines) -> dict[str, Any] | None:
    """Return the last readable line of ``runs.jsonl``, keyed as reported."""
    records = runs.records
    if not records:
        return None
    _, record = records[-1]
    return {key: record.get(key) for key in LAST_RUN_KEYS}


def _count_values(loaded: JsonLines, key: str, values: Sequence[str]) -> dict[str, int]:
    """Count how many records carry each of *values* under *key*."""
    counts = dict.fromkeys(values, 0)
    for _, record in loaded.records:
        value = record.get(key)
        if isinstance(value, str) and value in counts:
            counts[value] += 1
    return counts


def _watch_summary(watch: JsonLines, today: date) -> dict[str, Any]:
    """Summarize the monitored publications: open, closed, due, provisional, next date."""
    summary = {"open": 0, "closed": 0, "due": 0, "provisional": 0}
    earliest: date | None = None
    for _, record in watch.records:
        if has_value(record, "closed_run"):
            summary["closed"] += 1
            continue
        summary["open"] += 1
        if record.get("reason") == "provisional":
            summary["provisional"] += 1
        next_check = as_date(record.get("next_check"))
        if next_check is None:
            continue
        if next_check <= today:
            summary["due"] += 1
        if earliest is None or next_check < earliest:
            earliest = next_check
    return {**summary, "next_check": None if earliest is None else earliest.isoformat()}


def _legacy_artifacts(base: Path, target: str | None) -> dict[str, list[str]]:
    """Return the reports and working directories left next to (or before) the ledger.

    These are what tells a first run with no ledger apart from a run that
    continues an exploration made before the ledger existed.
    """
    reports = sorted_names(
        base / "reports",
        lambda child: (
            child.is_file() and child.name.startswith("report-") and child.name.endswith(".md")
        ),
    )
    explorations = sorted_names(
        base,
        lambda child: child.is_dir() and child.name.startswith("exploration-"),
    )
    if target is not None:
        # The naming convention of both is "<kind>-<target>-<date>", so the
        # target sits between two dashes whatever the date spelling.
        needle = f"-{target}-"
        reports = [name for name in reports if needle in name]
        explorations = [name for name in explorations if needle in name]
    return {"reports": reports, "explorations": explorations}
