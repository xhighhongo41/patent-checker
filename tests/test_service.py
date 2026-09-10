"""Tests for the service layer (patent_checker/service.py).

Network-free: every outward call the service makes (``OpsClient`` methods,
the ops/gp parse functions and the ``utils`` helpers) is monkeypatched with a
canned stand-in, so these tests check the returned dict shapes and the route
selection only -- the underlying logic is tested where it is implemented.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from patent_checker import service
from patent_checker.cache import DEFAULT_TTLS, Cache, pub_key, search_key
from patent_checker.config import ConfigError
from patent_checker.gp import fetch as gp_fetch
from patent_checker.gp.fetch import FetchedPage, GPUnavailable
from patent_checker.gp.parse import GPatentDoc
from patent_checker.models import Claim
from patent_checker.ops.client import OpsClient
from patent_checker.ops.parse import (
    OpsBiblio,
    OpsFamily,
    OpsLegalEvent,
    OpsSearchHit,
    OpsSearchPage,
)
from tests._fixtures import fixture_path

# How long a cached legal-status response stays usable. The legal kind always
# expires; the assertion just narrows the value away from ``None``.
LEGAL_TTL = DEFAULT_TTLS["legal"]
assert LEGAL_TTL is not None

_TOKEN_JSON = {"access_token": "test-token", "token_type": "Bearer", "expires_in": "1199"}


def _load_fixture(name: str) -> bytes:
    """Return the raw bytes of a saved OPS fixture, skipping if unavailable."""
    return fixture_path(f"ops/{name}").read_bytes()


class _StubOpsClient:
    """Minimal ``OpsClient`` stand-in: replays a canned return/exception per method name."""

    def __init__(self, **method_results: Any) -> None:
        self._results = method_results
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __enter__(self) -> _StubOpsClient:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def __getattr__(self, name: str):
        def method(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            result = self._results[name]
            if isinstance(result, BaseException):
                raise result

            return result

        return method


def _explode(*args: Any, **kwargs: Any) -> Any:
    """Stand-in for a call that must not happen in the test using it."""
    raise AssertionError(f"unexpected call with args={args!r}, kwargs={kwargs!r}")


def _as_json(result: dict[str, Any]) -> dict[str, Any]:
    """Round-trip a service result through ``json.dumps``/``json.loads``.

    The service returns whatever ``dataclasses.asdict`` produces, which keeps
    tuple fields as tuples; the contract is that the *serialized* result has
    the same shape as the v0.2 CLI output, so the shape tests compare that.
    """
    return json.loads(json.dumps(result, ensure_ascii=False))


@pytest.fixture
def no_outward_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace every parse/fetch/utils hook of the service with a failing stub.

    Used by the "not configured" tests to prove that the ConfigError is
    raised before anything is fetched or parsed.
    """
    for name in (
        "parse_search_xml",
        "parse_search_biblio_xml",
        "parse_biblio_xml",
        "parse_legal_xml",
        "parse_family_xml",
        "parse_claims_xml",
        "search_plan_check",
        "fetch_patent_html",
        "parse_patent_html",
        "dedup_families",
        "verify_batch",
        "usage_report",
    ):
        monkeypatch.setattr(service, name, _explode)


def _sample_biblio(**overrides: Any) -> OpsBiblio:
    """Build a minimal OpsBiblio for the biblio-shaped tests."""
    fields: dict[str, Any] = {
        "pub": "US.1.A1",
        "family_id": "100",
        "title": "t",
        "abstract": "a",
        "applicants": ("Acme",),
        "inventors": ("Doe",),
        "ipc": (),
        "cpc": (),
        "publication_date": "20200101",
        "cited_patents": (),
        "npl_citation_count": 0,
    }
    fields.update(overrides)
    return OpsBiblio(**fields)


def _sample_gp_doc(**overrides: Any) -> GPatentDoc:
    """Build a minimal GPatentDoc for the claims-route tests."""
    fields: dict[str, Any] = {
        "pub_number": "US11468338B2",
        "title": "t",
        "abstract": "a",
        "claims": (Claim(number=1, text="1. A widget.", depends_on=()),),
        "cpc_codes": (),
        "status_display": "Active",
        "expiration": "2040-01-01",
        "priority_date": "",
        "publication_date": "",
        "assignee": "Acme",
        "backward_refs": (),
        "forward_refs": (),
        "similar": (),
        "claims_fallback_text": "",
    }
    fields.update(overrides)
    return GPatentDoc(**fields)


def _fetched_page(**overrides: Any) -> FetchedPage:
    """Build a FetchedPage stand-in for the Google Patents route."""
    fields: dict[str, Any] = {
        "pub": "US11468338B2",
        "html": "<html></html>",
        "path": None,
        "cached": False,
    }
    fields.update(overrides)
    return FetchedPage(**fields)


# --- require_ops / ops_fulltext_candidate --------------------------------


def test_require_ops_returns_the_client_it_is_given() -> None:
    """A supplied client is handed back unchanged (the caller owns its lifecycle)."""
    stub = _StubOpsClient()

    assert service.require_ops(stub) is stub


def test_require_ops_without_client_raises_config_error() -> None:
    """A missing client is the "OPS not configured" case, with the shared message."""
    with pytest.raises(ConfigError) as exc_info:
        service.require_ops(None)

    assert str(exc_info.value) == service.OPS_NOT_CONFIGURED_MESSAGE


@pytest.mark.parametrize(
    ("pub", "expected"),
    [("EP1234567A1", True), ("WO2020123456A1", True), ("US11468338B2", False)],
)
def test_ops_fulltext_candidate(pub: str, expected: bool) -> None:
    """Only EP and WO publications have OPS full text to fall back to."""
    assert service.ops_fulltext_candidate(pub) is expected


def test_ops_fulltext_candidate_unparseable_input_raises_value_error() -> None:
    """An unparseable publication number propagates parse_pubnum's ValueError."""
    with pytest.raises(ValueError):
        service.ops_fulltext_candidate("not a pub")


# --- search / search-biblio / plan-check ---------------------------------


def test_search_returns_page_fields_and_raw_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """search returns the OpsSearchPage fields plus raw_path (None without a cache)."""
    page = OpsSearchPage(
        total_count=2,
        query="ti=drone",
        begin=1,
        end=25,
        hits=(OpsSearchHit(pub="US.1.A1", family_id="100"),),
    )
    monkeypatch.setattr(service, "parse_search_xml", lambda xml: page)
    stub = _StubOpsClient(search=b"<xml/>")

    result = service.search("ti=drone", client=stub)

    assert result == {
        "query": "ti=drone",
        "total": 2,
        "begin": 1,
        "end": 25,
        "hits": [{"pub": "US.1.A1", "family_id": "100"}],
        "raw_path": None,
    }
    assert stub.calls == [("search", ("ti=drone",), {"begin": 1, "end": 25})]


def test_search_forwards_begin_and_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """The paging window is passed through to the client verbatim."""
    page = OpsSearchPage(total_count=0, query="ti=drone", begin=26, end=50, hits=())
    monkeypatch.setattr(service, "parse_search_xml", lambda xml: page)
    stub = _StubOpsClient(search=b"<xml/>")

    service.search("ti=drone", begin=26, end=50, client=stub)

    assert stub.calls == [("search", ("ti=drone",), {"begin": 26, "end": 50})]


def test_search_biblio_returns_docs_and_raw_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """search_biblio returns total/begin/end plus one asdict entry per doc."""
    biblio = _sample_biblio()
    monkeypatch.setattr(
        service,
        "parse_search_biblio_xml",
        lambda xml: type(
            "Page", (), {"total_count": 1, "begin": 1, "end": 25, "docs": (biblio,)}
        )(),
    )
    stub = _StubOpsClient(search_biblio=b"<xml/>")

    data = _as_json(service.search_biblio("ti=drone", client=stub))

    assert data["total"] == 1
    assert data["begin"] == 1
    assert data["end"] == 25
    assert data["docs"] == [
        {
            "pub": "US.1.A1",
            "family_id": "100",
            "title": "t",
            "abstract": "a",
            "applicants": ["Acme"],
            "inventors": ["Doe"],
            "ipc": [],
            "cpc": [],
            "publication_date": "20200101",
            "cited_patents": [],
            "npl_citation_count": 0,
        }
    ]
    assert data["raw_path"] is None


def test_plan_check_forwards_client_and_max_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """plan_check hands the caller's client, max_total, cache and refresh to search_plan_check.

    Extended (T8-core) to also cover cache/refresh forwarding, so this one test
    keeps documenting every keyword plan_check passes through.
    """
    captured: dict[str, Any] = {}

    def fake_plan_check(
        queries: list[str],
        *,
        client: Any = None,
        max_total: int | None = None,
        cache: Any = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        captured["queries"] = queries
        captured["client"] = client
        captured["max_total"] = max_total
        captured["cache"] = cache
        captured["refresh"] = refresh
        return {"results": [], "total_sum": 0, "exceeded": False, "max_total": max_total}

    monkeypatch.setattr(service, "search_plan_check", fake_plan_check)
    stub = _StubOpsClient()
    cache = object()

    result = service.plan_check(
        ("ti=drone", "ab=foo"), max_total=100, client=stub, cache=cache, refresh=True
    )

    assert captured["queries"] == ["ti=drone", "ab=foo"]
    assert captured["client"] is stub
    assert captured["max_total"] == 100
    assert captured["cache"] is cache
    assert captured["refresh"] is True
    assert result == {"results": [], "total_sum": 0, "exceeded": False, "max_total": 100}


# --- single-document lookups ---------------------------------------------


def test_biblio_returns_fields_and_raw_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """biblio returns the OpsBiblio fields plus raw_path (None without a cache)."""
    monkeypatch.setattr(service, "parse_biblio_xml", lambda xml: _sample_biblio(inventors=()))
    stub = _StubOpsClient(biblio=b"<xml/>")

    data = _as_json(service.biblio("US.1.A1", client=stub))

    assert data["pub"] == "US.1.A1"
    assert data["applicants"] == ["Acme"]
    assert data["raw_path"] is None
    assert stub.calls == [("biblio", ("US.1.A1",), {})]


def test_legal_returns_docdb_pub_and_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """legal keys the result by the DOCDB spelling of the requested publication."""
    events = (OpsLegalEvent(code="A1", desc="desc", gazette_date="20200101", pre_lines=("line",)),)
    monkeypatch.setattr(service, "parse_legal_xml", lambda xml: events)
    stub = _StubOpsClient(legal=b"<xml/>")

    data = _as_json(service.legal("US11468338B2", client=stub))

    assert data == {
        "pub": "US.11468338.B2",
        "events": [
            {"code": "A1", "desc": "desc", "gazette_date": "20200101", "pre_lines": ["line"]}
        ],
        "raw_path": None,
    }


def test_family_returns_members_as_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """family returns family_id/members/raw_path (None without a cache), members as a list."""
    monkeypatch.setattr(
        service,
        "parse_family_xml",
        lambda xml: OpsFamily(family_id="100", members=("US.1.A1", "EP.2.A1")),
    )
    stub = _StubOpsClient(family=b"<xml/>")

    result = service.family("US.1.A1", client=stub)

    assert result == {
        "family_id": "100",
        "members": ["US.1.A1", "EP.2.A1"],
        "raw_path": None,
    }


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: service.search("ti=drone", client=None), id="search"),
        pytest.param(lambda: service.search_biblio("ti=drone", client=None), id="search_biblio"),
        pytest.param(lambda: service.plan_check(["ti=drone"], client=None), id="plan_check"),
        pytest.param(lambda: service.biblio("US.1.A1", client=None), id="biblio"),
        pytest.param(lambda: service.legal("US.1.A1", client=None), id="legal"),
        pytest.param(lambda: service.family("US.1.A1", client=None), id="family"),
    ],
)
@pytest.mark.usefixtures("no_outward_calls")
def test_ops_functions_without_client_raise_config_error(call: Callable[[], Any]) -> None:
    """Every OPS-dependent function fails with ConfigError before doing any work."""
    with pytest.raises(ConfigError) as exc_info:
        call()

    assert str(exc_info.value) == service.OPS_NOT_CONFIGURED_MESSAGE


# --- claims (route selection) --------------------------------------------


def test_claims_google_patents_route_without_gp_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no gp_client, fetch_patent_html is called without one (source='gp')."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_fetch(*args: Any, **kwargs: Any) -> FetchedPage:
        calls.append((args, kwargs))
        return _fetched_page()

    monkeypatch.setattr(service, "fetch_patent_html", fake_fetch)
    monkeypatch.setattr(service, "parse_patent_html", lambda html: _sample_gp_doc())

    data = _as_json(service.claims("US11468338B2"))

    assert calls == [(("US11468338B2",), {"cache": None, "force": False})]
    assert data == {
        "source": "gp",
        "pub": "US11468338B2",
        "claims": [{"number": 1, "text": "1. A widget.", "depends_on": []}],
        "claims_fallback_text": "",
        "status_display": "Active",
        "expiration": "2040-01-01",
        "assignee": "Acme",
        "raw_path": None,
    }


def test_claims_passes_a_supplied_gp_client_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller-owned httpx.Client is forwarded to fetch_patent_html as client=."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_fetch(*args: Any, **kwargs: Any) -> FetchedPage:
        calls.append((args, kwargs))
        return _fetched_page()

    monkeypatch.setattr(service, "fetch_patent_html", fake_fetch)
    monkeypatch.setattr(service, "parse_patent_html", lambda html: _sample_gp_doc())
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html></html>"))

    with httpx.Client(transport=transport) as gp_client:
        result = service.claims("US11468338B2", gp_client=gp_client)

        assert calls == [(("US11468338B2",), {"client": gp_client, "cache": None, "force": False})]

    assert result["source"] == "gp"


def test_claims_gp_unavailable_falls_back_to_ops_fulltext_for_ep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GP 404 for an EP/WO document, with a client at hand, tries OPS full text."""
    unavailable = GPUnavailable(pub="EP1234567A1", status_code=404, retry_after_hint="wait")
    monkeypatch.setattr(service, "fetch_patent_html", lambda pub, **kwargs: unavailable)
    monkeypatch.setattr(
        service,
        "parse_claims_xml",
        lambda xml: (Claim(number=1, text="1. Claim text.", depends_on=()),),
    )
    stub = _StubOpsClient(claims=b"<xml/>")

    data = _as_json(service.claims("EP1234567A1", client=stub))

    assert data == {
        "source": "ops-fulltext",
        "pub": "EP1234567A1",
        "claims": [{"number": 1, "text": "1. Claim text.", "depends_on": []}],
        "raw_path": None,
    }


def test_claims_gp_unavailable_for_us_never_calls_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    """A US document has no OPS full text, so the client is left untouched."""
    unavailable = GPUnavailable(
        pub="US20240111636A1", status_code=404, retry_after_hint="wait a bit"
    )
    monkeypatch.setattr(service, "fetch_patent_html", lambda pub, **kwargs: unavailable)
    monkeypatch.setattr(service, "parse_claims_xml", _explode)
    stub = _StubOpsClient()

    result = service.claims("US20240111636A1", client=stub)

    assert result == {
        "unavailable": True,
        "pub": "US20240111636A1",
        "retry_after_hint": "wait a bit",
    }
    assert stub.calls == []


def test_claims_gp_unavailable_for_ep_without_client_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an OPS client there is no fallback route, so the result is unavailable."""
    unavailable = GPUnavailable(pub="EP1234567A1", status_code=404, retry_after_hint="wait")
    monkeypatch.setattr(service, "fetch_patent_html", lambda pub, **kwargs: unavailable)
    monkeypatch.setattr(service, "parse_claims_xml", _explode)

    result = service.claims("EP1234567A1")

    assert result == {"unavailable": True, "pub": "EP1234567A1", "retry_after_hint": "wait"}


def test_claims_ops_fulltext_404_is_also_reported_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When OPS full text also 404s, the result is still the normal unavailable shape."""
    unavailable = GPUnavailable(pub="WO2020123456A1", status_code=404, retry_after_hint="wait")
    monkeypatch.setattr(service, "fetch_patent_html", lambda pub, **kwargs: unavailable)
    not_found = httpx.HTTPStatusError(
        "404", request=httpx.Request("GET", "https://ops.epo.org"), response=httpx.Response(404)
    )
    stub = _StubOpsClient(claims=not_found)

    result = service.claims("WO2020123456A1", client=stub)

    assert result == {"unavailable": True, "pub": "WO2020123456A1", "retry_after_hint": "wait"}


def test_claims_ops_fulltext_server_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any OPS error other than 404 is a real failure and reaches the caller."""
    unavailable = GPUnavailable(pub="EP1234567A1", status_code=404, retry_after_hint="wait")
    monkeypatch.setattr(service, "fetch_patent_html", lambda pub, **kwargs: unavailable)
    server_error = httpx.HTTPStatusError(
        "500", request=httpx.Request("GET", "https://ops.epo.org"), response=httpx.Response(500)
    )
    stub = _StubOpsClient(claims=server_error)

    with pytest.raises(httpx.HTTPStatusError):
        service.claims("EP1234567A1", client=stub)


# --- normalize / dedup / verify / usage ----------------------------------


def test_normalize_returns_every_spelling() -> None:
    """normalize returns every spelling of a valid publication number."""
    assert service.normalize("US.11468338.B2") == {
        "input": "US.11468338.B2",
        "country": "US",
        "number": "11468338",
        "kind": "B2",
        "docdb": "US.11468338.B2",
        "epodoc": "US11468338B2",
        "google": "US11468338B2",
    }


def test_normalize_unparseable_input_raises_value_error() -> None:
    """An unparseable publication number propagates parse_pubnum's ValueError."""
    with pytest.raises(ValueError):
        service.normalize("not-a-pub")


def test_dedup_wraps_families_with_their_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """dedup returns dedup_families()'s list under "families" plus its length."""
    families = [{"family_id": "1", "members": ["US.1.A1", "EP.2.A1"]}]
    monkeypatch.setattr(service, "dedup_families", lambda hits: families)

    result = service.dedup([{"pub": "US.1.A1", "family_id": "1"}])

    assert result == {"families": families, "count": 1}


def test_verify_returns_verify_batch_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """verify passes both lists to verify_batch and returns its result verbatim."""
    captured: dict[str, Any] = {}
    report = {"ok": True, "input_count": 2, "output_count": 2}

    def fake_verify(input_pubs: Any, output_records: Any) -> dict[str, Any]:
        captured["input_pubs"] = input_pubs
        captured["output_records"] = output_records
        return report

    monkeypatch.setattr(service, "verify_batch", fake_verify)

    result = service.verify(["US.1.A1", "EP.2.A1"], ["US.1.A1", "EP.2.A1"])

    assert captured == {
        "input_pubs": ["US.1.A1", "EP.2.A1"],
        "output_records": ["US.1.A1", "EP.2.A1"],
    }
    assert result is report


def test_usage_without_path_calls_usage_report_with_no_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """usage() lets utils.usage_report resolve the default log path itself."""
    calls: list[tuple[Any, ...]] = []
    report = {"available": False, "path": "/tmp/headers.jsonl"}

    def fake_usage_report(*args: Any) -> dict[str, Any]:
        calls.append(args)
        return report

    monkeypatch.setattr(service, "usage_report", fake_usage_report)

    result = service.usage()

    assert calls == [()]
    assert result is report


def test_usage_with_path_passes_it_positionally(monkeypatch: pytest.MonkeyPatch) -> None:
    """usage(path) forwards the explicit log path to utils.usage_report."""
    calls: list[tuple[Any, ...]] = []

    def fake_usage_report(*args: Any) -> dict[str, Any]:
        calls.append(args)
        return {"available": False, "path": "/x"}

    monkeypatch.setattr(service, "usage_report", fake_usage_report)

    service.usage(Path("/x"))

    assert calls == [(Path("/x"),)]


# --- cache integration -----------------------------------------------------


def test_search_serves_a_second_identical_call_from_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A repeat search is served from cache; a different end still calls the client."""
    page = OpsSearchPage(total_count=0, query="ti=drone", begin=1, end=25, hits=())
    monkeypatch.setattr(service, "parse_search_xml", lambda xml: page)
    stub = _StubOpsClient(search=b"<xml/>")
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))

    first = service.search("ti=drone", client=stub, cache=cache)
    assert "cached" not in first
    # The body is stored once, and raw_path names that single copy.
    assert first["raw_path"] == str(cache.content_path("search", search_key("ti=drone", 1, 25)))
    assert len(stub.calls) == 1

    second = service.search("ti=drone", client=stub, cache=cache)
    assert second["cached"] is True
    assert second["raw_path"] == str(cache.content_path("search", search_key("ti=drone", 1, 25)))
    assert len(stub.calls) == 1

    service.search("ti=drone", end=50, client=stub, cache=cache)
    assert len(stub.calls) == 2


def test_search_biblio_serves_a_second_identical_call_from_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A repeat search-biblio call is served from cache without calling the client again."""
    page = type(
        "Page", (), {"total_count": 1, "begin": 1, "end": 25, "docs": (_sample_biblio(),)}
    )()
    monkeypatch.setattr(service, "parse_search_biblio_xml", lambda xml: page)
    stub = _StubOpsClient(search_biblio=b"<xml/>")
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))

    first = service.search_biblio("ti=drone", client=stub, cache=cache)
    assert "cached" not in first
    assert len(stub.calls) == 1

    second = service.search_biblio("ti=drone", client=stub, cache=cache)
    assert second["cached"] is True
    assert second["raw_path"] == str(cache.content_path("searchbib", search_key("ti=drone", 1, 25)))
    assert len(stub.calls) == 1


def test_biblio_serves_a_second_identical_call_from_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A repeat biblio call is served from cache without calling the client again."""
    monkeypatch.setattr(service, "parse_biblio_xml", lambda xml: _sample_biblio())
    stub = _StubOpsClient(biblio=b"<xml/>")
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))

    first = service.biblio("US.1.A1", client=stub, cache=cache)
    assert "cached" not in first
    assert first["raw_path"] == str(cache.content_path("biblio", pub_key("US.1.A1")))
    assert len(stub.calls) == 1

    second = service.biblio("US.1.A1", client=stub, cache=cache)
    assert second["cached"] is True
    assert second["raw_path"] == str(cache.content_path("biblio", pub_key("US.1.A1")))
    assert len(stub.calls) == 1


def test_legal_serves_a_second_call_within_the_same_day_from_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A repeat legal call on the same day is served from cache."""
    monkeypatch.setattr(service, "parse_legal_xml", lambda xml: ())
    stub = _StubOpsClient(legal=b"<xml/>")
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))

    first = service.legal("US11468338B2", client=stub, cache=cache)
    assert "cached" not in first
    assert len(stub.calls) == 1

    second = service.legal("US11468338B2", client=stub, cache=cache)
    assert second["cached"] is True
    assert second["raw_path"] == str(cache.content_path("legal", pub_key("US11468338B2")))
    assert len(stub.calls) == 1


def test_legal_cache_re_fetches_once_the_ttl_has_passed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A legal cache entry older than the legal TTL is not reused."""
    monkeypatch.setattr(service, "parse_legal_xml", lambda xml: ())
    stub = _StubOpsClient(legal=b"<xml/>")
    clock_value = datetime(2026, 9, 1, 23, 59, 0)
    cache = Cache(tmp_path, clock=lambda: clock_value)

    service.legal("US11468338B2", client=stub, cache=cache)
    assert len(stub.calls) == 1

    clock_value = datetime(2026, 9, 1, 23, 59, 0) + LEGAL_TTL + timedelta(seconds=1)
    service.legal("US11468338B2", client=stub, cache=cache)
    assert len(stub.calls) == 2


def test_family_serves_a_second_identical_call_from_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A repeat family call is served from cache without calling the client again."""
    monkeypatch.setattr(
        service, "parse_family_xml", lambda xml: OpsFamily(family_id="100", members=("US.1.A1",))
    )
    stub = _StubOpsClient(family=b"<xml/>")
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))

    first = service.family("US.1.A1", client=stub, cache=cache)
    assert "cached" not in first
    assert len(stub.calls) == 1

    second = service.family("US.1.A1", client=stub, cache=cache)
    assert second["cached"] is True
    assert second["raw_path"] == str(cache.content_path("family", pub_key("US.1.A1")))
    assert len(stub.calls) == 1


def test_claims_ops_fulltext_success_is_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A repeat OPS full-text claims call is served from cache."""
    unavailable = GPUnavailable(pub="EP1234567A1", status_code=404, retry_after_hint="wait")
    monkeypatch.setattr(service, "fetch_patent_html", lambda pub, **kwargs: unavailable)
    monkeypatch.setattr(
        service,
        "parse_claims_xml",
        lambda xml: (Claim(number=1, text="1. Claim text.", depends_on=()),),
    )
    stub = _StubOpsClient(claims=b"<xml/>")
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))

    first = service.claims("EP1234567A1", client=stub, cache=cache)
    assert "cached" not in first
    assert len(stub.calls) == 1

    second = service.claims("EP1234567A1", client=stub, cache=cache)
    assert second["cached"] is True
    assert second["raw_path"] == str(cache.content_path("claims", pub_key("EP1234567A1")))
    assert len(stub.calls) == 1


def test_claims_unavailable_result_is_never_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 404-unavailable claims result is not stored, so the client is called again."""
    unavailable = GPUnavailable(pub="WO2020123456A1", status_code=404, retry_after_hint="wait")
    monkeypatch.setattr(service, "fetch_patent_html", lambda pub, **kwargs: unavailable)
    not_found = httpx.HTTPStatusError(
        "404", request=httpx.Request("GET", "https://ops.epo.org"), response=httpx.Response(404)
    )
    stub = _StubOpsClient(claims=not_found)
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))

    first = service.claims("WO2020123456A1", client=stub, cache=cache)
    assert first == {"unavailable": True, "pub": "WO2020123456A1", "retry_after_hint": "wait"}
    assert len(stub.calls) == 1

    second = service.claims("WO2020123456A1", client=stub, cache=cache)
    assert second == {"unavailable": True, "pub": "WO2020123456A1", "retry_after_hint": "wait"}
    assert len(stub.calls) == 2


def test_claims_gp_route_uses_the_same_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Google Patents route stores its page in the shared cache and reuses it.

    The fetcher itself is left in place here (only the HTML parser and the
    courtesy interval are replaced), because what is under test is that the
    page is served from the cache the service was given, without a request.
    """
    monkeypatch.setattr(gp_fetch, "_last_request_at", None)
    monkeypatch.setattr(
        gp_fetch,
        "time",
        SimpleNamespace(monotonic=time.monotonic, sleep=lambda _seconds: None),
    )
    monkeypatch.setattr(service, "parse_patent_html", lambda html: _sample_gp_doc())
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, text="<html></html>")

    cache = Cache(tmp_path / "cache", clock=lambda: datetime(2026, 9, 2, 10, 0, 0))

    with httpx.Client(transport=httpx.MockTransport(handler)) as gp_client:
        first = service.claims("US11468338B2", gp_client=gp_client, cache=cache)
        second = service.claims("US11468338B2", gp_client=gp_client, cache=cache)

    assert "cached" not in first
    assert first["raw_path"] == str(cache.content_path("gp", pub_key("US11468338B2")))
    assert second["cached"] is True
    assert second["raw_path"] == first["raw_path"]
    assert len(requests) == 1


def test_ops_function_config_error_wins_over_a_cache_hit(tmp_path: Path) -> None:
    """OPS availability is checked before the cache, even with a fresh entry on disk."""
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))
    cache.put("biblio", pub_key("US.1.A1"), b"<xml/>", ident="US.1.A1")

    with pytest.raises(ConfigError) as exc_info:
        service.biblio("US.1.A1", client=None, cache=cache)

    assert str(exc_info.value) == service.OPS_NOT_CONFIGURED_MESSAGE


def test_biblio_persists_exactly_one_body_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One service.biblio() call leaves exactly one copy of the response body.

    Up to v0.3 the body was written twice: once by OpsClient's
    observation-first capture under raw/ops/ and once by the cache. The
    client no longer keeps a copy, so the cache entry is the only one.
    """
    monkeypatch.setenv("PATENT_CHECKER_OPS_KEY", "dummy-key")
    monkeypatch.setenv("PATENT_CHECKER_OPS_SECRET", "dummy-secret")
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(tmp_path))
    fixture = _load_fixture("20260827-001754_biblio_US.11468338.B2.xml")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/accesstoken"):
            return httpx.Response(200, json=_TOKEN_JSON)
        return httpx.Response(200, content=fixture)

    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))

    with OpsClient(transport=httpx.MockTransport(handler)) as client:
        service.biblio("US.11468338.B2", client=client, cache=cache)

    body_files = [
        path
        for path in tmp_path.rglob("*")
        if path.is_file()
        and path.suffix == ".xml"
        and not path.name.endswith(".meta.json")
        and not path.name.endswith(".tmp")
        and path.name != "headers.jsonl"
    ]
    assert body_files == [cache.content_path("biblio", pub_key("US.11468338.B2"))]
    # The biblio request itself is still logged exactly once, next to the one
    # token request the client needed to make it.
    headers_log = tmp_path / "raw" / "ops" / "headers.jsonl"
    logged = [json.loads(line) for line in headers_log.read_text(encoding="utf-8").splitlines()]
    assert [record["kind"] for record in logged] == ["token", "biblio"]


# --- refresh ---------------------------------------------------------------

# One call per OPS-backed route, so the refresh behaviour can be checked for
# all of them with the same body.
_CACHED_ROUTES: dict[str, Callable[[Any, Cache, bool], dict[str, Any]]] = {
    "search": lambda client, cache, refresh: service.search(
        "ti=drone", client=client, cache=cache, refresh=refresh
    ),
    "search_biblio": lambda client, cache, refresh: service.search_biblio(
        "ti=drone", client=client, cache=cache, refresh=refresh
    ),
    "biblio": lambda client, cache, refresh: service.biblio(
        "US.1.A1", client=client, cache=cache, refresh=refresh
    ),
    "legal": lambda client, cache, refresh: service.legal(
        "US.1.A1", client=client, cache=cache, refresh=refresh
    ),
    "family": lambda client, cache, refresh: service.family(
        "US.1.A1", client=client, cache=cache, refresh=refresh
    ),
    "claims": lambda client, cache, refresh: service.claims(
        "EP1234567A1", client=client, cache=cache, refresh=refresh
    ),
}


@pytest.fixture
def canned_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace every OPS parse function with a stand-in that ignores the XML."""
    page = OpsSearchPage(total_count=0, query="ti=drone", begin=1, end=25, hits=())
    monkeypatch.setattr(service, "parse_search_xml", lambda xml: page)
    monkeypatch.setattr(
        service,
        "parse_search_biblio_xml",
        lambda xml: type("Page", (), {"total_count": 0, "begin": 1, "end": 25, "docs": ()})(),
    )
    monkeypatch.setattr(service, "parse_biblio_xml", lambda xml: _sample_biblio())
    monkeypatch.setattr(service, "parse_legal_xml", lambda xml: ())
    monkeypatch.setattr(
        service, "parse_family_xml", lambda xml: OpsFamily(family_id="100", members=())
    )
    monkeypatch.setattr(service, "parse_claims_xml", lambda xml: ())


@pytest.mark.parametrize("route", sorted(_CACHED_ROUTES))
@pytest.mark.usefixtures("canned_parsers")
def test_refresh_ignores_a_fresh_cache_entry_on_every_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, route: str
) -> None:
    """Without refresh the second call is a cache hit; with it, the client is called again."""
    unavailable = GPUnavailable(pub="EP1234567A1", status_code=404, retry_after_hint="wait")
    monkeypatch.setattr(service, "fetch_patent_html", lambda pub, **kwargs: unavailable)
    stub = _StubOpsClient(
        search=b"<xml/>",
        search_biblio=b"<xml/>",
        biblio=b"<xml/>",
        legal=b"<xml/>",
        family=b"<xml/>",
        claims=b"<xml/>",
    )
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))
    call = _CACHED_ROUTES[route]

    first = call(stub, cache, False)
    assert "cached" not in first
    assert len(stub.calls) == 1

    second = call(stub, cache, False)
    assert second["cached"] is True
    assert len(stub.calls) == 1

    third = call(stub, cache, True)
    assert "cached" not in third
    assert third["raw_path"] == second["raw_path"]
    assert len(stub.calls) == 2


def test_refresh_replaces_the_cached_body(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A refreshed call re-stores the body, so the next plain call serves the new one."""
    monkeypatch.setattr(service, "parse_biblio_xml", lambda xml: _sample_biblio())
    stub = _StubOpsClient(biblio=b"<fresh/>")
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))
    cache.put("biblio", pub_key("US.1.A1"), b"<stale/>", ident="US.1.A1")

    result = service.biblio("US.1.A1", client=stub, cache=cache, refresh=True)

    assert "cached" not in result
    assert result["raw_path"] == str(cache.content_path("biblio", pub_key("US.1.A1")))
    hit = cache.get("biblio", pub_key("US.1.A1"))
    assert hit is not None
    assert hit.content == b"<fresh/>"


def test_refresh_without_a_cache_just_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no cache to bypass, refresh changes nothing and writes nothing."""
    monkeypatch.setattr(service, "parse_biblio_xml", lambda xml: _sample_biblio())
    stub = _StubOpsClient(biblio=b"<xml/>")

    result = service.biblio("US.1.A1", client=stub, refresh=True)

    assert result["raw_path"] is None
    assert len(stub.calls) == 1


def test_refresh_on_the_gp_route_forces_a_new_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """claims(refresh=True) reaches Google Patents again even with a cached page."""
    calls: list[dict[str, Any]] = []

    def fake_fetch(pub: str, **kwargs: Any) -> FetchedPage:
        calls.append(kwargs)
        return _fetched_page()

    monkeypatch.setattr(service, "fetch_patent_html", fake_fetch)
    monkeypatch.setattr(service, "parse_patent_html", lambda html: _sample_gp_doc())
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))

    service.claims("US11468338B2", cache=cache, refresh=True)

    assert calls == [{"cache": cache, "force": True}]


# --- batch size limits ---------------------------------------------------


def _boom_parse(*args: Any, **kwargs: Any) -> Any:
    """Parser stand-in for a body that turns out to be unparseable."""
    raise ValueError("unparseable body")


def test_dedup_rejects_more_records_than_the_batch_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One dedup call cannot ask for unbounded work."""
    monkeypatch.setattr(service, "dedup_families", _explode)
    hits = [{"pub": "US.1.A1", "family_id": "1"}] * (service.MAX_BATCH_RECORDS + 1)

    with pytest.raises(ValueError, match="10000"):
        service.dedup(hits)


def test_dedup_accepts_exactly_the_batch_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The limit itself is allowed: only going past it is refused."""
    monkeypatch.setattr(service, "dedup_families", lambda hits: [])
    hits = [{"pub": "US.1.A1", "family_id": "1"}] * service.MAX_BATCH_RECORDS

    assert service.dedup(hits) == {"families": [], "count": 0}


def test_verify_rejects_too_many_input_pubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The input list of verify is bounded by the same limit."""
    monkeypatch.setattr(service, "verify_batch", _explode)
    pubs = ["US.1.A1"] * (service.MAX_BATCH_RECORDS + 1)

    with pytest.raises(ValueError, match="10000"):
        service.verify(pubs, ["US.1.A1"])


def test_verify_rejects_too_many_output_records(monkeypatch: pytest.MonkeyPatch) -> None:
    """The output list of verify is bounded too."""
    monkeypatch.setattr(service, "verify_batch", _explode)
    records = ["US.1.A1"] * (service.MAX_BATCH_RECORDS + 1)

    with pytest.raises(ValueError, match="10000"):
        service.verify(["US.1.A1"], records)


def test_verify_accepts_exactly_the_batch_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both lists may be exactly as long as the limit."""
    monkeypatch.setattr(service, "verify_batch", lambda inputs, outputs: {"ok": True})
    pubs = ["US.1.A1"] * service.MAX_BATCH_RECORDS

    assert service.verify(pubs, pubs) == {"ok": True}


# --- a body that cannot be parsed is not cached --------------------------


def test_search_does_not_cache_an_unparseable_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A search response the parser rejects is not kept on disk."""
    monkeypatch.setattr(service, "parse_search_xml", _boom_parse)
    stub = _StubOpsClient(search=b"<broken/>")
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))

    with pytest.raises(ValueError):
        service.search("ti=drone", client=stub, cache=cache)

    assert cache.entries() == []


def test_search_biblio_does_not_cache_an_unparseable_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A biblio-constituent search response the parser rejects is not kept on disk."""
    monkeypatch.setattr(service, "parse_search_biblio_xml", _boom_parse)
    stub = _StubOpsClient(search_biblio=b"<broken/>")
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))

    with pytest.raises(ValueError):
        service.search_biblio("ti=drone", client=stub, cache=cache)

    assert cache.entries() == []


def test_biblio_does_not_cache_an_unparseable_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A biblio response the parser rejects is not kept on disk (biblio never expires)."""
    monkeypatch.setattr(service, "parse_biblio_xml", _boom_parse)
    stub = _StubOpsClient(biblio=b"<broken/>")
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))

    with pytest.raises(ValueError):
        service.biblio("US.1.A1", client=stub, cache=cache)

    assert cache.entries() == []


def test_legal_does_not_cache_an_unparseable_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A legal-status response the parser rejects is not kept on disk."""
    monkeypatch.setattr(service, "parse_legal_xml", _boom_parse)
    stub = _StubOpsClient(legal=b"<broken/>")
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))

    with pytest.raises(ValueError):
        service.legal("US.1.A1", client=stub, cache=cache)

    assert cache.entries() == []


def test_family_does_not_cache_an_unparseable_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A family response the parser rejects is not kept on disk."""
    monkeypatch.setattr(service, "parse_family_xml", _boom_parse)
    stub = _StubOpsClient(family=b"<broken/>")
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))

    with pytest.raises(ValueError):
        service.family("US.1.A1", client=stub, cache=cache)

    assert cache.entries() == []


def test_claims_ops_fulltext_does_not_cache_an_unparseable_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An OPS full-text claims body the parser rejects is not kept on disk."""
    unavailable = GPUnavailable(pub="EP1234567A1", status_code=404, retry_after_hint="wait")
    monkeypatch.setattr(service, "fetch_patent_html", lambda pub, **kwargs: unavailable)
    monkeypatch.setattr(service, "parse_claims_xml", _boom_parse)
    stub = _StubOpsClient(claims=b"<broken/>")
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))

    with pytest.raises(ValueError):
        service.claims("EP1234567A1", client=stub, cache=cache)

    assert cache.entries() == []


def test_claims_gp_route_drops_a_page_that_cannot_be_parsed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A freshly downloaded page the parser rejects does not stay in the cache forever."""
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))

    def fake_fetch(pub: str, **kwargs: Any) -> FetchedPage:
        # The fetcher stores the page before the service can parse it.
        path = cache.put("gp", pub_key(pub), b"<html></html>", ident=pub)
        return _fetched_page(path=path)

    monkeypatch.setattr(service, "fetch_patent_html", fake_fetch)
    monkeypatch.setattr(service, "parse_patent_html", _boom_parse)

    with pytest.raises(ValueError):
        service.claims("US11468338B2", cache=cache)

    assert cache.get("gp", pub_key("US11468338B2")) is None
    assert cache.entries() == []


def test_claims_gp_route_keeps_a_cached_page_the_parser_rejects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only a page stored by this call is dropped; an entry served from cache is left alone."""
    cache = Cache(tmp_path, clock=lambda: datetime(2026, 9, 2, 10, 0, 0))
    path = cache.put("gp", pub_key("US11468338B2"), b"<html></html>", ident="US11468338B2")
    monkeypatch.setattr(
        service, "fetch_patent_html", lambda pub, **kwargs: _fetched_page(path=path, cached=True)
    )
    monkeypatch.setattr(service, "parse_patent_html", _boom_parse)

    with pytest.raises(ValueError):
        service.claims("US11468338B2", cache=cache)

    assert cache.get("gp", pub_key("US11468338B2")) is not None
