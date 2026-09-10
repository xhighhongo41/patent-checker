"""Tests for the CLI (patent_checker/cli/main.py).

Network-free: :class:`~patent_checker.ops.client.OpsClient`, the ops/gp parse
functions and the ``utils`` helpers are monkeypatched with canned stand-ins.
Only the JSON shape on stdout and the process exit code are checked here (CLI
is I/O conversion only; the underlying logic is tested where it is
implemented).
"""

from __future__ import annotations

import errno
import io
import json
import os
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import dotenv
import httpx
import pytest

import patent_checker.server.app as server_app
import patent_checker.server.settings as server_settings
import patent_checker.server.tools as server_tools
from patent_checker import consent, installer, service
from patent_checker.cache import Cache, default_cache
from patent_checker.cli import main as cli_main
from patent_checker.config import ConfigError
from patent_checker.gp.fetch import FetchedPage, GPUnavailable
from patent_checker.gp.parse import GPatentDoc
from patent_checker.models import Claim
from patent_checker.ops.parse import (
    OpsBiblio,
    OpsFamily,
    OpsLegalEvent,
    OpsSearchHit,
    OpsSearchPage,
)


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep CLI tests hermetic: no file cache reads or writes.

    Without a cache nothing stores the response body, so every ``raw_path``
    below is ``null``; the paths themselves are covered where the cache is.
    """
    monkeypatch.setattr(cli_main, "_cache", lambda: None)


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``main()``'s ``.env`` read away from the developer's own file.

    ``main()`` loads the nearest ``.env`` for every subcommand (v1.0); the
    test suite runs from the repository, whose ``.env`` holds real OPS
    credentials. Replacing the loader keeps them out of ``os.environ``. The
    test that covers the ``.env`` behaviour itself puts the real loader back
    and works in ``tmp_path``.
    """
    monkeypatch.setattr(cli_main.config, "load_dotenv", lambda *args, **kwargs: False)


@pytest.fixture
def restore_environ() -> Iterator[None]:
    """Undo whatever a test lets ``python-dotenv`` write into ``os.environ``.

    ``load_dotenv`` sets variables directly, so ``monkeypatch`` cannot track
    them; the whole environment is snapshotted and put back instead.
    """
    saved = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(saved)


class _PlainStream:
    """A minimal stdout/stderr stand-in: collects what was written to it."""

    def __init__(self) -> None:
        self.text = ""

    def write(self, text: str) -> int:
        """Collect what was printed."""
        self.text += text
        return len(text)

    def flush(self) -> None:
        """Accept flushes; nothing is buffered."""


class _RecordingStream(_PlainStream):
    """A stream that records every ``reconfigure`` call, optionally refusing it.

    Args:
        fails: Raise instead of accepting the new encoding, like a stream
            whose encoding cannot be changed any more.
    """

    def __init__(self, *, fails: bool = False) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []
        self._fails = fails

    def reconfigure(self, **kwargs: Any) -> None:
        """Record the request, or refuse it like a stream that cannot be re-encoded."""
        self.calls.append(kwargs)
        if self._fails:
            raise ValueError("cannot reconfigure this stream")


def _invoke(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    """Run ``main(argv)``, tolerating either a return code or a SystemExit.

    Both are used across the CLI: handler-level errors return a code from
    ``main()``, while argparse's own errors (via ``_JsonArgumentParser``)
    raise ``SystemExit``. Either way stdout carries the same JSON envelope.
    """
    try:
        rc = cli_main.main(argv)
    except SystemExit as exc:
        rc = exc.code
    out = capsys.readouterr().out
    return rc, json.loads(out)


def _invoke_with_stderr(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, Any], str]:
    """Run ``main(argv)`` like :func:`_invoke`, also returning what went to stderr.

    Used by the input-validation tests: a rejected payload must be reported
    through the JSON envelope, never as a Python traceback.
    """
    try:
        rc = cli_main.main(argv)
    except SystemExit as exc:
        rc = exc.code
    captured = capsys.readouterr()
    return rc, json.loads(captured.out), captured.err


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


# --- search / search-biblio / plan-check ------------------------------------


def test_search_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A successful search prints the OpsSearchPage fields plus the raw path."""
    monkeypatch.setattr(cli_main, "ops_configured", lambda: True)
    monkeypatch.setattr(cli_main, "OpsClient", lambda: _StubOpsClient(search=b"<xml/>"))
    page = OpsSearchPage(
        total_count=2,
        query="ti=drone",
        begin=1,
        end=25,
        hits=(OpsSearchHit(pub="US.1.A1", family_id="100"),),
    )
    monkeypatch.setattr(service, "parse_search_xml", lambda xml: page)

    rc, data = _invoke(["search", "ti=drone"], capsys)

    assert rc == 0
    assert data == {
        "query": "ti=drone",
        "total": 2,
        "begin": 1,
        "end": 25,
        "hits": [{"pub": "US.1.A1", "family_id": "100"}],
        "raw_path": None,
    }


def test_search_without_ops_configured_is_ops_not_configured_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A search command with no OPS credentials reports ops_not_configured, exit code 4."""
    monkeypatch.setattr(cli_main, "ops_configured", lambda: False)

    rc, data = _invoke(["search", "ti=drone"], capsys)

    assert rc == 4
    assert data["error"]["type"] == "ops_not_configured"


def test_search_http_error_is_external_api_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An httpx.HTTPError from the OPS call is reported as external_api_error, exit code 3."""
    monkeypatch.setattr(cli_main, "ops_configured", lambda: True)
    request = httpx.Request("GET", "https://ops.epo.org")
    monkeypatch.setattr(
        cli_main,
        "OpsClient",
        lambda: _StubOpsClient(search=httpx.ConnectError("boom", request=request)),
    )

    rc, data = _invoke(["search", "ti=drone"], capsys)

    assert rc == 3
    assert data["error"]["type"] == "external_api_error"


def test_search_biblio_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """search-biblio prints total/begin/end plus one asdict entry per doc."""
    monkeypatch.setattr(cli_main, "ops_configured", lambda: True)
    monkeypatch.setattr(cli_main, "OpsClient", lambda: _StubOpsClient(search_biblio=b"<xml/>"))
    biblio = OpsBiblio(
        pub="US.1.A1",
        family_id="100",
        title="t",
        abstract="a",
        applicants=("Acme",),
        inventors=("Doe",),
        ipc=(),
        cpc=(),
        publication_date="20200101",
        cited_patents=(),
        npl_citation_count=0,
    )
    monkeypatch.setattr(
        service,
        "parse_search_biblio_xml",
        lambda xml: type(
            "Page", (), {"total_count": 1, "begin": 1, "end": 25, "docs": (biblio,)}
        )(),
    )

    rc, data = _invoke(["search-biblio", "ti=drone"], capsys)

    assert rc == 0
    assert data["total"] == 1
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


def test_plan_check_reads_queries_from_positional_args_and_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """plan-check merges positional QUERY args with --file lines, skipping blanks/comments."""
    monkeypatch.setattr(cli_main, "ops_configured", lambda: True)
    query_file = tmp_path / "queries.txt"
    query_file.write_text("ti=drone\n# a comment\n\nab=foo\n", encoding="utf-8")

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
        captured["refresh"] = refresh
        return {"results": [], "total_sum": 0, "exceeded": False, "max_total": max_total}

    monkeypatch.setattr(service, "search_plan_check", fake_plan_check)
    monkeypatch.setattr(cli_main, "OpsClient", lambda: _StubOpsClient())

    rc, data = _invoke(
        ["plan-check", "extra=1", "--file", str(query_file), "--max-total", "100"], capsys
    )

    assert rc == 0
    assert captured["queries"] == ["extra=1", "ti=drone", "ab=foo"]
    assert data["max_total"] == 100


# --- biblio / legal / family --------------------------------------------


def test_biblio_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """biblio prints the OpsBiblio fields plus the raw path."""
    monkeypatch.setattr(cli_main, "ops_configured", lambda: True)
    monkeypatch.setattr(cli_main, "OpsClient", lambda: _StubOpsClient(biblio=b"<xml/>"))
    biblio = OpsBiblio(
        pub="US.1.A1",
        family_id="100",
        title="t",
        abstract="a",
        applicants=("Acme",),
        inventors=(),
        ipc=(),
        cpc=(),
        publication_date="20200101",
        cited_patents=(),
        npl_citation_count=0,
    )
    monkeypatch.setattr(service, "parse_biblio_xml", lambda xml: biblio)

    rc, data = _invoke(["biblio", "US.1.A1"], capsys)

    assert rc == 0
    assert data["pub"] == "US.1.A1"
    assert data["applicants"] == ["Acme"]
    assert data["raw_path"] is None


def test_legal_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """legal prints one asdict entry per event, keyed under events, plus raw_path."""
    monkeypatch.setattr(cli_main, "ops_configured", lambda: True)
    monkeypatch.setattr(cli_main, "OpsClient", lambda: _StubOpsClient(legal=b"<xml/>"))
    events = (OpsLegalEvent(code="A1", desc="desc", gazette_date="20200101", pre_lines=("line",)),)
    monkeypatch.setattr(service, "parse_legal_xml", lambda xml: events)

    rc, data = _invoke(["legal", "US.1.A1"], capsys)

    assert rc == 0
    assert data["pub"] == "US.1.A1"
    assert data["events"] == [
        {"code": "A1", "desc": "desc", "gazette_date": "20200101", "pre_lines": ["line"]}
    ]
    assert data["raw_path"] is None


def test_legal_without_events_carries_a_note(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty event list is labelled, so it cannot be read as a failed fetch.

    OPS answers "this publication has no INPADOC event" with a well-formed
    document; without the note the output is indistinguishable from a lookup
    that returned nothing at all.
    """
    monkeypatch.setattr(cli_main, "ops_configured", lambda: True)
    monkeypatch.setattr(cli_main, "OpsClient", lambda: _StubOpsClient(legal=b"<xml/>"))
    monkeypatch.setattr(service, "parse_legal_xml", lambda xml: ())

    rc, data = _invoke(["legal", "US.1.A1"], capsys)

    assert rc == 0
    assert data["events"] == []
    assert data["note"] == service.LEGAL_NO_EVENTS_NOTE


def test_family_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """family prints family_id/members/raw_path."""
    monkeypatch.setattr(cli_main, "ops_configured", lambda: True)
    monkeypatch.setattr(cli_main, "OpsClient", lambda: _StubOpsClient(family=b"<xml/>"))
    family = OpsFamily(family_id="100", members=("US.1.A1", "EP.2.A1"))
    monkeypatch.setattr(service, "parse_family_xml", lambda xml: family)

    rc, data = _invoke(["family", "US.1.A1"], capsys)

    assert rc == 0
    assert data == {"family_id": "100", "members": ["US.1.A1", "EP.2.A1"], "raw_path": None}


# --- claims (route selection) -------------------------------------------


def _sample_gp_doc(**overrides: Any) -> GPatentDoc:
    """Build a minimal GPatentDoc for claims-route tests."""
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


def test_claims_google_patents_route(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When Google Patents has the page, claims is read from it (source='gp')."""
    page = FetchedPage(pub="US11468338B2", html="<html></html>", path=None, cached=False)
    monkeypatch.setattr(service, "fetch_patent_html", lambda pub, **kwargs: page)
    monkeypatch.setattr(service, "parse_patent_html", lambda html: _sample_gp_doc())

    rc, data = _invoke(["claims", "US11468338B2"], capsys)

    assert rc == 0
    assert data["source"] == "gp"
    assert data["pub"] == "US11468338B2"
    assert data["claims"] == [{"number": 1, "text": "1. A widget.", "depends_on": []}]
    assert data["status_display"] == "Active"
    assert data["expiration"] == "2040-01-01"
    assert data["assignee"] == "Acme"


def test_claims_gp_unavailable_falls_back_to_ops_fulltext_for_ep(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A GP 404 for an EP/WO document, with OPS configured, tries OPS full text."""
    unavailable = GPUnavailable(pub="EP1234567A1", status_code=404, retry_after_hint="wait")
    monkeypatch.setattr(service, "fetch_patent_html", lambda pub, **kwargs: unavailable)
    monkeypatch.setattr(cli_main, "ops_configured", lambda: True)
    monkeypatch.setattr(cli_main, "OpsClient", lambda: _StubOpsClient(claims=b"<xml/>"))
    monkeypatch.setattr(
        service,
        "parse_claims_xml",
        lambda xml: (Claim(number=1, text="1. Claim text.", depends_on=()),),
    )

    rc, data = _invoke(["claims", "EP1234567A1"], capsys)

    assert rc == 0
    assert data["source"] == "ops-fulltext"
    assert data["pub"] == "EP1234567A1"
    assert data["claims"] == [{"number": 1, "text": "1. Claim text.", "depends_on": []}]
    assert data["raw_path"] is None


def test_claims_unavailable_when_no_fallback_route(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A GP 404 for a US document (no OPS full text) is a normal unavailable result."""
    unavailable = GPUnavailable(
        pub="US20240111636A1", status_code=404, retry_after_hint="wait a bit"
    )
    monkeypatch.setattr(service, "fetch_patent_html", lambda pub, **kwargs: unavailable)
    monkeypatch.setattr(cli_main, "ops_configured", lambda: False)

    rc, data = _invoke(["claims", "US20240111636A1"], capsys)

    assert rc == 0
    assert data == {
        "unavailable": True,
        "pub": "US20240111636A1",
        "retry_after_hint": "wait a bit",
    }


def test_claims_ops_fulltext_404_is_also_reported_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When OPS full text also 404s, the result is still the normal unavailable shape."""
    unavailable = GPUnavailable(pub="WO2020123456A1", status_code=404, retry_after_hint="wait")
    monkeypatch.setattr(service, "fetch_patent_html", lambda pub, **kwargs: unavailable)
    monkeypatch.setattr(cli_main, "ops_configured", lambda: True)
    not_found = httpx.HTTPStatusError(
        "404", request=httpx.Request("GET", "https://ops.epo.org"), response=httpx.Response(404)
    )
    monkeypatch.setattr(cli_main, "OpsClient", lambda: _StubOpsClient(claims=not_found))

    rc, data = _invoke(["claims", "WO2020123456A1"], capsys)

    assert rc == 0
    assert data == {"unavailable": True, "pub": "WO2020123456A1", "retry_after_hint": "wait"}


# --- --refresh ------------------------------------------------------------


# The subcommands that read through the cache, with an argument each accepts.
_REFRESHABLE_COMMANDS = [
    ("search", "ti=drone", "search"),
    ("search-biblio", "ti=drone", "search_biblio"),
    ("biblio", "US.1.A1", "biblio"),
    ("claims", "US11468338B2", "claims"),
    ("legal", "US.1.A1", "legal"),
    ("family", "US.1.A1", "family"),
    ("plan-check", "ti=drone", "plan_check"),
]


@pytest.mark.parametrize("refresh", [False, True])
@pytest.mark.parametrize(("command", "argument", "function"), _REFRESHABLE_COMMANDS)
def test_refresh_flag_is_forwarded_to_the_service(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    argument: str,
    function: str,
    refresh: bool,
) -> None:
    """--refresh reaches the service as refresh=True; without it the default False is sent."""
    monkeypatch.setattr(cli_main, "ops_configured", lambda: True)
    monkeypatch.setattr(cli_main, "OpsClient", lambda: _StubOpsClient())
    captured: dict[str, Any] = {}

    def recorder(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"called": function}

    monkeypatch.setattr(service, function, recorder)
    argv = [command, argument] + (["--refresh"] if refresh else [])

    rc, data = _invoke(argv, capsys)

    assert rc == 0
    assert data == {"called": function}
    assert captured["refresh"] is refresh


# --- normalize / dedup / verify / usage ----------------------------------


def test_normalize_success(capsys: pytest.CaptureFixture[str]) -> None:
    """normalize returns every spelling of a valid publication number."""
    rc, data = _invoke(["normalize", "US.11468338.B2"], capsys)

    assert rc == 0
    assert data == {
        "input": "US.11468338.B2",
        "country": "US",
        "number": "11468338",
        "kind": "B2",
        "docdb": "US.11468338.B2",
        "epodoc": "US11468338B2",
        "google": "US11468338B2",
    }


def test_normalize_unparseable_input_is_invalid_input_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unparseable publication number is reported as invalid_input, exit code 2."""
    rc, data = _invoke(["normalize", "not-a-pub"], capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"


def test_dedup_reads_from_stdin_when_path_is_dash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """dedup HITS_JSON_PATH reads from stdin when the path is '-'."""
    hits = [{"pub": "US.1.A1", "family_id": "1"}, {"pub": "EP.2.A1", "family_id": "1"}]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(hits)))

    rc, data = _invoke(["dedup", "-"], capsys)

    assert rc == 0
    assert data["count"] == 1
    assert data["families"][0]["members"] == ["US.1.A1", "EP.2.A1"]


def test_dedup_reads_from_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """dedup HITS_JSON_PATH reads a JSON array from a real file path."""
    hits_path = tmp_path / "hits.json"
    hits_path.write_text(json.dumps([{"pub": "US.1.A1", "family_id": "1"}]), encoding="utf-8")

    rc, data = _invoke(["dedup", str(hits_path)], capsys)

    assert rc == 0
    assert data["count"] == 1


def test_dedup_non_array_json_is_invalid_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A JSON file that is not an array is reported as invalid_input, not a crash."""
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")

    rc, data, err = _invoke_with_stderr(["dedup", str(path)], capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"
    assert "Traceback" not in err


def test_dedup_rejects_an_element_that_is_not_an_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hit that is not a JSON object is rejected by index, without a traceback."""
    path = tmp_path / "hits.json"
    path.write_text(json.dumps([{"pub": "US.1.A1"}, 2]), encoding="utf-8")

    rc, data, err = _invoke_with_stderr(["dedup", str(path)], capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"
    assert "[1]" in data["error"]["message"]
    assert "Traceback" not in err


def test_dedup_rejects_an_element_without_a_pub_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hit missing "pub" is invalid input rather than an uncaught KeyError."""
    path = tmp_path / "hits.json"
    path.write_text(json.dumps([{"x": 1}]), encoding="utf-8")

    rc, data, err = _invoke_with_stderr(["dedup", str(path)], capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"
    assert "[0]" in data["error"]["message"]
    assert "pub" in data["error"]["message"]
    assert "Traceback" not in err


def test_dedup_rejects_more_hits_than_the_batch_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A payload over MAX_BATCH_RECORDS is refused before any work is done."""
    path = tmp_path / "hits.json"
    hits = [{"pub": "US.1.A1"}] * (service.MAX_BATCH_RECORDS + 1)
    path.write_text(json.dumps(hits), encoding="utf-8")

    rc, data, err = _invoke_with_stderr(["dedup", str(path)], capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"
    assert str(service.MAX_BATCH_RECORDS) in data["error"]["message"]
    assert "Traceback" not in err


def test_batch_limit_is_shared_with_the_mcp_server() -> None:
    """The CLI and the MCP server refuse an oversized batch at the same size."""
    assert server_tools.MAX_RECORDS == service.MAX_BATCH_RECORDS


def test_verify_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """verify --input/--output cross-checks two JSON array files."""
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    input_path.write_text(json.dumps(["US.1.A1", "EP.2.A1"]), encoding="utf-8")
    output_path.write_text(json.dumps(["US.1.A1", "EP.2.A1"]), encoding="utf-8")

    rc, data = _invoke(["verify", "--input", str(input_path), "--output", str(output_path)], capsys)

    assert rc == 0
    assert data["ok"] is True
    assert data["input_count"] == 2
    assert data["output_count"] == 2


def _write_verify_files(tmp_path: Path, input_data: Any, output_data: Any) -> list[str]:
    """Write both verify payloads and return the argv tail naming them."""
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "output.json"
    input_path.write_text(json.dumps(input_data), encoding="utf-8")
    output_path.write_text(json.dumps(output_data), encoding="utf-8")
    return ["--input", str(input_path), "--output", str(output_path)]


def test_verify_rejects_an_input_element_that_is_not_a_string(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--input must hold publication-number strings; anything else is invalid input."""
    argv = ["verify", *_write_verify_files(tmp_path, ["US.1.A1", 2], [])]

    rc, data, err = _invoke_with_stderr(argv, capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"
    assert "[1]" in data["error"]["message"]
    assert "Traceback" not in err


def test_verify_rejects_an_output_element_that_is_neither_string_nor_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--output takes strings or objects carrying "pub"; a number is neither."""
    argv = ["verify", *_write_verify_files(tmp_path, ["US.1.A1"], [7])]

    rc, data, err = _invoke_with_stderr(argv, capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"
    assert "[0]" in data["error"]["message"]
    assert "Traceback" not in err


def test_verify_rejects_an_output_record_without_a_pub_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An output object missing "pub" is invalid input rather than an uncaught KeyError."""
    argv = ["verify", *_write_verify_files(tmp_path, ["US.1.A1"], [{"family_id": "1"}])]

    rc, data, err = _invoke_with_stderr(argv, capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"
    assert "pub" in data["error"]["message"]
    assert "Traceback" not in err


def test_verify_rejects_more_records_than_the_batch_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same MAX_BATCH_RECORDS ceiling applies to both verify payloads."""
    oversized = ["US.1.A1"] * (service.MAX_BATCH_RECORDS + 1)
    argv = ["verify", *_write_verify_files(tmp_path, oversized, [])]

    rc, data, err = _invoke_with_stderr(argv, capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"
    assert str(service.MAX_BATCH_RECORDS) in data["error"]["message"]
    assert "Traceback" not in err


def test_verify_accepts_output_records_carrying_a_pub_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The validation does not reject the documented record shape."""
    argv = ["verify", *_write_verify_files(tmp_path, ["US.1.A1"], [{"pub": "US.1.A1"}])]

    rc, data = _invoke(argv, capsys)

    assert rc == 0
    assert data["ok"] is True


def test_usage_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """usage prints utils.usage_report()'s result verbatim."""
    fake = {"available": False, "path": "/tmp/headers.jsonl"}
    monkeypatch.setattr(service, "usage_report", lambda: fake)

    rc, data = _invoke(["usage"], capsys)

    assert rc == 0
    assert data == fake


# --- .env, output encoding and I/O failures --------------------------------


def test_every_command_reads_the_nearest_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    restore_environ: None,
) -> None:
    """``cache status`` honours a ``.env`` next to the caller, like the OPS commands do.

    Before v1.0 only the commands that resolved OPS credentials read
    ``.env``, so ``cache status`` and ``clean`` could report (and clean) a
    different directory than the one the rest of the toolkit used.
    """
    monkeypatch.setattr(cli_main.config, "load_dotenv", dotenv.load_dotenv)
    monkeypatch.delenv("PATENT_CHECKER_DATA_DIR", raising=False)
    monkeypatch.delenv("PATENT_CHECKER_CACHE_DIR", raising=False)
    monkeypatch.setattr(cli_main, "_cache", default_cache)
    data_dir = tmp_path / "data"
    (tmp_path / ".env").write_text(f"PATENT_CHECKER_DATA_DIR={data_dir}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc, data = _invoke(["cache", "status"], capsys)

    assert rc == 0
    assert data["shared_dir"] == str(data_dir / "cache")
    assert data["local_dir"] == str(data_dir / "cache")


def test_output_streams_are_switched_to_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both output streams are reconfigured to UTF-8 before anything is printed.

    A Windows console defaults to a legacy code page, on which a Japanese
    title would abort the command with a UnicodeEncodeError.
    """
    out = _RecordingStream()
    err = _RecordingStream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    rc = cli_main.main(["normalize", "US11468338B2"])

    assert rc == 0
    assert out.calls == [{"encoding": "utf-8"}]
    assert err.calls == [{"encoding": "utf-8"}]
    assert "US.11468338.B2" in out.text


def test_a_stream_that_refuses_to_be_reconfigured_does_not_stop_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream that cannot be re-encoded is left as it is, and the command still runs."""
    out = _RecordingStream(fails=True)
    err = _PlainStream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    rc = cli_main.main(["normalize", "US11468338B2"])

    assert rc == 0
    assert "US.11468338.B2" in out.text


def test_a_missing_input_file_is_an_io_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file that cannot be read is reported as JSON, not as a traceback."""
    missing = tmp_path / "nonexistent.json"

    rc, data = _invoke(["dedup", str(missing)], capsys)

    assert rc == 2
    assert data["error"]["type"] == "io_error"
    assert str(missing) in data["error"]["message"]
    assert "No such file" in data["error"]["message"]


def test_an_unreadable_verify_file_is_an_io_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same holds for the second payload of ``verify``: the path is named."""
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(["US11468338B2"]), encoding="utf-8")
    missing = tmp_path / "output.json"

    rc, data = _invoke(["verify", "--input", str(input_path), "--output", str(missing)], capsys)

    assert rc == 2
    assert data["error"]["type"] == "io_error"
    assert str(missing) in data["error"]["message"]


# --- exit codes 3 and 4 across the subcommands -----------------------------

# One invocation per OPS-backed subcommand, with the service function it
# reaches, so that a failure of the shared service layer is checked to come
# out as the documented envelope for every one of them.
_SERVICE_ROUTES: tuple[tuple[str, list[str], str], ...] = (
    ("search", ["search", "ti=drone"], "search"),
    ("search-biblio", ["search-biblio", "ti=drone"], "search_biblio"),
    ("plan-check", ["plan-check", "ti=drone"], "plan_check"),
    ("biblio", ["biblio", "US.1.A1"], "biblio"),
    ("legal", ["legal", "US.1.A1"], "legal"),
    ("family", ["family", "US.1.A1"], "family"),
    ("claims", ["claims", "EP1672502A1"], "claims"),
    ("usage", ["usage"], "usage"),
)

_SERVICE_FAILURES: tuple[tuple[str, Exception, int, str], ...] = (
    (
        "http-error",
        httpx.ConnectError("boom", request=httpx.Request("GET", "https://ops.epo.org")),
        3,
        "external_api_error",
    ),
    ("config-error", ConfigError("malformed $PATENT_CHECKER_CACHE_TTL"), 4, "config_error"),
    (
        "ops-not-configured",
        ConfigError(service.OPS_NOT_CONFIGURED_MESSAGE),
        4,
        "ops_not_configured",
    ),
)


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_type"),
    [pytest.param(f, c, t, id=i) for i, f, c, t in _SERVICE_FAILURES],
)
@pytest.mark.parametrize(
    ("argv", "function"),
    [pytest.param(argv, function, id=name) for name, argv, function in _SERVICE_ROUTES],
)
def test_every_subcommand_maps_a_service_failure_to_its_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    function: str,
    failure: Exception,
    expected_code: int,
    expected_type: str,
) -> None:
    """Each subcommand reports the same failure with the same type and exit code."""

    def raising(*args: Any, **kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(cli_main, "ops_configured", lambda: True)
    monkeypatch.setattr(cli_main, "OpsClient", lambda: _StubOpsClient())
    monkeypatch.setattr(service, function, raising)

    rc, data = _invoke(argv, capsys)

    assert rc == expected_code
    assert data["error"]["type"] == expected_type


# --- consent --------------------------------------------------------------


def test_consent_status_reports_stubbed_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """consent status prints consent.consent_status()'s result verbatim."""
    fake_status = {
        "consented": True,
        "notice_version": "1",
        "needs_reconsent": False,
        "record": {"language": "en"},
        "languages": ["en", "ja"],
    }
    monkeypatch.setattr(consent, "consent_status", lambda: fake_status)

    rc, data = _invoke(["consent", "status"], capsys)

    assert rc == 0
    assert data == fake_status


def test_consent_show_prints_notice_markdown(capsys: pytest.CaptureFixture[str]) -> None:
    """consent show prints the raw notice Markdown (not JSON) for the default language."""
    rc = cli_main.main(["consent", "show"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "Patent Checker" in out


def test_consent_show_fallback_emits_stderr_note(capsys: pytest.CaptureFixture[str]) -> None:
    """An unsupported --lang falls back to English and notes it on stderr."""
    rc = cli_main.main(["consent", "show", "--lang", "de"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "note: falling back to en" in captured.err
    assert "Patent Checker" in captured.out


def test_consent_record_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """consent record prints the recorded path/version/language."""
    recorded_path = tmp_path / "consent.json"
    monkeypatch.setattr(consent, "record_consent", lambda *, language, scope: recorded_path)

    rc, data = _invoke(["consent", "record", "--lang", "en"], capsys)

    assert rc == 0
    assert data == {
        "recorded": True,
        "path": str(recorded_path),
        "notice_version": consent.NOTICE_VERSION,
        "language": "en",
    }


def test_consent_record_invalid_language_is_invalid_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ValueError from record_consent (e.g. unsupported language) is invalid_input."""

    def _raise(*, language: str, scope: str) -> Path:
        raise ValueError("unsupported notice language")

    monkeypatch.setattr(consent, "record_consent", _raise)

    rc, data = _invoke(["consent", "record", "--lang", "xx"], capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"


def test_consent_without_subcommand_is_invalid_input(capsys: pytest.CaptureFixture[str]) -> None:
    """'consent' with no status/show/record child is an argparse-level invalid_input error."""
    rc, data = _invoke(["consent"], capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"


# --- serve ------------------------------------------------------------


def test_serve_show_operator_notice_prints_english_by_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """serve --show-operator-notice prints the English notice text and exits 0."""
    _, expected_text = server_settings.operator_notice_text("en")

    rc = cli_main.main(["serve", "--show-operator-notice"])
    out = capsys.readouterr().out

    assert rc == 0
    assert out == expected_text + "\n"


def test_serve_show_operator_notice_lang_ja(capsys: pytest.CaptureFixture[str]) -> None:
    """serve --show-operator-notice --lang ja prints the Japanese notice text."""
    _, expected_text = server_settings.operator_notice_text("ja")

    rc = cli_main.main(["serve", "--show-operator-notice", "--lang", "ja"])
    out = capsys.readouterr().out

    assert rc == 0
    assert out == expected_text + "\n"


def test_serve_show_operator_notice_unsupported_lang_falls_back_to_en(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unsupported --lang falls back to English and notes it on stderr."""
    _, expected_text = server_settings.operator_notice_text("en")

    rc = cli_main.main(["serve", "--show-operator-notice", "--lang", "xx"])
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out == expected_text + "\n"
    assert "note: falling back to en" in captured.err


def test_serve_show_operator_notice_never_calls_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--show-operator-notice never resolves settings or starts the server."""
    run_calls: list[Any] = []
    monkeypatch.setattr(server_app, "run", lambda settings: run_calls.append(settings))

    rc = cli_main.main(["serve", "--show-operator-notice"])
    capsys.readouterr()

    assert rc == 0
    assert run_calls == []


def test_serve_defaults_call_load_settings_and_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """serve with no flags resolves settings with the default transport/host/port."""
    captured_kwargs: dict[str, Any] = {}
    sentinel_settings = object()

    def fake_load_settings(*, transport: str, host: str | None, port: int | None) -> Any:
        captured_kwargs.update(transport=transport, host=host, port=port)
        return sentinel_settings

    run_calls: list[Any] = []
    monkeypatch.setattr(server_settings, "load_settings", fake_load_settings)
    monkeypatch.setattr(server_app, "run", lambda settings: run_calls.append(settings))

    rc = cli_main.main(["serve"])
    out = capsys.readouterr().out

    assert rc == 0
    assert out == ""
    assert captured_kwargs == {"transport": "http", "host": None, "port": None}
    assert run_calls == [sentinel_settings]


def test_serve_passes_transport_host_port_through(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """serve --transport/--host/--port pass their values to load_settings."""
    captured_kwargs: dict[str, Any] = {}

    def fake_load_settings(*, transport: str, host: str | None, port: int | None) -> Any:
        captured_kwargs.update(transport=transport, host=host, port=port)
        return object()

    monkeypatch.setattr(server_settings, "load_settings", fake_load_settings)
    monkeypatch.setattr(server_app, "run", lambda settings: None)

    rc = cli_main.main(["serve", "--transport", "stdio", "--host", "0.0.0.0", "--port", "9000"])
    capsys.readouterr()

    assert rc == 0
    assert captured_kwargs == {"transport": "stdio", "host": "0.0.0.0", "port": 9000}


def test_serve_config_error_is_reported_as_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ConfigError from load_settings is reported as config_error, exit code 4."""

    def fake_load_settings(*, transport: str, host: str | None, port: int | None) -> Any:
        raise ConfigError("boom")

    run_calls: list[Any] = []
    monkeypatch.setattr(server_settings, "load_settings", fake_load_settings)
    monkeypatch.setattr(server_app, "run", lambda settings: run_calls.append(settings))

    rc, data = _invoke(["serve"], capsys)

    assert rc == 4
    assert data == {"error": {"type": "config_error", "message": "boom"}}
    assert run_calls == []


def test_serve_bind_failure_is_reported_as_a_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A busy port ends in the config-error envelope naming the port and --port."""

    def fake_load_settings(*, transport: str, host: str | None, port: int | None) -> Any:
        # Only host and port are read on this path; a namespace keeps the
        # test independent of the settings dataclass' other fields.
        return SimpleNamespace(transport="http", host="127.0.0.1", port=8642)

    def fake_run(settings: Any) -> None:
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(server_settings, "load_settings", fake_load_settings)
    monkeypatch.setattr(server_app, "run", fake_run)

    rc, data = _invoke(["serve"], capsys)

    assert rc == 4
    assert data["error"]["type"] == "config_error"
    message = data["error"]["message"]
    assert "8642" in message
    assert "--port" in message
    assert "Address already in use" in message


def test_serve_stdio_reports_a_config_error_on_stderr_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With stdio, stdout is the protocol channel, so not even an error may go there."""

    def fake_load_settings(*, transport: str, host: str | None, port: int | None) -> Any:
        raise ConfigError("boom")

    monkeypatch.setattr(server_settings, "load_settings", fake_load_settings)
    monkeypatch.setattr(server_app, "run", lambda settings: None)

    try:
        rc = cli_main.main(["serve", "--transport", "stdio"])
    except SystemExit as exc:
        rc = exc.code
    captured = capsys.readouterr()

    assert rc == 4
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": {"type": "config_error", "message": "boom"}}


def test_serve_stdio_io_failure_stays_off_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """stdio binds nothing, so an OSError there is an io_error -- and still not on stdout."""

    def fake_load_settings(*, transport: str, host: str | None, port: int | None) -> Any:
        return SimpleNamespace(transport="stdio", host="127.0.0.1", port=8642)

    def fake_run(settings: Any) -> None:
        raise OSError(errno.EPIPE, "Broken pipe")

    monkeypatch.setattr(server_settings, "load_settings", fake_load_settings)
    monkeypatch.setattr(server_app, "run", fake_run)

    try:
        rc = cli_main.main(["serve", "--transport", "stdio"])
    except SystemExit as exc:
        rc = exc.code
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["type"] == "io_error"
    assert "Broken pipe" in error["message"]


def test_serve_invalid_transport_is_invalid_input(capsys: pytest.CaptureFixture[str]) -> None:
    """An unsupported --transport value is rejected by argparse itself, exit code 2."""
    rc, data = _invoke(["serve", "--transport", "tcp"], capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"


# --- cache -----------------------------------------------------------------


def _files_under(root: Path) -> set[Path]:
    """Return every regular file under *root*, or an empty set if *root* does not exist."""
    if not root.exists():
        return set()
    return {path for path in root.rglob("*") if path.is_file()}


def test_cache_status_on_empty_cache_reports_zero_totals_and_dirs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """cache status on a cache with nothing written reports zero entries and its directories."""
    shared = tmp_path / "shared"
    local = tmp_path / "local"
    monkeypatch.setattr(cli_main, "_cache", lambda: Cache(shared, local))

    rc, data = _invoke(["cache", "status"], capsys)

    assert rc == 0
    assert data["shared_dir"] == str(shared)
    assert data["local_dir"] == str(local)
    assert data["same_root"] is False
    assert data["totals"]["entries"] == 0


def test_cache_status_reports_per_kind_counts_and_same_root_when_one_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """cache status reflects entries written via Cache.put, per kind and in total."""
    cache = Cache(tmp_path / "cache")  # local defaults to shared -> one root
    cache.put("biblio", "US.1.A1", b"<xml/>", ident="US.1.A1")
    cache.put("search", "abc123", b"<xml/>", ident="ti=drone")
    monkeypatch.setattr(cli_main, "_cache", lambda: cache)

    rc, data = _invoke(["cache", "status"], capsys)

    assert rc == 0
    assert data["same_root"] is True
    assert data["totals"]["entries"] == 2
    assert data["kinds"]["biblio"]["entries"] == 1
    assert data["kinds"]["search"]["entries"] == 1
    assert data["kinds"]["claims"]["entries"] == 0


def test_cache_status_config_error_from_cache_ttl_is_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ConfigError raised by _cache() (a malformed cache TTL) is reported as config_error."""

    def _raise() -> Cache:
        raise ConfigError("PATENT_CHECKER_CACHE_TTL: invalid TTL 'bogus'")

    monkeypatch.setattr(cli_main, "_cache", _raise)

    rc, data = _invoke(["cache", "status"], capsys)

    assert rc == 4
    assert data["error"]["type"] == "config_error"


def test_cache_without_subcommand_is_invalid_input(capsys: pytest.CaptureFixture[str]) -> None:
    """'cache' with no status/clear child is an argparse-level invalid_input error."""
    rc, data = _invoke(["cache"], capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"


def test_cache_clear_default_is_a_dry_run_that_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """cache clear without --yes lists the selection but leaves every file in place."""
    root = tmp_path / "cache"
    cache = Cache(root)
    cache.put("biblio", "US.1.A1", b"<xml/>", ident="US.1.A1")
    cache.put("legal", "US.1.A1", b"<xml/>", ident="US.1.A1")
    monkeypatch.setattr(cli_main, "_cache", lambda: cache)
    before = _files_under(root)

    rc, data = _invoke(["cache", "clear"], capsys)

    assert rc == 0
    assert data["dry_run"] is True
    assert data["selected"] == 2
    assert {entry["kind"] for entry in data["entries"]} == {"biblio", "legal"}
    assert _files_under(root) == before


def test_cache_clear_with_yes_deletes_only_the_selected_entries(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """cache clear --kind biblio --yes deletes only the biblio entry's files."""
    root = tmp_path / "cache"
    cache = Cache(root)
    cache.put("biblio", "US.1.A1", b"<xml/>", ident="US.1.A1")
    cache.put("legal", "US.1.A1", b"<xml/>", ident="US.1.A1")
    monkeypatch.setattr(cli_main, "_cache", lambda: cache)

    rc, data = _invoke(["cache", "clear", "--kind", "biblio", "--yes"], capsys)

    assert rc == 0
    assert data["dry_run"] is False
    assert data["selected"] == 1
    assert data["removed"] == 2  # body file + sidecar
    assert data["errors"] == []
    assert cache.content_path("biblio", "US.1.A1").exists() is False
    assert cache.content_path("legal", "US.1.A1").exists() is True


def test_cache_clear_kind_filter_accepts_repeated_flags(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--kind may be repeated to select more than one kind."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "US.1.A1", b"<xml/>", ident="US.1.A1")
    cache.put("legal", "US.1.A1", b"<xml/>", ident="US.1.A1")
    cache.put("family", "US.1.A1", b"<xml/>", ident="US.1.A1")
    monkeypatch.setattr(cli_main, "_cache", lambda: cache)

    rc, data = _invoke(["cache", "clear", "--kind", "biblio", "--kind", "legal"], capsys)

    assert rc == 0
    assert data["selected"] == 2
    assert {entry["kind"] for entry in data["entries"]} == {"biblio", "legal"}


def test_cache_clear_older_than_selects_by_age_boundary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--older-than N keeps entries fetched at least N days before the check (boundary included)."""
    clock_value = datetime(2026, 9, 1, 12, 0, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: clock_value)
    cache.put("biblio", "US.1.A1", b"<xml/>", ident="old")  # exactly 2 days old at check time

    clock_value = datetime(2026, 9, 2, 12, 0, 0)
    cache.put("legal", "US.1.A1", b"<xml/>", ident="new")  # 1 day old at check time

    clock_value = datetime(2026, 9, 3, 12, 0, 0)
    monkeypatch.setattr(cli_main, "_cache", lambda: cache)

    rc, data = _invoke(["cache", "clear", "--older-than", "2"], capsys)

    assert rc == 0
    assert [entry["kind"] for entry in data["entries"]] == ["biblio"]


def test_cache_clear_pub_selects_only_that_publications_entries(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--pub restricts the selection to the entries keyed by that publication number."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "US.1.A1", b"<xml/>", ident="US.1.A1")
    cache.put("biblio", "EP.2.A1", b"<xml/>", ident="EP.2.A1")
    monkeypatch.setattr(cli_main, "_cache", lambda: cache)

    rc, data = _invoke(["cache", "clear", "--pub", "US1A1"], capsys)

    assert rc == 0
    assert [entry["key"] for entry in data["entries"]] == ["US.1.A1"]


def test_cache_clear_expired_flag_selects_only_expired_entries(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--expired keeps only entries whose TTL has elapsed."""
    clock_value = datetime(2026, 9, 1, 12, 0, 0)
    cache = Cache(tmp_path / "cache", clock=lambda: clock_value)
    cache.put("legal", "US.1.A1", b"<xml/>", ident="US.1.A1")  # legal TTL is 7 days
    cache.put("claims", "US.1.A1", b"<xml/>", ident="US.1.A1")  # claims never expires

    clock_value = datetime(2026, 9, 1, 12, 0, 0) + timedelta(days=8)
    monkeypatch.setattr(cli_main, "_cache", lambda: cache)

    rc, data = _invoke(["cache", "clear", "--expired"], capsys)

    assert rc == 0
    assert [entry["kind"] for entry in data["entries"]] == ["legal"]


def test_cache_clear_broken_flag_selects_only_broken_entries(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--broken keeps only entries that cannot be served for a structural reason."""
    cache = Cache(tmp_path / "cache")
    cache.put("biblio", "US.1.A1", b"<xml/>", ident="US.1.A1")
    cache.content_path("biblio", "US.1.A1").unlink()  # body now missing -> broken
    cache.put("legal", "US.1.A1", b"<xml/>", ident="US.1.A1")
    monkeypatch.setattr(cli_main, "_cache", lambda: cache)

    rc, data = _invoke(["cache", "clear", "--broken"], capsys)

    assert rc == 0
    assert [entry["kind"] for entry in data["entries"]] == ["biblio"]
    assert data["entries"][0]["problem"] == "missing body"


def test_cache_clear_negative_older_than_is_invalid_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A negative --older-than is rejected as invalid_input, exit code 2."""
    monkeypatch.setattr(cli_main, "_cache", lambda: Cache(tmp_path / "cache"))

    rc, data = _invoke(["cache", "clear", "--older-than", "-1"], capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"


def test_cache_clear_unknown_kind_is_invalid_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """An unknown --kind is rejected (via Cache.select's ValueError) as invalid_input."""
    monkeypatch.setattr(cli_main, "_cache", lambda: Cache(tmp_path / "cache"))

    rc, data = _invoke(["cache", "clear", "--kind", "bogus"], capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"


def test_cache_clear_unparseable_pub_is_invalid_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A --pub value that is not a parseable publication number is invalid_input."""
    monkeypatch.setattr(cli_main, "_cache", lambda: Cache(tmp_path / "cache"))

    rc, data = _invoke(["cache", "clear", "--pub", "not-a-pub"], capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"


# --- clean -----------------------------------------------------------------


def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Point ``clean`` and ``cache status`` at a throwaway tree; return its paths.

    Every location the command can reach (project data base, shared cache,
    the server's data directory and the per-user consent record) is
    redirected under *tmp_path*, so no test can touch a real one.
    """
    project = tmp_path / "project"
    shared = tmp_path / "shared"
    user_data = tmp_path / "userdata"
    user_consent = tmp_path / "userconfig" / "consent.json"
    for path, text in (
        (project / "cache" / "ops" / "search" / "aaaa.xml", "<search/>"),
        (project / "cache" / "ops" / "biblio" / "US.1.A1.xml", "<biblio/>"),
        (project / "raw" / "ops" / "headers.jsonl", "{}\n"),
        (project / "raw" / "ops" / "20260101-120000_biblio_US1.xml", "<legacy/>"),
        (project / "raw" / "gp" / "US1A1.html", "<html></html>"),
        (project / "reports" / "report-x-20260101-1200.md", "# report"),
        (project / "consent.json", '{"notice_version": "1"}'),
        (shared / "ops" / "biblio" / "EP.2.A1.xml", "<biblio/>"),
        (user_data / "raw" / "ops" / "headers.jsonl", "{}\n"),
        (user_consent, '{"notice_version": "1"}'),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    monkeypatch.setattr(cli_main.config, "data_base", lambda: project)
    monkeypatch.setattr(cli_main.config, "user_data_dir", lambda: user_data)
    monkeypatch.setattr(cli_main.consent, "user_consent_path", lambda: user_consent)
    monkeypatch.setattr(cli_main, "_cache", lambda: Cache(shared, project / "cache"))
    return {
        "project": project,
        "shared": shared,
        "user_data": user_data,
        "user_consent": user_consent,
    }


def test_clean_default_is_a_dry_run_that_deletes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """clean without --yes lists this project's leftovers and leaves every file in place."""
    paths = _clean_env(monkeypatch, tmp_path)
    before = _files_under(tmp_path)

    rc, data = _invoke(["clean"], capsys)

    assert rc == 0
    assert data["dry_run"] is True
    assert data["scope"] == {"project": str(paths["project"]), "shared": None, "user_data": None}
    assert {item["category"] for item in data["items"]} == {
        "search-cache",
        "request-log",
        "legacy-raw",
        "legacy-cache",
    }
    assert data["total_files"] == 5
    assert data["total_bytes"] > 0
    assert "removed_files" not in data
    assert _files_under(tmp_path) == before


def test_clean_with_yes_removes_the_project_leftovers_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """clean --yes empties the cache and raw directories, sparing everything else."""
    paths = _clean_env(monkeypatch, tmp_path)

    rc, data = _invoke(["clean", "--yes"], capsys)

    assert rc == 0
    assert data["dry_run"] is False
    assert data["removed_files"] == data["total_files"]
    assert data["bytes"] == data["total_bytes"]
    assert data["errors"] == []
    # The removed paths are only counted: the list itself would be unbounded.
    assert "paths" not in data
    assert _files_under(paths["project"] / "cache") == set()
    assert _files_under(paths["project"] / "raw") == set()
    assert (paths["project"] / "reports" / "report-x-20260101-1200.md").exists()
    assert (paths["project"] / "consent.json").exists()
    assert _files_under(paths["shared"]) != set()
    assert _files_under(paths["user_data"]) != set()
    assert paths["user_consent"].exists()


def test_clean_include_artifacts_also_removes_what_the_agent_wrote(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--include-artifacts adds the reports, but still not the consent record."""
    paths = _clean_env(monkeypatch, tmp_path)

    rc, data = _invoke(["clean", "--include-artifacts", "--yes"], capsys)

    assert rc == 0
    assert "artifact" in {item["category"] for item in data["items"]}
    assert not (paths["project"] / "reports").exists()
    assert (paths["project"] / "consent.json").exists()


def test_clean_include_consent_removes_the_project_record_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--include-consent alone leaves the per-user record in place."""
    paths = _clean_env(monkeypatch, tmp_path)

    rc, data = _invoke(["clean", "--include-consent", "--yes"], capsys)

    assert rc == 0
    assert not (paths["project"] / "consent.json").exists()
    assert paths["user_consent"].exists()
    assert (paths["project"] / "reports" / "report-x-20260101-1200.md").exists()


def test_clean_shared_targets_the_shared_cache_and_the_server_data(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--shared names both extra scopes and, with --yes, empties them too."""
    paths = _clean_env(monkeypatch, tmp_path)

    rc, data = _invoke(["clean", "--shared", "--include-consent", "--yes"], capsys)

    assert rc == 0
    assert data["scope"]["shared"] == str(paths["shared"])
    assert data["scope"]["user_data"] == str(paths["user_data"])
    assert {"shared", "user"} <= {item["scope"] for item in data["items"]}
    assert _files_under(paths["shared"]) == set()
    assert _files_under(paths["user_data"]) == set()
    assert not paths["user_consent"].exists()


def test_clean_reports_removal_errors_and_still_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A path that could not be removed is reported in the JSON, not as a failure."""
    _clean_env(monkeypatch, tmp_path)
    failure = {
        "removed_files": 0,
        "bytes": 0,
        "paths": [],
        "errors": [{"path": "/elsewhere", "error": "outside the cleanup roots"}],
    }
    monkeypatch.setattr(cli_main.cleanup, "execute", lambda plan: failure)

    rc, data = _invoke(["clean", "--yes"], capsys)

    assert rc == 0
    assert data["removed_files"] == 0
    assert data["errors"] == failure["errors"]


def test_cache_status_reports_the_legacy_layout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """cache status carries a legacy section counting what the v0.3 layout still holds."""
    paths = _clean_env(monkeypatch, tmp_path)

    rc, data = _invoke(["cache", "status"], capsys)

    assert rc == 0
    legacy = data["legacy"]
    assert legacy["raw_ops_bodies"]["dir"] == str(paths["project"] / "raw" / "ops")
    assert legacy["raw_ops_bodies"]["files"] == 1  # headers.jsonl is not a leftover
    assert legacy["raw_gp"]["files"] == 1
    assert legacy["cache_pub_kinds"]["files"] == 1
    assert legacy["total_files"] == 3
    assert legacy["total_bytes"] > 0


# --- general CLI behavior -------------------------------------------------


def test_help_lists_all_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    """--help's usage text lists every subcommand name."""
    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--help"])
    out = capsys.readouterr().out

    assert exc_info.value.code == 0
    for name in (
        "search",
        "search-biblio",
        "plan-check",
        "biblio",
        "claims",
        "legal",
        "family",
        "normalize",
        "dedup",
        "verify",
        "usage",
        "consent",
        "serve",
        "cache",
        "clean",
        "install",
    ):
        assert name in out


def test_no_arguments_prints_help_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """Invoking with no arguments at all prints help text and exits 0."""
    rc = cli_main.main([])
    out = capsys.readouterr().out

    assert rc == 0
    assert "usage:" in out


def test_unknown_subcommand_is_invalid_input_error(capsys: pytest.CaptureFixture[str]) -> None:
    """An unrecognized subcommand is an argparse-level invalid_input error, exit code 2."""
    rc, data = _invoke(["not-a-command"], capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"


# --- install ---------------------------------------------------------------


def _install_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """Point ``install`` at a throwaway machine and return ``(home, project)``.

    The home directory, the working directory, the consent record, the
    locale and the ``PATH`` lookup are all redirected, so the command can
    neither read nor write anything belonging to the person running the
    tests, and the notice's language does not depend on their environment.
    """
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(cli_main, "_home", lambda: home)
    monkeypatch.setattr(cli_main, "_cwd", lambda: project)
    monkeypatch.setattr(cli_main, "_which", lambda program: None)
    monkeypatch.setattr(cli_main, "_stdin_is_tty", lambda: False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.chdir(project)
    return home, project


def test_install_list_agents_prints_every_known_agent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """install --list-agents prints the agent table and touches nothing."""
    _install_env(monkeypatch, tmp_path)
    before = _files_under(tmp_path)

    rc = cli_main.main(["install", "--list-agents"])
    out = capsys.readouterr().out

    assert rc == 0
    for key in installer.AGENT_KEYS:
        assert key in out
    assert _files_under(tmp_path) == before


def test_install_list_agents_warns_about_flags_it_ignores(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Listing agents ignores the installation flags, and now says so on stderr."""
    _install_env(monkeypatch, tmp_path)

    rc = cli_main.main(["install", "--list-agents", "--agent", "claude-code", "--dry-run"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "--agent" in captured.err
    assert "--dry-run" in captured.err
    assert captured.err.count("warning:") == 1
    # The listing itself is unaffected.
    assert "claude-code" in captured.out


def test_install_list_agents_stays_silent_without_extra_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--scope belongs to listing, so using it warns about nothing."""
    _install_env(monkeypatch, tmp_path)

    rc = cli_main.main(["install", "--list-agents", "--scope", "project"])
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.err == ""


def test_install_dry_run_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """install --dry-run reports the plan without creating a single file."""
    _install_env(monkeypatch, tmp_path)
    before = _files_under(tmp_path)

    rc = cli_main.main(["install", "--dry-run", "--agree", "--agent", "cursor", "--no-mcp"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "dry run" in out
    assert _files_under(tmp_path) == before


def test_install_writes_the_agent_configuration_without_printing_the_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The token reaches the agent's configuration file, never the report."""
    home, _ = _install_env(monkeypatch, tmp_path)
    token_file = tmp_path / "token"
    token_file.write_text("s3cret-token\n", encoding="utf-8")

    rc = cli_main.main(
        [
            "install",
            "--agree",
            "--agent",
            "cursor",
            "--no-skill",
            "--token-file",
            str(token_file),
        ]
    )
    captured = capsys.readouterr()

    assert rc == 0
    entry = json.loads((home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))["mcpServers"][
        "patent-checker"
    ]
    assert entry["headers"] == {"Authorization": "Bearer s3cret-token"}
    assert "s3cret-token" not in captured.out
    assert "s3cret-token" not in captured.err


def test_install_without_a_terminal_and_without_agree_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Consent cannot be assumed: the notice is shown and the run stops with code 2."""
    home, _ = _install_env(monkeypatch, tmp_path)

    rc = cli_main.main(["install", "--agent", "cursor", "--no-mcp"])
    captured = capsys.readouterr()

    assert rc == 2
    assert "Important Notice" in captured.out
    assert captured.err.startswith("error: ")
    assert not (home / ".config" / "patent-checker").exists()


def test_install_with_an_unknown_agent_is_invalid_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """An agent argparse does not know is rejected through the JSON envelope."""
    _install_env(monkeypatch, tmp_path)

    rc, data = _invoke(["install", "--agent", "emacs"], capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"


def test_install_shows_the_japanese_notice_for_a_japanese_locale(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Without --lang the notice follows the locale environment."""
    _install_env(monkeypatch, tmp_path)
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.setenv("LANG", "ja_JP.UTF-8")

    rc = cli_main.main(["install", "--dry-run", "--agree", "--agent", "cursor", "--no-mcp"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "重要なお知らせ" in out


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, "en"),
        ({"LANG": "ja_JP.UTF-8"}, "ja"),
        ({"LANG": "en_US.UTF-8"}, "en"),
        ({"LC_ALL": "ja_JP.UTF-8", "LANG": "en_US.UTF-8"}, "ja"),
        ({"LC_ALL": "C", "LANG": "ja_JP.UTF-8"}, "en"),
    ],
)
def test_default_lang_follows_the_locale_environment(
    environ: dict[str, str], expected: str
) -> None:
    """LC_ALL wins over LANG, and only a ja* value selects Japanese."""
    assert cli_main._default_lang(environ) == expected


def test_dedup_rejects_an_oversized_element(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI applies the same per-element size limit as the MCP server."""
    from patent_checker.validation import MAX_ITEM_CHARS

    path = tmp_path / "hits.json"
    path.write_text(json.dumps([{"pub": "US" + "1" * (MAX_ITEM_CHARS + 1)}]), encoding="utf-8")

    rc, data, err = _invoke_with_stderr(["dedup", str(path)], capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"
    assert "hits[0]" in data["error"]["message"]
    assert "Traceback" not in err


def test_verify_rejects_an_oversized_input_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The total-size limit applies to ``verify --input`` as well."""
    from patent_checker.validation import MAX_ITEM_CHARS, MAX_PAYLOAD_CHARS

    count = MAX_PAYLOAD_CHARS // MAX_ITEM_CHARS + 2
    inputs = tmp_path / "in.json"
    inputs.write_text(json.dumps(["U" * MAX_ITEM_CHARS] * count), encoding="utf-8")
    outputs = tmp_path / "out.json"
    outputs.write_text("[]", encoding="utf-8")

    rc, data, err = _invoke_with_stderr(
        ["verify", "--input", str(inputs), "--output", str(outputs)], capsys
    )

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"
    assert "Traceback" not in err
