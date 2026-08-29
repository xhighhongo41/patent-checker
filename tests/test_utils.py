"""Tests for the review-support utilities (patent_checker/utils.py).

Network-free: search_plan_check is exercised against a lightweight stub
client instead of a real OpsClient/httpx transport.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest

from patent_checker.utils import (
    dedup_families,
    search_plan_check,
    usage_report,
    verify_batch,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ops"

# X-Throttling-Control samples (see tests/test_ops_client.py for the source shape).
_IDLE_HEADER = (
    "idle (images=green:200, inpadoc=green:60, other=green:1000, "
    "retrieval=green:200, search=green:30)"
)
_BUSY_HEADER = (
    "busy (images=green:100, inpadoc=green:45, other=green:1000, "
    "retrieval=green:100, search=green:15)"
)
_BUSY_YELLOW_HEADER = (
    "busy (images=green:100, inpadoc=green:45, other=green:1000, "
    "retrieval=green:100, search=yellow:15)"
)


def _load_fixture(name: str) -> bytes:
    """Return the raw bytes of a saved OPS fixture, skipping if unavailable."""
    path = FIXTURE_DIR / name
    if not path.exists():
        pytest.skip(f"fixture not available: {path}")
    return path.read_bytes()


# --- dedup_families ------------------------------------------------------


def test_dedup_families_merges_hits_sharing_a_family_id() -> None:
    """Hits with the same family_id become a single record; members keep input order."""
    hits = [
        {"pub": "US.1.A1", "family_id": "100", "query_id": "q1"},
        {"pub": "EP.2.A1", "family_id": "100", "query_id": "q2"},
    ]
    result = dedup_families(hits)
    assert len(result) == 1
    assert result[0]["family_id"] == "100"
    assert result[0]["members"] == ["US.1.A1", "EP.2.A1"]


def test_dedup_families_keeps_distinct_families_in_first_seen_order() -> None:
    """Families appear in the output in the order they first occur in the input."""
    hits = [
        {"pub": "US.1.A1", "family_id": "200"},
        {"pub": "US.2.A1", "family_id": "100"},
        {"pub": "US.3.A1", "family_id": "200"},
    ]
    result = dedup_families(hits)
    assert [rec["family_id"] for rec in result] == ["200", "100"]
    assert result[0]["members"] == ["US.1.A1", "US.3.A1"]
    assert result[1]["members"] == ["US.2.A1"]


def test_dedup_families_representative_prefers_non_empty_abstract() -> None:
    """A non-empty abstract wins the representative slot even from a lower-ranked country."""
    hits = [
        {"pub": "US.1.A1", "family_id": "1", "abstract": ""},
        {"pub": "JP.2.A1", "family_id": "1", "abstract": "has text"},
    ]
    rep = dedup_families(hits)[0]["representative"]
    assert rep["pub"] == "JP.2.A1"


def test_dedup_families_representative_prefers_country_order_when_abstract_tied() -> None:
    """With abstract presence tied, REPRESENTATIVE_COUNTRY_ORDER breaks the tie."""
    hits = [
        {"pub": "JP.2.A1", "family_id": "1"},
        {"pub": "US.1.A1", "family_id": "1"},
    ]
    rep = dedup_families(hits)[0]["representative"]
    assert rep["pub"] == "US.1.A1"


def test_dedup_families_representative_ranks_unlisted_countries_last() -> None:
    """A country outside REPRESENTATIVE_COUNTRY_ORDER ranks after every listed one."""
    hits = [
        {"pub": "DE.9.A1", "family_id": "1"},
        {"pub": "JP.2.A1", "family_id": "1"},
    ]
    rep = dedup_families(hits)[0]["representative"]
    assert rep["pub"] == "JP.2.A1"


def test_dedup_families_representative_prefers_newer_publication_date() -> None:
    """With abstract and country tied, the more recent publication_date wins."""
    hits = [
        {"pub": "US.1.A1", "family_id": "1", "publication_date": "20200101"},
        {"pub": "US.2.A1", "family_id": "1", "publication_date": "20230101"},
    ]
    rep = dedup_families(hits)[0]["representative"]
    assert rep["pub"] == "US.2.A1"


def test_dedup_families_representative_falls_back_to_input_order() -> None:
    """When every ranked criterion ties, the earliest hit in the input wins."""
    hits = [
        {"pub": "US.1.A1", "family_id": "1"},
        {"pub": "US.2.A1", "family_id": "1"},
    ]
    rep = dedup_families(hits)[0]["representative"]
    assert rep["pub"] == "US.1.A1"


def test_dedup_families_query_ids_merged_first_seen_deduplicated() -> None:
    """query_ids merges every member's query_id, first-seen order, deduplicated."""
    hits = [
        {"pub": "US.1.A1", "family_id": "1", "query_id": "qa"},
        {"pub": "US.2.A1", "family_id": "1", "query_id": "qb"},
        {"pub": "US.3.A1", "family_id": "1", "query_id": "qa"},
        {"pub": "US.4.A1", "family_id": "1"},
    ]
    result = dedup_families(hits)
    assert result[0]["query_ids"] == ["qa", "qb"]


def test_dedup_families_missing_or_empty_family_id_is_standalone() -> None:
    """Missing/empty family_id hits are grouped by pub, not merged under family_id=""."""
    hits = [
        {"pub": "US.1.A1", "family_id": ""},
        {"pub": "US.2.A1"},
        {"pub": "US.1.A1", "family_id": ""},
    ]
    result = dedup_families(hits)
    assert len(result) == 2
    assert result[0]["family_id"] == ""
    assert result[0]["members"] == ["US.1.A1", "US.1.A1"]
    assert result[1]["members"] == ["US.2.A1"]


def test_dedup_families_empty_input_returns_empty_list() -> None:
    """An empty hit list produces an empty family list."""
    assert dedup_families([]) == []


def test_dedup_families_representative_carries_all_original_keys() -> None:
    """The representative dict keeps every key of the chosen hit, not just pub/family_id."""
    hits = [{"pub": "US.1.A1", "family_id": "1", "custom": "value", "abstract": "x"}]
    rep = dedup_families(hits)[0]["representative"]
    assert rep == {"pub": "US.1.A1", "family_id": "1", "custom": "value", "abstract": "x"}


# --- verify_batch ---------------------------------------------------------


def test_verify_batch_exact_match_is_ok() -> None:
    """A perfectly matching input/output pair reports ok=True with all lists empty."""
    result = verify_batch(["US.1.A1", "EP.2.B1"], ["US.1.A1", "EP.2.B1"])
    assert result == {
        "ok": True,
        "input_count": 2,
        "output_count": 2,
        "missing": [],
        "unexpected": [],
        "duplicates": [],
        "unparseable": [],
    }


def test_verify_batch_detects_missing() -> None:
    """A pub present in the input but absent from the output is reported as missing."""
    result = verify_batch(["US.1.A1", "US.2.A1"], ["US.1.A1"])
    assert result["ok"] is False
    assert result["missing"] == ["US.2.A1"]
    assert result["unexpected"] == []


def test_verify_batch_detects_unexpected() -> None:
    """A pub present in the output but absent from the input is reported as unexpected."""
    result = verify_batch(["US.1.A1"], ["US.1.A1", "US.9.A1"])
    assert result["ok"] is False
    assert result["unexpected"] == ["US.9.A1"]
    assert result["missing"] == []


def test_verify_batch_detects_duplicates_within_output() -> None:
    """A pub repeated within the output is reported under duplicates."""
    result = verify_batch(["US.1.A1"], [{"pub": "US.1.A1"}, {"pub": "US.1.A1"}])
    assert result["ok"] is False
    assert result["duplicates"] == ["US.1.A1"]
    assert result["missing"] == []
    assert result["unexpected"] == []


def test_verify_batch_treats_spelling_variants_as_the_same_publication() -> None:
    """docdb vs epodoc spellings of the same publication are treated as identical."""
    result = verify_batch(["US11468338B2"], ["US.11468338.B2"])
    assert result["ok"] is True
    assert result["missing"] == []
    assert result["unexpected"] == []


def test_verify_batch_unparseable_pub_matched_verbatim() -> None:
    """An unparseable pub is still matched when input and output spell it identically."""
    result = verify_batch(["not-a-pub"], ["not-a-pub"])
    assert result["ok"] is True
    assert result["unparseable"] == ["not-a-pub"]


def test_verify_batch_unparseable_pub_mismatch_is_reported() -> None:
    """Differently spelled unparseable pubs surface as both missing and unexpected."""
    result = verify_batch(["bad-input-1"], ["bad-output-2"])
    assert result["ok"] is False
    assert result["missing"] == ["bad-input-1"]
    assert result["unexpected"] == ["bad-output-2"]
    assert result["unparseable"] == ["bad-input-1", "bad-output-2"]


def test_verify_batch_accepts_mixed_string_and_dict_output_records() -> None:
    """output_records may mix plain pub strings and {"pub": ...} mappings."""
    result = verify_batch(["US.1.A1", "EP.2.B1"], [{"pub": "US.1.A1"}, "EP.2.B1"])
    assert result["ok"] is True


def test_verify_batch_dict_output_record_without_pub_key_raises_key_error() -> None:
    """A mapping output record missing the required "pub" key raises KeyError."""
    with pytest.raises(KeyError):
        verify_batch(["US.1.A1"], [{"not_pub": "US.1.A1"}])


# --- search_plan_check -----------------------------------------------------


class _StubOpsClient:
    """Fake OpsClient: search() replays canned responses (bytes) or raises them."""

    def __init__(self, responses: dict[str, bytes | BaseException]) -> None:
        self._responses = responses
        self.closed = False
        self.calls: list[tuple[str, int, int]] = []

    def search(self, cql: str, *, begin: int = 1, end: int = 25) -> tuple[bytes, Path]:
        """Record the call and replay the canned response for *cql*."""
        self.calls.append((cql, begin, end))
        response = self._responses[cql]
        if isinstance(response, BaseException):
            raise response
        return response, Path("unused.xml")

    def close(self) -> None:
        """Mark the stub as closed, so tests can assert it was (not) called."""
        self.closed = True


def test_search_plan_check_measures_every_query() -> None:
    """Each query's OPS total-result-count is read back with Range=1-2."""
    responses = {
        "q1": _load_fixture("20260827-074259_search_5214adad8e.xml"),  # total=1
        "q2": _load_fixture("20260827-074307_search_03be517d21.xml"),  # total=10
    }
    client = _StubOpsClient(responses)
    result = search_plan_check(["q1", "q2"], client=client)
    assert result["results"] == [
        {"query": "q1", "total": 1},
        {"query": "q2", "total": 10},
    ]
    assert result["total_sum"] == 11
    assert result["exceeded"] is False
    assert result["max_total"] is None
    assert all(begin == 1 and end == 2 for _q, begin, end in client.calls)


def test_search_plan_check_continues_after_a_failing_query() -> None:
    """A ValueError or httpx error on one query does not stop the others."""
    responses: dict[str, bytes | BaseException] = {
        "q1": _load_fixture("20260827-074259_search_5214adad8e.xml"),  # total=1
        "q2": ValueError("invalid Range"),
        "q3": httpx.ConnectError("boom"),
        "q4": _load_fixture("20260827-074311_search_2ffb388c44.xml"),  # total=7
    }
    client = _StubOpsClient(responses)
    result = search_plan_check(["q1", "q2", "q3", "q4"], client=client)
    assert result["results"][0] == {"query": "q1", "total": 1}
    assert result["results"][1]["query"] == "q2"
    assert "error" in result["results"][1]
    assert result["results"][2]["query"] == "q3"
    assert "error" in result["results"][2]
    assert result["results"][3] == {"query": "q4", "total": 7}
    assert result["total_sum"] == 8


def test_search_plan_check_reports_exceeded_when_over_max_total() -> None:
    """exceeded is True once total_sum exceeds max_total, False when it does not."""
    responses = {
        "q1": _load_fixture("20260827-074259_search_5214adad8e.xml"),  # total=1
        "q2": _load_fixture("20260827-074307_search_03be517d21.xml"),  # total=10
    }
    client = _StubOpsClient(responses)

    under = search_plan_check(["q1", "q2"], client=client, max_total=20)
    assert under["exceeded"] is False

    over = search_plan_check(["q1", "q2"], client=client, max_total=5)
    assert over["exceeded"] is True
    assert over["max_total"] == 5


def test_search_plan_check_does_not_close_an_injected_client() -> None:
    """A caller-supplied client is used as-is and left open."""
    responses = {"q1": _load_fixture("20260827-074259_search_5214adad8e.xml")}
    client = _StubOpsClient(responses)
    search_plan_check(["q1"], client=client)
    assert client.closed is False


# --- usage_report -----------------------------------------------------------


def test_usage_report_missing_file_reports_unavailable(tmp_path: Path) -> None:
    """A non-existent headers.jsonl path reports available=False rather than raising."""
    path = tmp_path / "headers.jsonl"
    result = usage_report(path)
    assert result == {"available": False, "path": str(path)}


def test_usage_report_aggregates_and_skips_broken_lines(tmp_path: Path) -> None:
    """Valid lines are aggregated by kind/status/throttling/date; broken lines are skipped."""
    today_1 = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
    today_2 = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
    past = "2020-01-01T00:00:00"

    lines = [
        f'{{"at": "{today_1}", "kind": "search", "url": "u1", '
        f'"status": 200, "throttling": "{_IDLE_HEADER}"}}',
        f'{{"at": "{today_2}", "kind": "biblio", "url": "u2", '
        f'"status": 200, "throttling": "{_BUSY_HEADER}"}}',
        f'{{"at": "{past}", "kind": "search", "url": "u3", '
        f'"status": 404, "throttling": "{_BUSY_YELLOW_HEADER}"}}',
        "{not valid json",
        "",
    ]
    path = tmp_path / "headers.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = usage_report(path)

    assert result["available"] is True
    assert result["path"] == str(path)
    assert result["total_requests"] == 3
    assert result["by_kind"] == {"search": 2, "biblio": 1}
    assert result["by_status"] == {"200": 2, "404": 1}
    assert result["non_green_events"] == 1
    assert result["system_states"] == {"idle": 1, "busy": 2}
    assert result["first_at"] == today_1
    assert result["last_at"] == past
    assert result["today"]["total_requests"] == 2
    assert result["today"]["by_kind"] == {"search": 1, "biblio": 1}
    assert result["skipped_lines"] == 1
