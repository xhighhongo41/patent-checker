"""Service layer shared by the CLI and the MCP server.

Every function here returns a JSON-serializable ``dict`` whose shape is
identical to the v0.2 CLI output of the subcommand with the same name, so the
two front ends cannot drift apart: the CLI is a thin argparse adapter over
these functions, and the MCP server calls them with a long-lived, shared
:class:`~patent_checker.ops.client.OpsClient`.

Ownership of that client stays with the caller: OPS-dependent functions take
it as a keyword argument and never open or close one themselves. Passing
``None`` means "OPS is not available", which is reported as a
:class:`~patent_checker.config.ConfigError`.

Exceptions propagate unchanged and are mapped by the caller:

- ``ValueError``: invalid input (CLI exit code 2, server ``ToolError``).
- ``httpx.HTTPError``: an external API call failed (CLI exit code 3).
- ``ConfigError``: EPO OPS is not configured (CLI exit code 4).

Response caching will be integrated at this layer in a later task, so both
front ends pick it up at once.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from patent_checker.config import ConfigError
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

# Countries for which EPO OPS carries full-text claims, used by the claims
# fallback route (Google Patents -> OPS full text -> none).
OPS_FULLTEXT_COUNTRIES: tuple[str, ...] = ("EP", "WO")

# Shared wording for the "OPS is not available" failure, so the CLI's
# pre-flight check and this layer's client check report the same thing.
OPS_NOT_CONFIGURED_MESSAGE = (
    "EPO OPS credentials are not configured; set PATENT_CHECKER_OPS_KEY and "
    "PATENT_CHECKER_OPS_SECRET (see .env.example)"
)


def require_ops(client: OpsClient | None) -> OpsClient:
    """Return *client*, or fail when the caller has no OPS client to offer.

    Args:
        client: The caller-owned OPS client, or ``None`` when OPS is not
            configured.

    Returns:
        The client, unchanged.

    Raises:
        ConfigError: If *client* is ``None``.
    """
    if client is None:
        raise ConfigError(OPS_NOT_CONFIGURED_MESSAGE)
    return client


def ops_fulltext_candidate(pub: str) -> bool:
    """Return True if *pub* is from a country whose full text OPS carries.

    Args:
        pub: Publication number in any spelling accepted by ``parse_pubnum``.

    Raises:
        ValueError: If *pub* is not a parseable publication number.
    """
    return parse_pubnum(pub).country in OPS_FULLTEXT_COUNTRIES


# --- search family -----------------------------------------------------


def search(cql: str, *, begin: int = 1, end: int = 25, client: OpsClient | None) -> dict[str, Any]:
    """Run a published-data CQL search and return one page of hits.

    Args:
        cql: CQL query expression.
        begin: First hit of the requested page (1-based, inclusive).
        end: Last hit of the requested page (inclusive).
        client: Caller-owned OPS client.

    Returns:
        ``{"query", "total", "begin", "end", "hits": [...], "raw_path"}``.

    Raises:
        ConfigError: If *client* is ``None``.
    """
    client = require_ops(client)
    xml, raw_path = client.search(cql, begin=begin, end=end)
    page = parse_search_xml(xml)
    return {
        "query": page.query,
        "total": page.total_count,
        "begin": page.begin,
        "end": page.end,
        "hits": [dataclasses.asdict(hit) for hit in page.hits],
        "raw_path": str(raw_path),
    }


def search_biblio(
    cql: str, *, begin: int = 1, end: int = 25, client: OpsClient | None
) -> dict[str, Any]:
    """Run a biblio-constituent CQL search and return one page of full biblio records.

    Args:
        cql: CQL query expression.
        begin: First hit of the requested page (1-based, inclusive).
        end: Last hit of the requested page (inclusive).
        client: Caller-owned OPS client.

    Returns:
        ``{"total", "begin", "end", "docs": [...], "raw_path"}``.

    Raises:
        ConfigError: If *client* is ``None``.
    """
    client = require_ops(client)
    xml, raw_path = client.search_biblio(cql, begin=begin, end=end)
    page = parse_search_biblio_xml(xml)
    return {
        "total": page.total_count,
        "begin": page.begin,
        "end": page.end,
        "docs": [dataclasses.asdict(doc) for doc in page.docs],
        "raw_path": str(raw_path),
    }


def plan_check(
    queries: Sequence[str], *, max_total: int | None = None, client: OpsClient | None
) -> dict[str, Any]:
    """Measure the hit count of every candidate query in a search plan.

    Args:
        queries: CQL query expressions to measure.
        max_total: Hit-count budget the summed totals are compared against.
        client: Caller-owned OPS client.

    Returns:
        :func:`patent_checker.utils.search_plan_check`'s report.

    Raises:
        ConfigError: If *client* is ``None``.
    """
    return search_plan_check(list(queries), client=require_ops(client), max_total=max_total)


# --- single-document lookups --------------------------------------------


def biblio(pub: str, *, client: OpsClient | None) -> dict[str, Any]:
    """Fetch bibliographic data for one publication.

    Args:
        pub: Publication number in any spelling accepted by ``parse_pubnum``.
        client: Caller-owned OPS client.

    Returns:
        The ``OpsBiblio`` fields plus ``"raw_path"``.

    Raises:
        ConfigError: If *client* is ``None``.
    """
    client = require_ops(client)
    xml, raw_path = client.biblio(pub)
    result = dataclasses.asdict(parse_biblio_xml(xml))
    result["raw_path"] = str(raw_path)
    return result


def legal(pub: str, *, client: OpsClient | None) -> dict[str, Any]:
    """Fetch INPADOC legal-status events for one publication.

    Args:
        pub: Publication number in any spelling accepted by ``parse_pubnum``.
        client: Caller-owned OPS client.

    Returns:
        ``{"pub" (DOCDB spelling), "events": [...], "raw_path"}``.

    Raises:
        ConfigError: If *client* is ``None``.
        ValueError: If *pub* is not a parseable publication number.
    """
    client = require_ops(client)
    xml, raw_path = client.legal(pub)
    events = parse_legal_xml(xml)
    return {
        "pub": parse_pubnum(pub).docdb(),
        "events": [dataclasses.asdict(event) for event in events],
        "raw_path": str(raw_path),
    }


def family(pub: str, *, client: OpsClient | None) -> dict[str, Any]:
    """Fetch the simple patent family of one publication.

    Args:
        pub: Publication number in any spelling accepted by ``parse_pubnum``.
        client: Caller-owned OPS client.

    Returns:
        ``{"family_id", "members": [...], "raw_path"}``.

    Raises:
        ConfigError: If *client* is ``None``.
    """
    client = require_ops(client)
    xml, raw_path = client.family(pub)
    result = parse_family_xml(xml)
    return {
        "family_id": result.family_id,
        "members": list(result.members),
        "raw_path": str(raw_path),
    }


def claims(
    pub: str, *, client: OpsClient | None = None, gp_client: httpx.Client | None = None
) -> dict[str, Any]:
    """Fetch claims: Google Patents first, OPS full text as a fallback for EP/WO.

    A document no source can serve is a normal result (the ``"unavailable"``
    shape), not an error. Unlike the other OPS-aware functions this one does
    not require a client: the Google Patents route needs no OPS credentials,
    so *client* is optional and only enables the fallback.

    Args:
        pub: Publication number in any spelling accepted by ``parse_pubnum``.
        client: Caller-owned OPS client, or ``None`` to skip the OPS
            full-text fallback.
        gp_client: Caller-owned HTTP client for Google Patents. A short-lived
            one is created per request when this is ``None``.

    Returns:
        ``{"source": "gp", ...}``, ``{"source": "ops-fulltext", ...}``, or
        ``{"unavailable": True, "pub", "retry_after_hint"}``.

    Raises:
        httpx.HTTPError: If a fetch fails for any reason other than the
            document being absent.
        ValueError: If *pub* is not a parseable publication number.
    """
    # Passing client=None explicitly is not the same as omitting it for every
    # possible stand-in of fetch_patent_html, so keep the two calls distinct.
    if gp_client is None:
        fetched = fetch_patent_html(pub)
    else:
        fetched = fetch_patent_html(pub, client=gp_client)

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
    if ops_fulltext_candidate(pub) and client is not None:
        try:
            xml, raw_path = client.claims(pub)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == httpx.codes.NOT_FOUND:
                return _claims_unavailable(fetched)
            raise
        return {
            "source": "ops-fulltext",
            "pub": pub,
            "claims": [dataclasses.asdict(claim) for claim in parse_claims_xml(xml)],
            "raw_path": str(raw_path),
        }

    return _claims_unavailable(fetched)


def _claims_unavailable(fetched: GPUnavailable) -> dict[str, Any]:
    """Build the "claims could not be fetched from any source" result."""
    return {"unavailable": True, "pub": fetched.pub, "retry_after_hint": fetched.retry_after_hint}


# --- offline helpers -----------------------------------------------------


def normalize(text: str) -> dict[str, Any]:
    """Parse a publication number and return every spelling used across sources.

    Args:
        text: Publication number in any supported spelling.

    Returns:
        ``{"input", "country", "number", "kind", "docdb", "epodoc", "google"}``.

    Raises:
        ValueError: If *text* is not a parseable publication number.
    """
    parsed = parse_pubnum(text)
    return {
        "input": text,
        "country": parsed.country,
        "number": parsed.number,
        "kind": parsed.kind,
        "docdb": parsed.docdb(),
        "epodoc": parsed.epodoc(),
        "google": parsed.google(),
    }


def dedup(hits: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Collapse a list of search hits into one record per patent family.

    Args:
        hits: Search hits, each carrying at least ``"pub"`` and
            ``"family_id"``.

    Returns:
        ``{"families": [...], "count": int}``.

    Raises:
        KeyError: If a hit is missing the required ``"pub"`` key.
    """
    families = dedup_families(hits)
    return {"families": families, "count": len(families)}


def verify(input_pubs: Sequence[Any], output_records: Sequence[Any]) -> dict[str, Any]:
    """Cross-check a delegated batch's output against its input publication list.

    Args:
        input_pubs: The publication numbers the batch was asked to cover.
        output_records: The records the batch produced (publication strings
            or mappings carrying one).

    Returns:
        :func:`patent_checker.utils.verify_batch`'s report.
    """
    return verify_batch(input_pubs, output_records)


def usage(headers_path: Path | None = None) -> dict[str, Any]:
    """Summarize the local OPS request-header log.

    Args:
        headers_path: Log file to read. The default location used by
            ``OpsClient`` is resolved by ``usage_report`` itself when this is
            ``None``.

    Returns:
        :func:`patent_checker.utils.usage_report`'s summary.
    """
    # Resolving the default is usage_report's job, so omit the argument
    # entirely rather than forwarding None.
    if headers_path is None:
        return usage_report()
    return usage_report(headers_path)
