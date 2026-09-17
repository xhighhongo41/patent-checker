"""The rules of each ledger file, one section per file.

These follow the tables of
``skills/patent-checker/references/ledger-format.md`` key by key: what is
required, what values are accepted, what a reference must resolve to. A
line with several faults reports all of them, and a check that would repeat
something already reported (a reference that cannot be resolved because the
file holding it could not be read, a key that is not there) is skipped.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from patent_checker.ledger.findings import FileFindings, Ledger, Unique
from patent_checker.ledger.layout import (
    CQL_DATE_RE,
    DATED_RUN_TYPES,
    FAMILIES_FILENAME,
    FAMILY_STAGE1_VALUES,
    FAMILY_STAGE2_VALUES,
    FEATURE_BASES,
    FEATURE_ID_RE,
    FEATURE_PRIORITIES,
    FEATURE_STATUSES,
    FEATURES_FILENAME,
    OBSERVATION_VALUES,
    QUERIES_FILENAME,
    QUERY_ID_RE,
    QUERY_STATUSES,
    RUN_ID_RE,
    RUN_MODES,
    RUN_STAGES,
    RUN_TYPES,
    RUNS_FILENAME,
    WATCH_FILENAME,
    WATCH_REASONS,
    WINDOW_RE,
)
from patent_checker.ledger.reading import JsonLines, as_timestamp, has_value, shown
from patent_checker.ledger.values import (
    check_date,
    check_enum,
    check_feature_list,
    check_id,
    check_pub,
    check_run_ref,
    check_shape,
    check_timestamp,
    require,
)
from patent_checker.watch import SNAPSHOT_FORMAT

# --- check: runs.jsonl ------------------------------------------------------


def check_runs(ctx: Ledger, loaded: JsonLines) -> None:
    """Check every line of ``runs.jsonl``, which the rest of the ledger refers to."""
    ff = FileFindings(ctx.findings, RUNS_FILENAME)
    unique = Unique(ff, '"run_id"')
    previous: datetime | None = None
    for line, record in loaded.records:
        _check_run(ctx, ff, unique, line, record)
        previous = _check_run_order(ff, line, record, previous)


def _check_run(
    ctx: Ledger, ff: FileFindings, unique: Unique, line: int, record: Mapping[str, Any]
) -> None:
    """Check one run's line."""
    if require(ff, line, record, "run_id") and check_id(
        ff, line, record["run_id"], '"run_id"', RUN_ID_RE, "YYYYMMDD-HHMM (the local start time)"
    ):
        unique.add(line, record["run_id"])
    for key, allowed in (("type", RUN_TYPES), ("stage", RUN_STAGES), ("mode", RUN_MODES)):
        if require(ff, line, record, key):
            check_enum(ff, line, record[key], f'"{key}"', allowed)
    if require(ff, line, record, "started_at"):
        check_timestamp(ff, line, record["started_at"], '"started_at"')
    require(ff, line, record, "server_version")
    if has_value(record, "finished_at"):
        check_timestamp(ff, line, record["finished_at"], '"finished_at"')
    _check_searched_through(ff, line, record)
    if has_value(record, "previous_run"):
        _check_previous_run(ctx, ff, line, record)
    # An optional key that is null is read as "not there", so only a run that
    # claims a report is asked to have one.
    if has_value(record, "report"):
        _check_report(ctx, ff, line, record["report"])


def _check_searched_through(ff: FileFindings, line: int, record: Mapping[str, Any]) -> None:
    """Check the date a run's searches reached, which the next run continues from."""
    if "searched_through" not in record:
        ff.error(
            line,
            "required-key",
            '"searched_through" is required and is not there; it is null only for a watch '
            "run and for an import whose date cannot be established",
        )
        return
    value = record["searched_through"]
    run_type = record.get("type")
    if value is None:
        if run_type in DATED_RUN_TYPES:
            ff.error(
                line,
                "searched-through",
                f'"searched_through" is null on a {run_type} run; it must name the last '
                "publication date the run's searches covered",
            )
        return
    check_date(ff, line, value, '"searched_through"')


def _check_previous_run(
    ctx: Ledger, ff: FileFindings, line: int, record: Mapping[str, Any]
) -> None:
    """Check the run this one continues, which may not be the run itself."""
    value = record["previous_run"]
    if isinstance(value, str) and value == record.get("run_id"):
        ff.error(
            line,
            "run-ref",
            '"previous_run" names this run itself; it names the earlier run this one continues',
        )
        return
    check_run_ref(ctx, ff, line, value, '"previous_run"')


def _check_report(ctx: Ledger, ff: FileFindings, line: int, value: Any) -> None:
    """Check that a run's report is where the run says it is."""
    if not isinstance(value, str) or not value:
        ff.error(
            line,
            "report-missing",
            f'"report" is {shown(value)}; expected a path relative to .patent-checker/',
        )
        return
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        ff.error(
            line,
            "report-missing",
            f'"report" is {shown(value)}; paths are relative to .patent-checker/ and use "/"',
        )
        return
    if not (ctx.base / relative).is_file():
        ff.error(
            line,
            "report-missing",
            f'"report" names {shown(value)}, and there is no such file under '
            ".patent-checker/; reports are never moved or overwritten",
        )


def _check_run_order(
    ff: FileFindings, line: int, record: Mapping[str, Any], previous: datetime | None
) -> datetime | None:
    """Warn when a run's line is older than the line above it."""
    started = as_timestamp(record.get("started_at"))
    if started is None:
        return previous
    comparable = previous is not None and (previous.tzinfo is None) == (started.tzinfo is None)
    if comparable and previous is not None and started < previous:
        ff.warning(
            line,
            "run-order",
            f'"started_at" is earlier than the line above ({shown(previous.isoformat())}); '
            f"{RUNS_FILENAME} holds one line per run, oldest first",
        )
    return started


# --- check: features.jsonl --------------------------------------------------


def check_features(ctx: Ledger, loaded: JsonLines) -> None:
    """Check every line of ``features.jsonl``."""
    ff = FileFindings(ctx.findings, FEATURES_FILENAME)
    unique = Unique(ff, '"id"')
    for line, record in loaded.records:
        _check_feature(ctx, ff, unique, line, record)


def _check_feature(
    ctx: Ledger, ff: FileFindings, unique: Unique, line: int, record: Mapping[str, Any]
) -> None:
    """Check one feature's line."""
    if require(ff, line, record, "id") and check_id(
        ff, line, record["id"], '"id"', FEATURE_ID_RE, "F followed by a number, such as F3"
    ):
        unique.add(line, record["id"])
    require(ff, line, record, "title")
    for key, allowed in (
        ("basis", FEATURE_BASES),
        ("priority", FEATURE_PRIORITIES),
        ("status", FEATURE_STATUSES),
    ):
        if require(ff, line, record, key):
            check_enum(ff, line, record[key], f'"{key}"', allowed)
    if require(ff, line, record, "since_run"):
        check_run_ref(ctx, ff, line, record["since_run"], '"since_run"')
    if has_value(record, "changed_run"):
        check_run_ref(ctx, ff, line, record["changed_run"], '"changed_run"')


# --- check: queries.jsonl ---------------------------------------------------


def check_queries(ctx: Ledger, loaded: JsonLines) -> None:
    """Check every line of ``queries.jsonl``."""
    ff = FileFindings(ctx.findings, QUERIES_FILENAME)
    unique = Unique(ff, '"id"')
    for line, record in loaded.records:
        _check_query(ctx, ff, unique, line, record)


def _check_query(
    ctx: Ledger, ff: FileFindings, unique: Unique, line: int, record: Mapping[str, Any]
) -> None:
    """Check one query's line."""
    if require(ff, line, record, "id") and check_id(
        ff, line, record["id"], '"id"', QUERY_ID_RE, "Q followed by a number, such as Q7"
    ):
        unique.add(line, record["id"])
    if require(ff, line, record, "cql"):
        _check_cql(ff, line, record["cql"])
    status_value: Any = None
    if require(ff, line, record, "status"):
        status_value = record["status"]
        check_enum(ff, line, status_value, '"status"', QUERY_STATUSES)
    if require(ff, line, record, "features"):
        check_feature_list(ctx, ff, line, record["features"], '"features"')
    if require(ff, line, record, "runs"):
        _check_query_runs(ctx, ff, line, record["runs"])
    _check_reason(ff, line, record, status_value)


def _check_cql(ff: FileFindings, line: int, value: Any) -> None:
    """Warn about a stored query that carries its own publication-date clause."""
    if isinstance(value, str) and CQL_DATE_RE.search(value):
        ff.warning(
            line,
            "cql-date-clause",
            '"cql" carries a publication-date clause; store the query without one, '
            "since each run adds its own window",
        )


def _check_reason(
    ff: FileFindings, line: int, record: Mapping[str, Any], status_value: Any
) -> None:
    """Warn when a query that is not run says nothing about why."""
    if status_value not in ("rejected", "dropped"):
        return
    reason = record.get("reason")
    if isinstance(reason, str) and reason.strip():
        return
    ff.warning(
        line,
        "reason-missing",
        f'"reason" is not there on a {status_value} query; the next run needs to know '
        "why it is not worth running again",
    )


def _check_query_runs(ctx: Ledger, ff: FileFindings, line: int, value: Any) -> None:
    """Check the measured hit counts a query carries, one per run that used it."""
    expected = 'an array of {"run_id", "total", "window"} objects'
    if not isinstance(value, list):
        check_shape(ff, line, value, '"runs"', expected)
        return
    for index, entry in enumerate(value):
        label = f'"runs[{index}]"'
        if not isinstance(entry, dict):
            check_shape(ff, line, entry, label, 'an object with "run_id", "total" and "window"')
            continue
        if require(ff, line, entry, "run_id", f"{label}.run_id"):
            check_run_ref(ctx, ff, line, entry["run_id"], f"{label}.run_id")
        if "total" in entry:
            _check_total(ff, line, entry["total"], f"{label}.total")
        else:
            require(ff, line, entry, "total", f"{label}.total")
        if require(ff, line, entry, "window", f"{label}.window"):
            _check_window(ff, line, entry["window"], f"{label}.window")


def _check_total(ff: FileFindings, line: int, value: Any, label: str) -> None:
    """Check a measured hit count, which may be null when it was not measured."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        ff.error(
            line,
            "window",
            f"{label} is {shown(value)}; expected the measured hit count "
            "(an integer of 0 or more) or null",
        )


def _check_window(ff: FileFindings, line: int, value: Any, label: str) -> None:
    """Check the publication-date window a run covered for a query."""
    if value == "all" or (isinstance(value, str) and WINDOW_RE.match(value)):
        return
    ff.error(
        line,
        "window",
        f'{label} is {shown(value)}; expected "all" or "YYYYMMDD-YYYYMMDD"',
    )


# --- check: families.jsonl --------------------------------------------------


def check_families(ctx: Ledger, loaded: JsonLines) -> None:
    """Check every line of ``families.jsonl``."""
    ff = FileFindings(ctx.findings, FAMILIES_FILENAME)
    unique = Unique(ff, '"family_id"')
    for line, record in loaded.records:
        _check_family(ctx, ff, unique, line, record)


def _check_family(
    ctx: Ledger, ff: FileFindings, unique: Unique, line: int, record: Mapping[str, Any]
) -> None:
    """Check one screened family's line."""
    if require(ff, line, record, "family_id"):
        family_id = record["family_id"]
        if not isinstance(family_id, str) or not family_id:
            check_shape(
                ff, line, family_id, '"family_id"', "the family identifier of the search hits"
            )
        else:
            unique.add(line, family_id)
    if require(ff, line, record, "pubs"):
        _check_family_pubs(ff, line, record["pubs"])
    if require(ff, line, record, "stage1"):
        check_enum(ff, line, record["stage1"], '"stage1"', FAMILY_STAGE1_VALUES)
    if has_value(record, "stage2"):
        check_enum(ff, line, record["stage2"], '"stage2"', FAMILY_STAGE2_VALUES)
    if require(ff, line, record, "judged_run"):
        check_run_ref(ctx, ff, line, record["judged_run"], '"judged_run"')
    if require(ff, line, record, "features"):
        check_feature_list(ctx, ff, line, record["features"], '"features"', allow_all=True)


def _check_family_pubs(ff: FileFindings, line: int, value: Any) -> None:
    """Check the publications seen for one family."""
    if not isinstance(value, list) or not value:
        check_shape(ff, line, value, '"pubs"', "a non-empty array of DOCDB publication numbers")
        return
    for index, item in enumerate(value):
        check_pub(ff, line, item, f'"pubs[{index}]"', require_kind=False, tolerate_unparsed=True)


# --- check: watch.jsonl -----------------------------------------------------


def check_watch(ctx: Ledger, loaded: JsonLines) -> None:
    """Check every line of ``watch.jsonl``."""
    ff = FileFindings(ctx.findings, WATCH_FILENAME)
    unique = Unique(ff, '"pub"')
    for line, record in loaded.records:
        _check_watch_entry(ctx, ff, unique, line, record)


def _check_watch_entry(
    ctx: Ledger, ff: FileFindings, unique: Unique, line: int, record: Mapping[str, Any]
) -> None:
    """Check one monitored publication's line."""
    pub_key: str | None = None
    if require(ff, line, record, "pub"):
        pub_key = check_pub(ff, line, record["pub"], '"pub"', require_kind=True)
        if pub_key is not None:
            unique.add(line, pub_key)
    if require(ff, line, record, "family_id"):
        _check_family_ref(ctx, ff, line, record["family_id"])
    if require(ff, line, record, "reason"):
        check_enum(ff, line, record["reason"], '"reason"', WATCH_REASONS)
    for key in ("added_run", "checked_run"):
        if require(ff, line, record, key):
            check_run_ref(ctx, ff, line, record[key], f'"{key}"')
    if has_value(record, "closed_run"):
        check_run_ref(ctx, ff, line, record["closed_run"], '"closed_run"')
    _check_next_check(ff, line, record)
    if has_value(record, "features"):
        check_feature_list(ctx, ff, line, record["features"], '"features"')
    _check_observation(ctx, ff, line, record.get("observation"))
    _check_status_reading(ctx, ff, line, record.get("status_reading"))
    _check_claims_read(ctx, ff, line, record.get("claims_read"))
    _check_history(ctx, ff, line, record.get("history"))
    _check_snapshot(ctx, ff, line, record, pub_key)


def _check_family_ref(ctx: Ledger, ff: FileFindings, line: int, value: Any) -> None:
    """Warn about a watched publication whose family was never screened."""
    if ctx.family_ids is None or (isinstance(value, str) and value in ctx.family_ids):
        return
    ff.warning(
        line,
        "watch-family-unknown",
        f'"family_id" is {shown(value)}, which has no line in {FAMILIES_FILENAME}; '
        "every screened family keeps a line there",
    )


def _check_next_check(ff: FileFindings, line: int, record: Mapping[str, Any]) -> None:
    """Check the date of the next check, required while monitoring goes on."""
    if not has_value(record, "next_check"):
        if has_value(record, "closed_run"):
            return
        ff.error(
            line,
            "required-key",
            '"next_check" is required while "closed_run" is unset; give the date of the next check',
        )
        return
    check_date(ff, line, record["next_check"], '"next_check"')


def _check_observation(ctx: Ledger, ff: FileFindings, line: int, value: Any) -> None:
    """Check the three-valued observation a watched publication carries."""
    if value is None:
        return
    if not isinstance(value, dict):
        check_shape(
            ff,
            line,
            value,
            '"observation"',
            'an object with "value", "decisive_element" and "run_id"',
        )
        return
    if has_value(value, "value"):
        check_enum(ff, line, value["value"], '"observation.value"', OBSERVATION_VALUES)
    if has_value(value, "run_id"):
        check_run_ref(ctx, ff, line, value["run_id"], '"observation.run_id"')


def _check_status_reading(ctx: Ledger, ff: FileFindings, line: int, value: Any) -> None:
    """Check the reading of the legal events, which is dated and attributed."""
    if value is None:
        return
    if not isinstance(value, dict):
        check_shape(
            ff,
            line,
            value,
            '"status_reading"',
            'an object with "summary", "decisive_event", "as_of" and "run_id"',
        )
        return
    if has_value(value, "as_of"):
        check_timestamp(ff, line, value["as_of"], '"status_reading.as_of"')
    if has_value(value, "run_id"):
        check_run_ref(ctx, ff, line, value["run_id"], '"status_reading.run_id"')
    event = value.get("decisive_event")
    if event is None:
        return
    if not isinstance(event, dict):
        check_shape(
            ff,
            line,
            event,
            '"status_reading.decisive_event"',
            'an object with "code" and "date"',
        )
        return
    if has_value(event, "date"):
        check_date(ff, line, event["date"], '"status_reading.decisive_event.date"')


def _check_claims_read(ctx: Ledger, ff: FileFindings, line: int, value: Any) -> None:
    """Check which publication's claims were read for a watched document."""
    if value is None:
        return
    if not isinstance(value, dict):
        check_shape(ff, line, value, '"claims_read"', 'an object with "pub", "source" and "run_id"')
        return
    if has_value(value, "pub"):
        check_pub(ff, line, value["pub"], '"claims_read.pub"', require_kind=True)
    if has_value(value, "run_id"):
        check_run_ref(ctx, ff, line, value["run_id"], '"claims_read.run_id"')


def _check_history(ctx: Ledger, ff: FileFindings, line: int, value: Any) -> None:
    """Check the per-run notes of what changed for a watched document."""
    if value is None:
        return
    if not isinstance(value, list):
        check_shape(ff, line, value, '"history"', 'an array of {"run_id", "summary"} objects')
        return
    for index, entry in enumerate(value):
        label = f'"history[{index}]"'
        if not isinstance(entry, dict):
            check_shape(ff, line, entry, label, 'an object with "run_id" and "summary"')
            continue
        if has_value(entry, "run_id"):
            check_run_ref(ctx, ff, line, entry["run_id"], f"{label}.run_id")


def _check_snapshot(
    ctx: Ledger, ff: FileFindings, line: int, record: Mapping[str, Any], pub_key: str | None
) -> None:
    """Check the stored ``watch_check`` snapshot the next comparison depends on."""
    if "snapshot" not in record:
        ff.error(
            line,
            "required-key",
            '"snapshot" is required and is not there; store what watch_check returned, verbatim',
        )
        return
    value = record["snapshot"]
    if value is None:
        _check_absent_snapshot(ctx, ff, line, record)
        return
    if not isinstance(value, dict):
        ff.error(
            line,
            "snapshot",
            f'"snapshot" is {shown(value)}; expected the object watch_check returned, '
            "stored verbatim",
        )
        return
    _check_snapshot_object(ff, line, value, pub_key)


def _check_snapshot_object(
    ff: FileFindings, line: int, snapshot: Mapping[str, Any], pub_key: str | None
) -> None:
    """Check a stored snapshot: its format, the document it covers and its two halves."""
    stored_format = snapshot.get("snapshot_format")
    # ``True == 1`` in Python, so a boolean has to be refused on its own.
    if isinstance(stored_format, bool) or stored_format != SNAPSHOT_FORMAT:
        ff.error(
            line,
            "snapshot",
            f'"snapshot.snapshot_format" is {shown(stored_format)}; this release compares '
            f"format {SNAPSHOT_FORMAT} snapshots, so take a fresh one",
        )
    _check_snapshot_pub(ff, line, snapshot, pub_key)
    for key in ("legal", "family"):
        if not isinstance(snapshot.get(key), dict):
            ff.error(
                line,
                "snapshot",
                f'"snapshot.{key}" is {shown(snapshot.get(key))}; expected the '
                f"{key} half of what watch_check returned",
            )


def _check_snapshot_pub(
    ff: FileFindings, line: int, snapshot: Mapping[str, Any], pub_key: str | None
) -> None:
    """Check that the snapshot covers the publication its line is about."""
    if not has_value(snapshot, "pub"):
        ff.error(
            line,
            "snapshot",
            '"snapshot.pub" is not there; a snapshot names the publication it covers',
        )
        return
    stored = check_pub(ff, line, snapshot["pub"], '"snapshot.pub"', require_kind=False)
    if stored is None or pub_key is None or stored == pub_key:
        return
    ff.error(
        line,
        "snapshot",
        f'"snapshot.pub" is {shown(snapshot["pub"])} but the line is about '
        f"{shown(pub_key)}; a snapshot is stored on the line it was taken for",
    )


def _check_absent_snapshot(
    ctx: Ledger, ff: FileFindings, line: int, record: Mapping[str, Any]
) -> None:
    """Check the two cases in which a watched publication may carry no snapshot."""
    if ctx.run_ids is None:
        # Without runs.jsonl there is no way to tell a degraded-mode run.
        return
    checked_run = record.get("checked_run")
    if isinstance(checked_run, str) and ctx.run_modes.get(checked_run) == "degraded":
        return
    error_text = record.get("snapshot_error")
    if isinstance(error_text, str) and error_text.strip():
        ff.warning(
            line,
            "snapshot-error",
            f'"snapshot" is null because watch_check failed ({shown(error_text)}); '
            "take one on the next run so the comparison has something to rest on",
        )
        return
    ff.error(
        line,
        "snapshot",
        '"snapshot" is null; only a degraded-mode run (see "checked_run") or a '
        '"snapshot_error" saying why watch_check failed explains a missing snapshot',
    )
