"""Where a check collects what it found, and how one target's report reads.

A finding is a plain dict -- ``{"file", "line", "rule", "message"}`` -- so
that it survives JSON on its way to the agent that has to fix the ledger.
:class:`Findings` gathers them for one target, :class:`FileFindings` binds
them to one file, :class:`Unique` watches over a file's identifiers, and
:class:`Ledger` carries what checking one file needs to know about the rest.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from patent_checker.ledger.layout import FILE_ORDER
from patent_checker.ledger.reading import shown


class Findings:
    """The errors and warnings of one ledger, in the order they were found."""

    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []

    def error(self, file: str, line: int | None, rule: str, message: str) -> None:
        """Record something the next run cannot rely on."""
        self.errors.append({"file": file, "line": line, "rule": rule, "message": message})

    def warning(self, file: str, line: int | None, rule: str, message: str) -> None:
        """Record something worth fixing that still leaves the ledger usable."""
        self.warnings.append({"file": file, "line": line, "rule": rule, "message": message})


class FileFindings:
    """A :class:`Findings` bound to one file, so callers pass a line and a rule."""

    def __init__(self, findings: Findings, filename: str) -> None:
        self._findings = findings
        self._filename = filename

    def error(self, line: int | None, rule: str, message: str) -> None:
        """Record an error in this file."""
        self._findings.error(self._filename, line, rule, message)

    def warning(self, line: int | None, rule: str, message: str) -> None:
        """Record a warning in this file."""
        self._findings.warning(self._filename, line, rule, message)


class Unique:
    """Remembers where each identifier of one file was first seen."""

    def __init__(self, ff: FileFindings, label: str) -> None:
        self._ff = ff
        self._label = label
        self._seen: dict[str, int] = {}

    def add(self, line: int, value: str) -> None:
        """Record *value*, reporting it when the file already carried it."""
        first = self._seen.get(value)
        if first is None:
            self._seen[value] = line
            return
        self._ff.error(
            line,
            "unique-id",
            f"{self._label} {shown(value)} is already used on line {first}; "
            "identifiers are never reused",
        )


@dataclass
class Ledger:
    """What checking one file needs to know about the rest of the ledger.

    Attributes:
        base: The data base, for resolving the path of a run's report.
        findings: Where errors and warnings are collected.
        run_ids: The run identifiers ``runs.jsonl`` holds, or ``None`` when
            that file could not be read (nothing may then be blamed for a
            reference it cannot resolve).
        run_modes: Mode per run identifier, for the snapshot a degraded run
            could not take.
        feature_ids: The identifiers ``features.jsonl`` holds, or ``None``.
        family_ids: The identifiers ``families.jsonl`` holds, or ``None``.
    """

    base: Path
    findings: Findings
    run_ids: frozenset[str] | None = None
    run_modes: dict[str, str] = field(default_factory=dict)
    feature_ids: frozenset[str] | None = None
    family_ids: frozenset[str] | None = None


def target_report(name: str, findings: Findings, counts: Mapping[str, int]) -> dict[str, Any]:
    """Assemble one target's result, with the findings ordered file by file."""
    empty = dict.fromkeys(("runs", "features", "queries", "families", "watch"), 0)
    return {
        "target": name,
        "ok": not findings.errors,
        "errors": sorted(findings.errors, key=_finding_order),
        "warnings": sorted(findings.warnings, key=_finding_order),
        "counts": {**empty, **counts},
    }


def _finding_order(finding: Mapping[str, Any]) -> tuple[int, int]:
    """Sort key putting findings in file order, then by line (file-wide ones first)."""
    try:
        rank = FILE_ORDER.index(finding["file"])
    except ValueError:
        rank = len(FILE_ORDER)
    line = finding["line"]
    return rank, -1 if line is None else line
