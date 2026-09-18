"""End-to-end tests through the service layer, using real saved fixtures.

Unlike ``tests/test_service.py`` (which monkeypatches the ops/gp parse
functions with canned stand-ins) these tests exercise the real parsers: an
``OpsClient``/``httpx.Client`` whose transport is a :class:`httpx.MockTransport`
answers with a saved fixture body, and the service function is called as is,
so a mismatch between the client, the parser and the cache layer would show
up here even if each of those is separately well tested. No test in this
module reaches the network; every fixture file is loaded through
:func:`tests._fixtures.fixture_path`, which skips (or, under
``--fixtures-required``, fails) when ``tests/fixtures/`` is not present.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from patent_checker import service, validation, watch
from patent_checker.cache import Cache, pub_key, search_key
from patent_checker.gp import fetch as gp_fetch
from patent_checker.ops import client as ops_client
from patent_checker.ops import parse as ops_parse
from patent_checker.ops.client import OpsClient
from patent_checker.pubnum import parse_pubnum
from tests._fixtures import FIXTURES_ROOT, fixture_path

_TOKEN_JSON = {"access_token": "test-token", "token_type": "Bearer", "expires_in": "1199"}


@pytest.fixture(autouse=True)
def _ops_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point OPS credentials/data dir at a scratch directory and never sleep for real."""
    monkeypatch.setenv("PATENT_CHECKER_OPS_KEY", "dummy-key")
    monkeypatch.setenv("PATENT_CHECKER_OPS_SECRET", "dummy-secret")
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(ops_client.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(gp_fetch, "_last_request_at", None)


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    """A file cache rooted under this test's own ``tmp_path``."""
    return Cache(tmp_path / "cache")


def _token_and(body: bytes) -> Callable[[httpx.Request], httpx.Response]:
    """Answer the OAuth2 handshake, then every other request with *body*."""

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/accesstoken"):
            return httpx.Response(200, json=_TOKEN_JSON)
        return httpx.Response(200, content=body)

    return responder


def _token_and_by_kind(a_body: bytes, b_body: bytes) -> Callable[[httpx.Request], httpx.Response]:
    """Answer the OAuth2 handshake, then dispatch on the ``...A``/``...B`` kind code."""

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/accesstoken"):
            return httpx.Response(200, json=_TOKEN_JSON)
        if request.url.path.endswith(".B"):
            return httpx.Response(200, content=b_body)
        return httpx.Response(200, content=a_body)

    return responder


def _token_and_by_endpoint(
    legal_body: bytes, family_body: bytes
) -> Callable[[httpx.Request], httpx.Response]:
    """Answer the OAuth2 handshake, then dispatch on the legal/family endpoint."""

    def responder(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/auth/accesstoken"):
            return httpx.Response(200, json=_TOKEN_JSON)
        if "/legal/" in path:
            return httpx.Response(200, content=legal_body)
        if "/family/" in path:
            return httpx.Response(200, content=family_body)
        raise AssertionError(f"unexpected request: {request.url}")

    return responder


def _ops_client_for(body: bytes) -> OpsClient:
    """An ``OpsClient`` whose transport answers every request with *body*."""
    return OpsClient(transport=httpx.MockTransport(_token_and(body)))


def _gp_client_answering_404() -> httpx.Client:
    """An ``httpx.Client`` reporting every request as not-yet-indexed (404)."""
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404)))


def _gp_client_serving(html: str) -> httpx.Client:
    """An ``httpx.Client`` answering every request with *html*."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=html))
    return httpx.Client(transport=transport)


# --- biblio ------------------------------------------------------------


def test_biblio_reaches_the_service_layer_and_writes_the_cache(cache: Cache) -> None:
    """A real biblio fixture round-trips through OpsClient, the parser and the cache."""
    xml = fixture_path("ops/20260827-001754_biblio_US.11468338.B2.xml").read_bytes()

    with _ops_client_for(xml) as client:
        result = service.biblio("US.11468338.B2", client=client, cache=cache)

    assert result["pub"] == "US.11468338.B2"
    assert result["title"]
    assert result["inventors"]
    key = pub_key("US.11468338.B2")
    assert cache.content_path("biblio", key).is_file()
    assert cache.meta_path("biblio", key).is_file()
    assert result["raw_path"] == str(cache.content_path("biblio", key))
    assert result["fetched_at"]


# --- claims: OPS full text and Google Patents ---------------------------


def test_claims_ops_fulltext_reaches_the_service_layer_and_writes_the_cache(cache: Cache) -> None:
    """An EP claims fixture is served through the OPS full-text fallback route.

    Google Patents is made to answer 404 (not yet indexed) so the service
    falls through to OPS, the only way the ``ops-fulltext`` route is taken.
    """
    xml = fixture_path("ops/20260828-130733_claims_EP.4645156.A1.xml").read_bytes()

    with _ops_client_for(xml) as client, _gp_client_answering_404() as gp_client:
        result = service.claims("EP.4645156.A1", client=client, gp_client=gp_client, cache=cache)

    assert result["source"] == "ops-fulltext"
    assert result["claims"]
    key = pub_key("EP.4645156.A1")
    assert cache.content_path("claims", key).is_file()
    assert cache.meta_path("claims", key).is_file()
    assert result["fetched_at"]
    assert result["pub_docdb"] == key


def test_claims_gp_page_reaches_the_service_layer_and_writes_the_cache(cache: Cache) -> None:
    """A saved Google Patents page round-trips through fetch, the parser and the cache."""
    html = fixture_path("gp/US11468338B2.html").read_text(encoding="utf-8")

    with _gp_client_serving(html) as gp_client:
        result = service.claims("US11468338B2", gp_client=gp_client, cache=cache)

    assert result["source"] == "gp"
    assert result["claims"]
    key = pub_key("US11468338B2")
    assert cache.content_path("gp", key).is_file()
    assert cache.meta_path("gp", key).is_file()
    assert result["fetched_at"]
    assert result["pub_docdb"]


# --- legal: a normal response and a GB pair with no events ---------------


def test_legal_reaches_the_service_layer_and_writes_the_cache(cache: Cache) -> None:
    """A populated legal fixture yields a non-empty tuple of events."""
    xml = fixture_path("ops/20260827-100613_legal_US.2007016547.A1.xml").read_bytes()

    with _ops_client_for(xml) as client:
        result = service.legal("US.2007016547.A1", client=client, cache=cache)

    assert result["events"]
    key = pub_key("US.2007016547.A1")
    assert cache.content_path("legal", key).is_file()
    assert cache.meta_path("legal", key).is_file()
    assert result["fetched_at"]


def test_legal_gb_empty_events_is_an_empty_tuple(cache: Cache) -> None:
    """A GB publication whose A and B responses both carry no event is a normal empty result."""
    empty_a = fixture_path("ops/20260828-125143_legal_GB.2553053.A.xml").read_bytes()
    empty_b = fixture_path("ops/20260829-capture_legal_GB.2553053.B.xml").read_bytes()
    assert b"<ops:legal " not in empty_a
    assert b"<ops:legal " not in empty_b

    with OpsClient(transport=httpx.MockTransport(_token_and_by_kind(empty_a, empty_b))) as client:
        result = service.legal("GB2553053A", client=client, cache=cache)

    assert result["events"] == []
    assert result["fetched_at"]


# --- family --------------------------------------------------------------


def test_family_reaches_the_service_layer_and_writes_the_cache(cache: Cache) -> None:
    """A real family fixture round-trips through OpsClient, the parser and the cache."""
    xml = fixture_path("ops/20260827-001755_family_US.11468338.B2.xml").read_bytes()

    with _ops_client_for(xml) as client:
        result = service.family("US.11468338.B2", client=client, cache=cache)

    assert result["family_id"]
    assert result["members"]
    key = pub_key("US.11468338.B2")
    assert cache.content_path("family", key).is_file()
    assert cache.meta_path("family", key).is_file()
    assert result["fetched_at"]


# --- search / search_biblio -----------------------------------------------


def test_search_reaches_the_service_layer_and_writes_the_cache(cache: Cache) -> None:
    """A real search fixture round-trips through OpsClient, the parser and the cache."""
    xml = fixture_path("ops/20260827-001753_search_ab40de2f9f.xml").read_bytes()
    cql, begin, end = "ta = computer", 1, 5

    with _ops_client_for(xml) as client:
        result = service.search(cql, begin=begin, end=end, client=client, cache=cache)

    assert result["hits"]
    assert result["total"] == 1958269
    key = search_key(cql, begin, end)
    assert cache.content_path("search", key).is_file()
    assert cache.meta_path("search", key).is_file()
    assert result["fetched_at"]


def test_search_biblio_reaches_the_service_layer_and_never_yields_a_bare_cpc_separator(
    cache: Cache,
) -> None:
    """A real searchbib fixture round-trips, and no CPC entry is the bare "/" separator.

    Regression guard for the US-classification/CPC mix-up fixed in v1.0 (see
    ``tests/test_ops_parse.py::test_extract_cpc_ignores_other_classification_schemes``).
    """
    xml = fixture_path("ops/20260827-092030_searchbib_bc6c779320_1-3.xml").read_bytes()
    cql, begin, end = "ti = drone", 1, 3

    with _ops_client_for(xml) as client:
        result = service.search_biblio(cql, begin=begin, end=end, client=client, cache=cache)

    assert result["docs"]
    for doc in result["docs"]:
        assert "/" not in doc["cpc"], doc
    key = search_key(cql, begin, end)
    assert cache.content_path("searchbib", key).is_file()
    assert cache.meta_path("searchbib", key).is_file()
    assert result["fetched_at"]


# --- watch -----------------------------------------------------------------


def test_watch_takes_a_snapshot_and_reports_a_repeat_run_as_unchanged(cache: Cache) -> None:
    """Real legal and family fixtures round-trip through watch: snapshot, then no change.

    The second run is given the first run's snapshot the way a caller
    stores it (through JSON), so this covers the whole loop the Skill
    follows: fetch, store, hand back, compare.
    """
    legal_xml = fixture_path("ops/20260827-001754_legal_US.11468338.B2.xml").read_bytes()
    family_xml = fixture_path("ops/20260827-001755_family_US.11468338.B2.xml").read_bytes()
    transport = httpx.MockTransport(_token_and_by_endpoint(legal_xml, family_xml))

    with OpsClient(transport=transport) as client:
        first = service.watch(["US.11468338.B2"], client=client, cache=cache)
        stored = json.loads(json.dumps(first["results"][0]["snapshot"], ensure_ascii=False))
        second = service.watch(["US.11468338.B2"], previous=[stored], client=client, cache=cache)

    entry = first["results"][0]
    assert first["first_count"] == 1
    assert entry["pub"] == "US.11468338.B2"
    assert entry["snapshot"]["legal"]["event_count"] > 0
    assert entry["snapshot"]["family"]["members"]

    repeat = second["results"][0]
    assert repeat["first_snapshot"] is False
    assert repeat["changed"] is False
    assert repeat["legal"]["new_events"] == []
    assert repeat["legal"]["missing_events"] == []
    assert second["unchanged_count"] == 1
    # A watch run reads through the normal TTLs, so the repeat run needed no
    # request at all.
    assert repeat["cached"] == {"legal": True, "family": True}


def test_the_largest_fixture_snapshot_stays_well_inside_the_payload_limits(
    cache: Cache,
) -> None:
    """A full watch list of the biggest snapshot these fixtures can produce still fits.

    The snapshot travels back to the server on every follow-up run, so its
    size decides whether the whole feature is usable. The worst case
    available here is the largest legal-status response (an EP publication
    whose lapse is recorded per contracting state) combined with the
    largest family; the two bodies do not belong to the same publication,
    which does not matter for a size measurement.
    """
    legal_xml = fixture_path("ops/20260827-100619_legal_EP.1672502.A1.xml").read_bytes()
    family_xml = fixture_path("ops/20260828-125051_family_GB.2553053.A.xml").read_bytes()
    transport = httpx.MockTransport(_token_and_by_endpoint(legal_xml, family_xml))

    with OpsClient(transport=transport) as client:
        result = service.watch(["EP.1672502.A1"], client=client, cache=cache)

    snapshot = result["results"][0]["snapshot"]
    size = len(json.dumps(snapshot, ensure_ascii=False))
    assert snapshot["legal"]["event_count"] > 50
    # One snapshot is one element of the "previous" batch, and a full watch
    # list of them is one payload; both limits are checked here, because the
    # element limit is the tighter of the two by far.
    assert size < watch.MAX_SNAPSHOT_CHARS // 10, (
        f"one snapshot is {size} characters: a stored snapshot is one element of the "
        f"'previous' batch, which is limited to {watch.MAX_SNAPSHOT_CHARS} characters"
    )
    assert watch.MAX_WATCH_PUBS * size < validation.MAX_PAYLOAD_CHARS // 10


# --- publication numbers across every saved search page ---------------------


def test_every_saved_search_hit_publication_number_parses() -> None:
    """Every DOCDB number these search pages report parses and round-trips.

    OPS answers searches with letter-bearing numbers (JP era-based ``H``/``S``
    numbers, TW ``I`` numbers, a HU ``P`` number). Before v1.2 the parser
    rejected 22 of the spellings saved here, so those documents could not be
    fetched, cached or watched although they showed up as hits. The docdb
    spelling OPS sends is also the spelling ``docdb()`` must reproduce: the
    number normalization ``parse_pubnum`` performs (the pre-2026 US A-kind
    shrink) only applies to spellings OPS never sends.
    """
    search_paths = sorted((FIXTURES_ROOT / "ops").glob("*_search_*.xml"))
    searchbib_paths = sorted((FIXTURES_ROOT / "ops").glob("*_searchbib_*.xml"))
    if not search_paths or not searchbib_paths:
        # Go through the shared helper so this sweep obeys the session's
        # "fixtures are required" switch instead of silently checking nothing.
        fixture_path("ops/20260827-001753_search_ab40de2f9f.xml")
    pubs: list[tuple[str, str]] = []
    for path in search_paths:
        page = ops_parse.parse_search_xml(path.read_bytes())
        pubs.extend((path.name, hit.pub) for hit in page.hits)
    for path in searchbib_paths:
        biblio_page = ops_parse.parse_search_biblio_xml(path.read_bytes())
        pubs.extend((path.name, doc.pub) for doc in biblio_page.docs)

    assert pubs
    for name, pub in pubs:
        assert parse_pubnum(pub).docdb() == pub, f"{name}: {pub}"
