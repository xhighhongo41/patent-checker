"""Tests for the MCP tool surface (``patent_checker.server.tools``/``app``).

Network-free: every tool is driven through FastMCP's in-memory ``Client``,
and everything the service layer would reach out to (the OPS client, Google
Patents, the ops/gp parse functions) is either a stub or an
``httpx.MockTransport``. The repository's own ``.env`` is never read: each
test runs from an empty ``tmp_path`` and ``.env`` loading is disabled
outright, so no real credential can leak into a test.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.metadata
import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from mcp import MCPError

from patent_checker import cache, config, service, utils, validation
from patent_checker.cache import Cache, pub_key, search_key
from patent_checker.gp import fetch as gp_fetch
from patent_checker.gp.fetch import FetchedPage
from patent_checker.gp.parse import GPatentDoc
from patent_checker.models import Claim
from patent_checker.net import AllowlistTransport, HostNotAllowedError
from patent_checker.ops.client import MIN_INTERVAL_SECONDS, OpsClient
from patent_checker.ops.parse import (
    OpsBiblio,
    OpsFamily,
    OpsLegalEvent,
    OpsSearchBiblioPage,
    OpsSearchHit,
    OpsSearchPage,
)
from patent_checker.server import app as server_app
from patent_checker.server import tools
from patent_checker.server.app import (
    ServerState,
    banner_lines,
    build_server,
    build_state,
    close_state,
    http_allowed_hosts,
)
from patent_checker.server.settings import (
    OPERATOR_NOTICE_VERSION,
    ServerSettings,
    load_settings,
)

_ENV_VARS = (
    "PATENT_CHECKER_OPERATOR_CONSENT",
    "PATENT_CHECKER_SERVER_TOKEN",
    "PATENT_CHECKER_SERVER_TOKEN_FILE",
    "PATENT_CHECKER_SERVER_HOST",
    "PATENT_CHECKER_SERVER_PORT",
    "PATENT_CHECKER_SERVER_ALLOWED_HOSTS",
    "PATENT_CHECKER_SERVER_RPS",
    "PATENT_CHECKER_SERVER_BURST",
    "PATENT_CHECKER_DATA_DIR",
    "PATENT_CHECKER_OPS_KEY",
    "PATENT_CHECKER_OPS_SECRET",
    "PATENT_CHECKER_OPS_KEY_FILE",
    "PATENT_CHECKER_OPS_SECRET_FILE",
    "PATENT_CHECKER_LOG_LEVEL",
)

_TOKEN_JSON = {"access_token": "test-token", "token_type": "Bearer", "expires_in": "1199"}


# --- fixtures and helpers ------------------------------------------------


@pytest.fixture(autouse=True)
def server_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Give every test a consenting, credential-free environment under ``tmp_path``.

    The rate limiter is set far above what any single test needs, so only the
    test that checks throttling has to care about it.
    """
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    # load_env() now searches upwards from the current working directory (see
    # config.load_env), so chdir alone already keeps the repository's real
    # .env (which holds actual OPS credentials) out of reach; load_dotenv is
    # still replaced with a no-op as a second, load_env()-independent guard.
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setenv("PATENT_CHECKER_OPERATOR_CONSENT", OPERATOR_NOTICE_VERSION)
    monkeypatch.setenv("PATENT_CHECKER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PATENT_CHECKER_SERVER_RPS", "1000")
    monkeypatch.setenv("PATENT_CHECKER_SERVER_BURST", "1000")
    yield tmp_path
    # build_state() sets a process-wide override; do not leak it into other tests.
    config.set_data_base(None)


@pytest.fixture
def settings() -> ServerSettings:
    """Resolved stdio settings for the environment prepared above."""
    return load_settings(transport="stdio")


class _StubOpsClient:
    """Minimal ``OpsClient`` stand-in: replays a canned return/exception per method name."""

    def __init__(self, **method_results: Any) -> None:
        self._results = method_results
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Callable[..., Any]:
        def method(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            result = self._results[name]
            if isinstance(result, BaseException):
                raise result
            return result

        return method

    def close(self) -> None:
        """Record the close so lifecycle tests can see it."""
        self.calls.append(("close", (), {}))


def _gp_not_found(request: httpx.Request) -> httpx.Response:
    """Answer every Google Patents request with the "no page yet" 404."""
    return httpx.Response(404, text="not found")


@pytest.fixture
def make_state(settings: ServerSettings) -> Iterator[Callable[..., ServerState]]:
    """Return a factory for ``ServerState`` objects whose clients are closed on teardown."""
    created: list[ServerState] = []

    def factory(
        *,
        ops_client: Any = None,
        gp_handler: Callable[[httpx.Request], httpx.Response] = _gp_not_found,
        state_settings: ServerSettings | None = None,
    ) -> ServerState:
        effective = state_settings if state_settings is not None else settings
        state = ServerState(
            settings=effective,
            ops_client=ops_client,
            gp_client=httpx.Client(transport=httpx.MockTransport(gp_handler)),
            cache=Cache(
                effective.cache_base, effective.data_base / "cache", ttls=effective.cache_ttls
            ),
        )
        created.append(state)
        return state

    yield factory
    for state in created:
        state.gp_client.close()


def _call(mcp: FastMCP, name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Call one tool through an in-memory client and return its structured result."""

    async def run() -> Any:
        async with Client(mcp) as client:
            result = await client.call_tool(name, arguments or {})
            return result.data

    return asyncio.run(run())


def _sample_biblio(**overrides: Any) -> OpsBiblio:
    """Build a minimal OpsBiblio for the biblio-shaped tests."""
    fields: dict[str, Any] = {
        "pub": "US.11468338.B2",
        "family_id": "100",
        "title": "t",
        "abstract": "a",
        "applicants": ("Acme",),
        "inventors": ("Doe",),
        "ipc": (),
        "cpc": (),
        "publication_date": "20220101",
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


# --- tool inventory ------------------------------------------------------


def test_list_tools_matches_tool_names(settings: ServerSettings, make_state: Any) -> None:
    """The server registers exactly the twelve documented tools."""
    mcp = build_server(settings, state=make_state())

    async def run() -> set[str]:
        async with Client(mcp) as client:
            return {tool.name for tool in await client.list_tools()}

    names = asyncio.run(run())
    assert names == set(tools.TOOL_NAMES)
    assert len(tools.TOOL_NAMES) == 12


def test_every_tool_description_states_what_it_returns(
    settings: ServerSettings, make_state: Any
) -> None:
    """The description is all the calling LLM reads, so it must carry the result shape.

    FastMCP drops a docstring's ``Returns:`` section from the description,
    which is why the tools describe their result in the docstring body; this
    test fails if that is ever "tidied up" back into a Returns section.
    """
    mcp = build_server(settings, state=make_state())

    async def run() -> dict[str, str | None]:
        async with Client(mcp) as client:
            return {tool.name: tool.description for tool in await client.list_tools()}

    descriptions = asyncio.run(run())
    assert set(descriptions) == set(tools.TOOL_NAMES)
    for name, description in descriptions.items():
        assert description, f"{name} has no description"
        assert "Returns" in description, f"{name} does not say what it returns"


@pytest.mark.parametrize(
    "name",
    [
        "ops_search",
        "ops_search_biblio",
        "search_plan_check",
        "get_biblio",
        "get_claims",
        "get_legal",
        "get_family",
    ],
)
def test_cached_results_are_documented_for_the_cached_tools(
    settings: ServerSettings, make_state: Any, name: str
) -> None:
    """A caller must be able to tell a cached answer from a fresh upstream one."""
    mcp = build_server(settings, state=make_state())

    async def run() -> str | None:
        async with Client(mcp) as client:
            return {tool.name: tool.description for tool in await client.list_tools()}[name]

    description = asyncio.run(run())
    assert description is not None
    assert "cached" in description


# --- happy paths ---------------------------------------------------------


def test_ops_search_returns_the_service_page(
    monkeypatch: pytest.MonkeyPatch, settings: ServerSettings, make_state: Any
) -> None:
    """ops_search forwards the paging window and returns the CLI's search keys."""
    page = OpsSearchPage(
        total_count=2,
        query="ti=drone",
        begin=1,
        end=25,
        hits=(OpsSearchHit(pub="US.1.A1", family_id="100"),),
    )
    monkeypatch.setattr(service, "parse_search_xml", lambda xml: page)
    stub = _StubOpsClient(search=b"<xml/>")
    state = make_state(ops_client=stub)
    mcp = build_server(settings, state=state)

    data = _call(mcp, "ops_search", {"cql": "ti=drone", "begin": 1, "end": 25})

    assert data["query"] == "ti=drone"
    assert data["total"] == 2
    assert data["hits"] == [{"pub": "US.1.A1", "family_id": "100"}]
    assert data["raw_path"] == str(
        state.cache.content_path("search", search_key("ti=drone", 1, 25))
    )
    assert stub.calls == [("search", ("ti=drone",), {"begin": 1, "end": 25})]


def test_ops_search_biblio_returns_docs(
    monkeypatch: pytest.MonkeyPatch, settings: ServerSettings, make_state: Any
) -> None:
    """ops_search_biblio returns one full biblio record per hit."""
    page = OpsSearchBiblioPage(total_count=1, begin=1, end=25, docs=(_sample_biblio(),))
    monkeypatch.setattr(service, "parse_search_biblio_xml", lambda xml: page)
    stub = _StubOpsClient(search_biblio=b"<xml/>")
    state = make_state(ops_client=stub)
    mcp = build_server(settings, state=state)

    data = _call(mcp, "ops_search_biblio", {"cql": "ti=drone"})

    assert data["total"] == 1
    assert data["docs"][0]["pub"] == "US.11468338.B2"
    assert data["docs"][0]["applicants"] == ["Acme"]
    assert data["raw_path"] == str(
        state.cache.content_path("searchbib", search_key("ti=drone", 1, 25))
    )
    assert stub.calls == [("search_biblio", ("ti=drone",), {"begin": 1, "end": 25})]


def test_search_plan_check_forwards_queries_and_budget(
    monkeypatch: pytest.MonkeyPatch, settings: ServerSettings, make_state: Any
) -> None:
    """search_plan_check hands the queries, the budget, the client and the cache to the service."""
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
        return {
            "results": [{"query": queries[0], "total": 7}],
            "total_sum": 7,
            "exceeded": False,
            "max_total": max_total,
        }

    monkeypatch.setattr(service, "search_plan_check", fake_plan_check)
    stub = _StubOpsClient()
    state = make_state(ops_client=stub)
    mcp = build_server(settings, state=state)

    data = _call(mcp, "search_plan_check", {"queries": ["ti=drone"], "max_total": 100})

    assert data == {
        "results": [{"query": "ti=drone", "total": 7}],
        "total_sum": 7,
        "exceeded": False,
        "max_total": 100,
    }
    assert captured["queries"] == ["ti=drone"]
    assert captured["client"] is stub
    assert captured["max_total"] == 100
    assert captured["cache"] is state.cache
    assert captured["refresh"] is False


def test_get_biblio_returns_biblio_fields(
    monkeypatch: pytest.MonkeyPatch, settings: ServerSettings, make_state: Any
) -> None:
    """get_biblio returns the OpsBiblio fields plus the raw path."""
    monkeypatch.setattr(service, "parse_biblio_xml", lambda xml: _sample_biblio())
    stub = _StubOpsClient(biblio=b"<xml/>")
    state = make_state(ops_client=stub)
    mcp = build_server(settings, state=state)

    data = _call(mcp, "get_biblio", {"pub": "US11468338B2"})

    assert data["pub"] == "US.11468338.B2"
    assert data["title"] == "t"
    assert data["raw_path"] == str(state.cache.content_path("biblio", pub_key("US11468338B2")))
    assert stub.calls == [("biblio", ("US11468338B2",), {})]


def test_get_legal_returns_events(
    monkeypatch: pytest.MonkeyPatch, settings: ServerSettings, make_state: Any
) -> None:
    """get_legal returns the docdb spelling and one entry per legal event."""
    event = OpsLegalEvent(code="PG25", desc="Lapsed", gazette_date="20240101", pre_lines=())
    monkeypatch.setattr(service, "parse_legal_xml", lambda xml: (event,))
    stub = _StubOpsClient(legal=b"<xml/>")
    mcp = build_server(settings, state=make_state(ops_client=stub))

    data = _call(mcp, "get_legal", {"pub": "US11468338B2"})

    assert data["pub"] == "US.11468338.B2"
    assert data["events"] == [
        {"code": "PG25", "desc": "Lapsed", "gazette_date": "20240101", "pre_lines": []}
    ]


def test_get_family_returns_members(
    monkeypatch: pytest.MonkeyPatch, settings: ServerSettings, make_state: Any
) -> None:
    """get_family returns the family id and its member publications."""
    monkeypatch.setattr(
        service, "parse_family_xml", lambda xml: OpsFamily(family_id="1", members=("US.1.A1",))
    )
    stub = _StubOpsClient(family=b"<xml/>")
    mcp = build_server(settings, state=make_state(ops_client=stub))

    data = _call(mcp, "get_family", {"pub": "US11468338B2"})

    assert data["family_id"] == "1"
    assert data["members"] == ["US.1.A1"]


def test_get_claims_uses_the_shared_google_patents_client(
    monkeypatch: pytest.MonkeyPatch, settings: ServerSettings, make_state: Any
) -> None:
    """get_claims takes the Google Patents route and passes the state's GP client."""
    captured: dict[str, Any] = {}

    def fake_fetch(
        pub: str, *, force: bool = False, client: Any = None, cache: Any = None
    ) -> FetchedPage:
        captured["pub"] = pub
        captured["client"] = client
        captured["cache"] = cache
        return FetchedPage(pub=pub, html="<html></html>", path=None, cached=False)

    monkeypatch.setattr(service, "fetch_patent_html", fake_fetch)
    monkeypatch.setattr(service, "parse_patent_html", lambda html: _sample_gp_doc())
    state = make_state(ops_client=_StubOpsClient())
    mcp = build_server(settings, state=state)

    data = _call(mcp, "get_claims", {"pub": "US11468338B2"})

    assert data["source"] == "gp"
    assert data["claims"] == [{"number": 1, "text": "1. A widget.", "depends_on": []}]
    assert captured["client"] is state.gp_client
    assert captured["cache"] is state.cache


def test_normalize_pubnum_returns_every_spelling(settings: ServerSettings, make_state: Any) -> None:
    """normalize_pubnum returns the docdb/epodoc/google spellings of one number."""
    mcp = build_server(settings, state=make_state())

    data = _call(mcp, "normalize_pubnum", {"text": "us 11468338 b2"})

    assert data["country"] == "US"
    assert data["docdb"] == "US.11468338.B2"
    assert data["google"] == "US11468338B2"


def test_dedup_families_collapses_hits(settings: ServerSettings, make_state: Any) -> None:
    """dedup_families returns one record per family plus the count."""
    mcp = build_server(settings, state=make_state())

    data = _call(
        mcp,
        "dedup_families",
        {
            "hits": [
                {"pub": "US11468338B2", "family_id": "100"},
                {"pub": "EP1672502A1", "family_id": "100"},
            ]
        },
    )

    assert data["count"] == 1
    assert len(data["families"]) == 1


def test_verify_batch_reports_missing_publications(
    settings: ServerSettings, make_state: Any
) -> None:
    """verify_batch cross-checks a batch's output against its input list."""
    mcp = build_server(settings, state=make_state())

    data = _call(
        mcp,
        "verify_batch",
        {
            "input_pubs": ["US11468338B2", "EP1672502A1"],
            "output_records": [{"pub": "US.11468338.B2"}],
        },
    )

    assert data["ok"] is False
    assert data["missing"] == ["EP.1672502.A1"]


def test_usage_report_reads_the_servers_headers_log(
    settings: ServerSettings, make_state: Any, tmp_path: Path
) -> None:
    """usage_report summarizes the header log under the server's own data directory."""
    log_dir = tmp_path / "raw" / "ops"
    log_dir.mkdir(parents=True)
    record = {
        "at": "2026-09-01T10:00:00",
        "kind": "biblio",
        "url": "published-data/publication/docdb/US.1.A1/biblio",
        "status": 200,
        "throttling": "idle (retrieval=green:200)",
    }
    (log_dir / "headers.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    mcp = build_server(settings, state=make_state())

    data = _call(mcp, "usage_report")

    assert data["available"] is True
    assert data["total_requests"] == 1
    assert data["by_kind"] == {"biblio": 1}


def test_usage_report_without_a_log_is_not_an_error(
    settings: ServerSettings, make_state: Any, tmp_path: Path
) -> None:
    """A server that has not called OPS yet reports the log as unavailable."""
    mcp = build_server(settings, state=make_state())

    data = _call(mcp, "usage_report")

    assert data["available"] is False
    assert data["path"] == str(tmp_path / "raw" / "ops" / "headers.jsonl")


def test_server_status_reports_the_running_configuration(
    settings: ServerSettings, make_state: Any, tmp_path: Path
) -> None:
    """server_status exposes version, transport, directories and the notice version."""
    state = make_state(ops_client=_StubOpsClient())
    mcp = build_server(settings, state=state)

    data = _call(mcp, "server_status")

    assert data == {
        "version": importlib.metadata.version("patent-checker"),
        "ops_configured": True,
        "transport": "stdio",
        "data_dir": str(tmp_path),
        "cache_dir": str(state.cache.shared),
        "search_cache_dir": str(state.cache.local),
        "cache_ttl": {kind: cache.format_ttl(ttl) for kind, ttl in cache.DEFAULT_TTLS.items()},
        "cache_entries": {kind: 0 for kind in cache.KINDS},
        "operator_notice_version": OPERATOR_NOTICE_VERSION,
        "log_level": "info",
    }


def test_server_status_reports_cache_entries_and_ttl_overrides(
    monkeypatch: pytest.MonkeyPatch, settings: ServerSettings, make_state: Any
) -> None:
    """A cached entry is counted and a TTL override is reflected in cache_ttl."""
    monkeypatch.setenv("PATENT_CHECKER_CACHE_TTL", "legal=1d")
    overridden_settings = load_settings(transport="stdio")
    state = make_state(ops_client=_StubOpsClient(), state_settings=overridden_settings)
    state.cache.put("biblio", pub_key("US11468338B2"), b"<xml/>", ident="US11468338B2")
    mcp = build_server(overridden_settings, state=state)

    data = _call(mcp, "server_status")

    assert data["cache_entries"]["biblio"] == 1
    assert data["cache_ttl"]["legal"] == "1d"
    assert data["cache_ttl"]["biblio"] == "90d"


def test_server_status_reports_the_configured_log_level(
    monkeypatch: pytest.MonkeyPatch, make_state: Any
) -> None:
    """server_status reflects a non-default ``PATENT_CHECKER_LOG_LEVEL``."""
    monkeypatch.setenv("PATENT_CHECKER_LOG_LEVEL", "debug")
    overridden_settings = load_settings(transport="stdio")
    state = make_state(ops_client=_StubOpsClient(), state_settings=overridden_settings)
    mcp = build_server(overridden_settings, state=state)

    data = _call(mcp, "server_status")

    assert data["log_level"] == "debug"


# --- error mapping -------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("get_biblio", {"pub": "not a publication"}),
        ("get_legal", {"pub": ""}),
        ("get_family", {"pub": "   "}),
        ("get_claims", {"pub": "not a publication"}),
        ("normalize_pubnum", {"text": "not a publication"}),
        ("ops_search", {"cql": ""}),
        ("ops_search", {"cql": "   "}),
        ("ops_search", {"cql": "a" * (tools.MAX_CQL_LENGTH + 1)}),
        ("ops_search", {"cql": "ti=drone\x00"}),
        ("ops_search_biblio", {"cql": "ti=drone\x7f"}),
        ("search_plan_check", {"queries": []}),
        ("search_plan_check", {"queries": ["ti=drone"] * (tools.MAX_QUERIES + 1)}),
        ("search_plan_check", {"queries": [""]}),
    ],
)
def test_invalid_input_is_reported_as_invalid_input(
    settings: ServerSettings, make_state: Any, name: str, arguments: dict[str, Any]
) -> None:
    """Every locally detectable input problem reaches the client as ``invalid_input``."""
    stub = _StubOpsClient()
    mcp = build_server(settings, state=make_state(ops_client=stub))

    with pytest.raises(ToolError) as exc_info:
        _call(mcp, name, arguments)

    assert str(exc_info.value).startswith(f"{tools.ERROR_INVALID_INPUT}:")
    # Validation happens before the service layer, so nothing was requested.
    assert stub.calls == []


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("dedup_families", {"hits": [{"pub": "US11468338B2", "family_id": "1"}]}),
        ("verify_batch", {"input_pubs": ["US11468338B2"], "output_records": []}),
        ("verify_batch", {"input_pubs": [], "output_records": ["US11468338B2"]}),
    ],
)
def test_oversized_offline_payloads_are_rejected(
    settings: ServerSettings, make_state: Any, name: str, arguments: dict[str, Any]
) -> None:
    """A list longer than MAX_RECORDS is refused instead of being processed."""
    oversized = {
        key: value * (tools.MAX_RECORDS + 1) if value else value for key, value in arguments.items()
    }
    mcp = build_server(settings, state=make_state())

    with pytest.raises(ToolError) as exc_info:
        _call(mcp, name, oversized)

    assert str(exc_info.value).startswith(f"{tools.ERROR_INVALID_INPUT}:")


def test_an_oversized_element_is_rejected(settings: ServerSettings, make_state: Any) -> None:
    """One huge element is refused even when the element count is well within the limit."""
    mcp = build_server(settings, state=make_state())

    with pytest.raises(ToolError) as exc_info:
        _call(
            mcp,
            "verify_batch",
            {"input_pubs": ["x" * (validation.MAX_ITEM_CHARS + 1)], "output_records": []},
        )

    assert str(exc_info.value).startswith(f"{tools.ERROR_INVALID_INPUT}:")


def test_an_oversized_total_payload_is_rejected(settings: ServerSettings, make_state: Any) -> None:
    """Many acceptable elements that together exceed the payload ceiling are refused."""
    item = "x" * 4000
    count = validation.MAX_PAYLOAD_CHARS // len(item) + 1
    mcp = build_server(settings, state=make_state())

    with pytest.raises(ToolError) as exc_info:
        _call(mcp, "verify_batch", {"input_pubs": [item] * count, "output_records": []})

    assert str(exc_info.value).startswith(f"{tools.ERROR_INVALID_INPUT}:")


def test_a_record_with_a_non_string_pub_is_invalid_input(
    settings: ServerSettings, make_state: Any
) -> None:
    """A mapping whose ``pub`` is not a string is the caller's mistake, not an internal error."""
    mcp = build_server(settings, state=make_state())

    with pytest.raises(ToolError) as exc_info:
        _call(mcp, "dedup_families", {"hits": [{"pub": 5, "family_id": "1"}]})

    message = str(exc_info.value)
    assert message.startswith(f"{tools.ERROR_INVALID_INPUT}:")
    assert "pub" in message


def test_records_missing_a_required_key_are_invalid_input(
    settings: ServerSettings, make_state: Any
) -> None:
    """A hit without ``"pub"`` is the caller's mistake, reported as invalid_input, not masked."""
    mcp = build_server(settings, state=make_state())

    with pytest.raises(ToolError) as exc_info:
        _call(mcp, "dedup_families", {"hits": [{"family_id": "1"}]})

    message = str(exc_info.value)
    assert message.startswith(f"{tools.ERROR_INVALID_INPUT}:")
    assert "pub" in message


def test_range_errors_from_the_client_are_invalid_input(
    settings: ServerSettings, make_state: Any
) -> None:
    """The OPS range pre-validation (ValueError) is mapped like any other input error."""
    stub = _StubOpsClient(search=ValueError("invalid Range 1-9999"))
    mcp = build_server(settings, state=make_state(ops_client=stub))

    with pytest.raises(ToolError) as exc_info:
        _call(mcp, "ops_search", {"cql": "ti=drone", "begin": 1, "end": 9999})

    assert str(exc_info.value).startswith(f"{tools.ERROR_INVALID_INPUT}:")


def test_transport_failures_are_reported_as_external_api_errors(
    settings: ServerSettings, make_state: Any
) -> None:
    """An httpx failure from OPS becomes ``external_api_error`` with its message."""
    stub = _StubOpsClient(search=httpx.ConnectError("connection refused"))
    mcp = build_server(settings, state=make_state(ops_client=stub))

    with pytest.raises(ToolError) as exc_info:
        _call(mcp, "ops_search", {"cql": "ti=drone"})

    message = str(exc_info.value)
    assert message.startswith(f"{tools.ERROR_EXTERNAL_API}:")
    assert "connection refused" in message


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("ops_search", {"cql": "ti=drone"}),
        ("ops_search_biblio", {"cql": "ti=drone"}),
        ("search_plan_check", {"queries": ["ti=drone"]}),
        ("get_biblio", {"pub": "US11468338B2"}),
        ("get_legal", {"pub": "US11468338B2"}),
        ("get_family", {"pub": "US11468338B2"}),
    ],
)
def test_ops_tools_without_credentials_report_ops_not_configured(
    settings: ServerSettings, make_state: Any, name: str, arguments: dict[str, Any]
) -> None:
    """Without an OPS client every OPS-backed tool fails with ``ops_not_configured``."""
    mcp = build_server(settings, state=make_state(ops_client=None))

    with pytest.raises(ToolError) as exc_info:
        _call(mcp, name, arguments)

    assert str(exc_info.value).startswith(f"{tools.ERROR_OPS_NOT_CONFIGURED}:")


def test_offline_and_google_patents_tools_still_work_without_ops(
    monkeypatch: pytest.MonkeyPatch, settings: ServerSettings, make_state: Any
) -> None:
    """Degraded mode: the non-OPS tools keep working and status says so."""
    # The Google Patents pacing global is process-wide; reset it so this test
    # never sleeps for the courtesy interval left over from another test.
    monkeypatch.setattr(gp_fetch, "_last_request_at", None)
    mcp = build_server(settings, state=make_state(ops_client=None))

    claims = _call(mcp, "get_claims", {"pub": "US11468338B2"})
    assert claims["unavailable"] is True
    assert claims["pub"] == "US11468338B2"

    assert _call(mcp, "normalize_pubnum", {"text": "US11468338B2"})["docdb"] == "US.11468338.B2"
    assert _call(mcp, "dedup_families", {"hits": []})["count"] == 0
    assert _call(mcp, "verify_batch", {"input_pubs": [], "output_records": []})["ok"] is True
    assert _call(mcp, "usage_report")["available"] is False
    assert _call(mcp, "server_status")["ops_configured"] is False


# --- cache, throttling and locking ---------------------------------------


def test_repeated_search_is_served_from_the_cache(
    monkeypatch: pytest.MonkeyPatch, settings: ServerSettings, make_state: Any
) -> None:
    """The second identical search is answered from disk and marked ``cached``."""
    page = OpsSearchPage(total_count=0, query="ti=drone", begin=1, end=25, hits=())
    monkeypatch.setattr(service, "parse_search_xml", lambda xml: page)
    stub = _StubOpsClient(search=b"<xml/>")
    mcp = build_server(settings, state=make_state(ops_client=stub))

    first = _call(mcp, "ops_search", {"cql": "ti=drone"})
    second = _call(mcp, "ops_search", {"cql": "ti=drone"})

    assert "cached" not in first
    assert second["cached"] is True
    assert len(stub.calls) == 1


def test_repeated_plan_check_query_is_served_from_the_cache(
    monkeypatch: pytest.MonkeyPatch, settings: ServerSettings, make_state: Any
) -> None:
    """The second search_plan_check call for the same query does not reach OPS.

    search_plan_check shares its cache entry with a Range=1-2 ops_search/search
    call for the same query, so this exercises the same on-disk cache as
    test_repeated_search_is_served_from_the_cache above.
    """
    page = OpsSearchPage(total_count=3, query="ti=drone", begin=1, end=2, hits=())
    monkeypatch.setattr(utils, "parse_search_xml", lambda xml: page)
    stub = _StubOpsClient(search=b"<xml/>")
    mcp = build_server(settings, state=make_state(ops_client=stub))

    first = _call(mcp, "search_plan_check", {"queries": ["ti=drone"]})
    second = _call(mcp, "search_plan_check", {"queries": ["ti=drone"]})

    assert first["results"] == [{"query": "ti=drone", "total": 3}]
    assert second["results"] == [{"query": "ti=drone", "total": 3, "cached": True}]
    assert len(stub.calls) == 1


def test_rate_limit_rejects_immediate_repeat_calls(
    monkeypatch: pytest.MonkeyPatch, make_state: Any
) -> None:
    """With rps=1/burst=1 a burst of calls is refused by the rate-limiting middleware.

    The MCP session handshake itself passes through the middleware and
    consumes the bucket, so with a burst capacity of one even the first tool
    call may already be rejected; what is asserted here is that a second
    immediate call never gets through, and that the client sees FastMCP's
    ``MCPError`` naming the rate limit (not a masked internal error).
    """
    monkeypatch.setenv("PATENT_CHECKER_SERVER_RPS", "1")
    monkeypatch.setenv("PATENT_CHECKER_SERVER_BURST", "1")
    throttled_settings = load_settings(transport="stdio")
    mcp = build_server(throttled_settings, state=make_state(state_settings=throttled_settings))

    async def run() -> list[Exception | None]:
        outcomes: list[Exception | None] = []
        async with Client(mcp) as client:
            for _ in range(2):
                try:
                    await client.call_tool("server_status", {})
                    outcomes.append(None)
                except MCPError as exc:
                    outcomes.append(exc)
        return outcomes

    outcomes = asyncio.run(run())

    last = outcomes[-1]
    assert isinstance(last, MCPError)
    assert "rate limit" in str(last).lower()


def test_concurrent_ops_calls_keep_the_upstream_interval(
    monkeypatch: pytest.MonkeyPatch, settings: ServerSettings, make_state: Any
) -> None:
    """Two concurrent OPS tool calls still leave the OPS minimum interval between requests.

    End-to-end check of the pacing requirement with a real ``OpsClient``
    (against a MockTransport): whatever the server does with its threads, two
    biblio requests must not leave the process less than
    ``MIN_INTERVAL_SECONDS["retrieval"]`` apart. The ops lock additionally
    closes the race window inside ``_wait_for_service`` (both threads reading
    the last-request time before either writes it); that the lock serializes
    the calls at all is asserted separately below.
    """
    monkeypatch.setenv("PATENT_CHECKER_OPS_KEY", "dummy-key")
    monkeypatch.setenv("PATENT_CHECKER_OPS_SECRET", "dummy-secret")
    monkeypatch.setattr(service, "parse_biblio_xml", lambda xml: _sample_biblio())
    request_times: list[float] = []
    times_lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/accesstoken"):
            return httpx.Response(200, json=_TOKEN_JSON)
        with times_lock:
            request_times.append(time.monotonic())
        return httpx.Response(200, content=b"<biblio/>")

    ops = OpsClient(transport=httpx.MockTransport(handler))
    mcp = build_server(settings, state=make_state(ops_client=ops))

    async def run() -> None:
        async with Client(mcp) as client:
            await asyncio.gather(
                client.call_tool("get_biblio", {"pub": "US11468338B2"}),
                client.call_tool("get_biblio", {"pub": "EP1672502A1"}),
            )

    try:
        asyncio.run(run())
    finally:
        ops.close()

    assert len(request_times) == 2
    gap = abs(request_times[1] - request_times[0])
    assert gap >= MIN_INTERVAL_SECONDS["retrieval"] - 0.1


def test_concurrent_ops_tools_do_not_overlap(
    monkeypatch: pytest.MonkeyPatch, settings: ServerSettings, make_state: Any
) -> None:
    """One shared lock keeps every OPS-backed tool call sequential, across tools.

    FastMCP runs sync tools in worker threads and does execute two calls at
    once (verified separately), so a maximum overlap of one proves the lock.
    """
    active = {"current": 0, "max": 0}
    active_lock = threading.Lock()

    def recording_call(pub: str, **kwargs: Any) -> dict[str, Any]:
        """Stand in for a service call, recording how many run at once."""
        with active_lock:
            active["current"] += 1
            active["max"] = max(active["max"], active["current"])
        time.sleep(0.2)
        with active_lock:
            active["current"] -= 1
        return {"pub": pub}

    monkeypatch.setattr(service, "biblio", recording_call)
    monkeypatch.setattr(service, "family", recording_call)
    mcp = build_server(settings, state=make_state(ops_client=_StubOpsClient()))

    async def run() -> None:
        async with Client(mcp) as client:
            await asyncio.gather(
                client.call_tool("get_biblio", {"pub": "US11468338B2"}),
                client.call_tool("get_family", {"pub": "EP1672502A1"}),
            )

    asyncio.run(run())

    assert active["max"] == 1


def test_concurrent_claims_calls_do_not_overlap(
    monkeypatch: pytest.MonkeyPatch, settings: ServerSettings, make_state: Any
) -> None:
    """The Google Patents lock keeps two concurrent get_claims calls strictly sequential.

    FastMCP runs sync tools in worker threads and does execute two calls at
    once (verified separately), so a maximum overlap of one proves the lock.
    """
    active = {"current": 0, "max": 0}
    active_lock = threading.Lock()

    def fake_fetch(
        pub: str, *, force: bool = False, client: Any = None, cache: Any = None
    ) -> FetchedPage:
        with active_lock:
            active["current"] += 1
            active["max"] = max(active["max"], active["current"])
        time.sleep(0.2)
        with active_lock:
            active["current"] -= 1
        return FetchedPage(pub=pub, html="<html></html>", path=None, cached=False)

    monkeypatch.setattr(service, "fetch_patent_html", fake_fetch)
    monkeypatch.setattr(service, "parse_patent_html", lambda html: _sample_gp_doc())
    mcp = build_server(settings, state=make_state())

    async def run() -> None:
        async with Client(mcp) as client:
            await asyncio.gather(
                client.call_tool("get_claims", {"pub": "US11468338B2"}),
                client.call_tool("get_claims", {"pub": "EP1672502A1"}),
            )

    asyncio.run(run())

    assert active["max"] == 1


# --- state lifecycle and startup surface ---------------------------------


def test_build_state_without_credentials_has_no_ops_client(settings: ServerSettings) -> None:
    """Without OPS credentials the state runs in degraded mode, but GP still works."""
    state = build_state(settings)
    try:
        assert state.ops_client is None
        assert state.cache.base == settings.data_base / "cache"
        # The guard refuses non-allowlisted hosts before any connection is made.
        with pytest.raises(HostNotAllowedError):
            state.gp_client.get("https://example.com/")
    finally:
        close_state(state)

    assert state.gp_client.is_closed


def test_build_state_wraps_both_transports_in_the_allowlist_guard(
    monkeypatch: pytest.MonkeyPatch, settings: ServerSettings
) -> None:
    """Supplied transports are used, but only through the allowlist guard."""
    monkeypatch.setenv("PATENT_CHECKER_OPS_KEY", "dummy-key")
    monkeypatch.setenv("PATENT_CHECKER_OPS_SECRET", "dummy-secret")
    gp_transport = httpx.MockTransport(lambda request: httpx.Response(200, text="<html/>"))
    ops_transport = httpx.MockTransport(lambda request: httpx.Response(200, json=_TOKEN_JSON))

    state = build_state(settings, ops_transport=ops_transport, gp_transport=gp_transport)
    try:
        assert state.ops_client is not None
        assert isinstance(state.gp_client._transport, AllowlistTransport)
        # The OPS client's inner transport is wrapped as well (private access:
        # the client offers no public view of its transport).
        assert isinstance(state.ops_client._client._transport, AllowlistTransport)
        assert state.gp_client.get("https://patents.google.com/patent/US1A/en").status_code == 200
        with pytest.raises(HostNotAllowedError):
            state.gp_client.get("https://example.com/")
    finally:
        close_state(state)

    assert state.gp_client.is_closed
    assert state.ops_client is not None
    assert state.ops_client._client.is_closed


def test_http_allowed_hosts_lists_the_bind_host_and_the_configured_hosts(
    settings: ServerSettings,
) -> None:
    """Only bare host names are listed: FastMCP strips the port before comparing."""
    http_settings = dataclasses.replace(
        settings, transport="http", host="127.0.0.1", port=8642, allowed_hosts=("a.example",)
    )

    assert http_allowed_hosts(http_settings) == ["127.0.0.1", "a.example"]


def test_http_allowed_hosts_does_not_append_the_port(settings: ServerSettings) -> None:
    """A ``host:port`` entry would be pointless and would break IPv6 literals."""
    http_settings = dataclasses.replace(
        settings, transport="http", host="::1", port=8642, allowed_hosts=("a.example",)
    )

    assert all(":8642" not in entry for entry in http_allowed_hosts(http_settings))


def test_http_allowed_hosts_keeps_the_configuration_order_without_duplicates(
    settings: ServerSettings,
) -> None:
    """A configured host equal to the bind host is listed once, in configuration order."""
    http_settings = dataclasses.replace(
        settings,
        transport="http",
        host="127.0.0.1",
        port=8642,
        allowed_hosts=("a.example", "127.0.0.1"),
    )

    assert http_allowed_hosts(http_settings) == ["127.0.0.1", "a.example"]


def test_banner_lines_never_leak_the_token(settings: ServerSettings) -> None:
    """The startup banner describes the configuration but never prints the token."""
    http_settings = dataclasses.replace(
        settings,
        transport="http",
        host="0.0.0.0",  # noqa: S104 - deliberately non-loopback, to trigger the TLS warning
        allowed_hosts=("a.example",),
        token="tok-secret-value",
    )

    lines = banner_lines(http_settings, ops_configured=True)

    joined = "\n".join(lines)
    assert "tok-secret-value" not in joined
    assert "ops_configured" in joined
    assert "TLS" in joined


def test_banner_lines_warn_about_the_stdio_trust_model(settings: ServerSettings) -> None:
    """Under stdio there is no authentication, which the banner states plainly."""
    lines = banner_lines(settings, ops_configured=False)

    joined = "\n".join(lines)
    assert "stdio" in joined
    assert "no authentication" in joined
    assert "ops_configured: False" in joined


def test_banner_lines_show_the_tls_notice_even_for_a_loopback_bind(
    settings: ServerSettings,
) -> None:
    """The TLS notice is unconditional for http, unlike the old loopback-only wording."""
    http_settings = dataclasses.replace(
        settings, transport="http", host="127.0.0.1", token="tok-secret-value"
    )

    lines = banner_lines(http_settings, ops_configured=True)

    joined = "\n".join(lines)
    assert (
        "TLS: not provided by this server; put a reverse proxy in front if the port is "
        "reachable from other machines" in joined
    )


# --- uvicorn logging configuration ----------------------------------------


def test_uvicorn_log_config_redirects_both_handlers_to_stderr() -> None:
    """Both the default and access-log uvicorn handlers write to stderr."""
    result = server_app.uvicorn_log_config("info")

    assert result["handlers"]["default"]["stream"] == "ext://sys.stderr"
    assert result["handlers"]["access"]["stream"] == "ext://sys.stderr"


def test_uvicorn_log_config_applies_the_level_to_the_three_loggers() -> None:
    """The resolved level is applied to the uvicorn/uvicorn.error/uvicorn.access loggers."""
    result = server_app.uvicorn_log_config("warning")

    assert result["loggers"]["uvicorn"]["level"] == "WARNING"
    assert result["loggers"]["uvicorn.error"]["level"] == "WARNING"
    assert result["loggers"]["uvicorn.access"]["level"] == "WARNING"


def test_uvicorn_log_config_does_not_mutate_uvicorn_s_own_default() -> None:
    """The shared ``uvicorn.config.LOGGING_CONFIG`` default is never mutated."""
    import copy

    import uvicorn.config

    before = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)

    server_app.uvicorn_log_config("error")

    assert uvicorn.config.LOGGING_CONFIG == before


# --- run() ------------------------------------------------------------------


def test_run_passes_log_level_and_uvicorn_log_config_for_http(
    monkeypatch: pytest.MonkeyPatch, settings: ServerSettings
) -> None:
    """The http transport's ``mcp.run()`` call carries the resolved log level and log config."""
    http_settings = dataclasses.replace(
        settings, transport="http", token="tok-secret-value", log_level="warning"
    )
    calls: list[dict[str, Any]] = []

    class _FakeMcp:
        def run(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(server_app, "build_server", lambda settings: _FakeMcp())

    server_app.run(http_settings)

    assert len(calls) == 1
    assert calls[0]["log_level"] == "warning"
    assert calls[0]["uvicorn_config"] == {"log_config": server_app.uvicorn_log_config("warning")}
