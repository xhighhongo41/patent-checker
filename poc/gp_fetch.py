"""Google Patents fetcher for the v0.1 PoC.

Downloads ``https://patents.google.com/patent/<PUB>/en`` pages with a local
file cache and a minimum interval between network requests (Google Patents
has no API; we keep the access pattern at human scale and never re-fetch a
cached document). Structured extraction lives in this module too and is
added on top of the saved HTML.

Command line usage::

    uv run python poc/gp_fetch.py US11468338B2 [PUB ...]
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from bs4 import BeautifulSoup, Tag
from config import data_dir
from pubnum import parse_pubnum

GP_URL_TEMPLATE = "https://patents.google.com/patent/{pub}/en"

# Courtesy interval between actual network requests (seconds).
MIN_INTERVAL_SECONDS = 2.0

_last_request_at: float | None = None


def _wait_for_interval() -> None:
    """Sleep until MIN_INTERVAL_SECONDS have passed since the last request."""
    global _last_request_at
    now = time.monotonic()
    if _last_request_at is not None:
        remaining = MIN_INTERVAL_SECONDS - (now - _last_request_at)
        if remaining > 0:
            time.sleep(remaining)
    _last_request_at = time.monotonic()


def cache_path(pub: str) -> Path:
    """Return the cache file path for a publication number (Google spelling)."""
    normalized = parse_pubnum(pub).google()
    return data_dir("gp") / f"{normalized}.html"


def fetch_patent_html(pub: str, *, force: bool = False) -> Path:
    """Fetch the Google Patents page for ``pub`` and return the cached path.

    The document is downloaded at most once: if the cache file exists and
    ``force`` is false, no network request is made.

    Raises:
        httpx.HTTPStatusError: If Google Patents answers with an error status.
        ValueError: If ``pub`` is not a parseable publication number.
    """
    path = cache_path(pub)
    if path.exists() and not force:
        return path

    _wait_for_interval()
    url = GP_URL_TEMPLATE.format(pub=parse_pubnum(pub).google())
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
    path.write_text(resp.text, encoding="utf-8")
    return path


@dataclass(frozen=True)
class Claim:
    """One claim: its number, flattened text, and dependency targets.

    Attributes:
        number: Claim number (1-based, from the ``num`` attribute).
        text: Whitespace-normalized full text of the claim (leading
            "N." numbering kept as printed).
        depends_on: Claim numbers referenced via ``<claim-ref>`` inside this
            claim, in order of first appearance, deduplicated. Empty for
            independent claims.
    """

    number: int
    text: str
    depends_on: tuple[int, ...] = ()


@dataclass(frozen=True)
class GPatentDoc:
    """Structured extraction of one Google Patents ``/patent/`` page.

    ``status_display`` and ``expiration`` reproduce what the page shows and
    are reference values only; authoritative legal status comes from EPO OPS
    (completion requirement R8).
    """

    pub_number: str
    title: str
    abstract: str
    claims: tuple[Claim, ...]
    cpc_codes: tuple[str, ...]
    status_display: str
    expiration: str
    priority_date: str
    publication_date: str
    assignee: str
    backward_refs: tuple[str, ...]
    forward_refs: tuple[str, ...]
    similar: tuple[str, ...]


# Leaf-level CPC codes, e.g. "G06N3/02" (section-only codes like "G06N" are skipped).
_CPC_LEAF_RE = re.compile(r"^[A-Z]\d{2}[A-Z]\d+/\d+$")

# claim-ref idref values look like "CLM-00001"; leading zeros are stripped by int().
_CLAIM_REF_NUMBER_RE = re.compile(r"CLM-0*(\d+)")

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_whitespace(text: str) -> str:
    """Collapse runs of whitespace into a single space and strip the ends."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def _has_reference_row_ancestor(tag: Tag) -> bool:
    """Return True if any ancestor of ``tag`` is a ``tr`` with an itemprop attribute."""
    for ancestor in tag.parents:
        if getattr(ancestor, "name", None) == "tr" and ancestor.has_attr("itemprop"):
            return True
    return False


def _first_outside_reference_rows(soup: BeautifulSoup, selector: str) -> Tag | None:
    """Return the first element matching ``selector`` with no reference-row ancestor.

    "Reference row" means a ``tr[itemprop]`` element such as the backward/forward
    reference or similar-documents tables; this is used to skip values that only
    describe one referenced document instead of the page's own document.
    """
    for element in soup.select(selector):
        if not _has_reference_row_ancestor(element):
            return element
    return None


def _extract_claim_dependencies(claim_div: Tag) -> tuple[int, ...]:
    """Return claim numbers referenced via ``<claim-ref>`` inside ``claim_div``."""
    seen: list[int] = []
    for ref in claim_div.find_all("claim-ref"):
        idref = ref.get("idref")
        if not idref:
            continue
        match = _CLAIM_REF_NUMBER_RE.search(idref)
        if match is None:
            continue
        number = int(match.group(1))
        if number not in seen:
            seen.append(number)
    return tuple(seen)


def _extract_claims(claims_sections: list[Tag]) -> tuple[Claim, ...]:
    """Return all claims across ``claims_sections``, concatenated in document order.

    Claim divs without a ``num`` attribute are style wrappers, not claims, and
    are excluded by the ``div.claim[num]`` selector.
    """
    claims: list[Claim] = []
    for section in claims_sections:
        for div in section.select("div.claim[num]"):
            number = int(div["num"])
            text = _normalize_whitespace(div.get_text())
            depends_on = _extract_claim_dependencies(div)
            claims.append(Claim(number=number, text=text, depends_on=depends_on))
    return tuple(claims)


def _extract_cpc_codes(soup: BeautifulSoup) -> tuple[str, ...]:
    """Return leaf CPC codes in document order, deduplicated."""
    codes: list[str] = []
    for span in soup.select("span[itemprop=Code]"):
        code = span.get_text(strip=True)
        if _CPC_LEAF_RE.match(code) and code not in codes:
            codes.append(code)
    return tuple(codes)


def _extract_reference_pub_numbers(soup: BeautifulSoup, row_itemprop: str) -> tuple[str, ...]:
    """Return publicationNumber values from ``tr[itemprop=row_itemprop]`` rows.

    Rows without a publication number (e.g. scholar entries) are skipped.
    """
    refs: list[str] = []
    for row in soup.select(f"tr[itemprop={row_itemprop}]"):
        span = row.select_one("span[itemprop=publicationNumber]")
        if span is not None:
            refs.append(span.get_text(strip=True))
    return tuple(refs)


def parse_patent_html(html: str) -> GPatentDoc:
    """Parse a Google Patents page into a :class:`GPatentDoc`.

    Extraction rules (verified against pages saved on 2026-08-27):

    - Publication number: first ``dd[itemprop=publicationNumber]``.
    - Title: first ``span[itemprop=title]`` that is not inside a
      ``tr[itemprop]`` reference row; trailing whitespace collapsed.
    - Abstract: text of ``section[itemprop=abstract]`` (empty string if the
      section is absent).
    - Claims: ``div.claim[num]`` inside ``section[itemprop=claims]``; claim
      text is the whitespace-normalized text of the div; dependencies come
      from ``claim-ref`` elements (``idref="CLM-00001"`` -> 1).
    - CPC codes: ``span[itemprop=Code]`` values that look like leaf codes
      (``^[A-Z]\\d{2}[A-Z]\\d+/\\d+$``), deduplicated, document order.
    - Status/expiration: ``span[itemprop=status]`` and
      ``time[itemprop=expiration]`` (empty strings if absent).
    - Priority/publication date and assignee: first matching itemprop
      element that is not inside a ``tr[itemprop]`` reference row.
    - backward_refs / forward_refs / similar: ``span[itemprop=
      publicationNumber]`` inside ``tr[itemprop=backwardReferences /
      forwardReferences / similarDocuments]`` rows; rows without a
      publication number (e.g. scholar entries) are skipped.

    Raises:
        ValueError: If the page has no publication number or no claims
            section (not a patent detail page).
        TypeError: If ``html`` is not a string.
    """
    if not isinstance(html, str):
        raise TypeError(f"html must be a str, got {type(html).__name__}")

    soup = BeautifulSoup(html, "lxml")

    pub_number_tag = soup.select_one("dd[itemprop=publicationNumber]")
    if pub_number_tag is None:
        raise ValueError("page has no publication number (not a patent detail page)")
    pub_number = pub_number_tag.get_text(strip=True)

    claims_sections = soup.select("section[itemprop=claims]")
    if not claims_sections:
        raise ValueError("page has no claims section (not a patent detail page)")
    claims = _extract_claims(claims_sections)

    title_tag = _first_outside_reference_rows(soup, "span[itemprop=title]")
    title = title_tag.get_text().strip() if title_tag is not None else ""

    abstract_section = soup.select_one("section[itemprop=abstract]")
    abstract = (
        _normalize_whitespace(abstract_section.get_text()) if abstract_section is not None else ""
    )

    cpc_codes = _extract_cpc_codes(soup)

    status_tag = soup.select_one("span[itemprop=status]")
    status_display = status_tag.get_text(strip=True) if status_tag is not None else ""

    expiration_tag = soup.select_one("time[itemprop=expiration]")
    expiration = expiration_tag.get_text(strip=True) if expiration_tag is not None else ""

    priority_date_tag = _first_outside_reference_rows(soup, "[itemprop=priorityDate]")
    priority_date = priority_date_tag.get_text(strip=True) if priority_date_tag is not None else ""

    publication_date_tag = _first_outside_reference_rows(soup, "[itemprop=publicationDate]")
    publication_date = (
        publication_date_tag.get_text(strip=True) if publication_date_tag is not None else ""
    )

    assignee_tag = _first_outside_reference_rows(soup, "[itemprop=assigneeCurrent]")
    assignee = assignee_tag.get_text(strip=True) if assignee_tag is not None else ""

    backward_refs = _extract_reference_pub_numbers(soup, "backwardReferences")
    forward_refs = _extract_reference_pub_numbers(soup, "forwardReferences")
    similar = _extract_reference_pub_numbers(soup, "similarDocuments")

    return GPatentDoc(
        pub_number=pub_number,
        title=title,
        abstract=abstract,
        claims=claims,
        cpc_codes=cpc_codes,
        status_display=status_display,
        expiration=expiration,
        priority_date=priority_date,
        publication_date=publication_date,
        assignee=assignee,
        backward_refs=backward_refs,
        forward_refs=forward_refs,
        similar=similar,
    )


def main(argv: list[str]) -> int:
    """Fetch each publication number given on the command line."""
    if not argv:
        print("usage: gp_fetch.py PUB [PUB ...]", file=sys.stderr)
        return 2
    for pub in argv:
        try:
            path = fetch_patent_html(pub)
        except (ValueError, httpx.HTTPError) as exc:
            print(f"{pub}: FAILED: {exc}", file=sys.stderr)
            return 1
        size = path.stat().st_size
        print(f"{pub}: {path} ({size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
