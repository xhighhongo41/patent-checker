"""Checking one value: the pieces every file's rules are built from.

Each helper reports at most one finding and says, in its message, what to
write instead. They sit below :mod:`patent_checker.ledger.rules` (which
decides *which* value is checked against what) so that the rules of a file
read as the format document's table does.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from patent_checker.ledger.findings import FileFindings, Ledger
from patent_checker.ledger.layout import FEATURES_FILENAME, RUNS_FILENAME
from patent_checker.ledger.reading import as_date, as_pubnum, as_timestamp, shown


def require(
    ff: FileFindings,
    line: int | None,
    record: Mapping[str, Any],
    key: str,
    label: str | None = None,
) -> bool:
    """Report a missing or null required key; return True when the value is usable.

    Null is refused because only two keys of the whole format may be null
    (``runs.searched_through`` and ``watch.snapshot``), and both are checked
    where their absence means something.
    """
    named = label or f'"{key}"'
    if key not in record:
        ff.error(line, "required-key", f"{named} is required and is not there")
        return False
    if record[key] is None:
        ff.error(line, "required-key", f"{named} is null; it must carry a value")
        return False
    return True


def check_enum(ff: FileFindings, line: int, value: Any, label: str, allowed: Sequence[str]) -> None:
    """Report a value that is not one of the accepted spellings."""
    if isinstance(value, str) and value in allowed:
        return
    ff.error(
        line,
        "enum",
        f"{label} is {shown(value)}; expected one of: {', '.join(allowed)}",
    )


def check_shape(ff: FileFindings, line: int, value: Any, label: str, expected: str) -> None:
    """Report a value whose shape is not the one the format prescribes.

    The format document files these under ``enum`` together with the values
    that are simply misspelled ("families.features is neither an array nor
    'all'"), so a container of the wrong shape is reported the same way.
    """
    ff.error(line, "enum", f"{label} is {shown(value)}; expected {expected}")


def check_id(
    ff: FileFindings, line: int, value: Any, label: str, pattern: re.Pattern[str], shape: str
) -> bool:
    """Report an identifier that is not spelled as the format prescribes."""
    if isinstance(value, str) and pattern.match(value):
        return True
    ff.error(line, "id-format", f"{label} is {shown(value)}; expected {shape}")
    return False


def check_date(ff: FileFindings, line: int, value: Any, label: str) -> None:
    """Report a value that should be a calendar date and is not."""
    if as_date(value) is None:
        ff.error(line, "date", f"{label} is {shown(value)}; expected a date spelled YYYY-MM-DD")


def check_timestamp(ff: FileFindings, line: int, value: Any, label: str) -> None:
    """Report a value that should be a timestamp and is not."""
    if as_timestamp(value) is None:
        ff.error(
            line,
            "timestamp",
            f"{label} is {shown(value)}; expected an ISO 8601 timestamp such as "
            "2026-03-01T09:30:00+00:00",
        )


def check_run_ref(ctx: Ledger, ff: FileFindings, line: int, value: Any, label: str) -> None:
    """Report a reference to a run that ``runs.jsonl`` does not hold."""
    if ctx.run_ids is None:
        # runs.jsonl could not be read: reporting every reference against it
        # would bury the one finding that matters.
        return
    if isinstance(value, str) and value in ctx.run_ids:
        return
    ff.error(
        line,
        "run-ref",
        f"{label} is {shown(value)}, which has no line in {RUNS_FILENAME}; "
        "a run is referred to by the run_id of its line",
    )


def check_feature_ref(ctx: Ledger, ff: FileFindings, line: int, value: Any, label: str) -> None:
    """Report a reference to a feature that ``features.jsonl`` does not hold."""
    if ctx.feature_ids is None:
        return
    if isinstance(value, str) and value in ctx.feature_ids:
        return
    ff.error(
        line,
        "feature-ref",
        f"{label} is {shown(value)}, which has no line in {FEATURES_FILENAME}; "
        "a feature keeps its id even once retired",
    )


def check_feature_list(
    ctx: Ledger, ff: FileFindings, line: int, value: Any, label: str, *, allow_all: bool = False
) -> None:
    """Check an array of feature ids (or, where the format allows it, ``"all"``)."""
    if allow_all and value == "all":
        return
    if not isinstance(value, list):
        expected = "an array of feature ids"
        if allow_all:
            expected += ', or the string "all"'
        check_shape(ff, line, value, label, expected)
        return
    for index, item in enumerate(value):
        check_feature_ref(ctx, ff, line, item, f"{label}[{index}]")


def check_pub(
    ff: FileFindings,
    line: int,
    value: Any,
    label: str,
    *,
    require_kind: bool,
    tolerate_unparsed: bool = False,
) -> str | None:
    """Check one publication number and return its DOCDB spelling.

    Args:
        require_kind: Refuse a number without a kind code, which names
            whatever kind is current instead of one document.
        tolerate_unparsed: Report a string this package cannot parse as a
            warning instead of an error. The search service returns a few
            numbers of that kind (an era-based JP number, an Indian
            application number); copied from a hit into a screened family
            they are correct as they stand, and nothing the agent could
            fix. A publication that is going to be fetched or watched must
            parse, so those callers leave this off.

    Returns:
        The DOCDB spelling, or ``None`` when the value could not be read as
        a publication number (which is reported).
    """
    parsed = as_pubnum(value)
    if parsed is None:
        if tolerate_unparsed and isinstance(value, str) and value.strip():
            ff.warning(
                line,
                "pub-unparsed",
                f"{label} is {shown(value)}, which this package cannot parse; kept as the "
                "search service spelled it, but it cannot be watched or fetched by that name",
            )
            return None
        ff.error(
            line,
            "pub-spelling",
            f"{label} is {shown(value)}, which cannot be read as a publication number; "
            "copy it from the tool result",
        )
        return None
    docdb = parsed.docdb()
    if value != docdb:
        ff.error(
            line,
            "pub-spelling",
            f"{label}: {shown(value)} is not in DOCDB spelling; write {shown(docdb)}",
        )
    if require_kind and not parsed.kind:
        ff.error(
            line,
            "pub-kind",
            f"{label} is {shown(value)}, which carries no kind code; without one it names "
            "whatever kind is current, which changes when the patent is granted",
        )
    return docdb
