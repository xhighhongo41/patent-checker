"""Command-line entry point for patent-checker.

Every subcommand prints one JSON object to stdout (``json.dumps(...,
ensure_ascii=False, indent=2)``); ``consent show`` is the only exception, as
it prints the raw notice Markdown so it can be shown to a human as-is. Errors
are reported the same way, as ``{"error": {"type": str, "message": str}}``,
so a calling Skill/agent can branch on the JSON alone rather than parsing
stderr text; the process exit code encodes the same distinction:

- ``0``: success (this includes ``claims`` reporting a document as
  unavailable -- "could not be fetched" is itself a valid, non-error result).
- ``2``: invalid input (a ``ValueError``, including a malformed argument
  argparse itself rejects).
- ``3``: an external API call failed (``httpx.HTTPError``).
- ``4``: EPO OPS is not configured (``patent_checker.config.ConfigError``, or
  a command that requires OPS finding :func:`~patent_checker.config.
  ops_configured` false before making any request).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from patent_checker import __version__, consent
from patent_checker.config import ConfigError, ops_configured
from patent_checker.gp.fetch import GPUnavailable, fetch_patent_html
from patent_checker.gp.parse import parse_patent_html
from patent_checker.ops.client import OpsClient
from patent_checker.ops.parse import (
    parse_biblio_xml,
    parse_claims_xml,
    parse_family_xml,
    parse_legal_xml,
    parse_search_biblio_xml,
    parse_search_xml,
)
from patent_checker.pubnum import parse_pubnum
from patent_checker.utils import dedup_families, search_plan_check, usage_report, verify_batch

# Countries for which EPO OPS carries full-text claims, used by the
# claims-command fallback route (Google Patents -> OPS full text -> none).
_OPS_FULLTEXT_COUNTRIES = ("EP", "WO")


class _JsonArgumentParser(argparse.ArgumentParser):
    """An ``ArgumentParser`` that reports its own errors as the shared JSON envelope.

    Without this override, an invalid argument (missing required option,
    unknown subcommand, ...) would print a plain-text usage message to
    stderr; a calling Skill/agent would then have to special-case argparse's
    own error format instead of the ``{"error": ...}`` shape every other
    failure uses.
    """

    def error(self, message: str) -> None:
        """Print the standard error envelope and exit with code 2 (invalid input)."""
        _print_json(_error_result("invalid_input", message))
        raise SystemExit(2)


def _print_json(data: dict[str, Any]) -> None:
    """Print *data* to stdout as indented, non-ASCII-escaped JSON."""
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _error_result(error_type: str, message: str) -> dict[str, Any]:
    """Build the shared ``{"error": {"type", "message"}}`` envelope."""
    return {"error": {"type": error_type, "message": message}}


def _require_ops() -> None:
    """Raise ConfigError when EPO OPS credentials cannot be resolved.

    Called at the top of every OPS-dependent handler so the check happens
    before any request is attempted, per the CLI's ops_not_configured
    contract.

    Raises:
        ConfigError: If OPS is not configured.
    """
    if not ops_configured():
        raise ConfigError(
            "EPO OPS credentials are not configured; set PATENT_CHECKER_OPS_KEY and "
            "PATENT_CHECKER_OPS_SECRET (see .env.example)"
        )


def _read_json_array(path: str, *, allow_stdin: bool = False) -> list[Any]:
    """Read *path* (or stdin, when *path* is ``"-"`` and allowed) as a JSON array.

    Raises:
        ValueError: If the content is not a JSON array (includes invalid
            JSON, since :class:`json.JSONDecodeError` is a ``ValueError``).
    """
    if allow_stdin and path == "-":
        text = sys.stdin.read()
    else:
        text = Path(path).read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError(f"expected a JSON array in {path!r}, got {type(data).__name__}")
    return data


def _read_query_file(path: str) -> list[str]:
    """Return the CQL queries in *path*, one per non-empty, non-comment line."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


# --- search family -----------------------------------------------------


def _cmd_search(args: argparse.Namespace) -> dict[str, Any]:
    """Run a published-data CQL search and return one page of hits."""
    _require_ops()
    with OpsClient() as client:
        xml, raw_path = client.search(args.cql, begin=args.begin, end=args.end)
    page = parse_search_xml(xml)
    return {
        "query": page.query,
        "total": page.total_count,
        "begin": page.begin,
        "end": page.end,
        "hits": [dataclasses.asdict(hit) for hit in page.hits],
        "raw_path": str(raw_path),
    }


def _cmd_search_biblio(args: argparse.Namespace) -> dict[str, Any]:
    """Run a biblio-constituent CQL search and return one page of full biblio records."""
    _require_ops()
    with OpsClient() as client:
        xml, raw_path = client.search_biblio(args.cql, begin=args.begin, end=args.end)
    page = parse_search_biblio_xml(xml)
    return {
        "total": page.total_count,
        "begin": page.begin,
        "end": page.end,
        "docs": [dataclasses.asdict(doc) for doc in page.docs],
        "raw_path": str(raw_path),
    }


def _cmd_plan_check(args: argparse.Namespace) -> dict[str, Any]:
    """Measure the hit count of every candidate query in a search plan."""
    _require_ops()
    queries = list(args.queries)
    if args.file:
        queries.extend(_read_query_file(args.file))
    return search_plan_check(queries, max_total=args.max_total)


# --- single-document lookups --------------------------------------------


def _cmd_biblio(args: argparse.Namespace) -> dict[str, Any]:
    """Fetch bibliographic data for one publication."""
    _require_ops()
    with OpsClient() as client:
        xml, raw_path = client.biblio(args.pub)
    result = dataclasses.asdict(parse_biblio_xml(xml))
    result["raw_path"] = str(raw_path)
    return result


def _cmd_claims(args: argparse.Namespace) -> dict[str, Any]:
    """Fetch claims: Google Patents first, OPS full text as a fallback for EP/WO.

    See the module docstring's exit-code note: an unavailable document is a
    normal (exit 0) result, not an error.
    """
    fetched = fetch_patent_html(args.pub)
    if isinstance(fetched, Path):
        doc = parse_patent_html(fetched.read_text(encoding="utf-8"))
        return {
            "source": "gp",
            "pub": doc.pub_number,
            "claims": [dataclasses.asdict(claim) for claim in doc.claims],
            "claims_fallback_text": doc.claims_fallback_text,
            "status_display": doc.status_display,
            "expiration": doc.expiration,
            "assignee": doc.assignee,
        }

    # fetched is a GPUnavailable: try the OPS full-text route for EP/WO before
    # giving up.
    assert isinstance(fetched, GPUnavailable)
    country = parse_pubnum(args.pub).country
    if country in _OPS_FULLTEXT_COUNTRIES and ops_configured():
        with OpsClient() as client:
            try:
                xml, raw_path = client.claims(args.pub)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == httpx.codes.NOT_FOUND:
                    return _claims_unavailable(fetched)
                raise
        return {
            "source": "ops-fulltext",
            "pub": args.pub,
            "claims": [dataclasses.asdict(claim) for claim in parse_claims_xml(xml)],
            "raw_path": str(raw_path),
        }

    return _claims_unavailable(fetched)


def _claims_unavailable(fetched: GPUnavailable) -> dict[str, Any]:
    """Build the "claims could not be fetched from any source" result."""
    return {"unavailable": True, "pub": fetched.pub, "retry_after_hint": fetched.retry_after_hint}


def _cmd_legal(args: argparse.Namespace) -> dict[str, Any]:
    """Fetch INPADOC legal-status events for one publication."""
    _require_ops()
    with OpsClient() as client:
        xml, raw_path = client.legal(args.pub)
    events = parse_legal_xml(xml)
    return {
        "pub": parse_pubnum(args.pub).docdb(),
        "events": [dataclasses.asdict(event) for event in events],
        "raw_path": str(raw_path),
    }


def _cmd_family(args: argparse.Namespace) -> dict[str, Any]:
    """Fetch the simple patent family of one publication."""
    _require_ops()
    with OpsClient() as client:
        xml, raw_path = client.family(args.pub)
    family = parse_family_xml(xml)
    return {
        "family_id": family.family_id,
        "members": list(family.members),
        "raw_path": str(raw_path),
    }


# --- offline helpers -----------------------------------------------------


def _cmd_normalize(args: argparse.Namespace) -> dict[str, Any]:
    """Parse a publication number and return every spelling used across sources."""
    parsed = parse_pubnum(args.text)
    return {
        "input": args.text,
        "country": parsed.country,
        "number": parsed.number,
        "kind": parsed.kind,
        "docdb": parsed.docdb(),
        "epodoc": parsed.epodoc(),
        "google": parsed.google(),
    }


def _cmd_dedup(args: argparse.Namespace) -> dict[str, Any]:
    """Collapse a list of search hits into one record per patent family."""
    hits = _read_json_array(args.path, allow_stdin=True)
    families = dedup_families(hits)
    return {"families": families, "count": len(families)}


def _cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    """Cross-check a delegated batch's output against its input publication list."""
    input_pubs = _read_json_array(args.input)
    output_records = _read_json_array(args.output)
    return verify_batch(input_pubs, output_records)


def _cmd_usage(args: argparse.Namespace) -> dict[str, Any]:
    """Summarize the local OPS request-header log."""
    return usage_report()


# --- consent ---------------------------------------------------------------


def _cmd_consent_status(args: argparse.Namespace) -> dict[str, Any]:
    """Report the current consent state."""
    return consent.consent_status()


def _cmd_consent_show(args: argparse.Namespace) -> None:
    """Print the notice text for a language, falling back to English."""
    lang, text = consent.notice_text(args.lang)
    if lang != args.lang:
        print(f"note: falling back to {lang}", file=sys.stderr)
    print(text)
    return None


def _cmd_consent_record(args: argparse.Namespace) -> dict[str, Any]:
    """Record explicit consent to the current notice version."""
    scope = "project" if args.project else "user"
    path = consent.record_consent(language=args.lang, scope=scope)
    return {
        "recorded": True,
        "path": str(path),
        "notice_version": consent.NOTICE_VERSION,
        "language": args.lang,
    }


# --- argument parser -------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser with every subcommand registered."""
    parser = _JsonArgumentParser(
        prog="patent-checker",
        description="Explore prior art and legal status for patent publications.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    search_parser = subparsers.add_parser("search", help="Run a published-data CQL search")
    search_parser.add_argument("cql", help="CQL query expression")
    search_parser.add_argument("--begin", type=int, default=1)
    search_parser.add_argument("--end", type=int, default=25)
    search_parser.set_defaults(handler=_cmd_search)

    search_biblio_parser = subparsers.add_parser(
        "search-biblio", help="Run a CQL search returning full biblio per hit"
    )
    search_biblio_parser.add_argument("cql", help="CQL query expression")
    search_biblio_parser.add_argument("--begin", type=int, default=1)
    search_biblio_parser.add_argument("--end", type=int, default=25)
    search_biblio_parser.set_defaults(handler=_cmd_search_biblio)

    plan_check_parser = subparsers.add_parser(
        "plan-check", help="Measure the hit count of candidate search queries"
    )
    plan_check_parser.add_argument(
        "queries", nargs="*", metavar="QUERY", help="CQL query expression"
    )
    plan_check_parser.add_argument("--file", help="File with one CQL query per line")
    plan_check_parser.add_argument("--max-total", type=int, default=None)
    plan_check_parser.set_defaults(handler=_cmd_plan_check)

    biblio_parser = subparsers.add_parser("biblio", help="Fetch bibliographic data")
    biblio_parser.add_argument("pub", help="Publication number")
    biblio_parser.set_defaults(handler=_cmd_biblio)

    claims_parser = subparsers.add_parser(
        "claims", help="Fetch claims (GP, OPS fallback for EP/WO)"
    )
    claims_parser.add_argument("pub", help="Publication number")
    claims_parser.set_defaults(handler=_cmd_claims)

    legal_parser = subparsers.add_parser("legal", help="Fetch INPADOC legal-status events")
    legal_parser.add_argument("pub", help="Publication number")
    legal_parser.set_defaults(handler=_cmd_legal)

    family_parser = subparsers.add_parser("family", help="Fetch the simple patent family")
    family_parser.add_argument("pub", help="Publication number")
    family_parser.set_defaults(handler=_cmd_family)

    normalize_parser = subparsers.add_parser("normalize", help="Normalize a publication number")
    normalize_parser.add_argument("text", help="Publication number in any supported spelling")
    normalize_parser.set_defaults(handler=_cmd_normalize)

    dedup_parser = subparsers.add_parser("dedup", help="Collapse search hits into families")
    dedup_parser.add_argument(
        "path", metavar="HITS_JSON_PATH", help="JSON array file, or '-' for stdin"
    )
    dedup_parser.set_defaults(handler=_cmd_dedup)

    verify_parser = subparsers.add_parser("verify", help="Cross-check a delegated batch's output")
    verify_parser.add_argument("--input", required=True, help="JSON array of input pub strings")
    verify_parser.add_argument("--output", required=True, help="JSON array of output records")
    verify_parser.set_defaults(handler=_cmd_verify)

    usage_parser = subparsers.add_parser("usage", help="Summarize the local OPS request-header log")
    usage_parser.set_defaults(handler=_cmd_usage)

    _add_consent_parser(subparsers)

    return parser


def _add_consent_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``consent`` subcommand and its ``status``/``show``/``record`` children."""
    consent_parser = subparsers.add_parser("consent", help="Manage the legal-notice consent gate")
    consent_subparsers = consent_parser.add_subparsers(dest="consent_command", required=True)

    status_parser = consent_subparsers.add_parser("status", help="Report the current consent state")
    status_parser.set_defaults(handler=_cmd_consent_status)

    show_parser = consent_subparsers.add_parser("show", help="Print the notice text")
    show_parser.add_argument("--lang", default="en")
    show_parser.set_defaults(handler=_cmd_consent_show)

    record_parser = consent_subparsers.add_parser("record", help="Record explicit consent")
    record_parser.add_argument("--lang", required=True)
    record_parser.add_argument(
        "--project", action="store_true", help="Record for this project instead of this user"
    )
    record_parser.set_defaults(handler=_cmd_consent_record)


# --- entry point -------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Parse command-line arguments and dispatch to a subcommand.

    With no arguments, prints help text and returns 0. See the module
    docstring for the output/exit-code contract.
    """
    parser = _build_parser()

    # Resolve the effective argument list up front so the "no arguments"
    # case can be detected regardless of whether argv was passed explicitly.
    effective_argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(effective_argv)

    handler = getattr(args, "handler", None)
    if not effective_argv or handler is None:
        parser.print_help()
        return 0

    try:
        result = handler(args)
    except ValueError as exc:
        _print_json(_error_result("invalid_input", str(exc)))
        return 2
    except ConfigError as exc:
        _print_json(_error_result("ops_not_configured", str(exc)))
        return 4
    except httpx.HTTPError as exc:
        _print_json(_error_result("external_api_error", str(exc)))
        return 3

    if result is not None:
        _print_json(result)
    return 0
