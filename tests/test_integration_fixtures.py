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

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from patent_checker import service
from patent_checker.cache import Cache, pub_key, search_key
from patent_checker.gp import fetch as gp_fetch
from patent_checker.ops import client as ops_client
from patent_checker.ops.client import OpsClient
from tests._fixtures import fixture_path

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


def test_legal_gb_empty_events_is_an_empty_tuple(cache: Cache) -> None:
    """A GB publication whose A and B responses both carry no event is a normal empty result."""
    empty_a = fixture_path("ops/20260828-125143_legal_GB.2553053.A.xml").read_bytes()
    empty_b = fixture_path("ops/20260829-capture_legal_GB.2553053.B.xml").read_bytes()
    assert b"<ops:legal " not in empty_a
    assert b"<ops:legal " not in empty_b

    with OpsClient(transport=httpx.MockTransport(_token_and_by_kind(empty_a, empty_b))) as client:
        result = service.legal("GB2553053A", client=client, cache=cache)

    assert result["events"] == []


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
