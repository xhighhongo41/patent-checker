"""Tests for the CLI (patent_checker/cli/main.py).

Network-free: :class:`~patent_checker.ops.client.OpsClient`, the ops/gp parse
functions and the ``utils`` helpers are monkeypatched with canned stand-ins.
Only the JSON shape on stdout and the process exit code are checked here (CLI
is I/O conversion only; the underlying logic is tested where it is
implemented).
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from patent_checker import consent
from patent_checker.cli import main as cli_main
from patent_checker.gp.fetch import GPUnavailable
from patent_checker.gp.parse import GPatentDoc
from patent_checker.models import Claim
from patent_checker.ops.parse import (
    OpsBiblio,
    OpsFamily,
    OpsLegalEvent,
    OpsSearchHit,
    OpsSearchPage,
)


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
    monkeypatch.setattr(
        cli_main, "OpsClient", lambda: _StubOpsClient(search=(b"<xml/>", Path("/tmp/raw.xml")))
    )
    page = OpsSearchPage(
        total_count=2,
        query="ti=drone",
        begin=1,
        end=25,
        hits=(OpsSearchHit(pub="US.1.A1", family_id="100"),),
    )
    monkeypatch.setattr(cli_main, "parse_search_xml", lambda xml: page)

    rc, data = _invoke(["search", "ti=drone"], capsys)

    assert rc == 0
    assert data == {
        "query": "ti=drone",
        "total": 2,
        "begin": 1,
        "end": 25,
        "hits": [{"pub": "US.1.A1", "family_id": "100"}],
        "raw_path": "/tmp/raw.xml",
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
    monkeypatch.setattr(
        cli_main,
        "OpsClient",
        lambda: _StubOpsClient(search_biblio=(b"<xml/>", Path("/tmp/sb.xml"))),
    )
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
        cli_main,
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
    assert data["raw_path"] == "/tmp/sb.xml"


def test_plan_check_reads_queries_from_positional_args_and_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """plan-check merges positional QUERY args with --file lines, skipping blanks/comments."""
    monkeypatch.setattr(cli_main, "ops_configured", lambda: True)
    query_file = tmp_path / "queries.txt"
    query_file.write_text("ti=drone\n# a comment\n\nab=foo\n", encoding="utf-8")

    captured: dict[str, Any] = {}

    def fake_plan_check(queries: list[str], *, max_total: int | None = None) -> dict[str, Any]:
        captured["queries"] = queries
        return {"results": [], "total_sum": 0, "exceeded": False, "max_total": max_total}

    monkeypatch.setattr(cli_main, "search_plan_check", fake_plan_check)

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
    monkeypatch.setattr(
        cli_main, "OpsClient", lambda: _StubOpsClient(biblio=(b"<xml/>", Path("/tmp/b.xml")))
    )
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
    monkeypatch.setattr(cli_main, "parse_biblio_xml", lambda xml: biblio)

    rc, data = _invoke(["biblio", "US.1.A1"], capsys)

    assert rc == 0
    assert data["pub"] == "US.1.A1"
    assert data["applicants"] == ["Acme"]
    assert data["raw_path"] == "/tmp/b.xml"


def test_legal_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """legal prints one asdict entry per event, keyed under events, plus raw_path."""
    monkeypatch.setattr(cli_main, "ops_configured", lambda: True)
    monkeypatch.setattr(
        cli_main, "OpsClient", lambda: _StubOpsClient(legal=(b"<xml/>", Path("/tmp/l.xml")))
    )
    events = (OpsLegalEvent(code="A1", desc="desc", gazette_date="20200101", pre_lines=("line",)),)
    monkeypatch.setattr(cli_main, "parse_legal_xml", lambda xml: events)

    rc, data = _invoke(["legal", "US.1.A1"], capsys)

    assert rc == 0
    assert data["pub"] == "US.1.A1"
    assert data["events"] == [
        {"code": "A1", "desc": "desc", "gazette_date": "20200101", "pre_lines": ["line"]}
    ]
    assert data["raw_path"] == "/tmp/l.xml"


def test_family_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """family prints family_id/members/raw_path."""
    monkeypatch.setattr(cli_main, "ops_configured", lambda: True)
    monkeypatch.setattr(
        cli_main, "OpsClient", lambda: _StubOpsClient(family=(b"<xml/>", Path("/tmp/f.xml")))
    )
    family = OpsFamily(family_id="100", members=("US.1.A1", "EP.2.A1"))
    monkeypatch.setattr(cli_main, "parse_family_xml", lambda xml: family)

    rc, data = _invoke(["family", "US.1.A1"], capsys)

    assert rc == 0
    assert data == {"family_id": "100", "members": ["US.1.A1", "EP.2.A1"], "raw_path": "/tmp/f.xml"}


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
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """When Google Patents has the page, claims is read from it (source='gp')."""
    page_path = tmp_path / "US11468338B2.html"
    page_path.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(cli_main, "fetch_patent_html", lambda pub: page_path)
    monkeypatch.setattr(cli_main, "parse_patent_html", lambda html: _sample_gp_doc())

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
    monkeypatch.setattr(cli_main, "fetch_patent_html", lambda pub: unavailable)
    monkeypatch.setattr(cli_main, "ops_configured", lambda: True)
    monkeypatch.setattr(
        cli_main, "OpsClient", lambda: _StubOpsClient(claims=(b"<xml/>", Path("/tmp/c.xml")))
    )
    monkeypatch.setattr(
        cli_main,
        "parse_claims_xml",
        lambda xml: (Claim(number=1, text="1. Claim text.", depends_on=()),),
    )

    rc, data = _invoke(["claims", "EP1234567A1"], capsys)

    assert rc == 0
    assert data["source"] == "ops-fulltext"
    assert data["pub"] == "EP1234567A1"
    assert data["claims"] == [{"number": 1, "text": "1. Claim text.", "depends_on": []}]
    assert data["raw_path"] == "/tmp/c.xml"


def test_claims_unavailable_when_no_fallback_route(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A GP 404 for a US document (no OPS full text) is a normal unavailable result."""
    unavailable = GPUnavailable(
        pub="US20240111636A1", status_code=404, retry_after_hint="wait a bit"
    )
    monkeypatch.setattr(cli_main, "fetch_patent_html", lambda pub: unavailable)
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
    monkeypatch.setattr(cli_main, "fetch_patent_html", lambda pub: unavailable)
    monkeypatch.setattr(cli_main, "ops_configured", lambda: True)
    not_found = httpx.HTTPStatusError(
        "404", request=httpx.Request("GET", "https://ops.epo.org"), response=httpx.Response(404)
    )
    monkeypatch.setattr(cli_main, "OpsClient", lambda: _StubOpsClient(claims=not_found))

    rc, data = _invoke(["claims", "WO2020123456A1"], capsys)

    assert rc == 0
    assert data == {"unavailable": True, "pub": "WO2020123456A1", "retry_after_hint": "wait"}


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

    rc, data = _invoke(["dedup", str(path)], capsys)

    assert rc == 2
    assert data["error"]["type"] == "invalid_input"


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


def test_usage_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """usage prints utils.usage_report()'s result verbatim."""
    fake = {"available": False, "path": "/tmp/headers.jsonl"}
    monkeypatch.setattr(cli_main, "usage_report", lambda: fake)

    rc, data = _invoke(["usage"], capsys)

    assert rc == 0
    assert data == fake


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
