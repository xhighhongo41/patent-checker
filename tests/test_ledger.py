"""Tests for the ledger reader (patent_checker/ledger.py).

Every test builds its own ledger under ``tmp_path``: the reader must never
look at a real project directory, and must never write anything at all --
the ledger belongs to the agent that runs the Skill.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from patent_checker.ledger import check, status

_TARGET = "sample-app"
_FIRST_RUN = "20260301-0930"
_SECOND_RUN = "20260901-1015"

# A day before every ``next_check`` of the ledger built below, so that a test
# that does not care about due entries never depends on the real clock.
_TODAY = date(2026, 9, 17)


# --- Building a ledger ------------------------------------------------------


def _runs(target: str) -> list[dict[str, Any]]:
    """Return the two runs of the built ledger, oldest first."""
    return [
        {
            "run_id": _FIRST_RUN,
            "type": "baseline",
            "started_at": "2026-03-01T09:30:00+00:00",
            "finished_at": "2026-03-01T14:10:00+00:00",
            "stage": "development",
            "mode": "standard",
            "server_version": "1.1.0",
            "searched_through": "2026-03-01",
            "target_version": "0.3.0",
            "target_commit": "0a1b2c3",
            "report": f"reports/report-{target}-{_FIRST_RUN}.md",
            "artifacts": f"exploration-{target}-20260301",
        },
        {
            "run_id": _SECOND_RUN,
            "type": "follow-up",
            "started_at": "2026-09-01T10:15:00+00:00",
            "finished_at": "2026-09-01T12:40:00+00:00",
            "stage": "pre-release",
            "mode": "standard",
            "server_version": "1.1.0",
            "searched_through": "2026-09-01",
            "target_version": "0.4.0",
            "target_commit": "9f8e7d6",
            "report": f"reports/report-{target}-{_SECOND_RUN}.md",
            "previous_run": _FIRST_RUN,
        },
    ]


def _features() -> list[dict[str, Any]]:
    """Return the two features of the built ledger (F2 is referenced by nothing)."""
    return [
        {
            "id": "F1",
            "title": "Rebuilds the central directory of a damaged archive",
            "basis": "code",
            "priority": "high",
            "status": "active",
            "since_run": _FIRST_RUN,
            "changed_run": _SECOND_RUN,
            "pointers": ["src/repair/archive.py:120"],
        },
        {
            "id": "F2",
            "title": "Streams one archive member without unpacking the archive",
            "basis": "design",
            "priority": "low",
            "status": "retired",
            "since_run": _FIRST_RUN,
        },
    ]


def _queries() -> list[dict[str, Any]]:
    """Return the two queries of the built ledger (Q2 is referenced by nothing)."""
    return [
        {
            "id": "Q1",
            "cql": "ta=(archive or container) and ta=(repair* or recover*)",
            "status": "adopted",
            "features": ["F1"],
            "runs": [
                {"run_id": _FIRST_RUN, "total": 212, "window": "all"},
                {"run_id": _SECOND_RUN, "total": 9, "window": "20260130-20260901"},
            ],
        },
        {
            "id": "Q2",
            "cql": "ta=(archive) and ta=(stream*)",
            "status": "rejected",
            "features": [],
            "runs": [{"run_id": _FIRST_RUN, "total": 41822, "window": "all"}],
            "reason": "too broad to read: 41822 hits",
        },
    ]


def _families() -> list[dict[str, Any]]:
    """Return the two screened families of the built ledger."""
    return [
        {
            "family_id": "99887766",
            "pubs": ["US.2099123456.A1", "US.99999999.B2"],
            "stage1": "A",
            "stage2": "close-read",
            "judged_run": _FIRST_RUN,
            "features": "all",
        },
        {
            "family_id": "99887767",
            "pubs": ["EP.9999999.A1"],
            "stage1": "B",
            "stage2": "boundary",
            "judged_run": _SECOND_RUN,
            "features": ["F1"],
        },
    ]


def _snapshot(pub: str, family_id: str, members: Sequence[str]) -> dict[str, Any]:
    """Return a stored ``watch_check`` snapshot, shaped as the tool returns it."""
    return {
        "pub": pub,
        "snapshot_format": 1,
        "legal": {
            "fetched_at": "2026-09-01T10:40:12+00:00",
            "event_count": 2,
            "events": [["20210615", "STCF", "5f0c2a1e"], ["20251104", "MAFP", "b81d9c07"]],
        },
        "family": {
            "fetched_at": "2026-09-01T10:40:16+00:00",
            "family_id": family_id,
            "members": list(members),
        },
    }


def _watch() -> list[dict[str, Any]]:
    """Return the two monitored publications of the built ledger, both open."""
    return [
        {
            "pub": "US.99999999.B2",
            "family_id": "99887766",
            "reason": "in-force",
            "added_run": _FIRST_RUN,
            "checked_run": _SECOND_RUN,
            "features": ["F1"],
            "observation": {
                "value": "lacks",
                "decisive_element": "claim 1: checksum comparison before rebuild",
                "run_id": _FIRST_RUN,
            },
            "status_reading": {
                "summary": "granted; maintenance fee paid",
                "decisive_event": {"code": "MAFP", "date": "2025-11-04"},
                "as_of": "2026-09-01T10:40:12+00:00",
                "run_id": _SECOND_RUN,
            },
            "claims_read": {"pub": "US.99999999.B2", "source": "gp", "run_id": _FIRST_RUN},
            "history": [{"run_id": _SECOND_RUN, "summary": "maintenance fee recorded"}],
            "next_check": "2027-03-01",
            "snapshot": _snapshot(
                "US.99999999.B2", "99887766", ["US.2099123456.A1", "US.99999999.B2"]
            ),
        },
        {
            "pub": "EP.9999999.A1",
            "family_id": "99887767",
            "reason": "provisional",
            "added_run": _SECOND_RUN,
            "checked_run": _SECOND_RUN,
            "features": ["F1"],
            "next_check": "2026-10-01",
            "snapshot": _snapshot("EP.9999999.A1", "99887767", ["EP.9999999.A1"]),
        },
    ]


def _write_json(path: Path, record: Mapping[str, Any]) -> None:
    """Write one JSON object as a whole file."""
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    """Write *records* as one JSON object per line."""
    body = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    path.write_text(body, encoding="utf-8")


def _build_ledger(tmp_path: Path, target: str = _TARGET) -> Path:
    """Write a minimal but complete and valid ledger; return the data base.

    Two runs, two features, two queries, two screened families, two
    monitored publications, the translation table and the reports the runs
    name -- the smallest ledger that exercises every rule the checker knows.
    """
    base = tmp_path / ".patent-checker"
    directory = base / "ledger" / target
    directory.mkdir(parents=True, exist_ok=True)

    _write_json(
        directory / "ledger.json",
        {"format": 1, "target": target, "created_at": "2026-03-01T09:30:00+00:00"},
    )
    _write_jsonl(directory / "runs.jsonl", _runs(target))
    _write_jsonl(directory / "features.jsonl", _features())
    _write_jsonl(directory / "queries.jsonl", _queries())
    _write_jsonl(directory / "families.jsonl", _families())
    _write_jsonl(directory / "watch.jsonl", _watch())
    (directory / "translation.md").write_text(
        "| plain wording | patent vocabulary | added |\n"
        "| --- | --- | --- |\n"
        f"| damaged archive | corrupted data container | {_FIRST_RUN} |\n",
        encoding="utf-8",
    )

    (base / "reports").mkdir(parents=True, exist_ok=True)
    for run in _runs(target):
        (base / str(run["report"])).write_text(f"# report {run['run_id']}\n", encoding="utf-8")
    (base / f"exploration-{target}-20260301").mkdir(exist_ok=True)
    return base


# --- Breaking one thing in a built ledger -----------------------------------

_Mutator = Callable[[Path], None]


def _records(path: Path) -> list[dict[str, Any]]:
    """Return the records of a JSON Lines file."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _edit(filename: str, index: int, change: Callable[[dict[str, Any]], Any]) -> _Mutator:
    """Return a mutator applying *change* to one record of a JSON Lines file."""

    def mutate(directory: Path) -> None:
        path = directory / filename
        records = _records(path)
        change(records[index])
        _write_jsonl(path, records)

    return mutate


def _edit_manifest(change: Callable[[dict[str, Any]], Any]) -> _Mutator:
    """Return a mutator applying *change* to ``ledger.json``."""

    def mutate(directory: Path) -> None:
        path = directory / "ledger.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        change(record)
        _write_json(path, record)

    return mutate


def _remove(filename: str) -> _Mutator:
    """Return a mutator deleting one file of the ledger."""

    def mutate(directory: Path) -> None:
        (directory / filename).unlink()

    return mutate


def _raw_line(filename: str, index: int, text: str) -> _Mutator:
    """Return a mutator replacing one line of a JSON Lines file verbatim."""

    def mutate(directory: Path) -> None:
        path = directory / filename
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[index] = text
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return mutate


def _raw_text(filename: str, text: str) -> _Mutator:
    """Return a mutator replacing a whole file with *text*."""

    def mutate(directory: Path) -> None:
        (directory / filename).write_text(text, encoding="utf-8")

    return mutate


def _raw_bytes(filename: str, data: bytes) -> _Mutator:
    """Return a mutator replacing a whole file with *data*."""

    def mutate(directory: Path) -> None:
        (directory / filename).write_bytes(data)

    return mutate


def _add_bom(filename: str) -> _Mutator:
    """Return a mutator putting a UTF-8 byte-order mark in front of a file."""

    def mutate(directory: Path) -> None:
        path = directory / filename
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

    return mutate


def _finding(
    findings: Sequence[Mapping[str, Any]], rule: str, file: str, line: int | None
) -> Mapping[str, Any] | None:
    """Return the first finding matching *rule*, *file* and *line*, or None."""
    for finding in findings:
        if finding["rule"] == rule and finding["file"] == file and finding["line"] == line:
            return finding
    return None


# --- check: a correct ledger ------------------------------------------------


def test_check_accepts_a_correct_ledger(tmp_path: Path) -> None:
    """A ledger written as the format document prescribes reports nothing at all."""
    base = _build_ledger(tmp_path)

    result = check(base, target=_TARGET)

    assert result == {
        "ok": True,
        "path": str(base / "ledger"),
        "targets": [
            {
                "target": _TARGET,
                "ok": True,
                "errors": [],
                "warnings": [],
                "counts": {"runs": 2, "features": 2, "queries": 2, "families": 2, "watch": 2},
            }
        ],
    }


def test_check_ignores_unknown_keys_and_unknown_files(tmp_path: Path) -> None:
    """Notes of the agent's own may sit next to the required keys and files."""
    base = _build_ledger(tmp_path)
    directory = base / "ledger" / _TARGET
    for filename in ("runs.jsonl", "features.jsonl", "queries.jsonl", "families.jsonl"):
        _edit(filename, 0, lambda record: record.update({"my_note": {"why": "kept"}}))(directory)
    _edit("watch.jsonl", 0, lambda record: record.update({"my_note": ["kept"]}))(directory)
    _edit_manifest(lambda record: record.update({"notes": "written by hand"}))(directory)
    (directory / "notes.md").write_text("# my notes\n", encoding="utf-8")
    (directory / "hits.jsonl").write_text("not json at all\n", encoding="utf-8")
    (directory / "scratch").mkdir()

    result = check(base, target=_TARGET)

    assert result["targets"][0]["errors"] == []
    assert result["targets"][0]["warnings"] == []
    assert result["ok"] is True


def test_check_accepts_crlf_line_endings(tmp_path: Path) -> None:
    """A ledger written on Windows reads exactly like one written on Unix."""
    base = _build_ledger(tmp_path)
    directory = base / "ledger" / _TARGET
    for filename in ("runs.jsonl", "features.jsonl", "queries.jsonl", "families.jsonl"):
        path = directory / filename
        path.write_bytes(path.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))

    result = check(base, target=_TARGET)

    assert result["ok"] is True
    assert result["targets"][0]["errors"] == []


def test_check_accepts_a_null_snapshot_from_a_degraded_run(tmp_path: Path) -> None:
    """A run without a legal-status source leaves no snapshot, and that is not an error."""
    base = _build_ledger(tmp_path)
    directory = base / "ledger" / _TARGET
    _edit("runs.jsonl", 1, lambda record: record.update({"mode": "degraded"}))(directory)
    _edit("watch.jsonl", 0, lambda record: record.update({"snapshot": None}))(directory)

    result = check(base, target=_TARGET)

    assert result["targets"][0]["errors"] == []
    assert result["targets"][0]["warnings"] == []


# --- check: every error rule ------------------------------------------------

_ERROR_CASES = [
    pytest.param(
        _remove("features.jsonl"), "missing-file", "features.jsonl", None, id="missing-state-file"
    ),
    pytest.param(
        _remove("ledger.json"), "missing-file", "ledger.json", None, id="missing-manifest"
    ),
    pytest.param(_add_bom("runs.jsonl"), "encoding", "runs.jsonl", None, id="encoding-bom"),
    pytest.param(
        _raw_bytes("queries.jsonl", b'{"id": "Q1"}\n\xff\xfe\n'),
        "encoding",
        "queries.jsonl",
        None,
        id="encoding-not-utf8",
    ),
    pytest.param(
        _raw_line("runs.jsonl", 1, "{not json"), "json", "runs.jsonl", 2, id="json-broken-line"
    ),
    pytest.param(
        _raw_line("watch.jsonl", 0, "[]"), "json", "watch.jsonl", 1, id="json-not-an-object"
    ),
    pytest.param(_raw_text("ledger.json", "{"), "json", "ledger.json", None, id="json-manifest"),
    pytest.param(
        _edit_manifest(lambda record: record.update({"format": 2})),
        "manifest",
        "ledger.json",
        None,
        id="manifest-format",
    ),
    pytest.param(
        _edit_manifest(lambda record: record.update({"target": "other-app"})),
        "manifest",
        "ledger.json",
        None,
        id="manifest-target",
    ),
    pytest.param(
        _edit_manifest(lambda record: record.update({"created_at": "March 2026"})),
        "manifest",
        "ledger.json",
        None,
        id="manifest-created-at",
    ),
    pytest.param(
        _edit("runs.jsonl", 0, lambda record: record.pop("type")),
        "required-key",
        "runs.jsonl",
        1,
        id="required-key-missing",
    ),
    pytest.param(
        _edit("features.jsonl", 0, lambda record: record.update({"priority": None})),
        "required-key",
        "features.jsonl",
        1,
        id="required-key-null",
    ),
    pytest.param(
        _edit("watch.jsonl", 1, lambda record: record.pop("next_check")),
        "required-key",
        "watch.jsonl",
        2,
        id="required-key-next-check",
    ),
    pytest.param(
        _edit("runs.jsonl", 0, lambda record: record.update({"type": "exploration"})),
        "enum",
        "runs.jsonl",
        1,
        id="enum-run-type",
    ),
    pytest.param(
        _edit(
            "watch.jsonl", 0, lambda record: record["observation"].update({"value": "infringes"})
        ),
        "enum",
        "watch.jsonl",
        1,
        id="enum-observation-value",
    ),
    pytest.param(
        _edit("families.jsonl", 0, lambda record: record.update({"features": 3})),
        "enum",
        "families.jsonl",
        1,
        id="enum-family-features",
    ),
    pytest.param(
        _edit("runs.jsonl", 1, lambda record: record.update({"run_id": "2026-09-01 10:15"})),
        "id-format",
        "runs.jsonl",
        2,
        id="id-format-run",
    ),
    pytest.param(
        _edit("features.jsonl", 1, lambda record: record.update({"id": "FX"})),
        "id-format",
        "features.jsonl",
        2,
        id="id-format-feature",
    ),
    pytest.param(
        _edit("queries.jsonl", 1, lambda record: record.update({"id": "query-2"})),
        "id-format",
        "queries.jsonl",
        2,
        id="id-format-query",
    ),
    pytest.param(
        _edit("features.jsonl", 1, lambda record: record.update({"id": "F1"})),
        "unique-id",
        "features.jsonl",
        2,
        id="unique-id-feature",
    ),
    pytest.param(
        _edit("families.jsonl", 1, lambda record: record.update({"family_id": "99887766"})),
        "unique-id",
        "families.jsonl",
        2,
        id="unique-id-family",
    ),
    pytest.param(
        _edit("watch.jsonl", 0, lambda record: record.update({"next_check": "2027/03/01"})),
        "date",
        "watch.jsonl",
        1,
        id="date-next-check",
    ),
    pytest.param(
        _edit("runs.jsonl", 0, lambda record: record.update({"searched_through": "20260301"})),
        "date",
        "runs.jsonl",
        1,
        id="date-searched-through",
    ),
    pytest.param(
        _edit(
            "watch.jsonl",
            0,
            lambda record: record["status_reading"]["decisive_event"].update(
                {"date": "2025-13-04"}
            ),
        ),
        "date",
        "watch.jsonl",
        1,
        id="date-decisive-event",
    ),
    pytest.param(
        _edit("runs.jsonl", 0, lambda record: record.update({"started_at": "yesterday morning"})),
        "timestamp",
        "runs.jsonl",
        1,
        id="timestamp-started-at",
    ),
    pytest.param(
        _edit("runs.jsonl", 1, lambda record: record.update({"finished_at": "later"})),
        "timestamp",
        "runs.jsonl",
        2,
        id="timestamp-finished-at",
    ),
    pytest.param(
        _edit("watch.jsonl", 0, lambda record: record["status_reading"].update({"as_of": "n/a"})),
        "timestamp",
        "watch.jsonl",
        1,
        id="timestamp-as-of",
    ),
    pytest.param(
        _edit("features.jsonl", 0, lambda record: record.update({"since_run": "20250101-0000"})),
        "run-ref",
        "features.jsonl",
        1,
        id="run-ref-since-run",
    ),
    pytest.param(
        _edit("runs.jsonl", 1, lambda record: record.update({"previous_run": _SECOND_RUN})),
        "run-ref",
        "runs.jsonl",
        2,
        id="run-ref-self",
    ),
    pytest.param(
        _edit(
            "queries.jsonl", 0, lambda record: record["runs"][0].update({"run_id": "20200101-0"})
        ),
        "run-ref",
        "queries.jsonl",
        1,
        id="run-ref-query-run",
    ),
    pytest.param(
        _edit("families.jsonl", 1, lambda record: record.update({"judged_run": "20200101-0000"})),
        "run-ref",
        "families.jsonl",
        2,
        id="run-ref-judged-run",
    ),
    pytest.param(
        _edit("watch.jsonl", 0, lambda record: record.update({"checked_run": "20200101-0000"})),
        "run-ref",
        "watch.jsonl",
        1,
        id="run-ref-checked-run",
    ),
    pytest.param(
        _edit("queries.jsonl", 0, lambda record: record.update({"features": ["F9"]})),
        "feature-ref",
        "queries.jsonl",
        1,
        id="feature-ref-query",
    ),
    pytest.param(
        _edit("families.jsonl", 1, lambda record: record.update({"features": ["F9"]})),
        "feature-ref",
        "families.jsonl",
        2,
        id="feature-ref-family",
    ),
    pytest.param(
        _edit("watch.jsonl", 0, lambda record: record.update({"features": ["F9"]})),
        "feature-ref",
        "watch.jsonl",
        1,
        id="feature-ref-watch",
    ),
    pytest.param(
        _edit("families.jsonl", 0, lambda record: record["pubs"].__setitem__(1, "US99999999B2")),
        "pub-spelling",
        "families.jsonl",
        1,
        id="pub-spelling-family",
    ),
    pytest.param(
        _edit("watch.jsonl", 0, lambda record: record["claims_read"].update({"pub": "not a pub"})),
        "pub-spelling",
        "watch.jsonl",
        1,
        id="pub-spelling-claims-read",
    ),
    pytest.param(
        _edit(
            "watch.jsonl",
            1,
            lambda record: record.update(
                {"pub": "EP.9999999", "snapshot": {**record["snapshot"], "pub": "EP.9999999"}}
            ),
        ),
        "pub-kind",
        "watch.jsonl",
        2,
        id="pub-kind-watch",
    ),
    pytest.param(
        _edit("runs.jsonl", 0, lambda record: record.update({"searched_through": None})),
        "searched-through",
        "runs.jsonl",
        1,
        id="searched-through-null",
    ),
    pytest.param(
        _edit("runs.jsonl", 1, lambda record: record.update({"report": "reports/not-written.md"})),
        "report-missing",
        "runs.jsonl",
        2,
        id="report-missing",
    ),
    pytest.param(
        _edit("queries.jsonl", 0, lambda record: record["runs"][0].update({"window": "2026"})),
        "window",
        "queries.jsonl",
        1,
        id="window-spelling",
    ),
    pytest.param(
        _edit("queries.jsonl", 0, lambda record: record["runs"][0].update({"total": -1})),
        "window",
        "queries.jsonl",
        1,
        id="window-total",
    ),
    pytest.param(
        _edit("watch.jsonl", 0, lambda record: record["snapshot"].update({"snapshot_format": 2})),
        "snapshot",
        "watch.jsonl",
        1,
        id="snapshot-format",
    ),
    pytest.param(
        _edit("watch.jsonl", 0, lambda record: record.update({"snapshot": None})),
        "snapshot",
        "watch.jsonl",
        1,
        id="snapshot-null",
    ),
    pytest.param(
        _edit("watch.jsonl", 0, lambda record: record["snapshot"].update({"pub": "EP.9999999.A1"})),
        "snapshot",
        "watch.jsonl",
        1,
        id="snapshot-pub",
    ),
    pytest.param(
        _edit("watch.jsonl", 0, lambda record: record["snapshot"].update({"legal": "none"})),
        "snapshot",
        "watch.jsonl",
        1,
        id="snapshot-legal",
    ),
]


@pytest.mark.parametrize(("break_it", "rule", "file", "line"), _ERROR_CASES)
def test_check_reports_one_broken_thing(
    tmp_path: Path, break_it: _Mutator, rule: str, file: str, line: int | None
) -> None:
    """Breaking the ledger in one place names that rule, that file and that line."""
    base = _build_ledger(tmp_path)

    break_it(base / "ledger" / _TARGET)
    result = check(base, target=_TARGET)

    report = result["targets"][0]
    assert result["ok"] is False
    assert report["ok"] is False
    finding = _finding(report["errors"], rule, file, line)
    assert finding is not None, report["errors"]
    assert finding["message"]


# --- check: every warning rule ----------------------------------------------

_WARNING_CASES = [
    pytest.param(
        _remove("translation.md"),
        "translation-missing",
        "translation.md",
        None,
        id="translation-missing",
    ),
    pytest.param(
        _edit("queries.jsonl", 1, lambda record: record.pop("reason")),
        "reason-missing",
        "queries.jsonl",
        2,
        id="reason-missing",
    ),
    pytest.param(
        _edit(
            "watch.jsonl",
            0,
            lambda record: record.update({"snapshot": None, "snapshot_error": "OPS returned 503"}),
        ),
        "snapshot-error",
        "watch.jsonl",
        1,
        id="snapshot-error",
    ),
    pytest.param(
        _edit(
            "queries.jsonl",
            0,
            lambda record: record.update({"cql": record["cql"] + ' and pd within "2026"'}),
        ),
        "cql-date-clause",
        "queries.jsonl",
        1,
        id="cql-date-clause",
    ),
    pytest.param(
        _edit(
            "runs.jsonl", 1, lambda record: record.update({"started_at": "2026-01-01T00:00:00Z"})
        ),
        "run-order",
        "runs.jsonl",
        2,
        id="run-order",
    ),
    pytest.param(
        # A number the package cannot parse (here: an inner letter block
        # followed by too few digits for any office). Copied from a search
        # hit into a screened family it is nothing the agent could fix.
        _edit("families.jsonl", 0, lambda record: record["pubs"].append("IN.985DE201.A")),
        "pub-unparsed",
        "families.jsonl",
        1,
        id="pub-unparsed-family",
    ),
    pytest.param(
        _edit("watch.jsonl", 0, lambda record: record.update({"family_id": "12345678"})),
        "watch-family-unknown",
        "watch.jsonl",
        1,
        id="watch-family-unknown",
    ),
]


@pytest.mark.parametrize(("break_it", "rule", "file", "line"), _WARNING_CASES)
def test_check_warns_without_failing(
    tmp_path: Path, break_it: _Mutator, rule: str, file: str, line: int | None
) -> None:
    """A warning names its rule, file and line, and leaves the ledger acceptable."""
    base = _build_ledger(tmp_path)

    break_it(base / "ledger" / _TARGET)
    result = check(base, target=_TARGET)

    report = result["targets"][0]
    assert report["errors"] == []
    assert result["ok"] is True
    finding = _finding(report["warnings"], rule, file, line)
    assert finding is not None, report["warnings"]
    assert finding["message"]


def test_check_warns_about_a_directory_name_that_is_not_a_target_name(tmp_path: Path) -> None:
    """A target name is lower-case letters, digits, "-" and "_", like the report file name."""
    base = _build_ledger(tmp_path)
    directory = base / "ledger" / _TARGET
    _edit_manifest(lambda record: record.update({"target": "Sample App"}))(directory)
    directory.rename(directory.parent / "Sample App")

    result = check(base, target="Sample App")

    report = result["targets"][0]
    assert report["errors"] == []
    assert _finding(report["warnings"], "target-name", "ledger.json", None) is not None


# --- check: messages, cascades and scope ------------------------------------


def test_check_gives_the_docdb_spelling_of_a_wrongly_written_publication(tmp_path: Path) -> None:
    """A wrong spelling is reported together with the spelling to write instead."""
    base = _build_ledger(tmp_path)
    directory = base / "ledger" / _TARGET
    _edit("families.jsonl", 0, lambda record: record["pubs"].__setitem__(1, "US99999999B2"))(
        directory
    )

    result = check(base, target=_TARGET)

    finding = _finding(result["targets"][0]["errors"], "pub-spelling", "families.jsonl", 1)
    assert finding is not None
    assert "US99999999B2" in finding["message"]
    assert "US.99999999.B2" in finding["message"]


def test_check_compares_watched_publications_in_docdb_spelling(tmp_path: Path) -> None:
    """The same publication written two ways is still the same watch entry."""
    base = _build_ledger(tmp_path)
    directory = base / "ledger" / _TARGET
    _edit("watch.jsonl", 1, lambda record: record.update({"pub": "US99999999B2"}))(directory)

    result = check(base, target=_TARGET)

    assert _finding(result["targets"][0]["errors"], "unique-id", "watch.jsonl", 2) is not None


def test_check_reports_every_error_of_one_line(tmp_path: Path) -> None:
    """One line with three faults is reported three times, not once."""
    base = _build_ledger(tmp_path)
    directory = base / "ledger" / _TARGET
    _edit(
        "features.jsonl",
        1,
        lambda record: record.update(
            {"id": "feature-2", "priority": "urgent", "since_run": "20200101-0000"}
        ),
    )(directory)

    errors = check(base, target=_TARGET)["targets"][0]["errors"]

    rules = {error["rule"] for error in errors if error["line"] == 2}
    assert rules == {"id-format", "enum", "run-ref"}


def test_check_stops_reading_a_line_that_is_not_json(tmp_path: Path) -> None:
    """A broken line is reported once; nothing else can be said about it."""
    base = _build_ledger(tmp_path)
    directory = base / "ledger" / _TARGET
    _raw_line("watch.jsonl", 0, '{"pub": "US.99999999.B2"')(directory)

    report = check(base, target=_TARGET)["targets"][0]

    on_that_line = [error for error in report["errors"] if error["file"] == "watch.jsonl"]
    assert [error["rule"] for error in on_that_line] == ["json"]
    assert report["counts"]["watch"] == 1


def test_check_does_not_blame_every_line_for_a_missing_runs_file(tmp_path: Path) -> None:
    """With no runs.jsonl there is nothing to resolve run references against."""
    base = _build_ledger(tmp_path)

    _remove("runs.jsonl")(base / "ledger" / _TARGET)
    report = check(base, target=_TARGET)["targets"][0]

    assert _finding(report["errors"], "missing-file", "runs.jsonl", None) is not None
    assert [error for error in report["errors"] if error["rule"] == "run-ref"] == []
    assert report["counts"]["runs"] == 0


def test_check_covers_every_ledger_when_no_target_is_given(tmp_path: Path) -> None:
    """Without a target, every ledger of the project is checked, in name order."""
    base = _build_ledger(tmp_path, target="second-app")
    _build_ledger(tmp_path, target=_TARGET)
    _edit("runs.jsonl", 0, lambda record: record.update({"stage": "shipping"}))(
        base / "ledger" / "second-app"
    )

    result = check(base)

    assert [report["target"] for report in result["targets"]] == ["sample-app", "second-app"]
    assert [report["ok"] for report in result["targets"]] == [True, False]
    assert result["ok"] is False


@pytest.mark.parametrize("target", [None, "ghost-app"], ids=["no-target", "named-target"])
def test_check_reports_a_missing_ledger(tmp_path: Path, target: str | None) -> None:
    """With nothing to read, the answer is one finding, not an exception."""
    base = tmp_path / ".patent-checker"

    result = check(base, target=target)

    assert result["ok"] is False
    assert result["path"] == str(base / "ledger")
    report = result["targets"][0]
    assert report["target"] == (target or "")
    assert report["ok"] is False
    assert _finding(report["errors"], "missing-ledger", "ledger.json", None) is not None
    assert report["counts"] == {"runs": 0, "features": 0, "queries": 0, "families": 0, "watch": 0}


def test_check_reports_a_missing_ledger_for_an_unknown_target(tmp_path: Path) -> None:
    """A target name nobody explored yet is missing, whatever else the project holds."""
    base = _build_ledger(tmp_path)

    result = check(base, target="other-app")

    assert result["targets"][0]["target"] == "other-app"
    assert _finding(result["targets"][0]["errors"], "missing-ledger", "ledger.json", None)


def test_check_resolves_the_data_base_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting the data base falls back to the configured one, like every other command."""
    base = _build_ledger(tmp_path)
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(base))

    result = check()

    assert result["ok"] is True
    assert [report["target"] for report in result["targets"]] == [_TARGET]


# --- status -----------------------------------------------------------------


def test_status_on_a_data_base_that_holds_nothing(tmp_path: Path) -> None:
    """No ledger and no earlier artifacts: the answer is empty, not an error."""
    base = tmp_path / ".patent-checker"

    result = status(base, today=_TODAY)

    assert result == {
        "exists": False,
        "path": str(base / "ledger"),
        "targets": [],
        "ledgers": [],
        "legacy": {"reports": [], "explorations": []},
    }


def test_status_lists_artifacts_from_before_the_ledger(tmp_path: Path) -> None:
    """Reports and working directories of an earlier exploration are what "legacy" is for."""
    base = tmp_path / ".patent-checker"
    (base / "reports").mkdir(parents=True)
    (base / "reports" / f"report-{_TARGET}-20251101-0900.md").write_text("#", encoding="utf-8")
    (base / "reports" / "notes.md").write_text("#", encoding="utf-8")
    (base / f"exploration-{_TARGET}-20251101").mkdir()
    (base / "consent.json").write_text("{}", encoding="utf-8")

    result = status(base, today=_TODAY)

    assert result["exists"] is False
    assert result["legacy"] == {
        "reports": [f"report-{_TARGET}-20251101-0900.md"],
        "explorations": [f"exploration-{_TARGET}-20251101"],
    }


def test_status_summarizes_a_complete_ledger(tmp_path: Path) -> None:
    """Every count the Skill reads before choosing the kind of run comes from one call."""
    base = _build_ledger(tmp_path)

    result = status(base, today=_TODAY)

    assert result["exists"] is True
    assert result["targets"] == [_TARGET]
    entry = result["ledgers"][0]
    assert entry["target"] == _TARGET
    assert entry["format"] == 1
    assert entry["runs"] == 2
    assert entry["features"] == {"active": 1, "retired": 1}
    assert entry["queries"] == {"adopted": 1, "rejected": 1, "dropped": 0}
    assert entry["families"] == 2
    assert entry["watch"] == {
        "open": 2,
        "closed": 0,
        "due": 0,
        "provisional": 1,
        "next_check": "2026-10-01",
    }
    assert entry["problems"] == []
    assert entry["last_run"] == {
        "run_id": _SECOND_RUN,
        "type": "follow-up",
        "stage": "pre-release",
        "mode": "standard",
        "started_at": "2026-09-01T10:15:00+00:00",
        "target_version": "0.4.0",
        "target_commit": "9f8e7d6",
        "searched_through": "2026-09-01",
        "report": f"reports/report-{_TARGET}-{_SECOND_RUN}.md",
    }
    assert result["legacy"]["reports"] == [
        f"report-{_TARGET}-{_FIRST_RUN}.md",
        f"report-{_TARGET}-{_SECOND_RUN}.md",
    ]
    assert result["legacy"]["explorations"] == [f"exploration-{_TARGET}-20260301"]


@pytest.mark.parametrize(
    ("today", "due"),
    [
        (date(2026, 9, 17), 0),
        (date(2026, 10, 1), 1),
        (date(2027, 3, 1), 2),
    ],
    ids=["none-due", "one-due", "both-due"],
)
def test_status_counts_what_is_due_on_the_given_day(tmp_path: Path, today: date, due: int) -> None:
    """ "Due" is "next_check on or before today", and today is the caller's to decide."""
    base = _build_ledger(tmp_path)

    result = status(base, target=_TARGET, today=today)

    assert result["ledgers"][0]["watch"]["due"] == due


def test_status_counts_a_closed_watch_entry_apart(tmp_path: Path) -> None:
    """A closed line stays in the ledger and stops counting as open, due or provisional."""
    base = _build_ledger(tmp_path)
    _edit(
        "watch.jsonl",
        1,
        lambda record: (
            record.pop("next_check"),
            record.update({"closed_run": _SECOND_RUN, "closed_reason": "application withdrawn"}),
        ),
    )(base / "ledger" / _TARGET)

    watch = status(base, target=_TARGET, today=date(2027, 6, 1))["ledgers"][0]["watch"]

    assert watch == {
        "open": 1,
        "closed": 1,
        "due": 1,
        "provisional": 0,
        "next_check": "2027-03-01",
    }


def test_status_reports_one_target_when_asked_for_one(tmp_path: Path) -> None:
    """Both the ledger list and the legacy artifacts are narrowed to the named target."""
    base = _build_ledger(tmp_path, target="second-app")
    _build_ledger(tmp_path, target=_TARGET)

    result = status(base, target="second-app", today=_TODAY)

    assert result["exists"] is True
    assert result["targets"] == ["sample-app", "second-app"]
    assert [entry["target"] for entry in result["ledgers"]] == ["second-app"]
    assert result["legacy"]["reports"] == [
        f"report-second-app-{_FIRST_RUN}.md",
        f"report-second-app-{_SECOND_RUN}.md",
    ]
    assert result["legacy"]["explorations"] == ["exploration-second-app-20260301"]


def test_status_on_a_target_that_has_no_ledger(tmp_path: Path) -> None:
    """Asking about an unexplored target says so without hiding the other ledgers."""
    base = _build_ledger(tmp_path)

    result = status(base, target="other-app", today=_TODAY)

    assert result["exists"] is False
    assert result["ledgers"] == []
    assert result["targets"] == [_TARGET]
    assert result["legacy"] == {"reports": [], "explorations": []}


def test_status_survives_a_broken_line(tmp_path: Path) -> None:
    """A summary counts what it can read and says what it could not."""
    base = _build_ledger(tmp_path)
    _raw_line("runs.jsonl", 1, "{oops")(base / "ledger" / _TARGET)

    entry = status(base, target=_TARGET, today=_TODAY)["ledgers"][0]

    assert entry["runs"] == 1
    assert entry["last_run"]["run_id"] == _FIRST_RUN
    assert any("runs.jsonl" in problem and "2" in problem for problem in entry["problems"])


def test_status_survives_a_manifest_and_a_file_it_cannot_read(tmp_path: Path) -> None:
    """A missing file and an unreadable manifest are problems, not exceptions."""
    base = _build_ledger(tmp_path)
    directory = base / "ledger" / _TARGET
    _raw_text("ledger.json", "{")(directory)
    _remove("queries.jsonl")(directory)

    entry = status(base, target=_TARGET, today=_TODAY)["ledgers"][0]

    assert entry["format"] is None
    assert entry["queries"] == {"adopted": 0, "rejected": 0, "dropped": 0}
    assert any("ledger.json" in problem for problem in entry["problems"])
    assert any("queries.jsonl" in problem for problem in entry["problems"])


def test_status_fills_an_absent_last_run_key_with_none(tmp_path: Path) -> None:
    """A run that says nothing about the target version still reports the same keys."""
    base = _build_ledger(tmp_path)
    _edit(
        "runs.jsonl",
        1,
        lambda record: (record.pop("target_version"), record.pop("target_commit")),
    )(base / "ledger" / _TARGET)

    last_run = status(base, target=_TARGET, today=_TODAY)["ledgers"][0]["last_run"]

    assert last_run["target_version"] is None
    assert last_run["target_commit"] is None
    assert last_run["run_id"] == _SECOND_RUN


def test_status_resolves_the_data_base_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting the data base falls back to the configured one, like every other command."""
    base = _build_ledger(tmp_path)
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(base))

    result = status(today=_TODAY)

    assert result["exists"] is True
    assert result["targets"] == [_TARGET]


# --- Reading only -----------------------------------------------------------


def _tree(root: Path) -> dict[str, tuple[int, bytes] | None]:
    """Return every path under *root* with its content and modification time."""
    snapshot: dict[str, tuple[int, bytes] | None] = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        snapshot[key] = None if path.is_dir() else (path.stat().st_mtime_ns, path.read_bytes())
    return snapshot


def test_reading_a_ledger_changes_nothing_on_disk(tmp_path: Path) -> None:
    """The package reads the ledger; the agent writes it. No call may create a file."""
    base = _build_ledger(tmp_path, target="second-app")
    _build_ledger(tmp_path, target=_TARGET)
    before = _tree(tmp_path)

    status(base, today=_TODAY)
    status(base, target=_TARGET, today=_TODAY)
    status(base, target="other-app", today=_TODAY)
    check(base)
    check(base, target=_TARGET)
    check(base, target="other-app")

    assert _tree(tmp_path) == before


def test_reading_a_data_base_that_does_not_exist_creates_nothing(tmp_path: Path) -> None:
    """Neither call may bring the data directory into being just by looking at it."""
    base = tmp_path / ".patent-checker"

    status(base, today=_TODAY)
    check(base)

    assert list(tmp_path.iterdir()) == []
