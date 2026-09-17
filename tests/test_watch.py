"""Tests for the watch comparison helpers (patent_checker/watch.py).

Offline and pure: every function under test takes plain mappings (the shapes
``service.legal``/``service.family`` return) and gives plain dicts back, so
no fixture, cache or client appears here. What is pinned down is the
comparison itself -- which events count as new, which as missing, and when a
publication is reported as changed at all.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from patent_checker import watch
from patent_checker.validation import InvalidInput

FETCHED_AT = "2026-09-02T10:00:00+00:00"
LATER_FETCHED_AT = "2026-09-17T10:00:00+00:00"


def _event(
    code: str = "PG25",
    desc: str = "Lapsed in a contracting state",
    gazette_date: str = "20240101",
    pre_lines: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build one legal event in the shape ``service.legal`` returns."""
    return {"code": code, "desc": desc, "gazette_date": gazette_date, "pre_lines": list(pre_lines)}


def _legal(
    *events: dict[str, Any],
    pub: str = "EP.1672502.A1",
    fetched_at: str = FETCHED_AT,
    note: str | None = None,
) -> dict[str, Any]:
    """Build a ``service.legal`` result carrying *events*."""
    result: dict[str, Any] = {
        "pub": pub,
        "events": list(events),
        "raw_path": None,
        "fetched_at": fetched_at,
    }
    if note is not None:
        result["note"] = note
    return result


def _family(
    *members: str,
    family_id: str = "100",
    fetched_at: str = FETCHED_AT,
) -> dict[str, Any]:
    """Build a ``service.family`` result carrying *members*."""
    return {
        "family_id": family_id,
        "members": list(members),
        "raw_path": None,
        "fetched_at": fetched_at,
    }


def _snapshot(legal: dict[str, Any], family: dict[str, Any]) -> dict[str, Any]:
    """Build a snapshot the way a caller stores it: through JSON.

    The contract is that a snapshot survives being written to a ledger and
    handed back verbatim, so the tests compare against the JSON round-trip
    rather than the in-process object.
    """
    return json.loads(json.dumps(watch.build_snapshot(legal, family)))


# --- event_key -----------------------------------------------------------


def test_event_key_is_deterministic_for_the_same_event() -> None:
    """The same event yields the same key, so a repeat run compares equal."""
    event = _event(pre_lines=("Designated state: DE",))

    assert watch.event_key(event) == watch.event_key(dict(event))


def test_event_key_carries_the_date_the_code_and_a_digest() -> None:
    """The key is [gazette_date, code, digest], the digest an 8-character hex string."""
    key = watch.event_key(_event(code="PG25", gazette_date="20240101"))

    assert key[0] == "20240101"
    assert key[1] == "PG25"
    assert len(key[2]) == 8
    assert all(character in "0123456789abcdef" for character in key[2])


def test_event_key_digest_differs_when_the_description_differs() -> None:
    """Same day and same code, different text: two events, not one."""
    first = watch.event_key(_event(desc="Lapsed in DE"))
    second = watch.event_key(_event(desc="Lapsed in FR"))

    assert first[:2] == second[:2]
    assert first[2] != second[2]


def test_event_key_digest_differs_when_the_pre_lines_differ() -> None:
    """The pre-text lines carry the contracting state, so they are part of the digest."""
    first = watch.event_key(_event(pre_lines=("Designated state: DE",)))
    second = watch.event_key(_event(pre_lines=("Designated state: FR",)))

    assert first[2] != second[2]


@pytest.mark.parametrize(
    "event",
    [
        pytest.param({}, id="empty"),
        pytest.param({"code": None, "desc": None, "gazette_date": None}, id="null-fields"),
        pytest.param({"code": 5, "desc": 5, "gazette_date": 5}, id="non-string-fields"),
    ],
)
def test_event_key_reads_an_unusable_field_as_empty(event: dict[str, Any]) -> None:
    """A field OPS did not send is an empty string, never a crash mid-comparison."""
    key = watch.event_key(event)

    assert key[0] == ""
    assert key[1] == ""
    assert len(key[2]) == 8


# --- build_snapshot ------------------------------------------------------


def test_build_snapshot_has_the_documented_shape() -> None:
    """The snapshot names the publication, the format version and both sections."""
    snapshot = watch.build_snapshot(_legal(_event()), _family("EP.1672502.A1"))

    assert snapshot["pub"] == "EP.1672502.A1"
    assert snapshot["snapshot_format"] == watch.SNAPSHOT_FORMAT
    assert snapshot["legal"]["fetched_at"] == FETCHED_AT
    assert snapshot["legal"]["event_count"] == 1
    assert snapshot["legal"]["events"] == [watch.event_key(_event())]
    assert snapshot["family"] == {
        "fetched_at": FETCHED_AT,
        "family_id": "100",
        "members": ["EP.1672502.A1"],
    }


def test_build_snapshot_sorts_events_and_members() -> None:
    """Both lists are sorted, so the same data always serializes the same way."""
    legal = _legal(
        _event(code="PG25", gazette_date="20240101"),
        _event(code="MM4A", gazette_date="20230101"),
        _event(code="AA1A", gazette_date="20240101"),
    )
    family = _family("US.1.A1", "EP.2.A1", "JP.3.A")

    snapshot = watch.build_snapshot(legal, family)

    assert snapshot["legal"]["events"] == sorted(snapshot["legal"]["events"])
    assert snapshot["family"]["members"] == ["EP.2.A1", "JP.3.A", "US.1.A1"]


def test_build_snapshot_keeps_the_no_events_note() -> None:
    """ "OPS reported nothing" must survive into the snapshot: it is an answer, not a gap."""
    snapshot = watch.build_snapshot(_legal(note="no legal events"), _family())

    assert snapshot["legal"]["note"] == "no legal events"
    assert snapshot["legal"]["event_count"] == 0


def test_build_snapshot_without_a_note_leaves_the_key_out() -> None:
    """A populated legal result carries no note key at all."""
    snapshot = watch.build_snapshot(_legal(_event()), _family())

    assert "note" not in snapshot["legal"]


# --- diff_snapshots: the first run ---------------------------------------


def test_diff_snapshots_without_a_previous_snapshot_compares_nothing() -> None:
    """A first run records the state; it cannot report a change against nothing."""
    result = watch.diff_snapshots(None, _legal(_event()), _family("US.1.A1"))

    assert result["pub"] == "EP.1672502.A1"
    assert result["first_snapshot"] is True
    assert result["changed"] is False
    assert result["legal"]["previous_fetched_at"] is None
    assert result["legal"]["new_events"] == []
    assert result["legal"]["missing_events"] == []
    assert result["family"]["previous_fetched_at"] is None
    assert result["family"]["new_members"] == []
    assert result["family"]["missing_members"] == []
    assert result["family"]["family_id_changed"] is None
    assert result["snapshot"] == watch.build_snapshot(_legal(_event()), _family("US.1.A1"))


# --- diff_snapshots: legal events ----------------------------------------


def test_diff_snapshots_reports_no_change_for_an_identical_response() -> None:
    """The same events and the same family: nothing changed."""
    legal, family = _legal(_event()), _family("US.1.A1")
    previous = _snapshot(legal, family)

    result = watch.diff_snapshots(previous, legal, family)

    assert result["first_snapshot"] is False
    assert result["changed"] is False
    assert result["legal"]["new_events"] == []
    assert result["legal"]["missing_events"] == []


def test_diff_snapshots_reports_a_new_event_in_full() -> None:
    """A new event is returned whole, so the caller need not fetch it again."""
    before = _legal(_event(code="MM4A", gazette_date="20230101"))
    added = _event(code="PG25", gazette_date="20240101", pre_lines=("Designated state: DE",))
    after = _legal(_event(code="MM4A", gazette_date="20230101"), added)
    previous = _snapshot(before, _family("US.1.A1"))

    result = watch.diff_snapshots(previous, after, _family("US.1.A1"))

    assert result["changed"] is True
    assert result["legal"]["new_events"] == [
        {
            "code": "PG25",
            "desc": added["desc"],
            "gazette_date": "20240101",
            "pre_lines": ["Designated state: DE"],
        }
    ]
    assert result["legal"]["missing_events"] == []


def test_diff_snapshots_reports_a_missing_event_by_key() -> None:
    """An event the office withdrew is reported by key: its text is no longer available."""
    withdrawn = _event(code="MM4A", gazette_date="20230101")
    previous = _snapshot(_legal(withdrawn, _event()), _family("US.1.A1"))

    result = watch.diff_snapshots(previous, _legal(_event()), _family("US.1.A1"))

    assert result["changed"] is True
    assert result["legal"]["new_events"] == []
    assert result["legal"]["missing_events"] == [watch.event_key(withdrawn)]


def test_diff_snapshots_separates_same_day_events_that_differ_in_content() -> None:
    """EP lapses one contracting state at a time: same date, same code, different text."""
    germany = _event(pre_lines=("Designated state: DE",))
    france = _event(pre_lines=("Designated state: FR",))
    previous = _snapshot(_legal(germany), _family("EP.1672502.A1"))

    result = watch.diff_snapshots(previous, _legal(germany, france), _family("EP.1672502.A1"))

    assert result["changed"] is True
    assert result["legal"]["new_events"] == [
        {
            "code": france["code"],
            "desc": france["desc"],
            "gazette_date": france["gazette_date"],
            "pre_lines": ["Designated state: FR"],
        }
    ]
    assert result["legal"]["missing_events"] == []


def test_diff_snapshots_counts_repeated_identical_events() -> None:
    """Two identical events becoming three is one new event, not none (multiset comparison)."""
    event = _event()
    previous = _snapshot(_legal(event, event), _family("US.1.A1"))

    result = watch.diff_snapshots(previous, _legal(event, event, event), _family("US.1.A1"))

    assert result["changed"] is True
    assert len(result["legal"]["new_events"]) == 1
    assert result["legal"]["missing_events"] == []


def test_diff_snapshots_counts_a_dropped_duplicate_as_missing() -> None:
    """The mirror image: three identical events becoming two leaves one missing."""
    event = _event()
    previous = _snapshot(_legal(event, event, event), _family("US.1.A1"))

    result = watch.diff_snapshots(previous, _legal(event, event), _family("US.1.A1"))

    assert result["legal"]["missing_events"] == [watch.event_key(event)]
    assert result["legal"]["new_events"] == []


# --- diff_snapshots: family ----------------------------------------------


def test_diff_snapshots_reports_a_new_family_member() -> None:
    """A publication that joined the family since the last run is named."""
    previous = _snapshot(_legal(_event()), _family("US.1.A1"))

    result = watch.diff_snapshots(previous, _legal(_event()), _family("US.1.A1", "JP.3.A"))

    assert result["changed"] is True
    assert result["family"]["new_members"] == ["JP.3.A"]
    assert result["family"]["missing_members"] == []


def test_diff_snapshots_reports_a_missing_family_member() -> None:
    """A member that is no longer listed is reported too, without being interpreted."""
    previous = _snapshot(_legal(_event()), _family("US.1.A1", "JP.3.A"))

    result = watch.diff_snapshots(previous, _legal(_event()), _family("US.1.A1"))

    assert result["changed"] is True
    assert result["family"]["missing_members"] == ["JP.3.A"]
    assert result["family"]["new_members"] == []


def test_diff_snapshots_reports_a_changed_family_id() -> None:
    """A different family id means the document was re-grouped upstream."""
    previous = _snapshot(_legal(_event()), _family("US.1.A1", family_id="100"))

    result = watch.diff_snapshots(previous, _legal(_event()), _family("US.1.A1", family_id="200"))

    assert result["changed"] is True
    assert result["family"]["family_id_changed"] == {"from": "100", "to": "200"}


def test_diff_snapshots_ignores_a_changed_fetched_at() -> None:
    """Fetching the same data again is not a change; both timestamps are still reported."""
    legal, family = _legal(_event()), _family("US.1.A1")
    previous = _snapshot(legal, family)
    again_legal = _legal(_event(), fetched_at=LATER_FETCHED_AT)
    again_family = _family("US.1.A1", fetched_at=LATER_FETCHED_AT)

    result = watch.diff_snapshots(previous, again_legal, again_family)

    assert result["changed"] is False
    assert result["legal"]["fetched_at"] == LATER_FETCHED_AT
    assert result["legal"]["previous_fetched_at"] == FETCHED_AT
    assert result["family"]["fetched_at"] == LATER_FETCHED_AT
    assert result["family"]["previous_fetched_at"] == FETCHED_AT


# --- diff_snapshots: since ------------------------------------------------


@pytest.mark.parametrize(
    "gazette_date",
    [pytest.param("20240101", id="docdb-spelling"), pytest.param("2024-01-01", id="iso-spelling")],
)
def test_diff_snapshots_since_selects_events_on_or_after_the_bound(gazette_date: str) -> None:
    """Both gazette-date spellings are understood, and the bound itself is included."""
    recent = _event(code="PG25", gazette_date=gazette_date)
    old = _event(code="MM4A", gazette_date="20230101")
    legal = _legal(old, recent)

    result = watch.diff_snapshots(None, legal, _family(), since=date(2024, 1, 1))

    assert result["legal"]["events_since"] == [
        {
            "code": "PG25",
            "desc": recent["desc"],
            "gazette_date": gazette_date,
            "pre_lines": [],
        }
    ]
    assert result["legal"]["undated_events"] == 0


def test_diff_snapshots_since_counts_events_without_a_usable_date() -> None:
    """An event with no readable gazette date is counted, never silently dropped."""
    legal = _legal(_event(gazette_date=""), _event(gazette_date="not-a-date"), _event())

    result = watch.diff_snapshots(None, legal, _family(), since=date(2020, 1, 1))

    assert result["legal"]["undated_events"] == 2
    assert len(result["legal"]["events_since"]) == 1


def test_diff_snapshots_since_does_not_make_a_result_changed() -> None:
    """events_since is a reading aid; whether something changed is decided by the diff."""
    legal, family = _legal(_event()), _family("US.1.A1")
    previous = _snapshot(legal, family)

    result = watch.diff_snapshots(previous, legal, family, since=date(2020, 1, 1))

    assert result["legal"]["events_since"]
    assert result["changed"] is False


def test_diff_snapshots_without_since_leaves_both_keys_out() -> None:
    """Without a bound the result carries neither events_since nor undated_events."""
    result = watch.diff_snapshots(None, _legal(_event()), _family())

    assert "events_since" not in result["legal"]
    assert "undated_events" not in result["legal"]


def test_diff_snapshots_survives_a_json_round_trip_of_the_previous_snapshot() -> None:
    """A stored snapshot comes back with lists where tuples were: the diff must not notice."""
    legal, family = _legal(_event()), _family("US.1.A1")
    stored = json.loads(json.dumps(watch.build_snapshot(legal, family)))

    result = watch.diff_snapshots(stored, legal, family)

    assert result["changed"] is False
    assert result["legal"]["missing_events"] == []


# --- index_previous -------------------------------------------------------


def test_index_previous_keys_snapshots_by_their_docdb_publication() -> None:
    """A snapshot stored under any spelling is found under the DOCDB one."""
    snapshot = watch.build_snapshot(_legal(_event(), pub="US11468338B2"), _family())

    indexed = watch.index_previous([snapshot])

    assert list(indexed) == ["US.11468338.B2"]
    assert indexed["US.11468338.B2"] is snapshot


def test_index_previous_accepts_an_empty_list() -> None:
    """No stored snapshot is a normal first run, not an error."""
    assert watch.index_previous([]) == {}


@pytest.mark.parametrize(
    ("record", "message"),
    [
        pytest.param("US.1.A1", "previous[0]", id="not-a-mapping"),
        pytest.param({"snapshot_format": 1, "legal": {}, "family": {}}, "pub", id="missing-pub"),
        pytest.param(
            {"pub": 5, "snapshot_format": 1, "legal": {}, "family": {}}, "pub", id="non-string-pub"
        ),
        pytest.param(
            {"pub": "not a pub", "snapshot_format": 1, "legal": {}, "family": {}},
            "previous[0]",
            id="unparseable-pub",
        ),
        pytest.param(
            {"pub": "US.1.A1", "legal": {}, "family": {}}, "snapshot_format", id="missing-format"
        ),
        pytest.param(
            {"pub": "US.1.A1", "snapshot_format": 1, "family": {}}, "legal", id="missing-legal"
        ),
        pytest.param(
            {"pub": "US.1.A1", "snapshot_format": 1, "legal": {}}, "family", id="missing-family"
        ),
        pytest.param(
            {"pub": "US.1.A1", "snapshot_format": 1, "legal": [], "family": {}},
            "legal",
            id="legal-not-a-mapping",
        ),
    ],
)
def test_index_previous_rejects_a_malformed_record(record: Any, message: str) -> None:
    """Every violation names the offending element, so the caller can fix its ledger."""
    with pytest.raises(InvalidInput) as exc_info:
        watch.index_previous([record])

    assert message in str(exc_info.value)


def test_index_previous_rejects_an_unknown_snapshot_format() -> None:
    """A snapshot from another format version is refused, naming what is accepted."""
    record = {"pub": "US.1.A1", "snapshot_format": 2, "legal": {}, "family": {}}

    with pytest.raises(InvalidInput) as exc_info:
        watch.index_previous([record])

    assert str(watch.SNAPSHOT_FORMAT) in str(exc_info.value)


def test_index_previous_rejects_the_same_publication_twice() -> None:
    """Two snapshots of one publication leave no way to say which is the previous one."""
    first = watch.build_snapshot(_legal(_event(), pub="US11468338B2"), _family())
    second = watch.build_snapshot(_legal(pub="US.11468338.B2"), _family())

    with pytest.raises(InvalidInput) as exc_info:
        watch.index_previous([first, second])

    assert "US.11468338.B2" in str(exc_info.value)
