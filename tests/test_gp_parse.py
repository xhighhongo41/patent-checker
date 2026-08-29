"""Tests for Google Patents HTML parsing (poc/gp_fetch.py)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from patent_checker.gp.parse import GPatentDoc, parse_patent_html

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "gp"

_CPC_LEAF_RE = re.compile(r"^[A-Z]\d{2}[A-Z]\d+/\d+$")


def _load_fixture(pub: str) -> str:
    """Return the saved Google Patents HTML for ``pub``, skipping if unavailable."""
    path = FIXTURE_DIR / f"{pub}.html"
    if not path.exists():
        pytest.skip(f"fixture not available: {path}")
    return path.read_text(encoding="utf-8")


# --- US11468338B2: values below were confirmed against the raw HTML by grep and
# --- must not be adjusted to match implementation output. ---


@pytest.fixture(scope="module")
def us11468338b2() -> GPatentDoc:
    """Parsed document for the fully-verified US11468338B2 fixture."""
    html = _load_fixture("US11468338B2")
    return parse_patent_html(html)


def test_us11468338b2_pub_number(us11468338b2: GPatentDoc) -> None:
    """Publication number is read from the document-level dd element."""
    assert us11468338b2.pub_number == "US11468338B2"


def test_us11468338b2_title(us11468338b2: GPatentDoc) -> None:
    """Title matches the document-level span, trailing whitespace stripped."""
    assert us11468338b2.title == "Compiling models for dedicated hardware"


def test_us11468338b2_claim1_is_independent(us11468338b2: GPatentDoc) -> None:
    """There are 20 claims and claim 1 is an independent NN-model claim."""
    assert len(us11468338b2.claims) == 20
    claim1 = us11468338b2.claims[0]
    assert claim1.number == 1
    assert claim1.depends_on == ()
    assert "neural network (NN) model" in claim1.text


def test_us11468338b2_claim2_depends_on_claim1(us11468338b2: GPatentDoc) -> None:
    """Claim 2 is dependent on claim 1."""
    assert us11468338b2.claims[1].depends_on == (1,)


def test_us11468338b2_status_and_expiration(us11468338b2: GPatentDoc) -> None:
    """Legal status display and adjusted expiration date are extracted verbatim."""
    assert us11468338b2.status_display == "Active"
    assert us11468338b2.expiration == "2041-04-19"


def test_us11468338b2_backward_refs(us11468338b2: GPatentDoc) -> None:
    """There are 12 backward references, including a known publication."""
    assert len(us11468338b2.backward_refs) == 12
    assert "US20060112377A1" in us11468338b2.backward_refs


def test_us11468338b2_forward_refs(us11468338b2: GPatentDoc) -> None:
    """There are 4 forward references."""
    assert len(us11468338b2.forward_refs) == 4


def test_us11468338b2_similar(us11468338b2: GPatentDoc) -> None:
    """Similar documents are bounded; rows without a publication number are skipped."""
    assert 1 <= len(us11468338b2.similar) <= 21


def test_us11468338b2_abstract(us11468338b2: GPatentDoc) -> None:
    """Abstract text contains the expected opening phrase."""
    assert "The subject tec" in us11468338b2.abstract


def test_us11468338b2_cpc_codes(us11468338b2: GPatentDoc) -> None:
    """CPC codes are all leaf-level codes matching the group/subgroup pattern."""
    assert len(us11468338b2.cpc_codes) >= 1
    assert all(_CPC_LEAF_RE.match(code) for code in us11468338b2.cpc_codes)


# --- Remaining fixtures: only structural smoke checks (individual field values
# --- have not been independently verified for these documents). ---


@pytest.mark.parametrize("pub", ["US11836520B2", "US20200104750A1", "US11461300B2"])
def test_other_fixtures_parse_without_error(pub: str) -> None:
    """Other saved pages parse without raising and yield a plausible document."""
    html = _load_fixture(pub)
    doc = parse_patent_html(html)
    assert doc.pub_number == pub
    assert len(doc.claims) >= 1


# --- Synthetic HTML tests (no fixture dependency; always run). ---

_SYNTHETIC_CLAIM_SECTION = """
<section itemprop="claims" itemscope>
  <div class="claim">
    <div num="00001" class="claim">
      <div class="claim-text">1. A widget assembly.</div>
    </div>
  </div>
</section>
"""


def test_reference_row_values_do_not_shadow_document_level_values() -> None:
    """A reference row appearing before document-level fields must not win.

    The synthetic page places a ``tr[itemprop=backwardReferences]`` row (with
    its own title/priorityDate/publicationDate) ahead of the document-level
    title and dates; the parser must still pick the document-level values.
    """
    html = f"""
    <html><body><article>
      <table><tbody>
        <tr itemprop="backwardReferences" itemscope>
          <td><span itemprop="publicationNumber">XX0000001A1</span></td>
          <td><span itemprop="title">Reference row widget</span></td>
          <td><span itemprop="priorityDate">1999-01-01</span></td>
          <td><span itemprop="publicationDate">1999-06-01</span></td>
        </tr>
      </tbody></table>

      <dd itemprop="publicationNumber">XX0000002A1</dd>
      <span itemprop="title">Document-level widget assembly</span>
      <dd><time itemprop="priorityDate" datetime="2020-01-01">2020-01-01</time></dd>
      <dd><time itemprop="publicationDate" datetime="2020-06-01">2020-06-01</time></dd>

      {_SYNTHETIC_CLAIM_SECTION}
    </article></body></html>
    """
    doc = parse_patent_html(html)
    assert doc.title == "Document-level widget assembly"
    assert doc.priority_date == "2020-01-01"
    assert doc.publication_date == "2020-06-01"


def test_missing_claims_section_raises_value_error() -> None:
    """A page with a publication number but no claims section is rejected."""
    html = """
    <html><body><article>
      <dd itemprop="publicationNumber">XX0000003A1</dd>
      <span itemprop="title">No claims here</span>
    </article></body></html>
    """
    with pytest.raises(ValueError):
        parse_patent_html(html)


def test_missing_publication_number_raises_value_error() -> None:
    """A page with a claims section but no publication number is rejected."""
    html = f"""
    <html><body><article>
      <span itemprop="title">No publication number here</span>
      {_SYNTHETIC_CLAIM_SECTION}
    </article></body></html>
    """
    with pytest.raises(ValueError):
        parse_patent_html(html)


@pytest.mark.parametrize("value", [None, 12345, b"<html></html>"])
def test_non_string_input_raises_type_error(value: object) -> None:
    """Non-string input is rejected with a TypeError."""
    with pytest.raises(TypeError):
        parse_patent_html(value)  # type: ignore[arg-type]


def test_multiple_claims_sections_are_concatenated() -> None:
    """Claims from multiple claims sections are concatenated in document order.

    This models a translated (e.g. JP/CN) page that repeats the claims once
    in the original language and once in machine translation.
    """
    html = """
    <html><body><article>
      <dd itemprop="publicationNumber">XX0000004A1</dd>

      <section itemprop="claims" itemscope>
        <div class="claim">
          <div num="00001" class="claim">
            <div class="claim-text">1. Original language widget claim.</div>
          </div>
        </div>
      </section>

      <section itemprop="claims" itemscope>
        <div class="claim">
          <div num="00001" class="claim">
            <div class="claim-text">1. Machine-translated widget claim.</div>
          </div>
        </div>
      </section>
    </article></body></html>
    """
    doc = parse_patent_html(html)
    assert len(doc.claims) == 2
    assert "Original language" in doc.claims[0].text
    assert "Machine-translated" in doc.claims[1].text


# --- Fallback (1): claims section present, but no claim carries a ``num`` attribute.


def test_claims_without_num_attribute_fall_back_to_section_text() -> None:
    """Old-style pages without ``num`` attributes yield section text, not an error.

    WO A2 / older CN / KR pages mark claims up without ``num`` attributes, so
    no numbered claim can be recovered. The parser must keep the text instead
    of rejecting the page.
    """
    html = """
    <html><body><article>
      <dd itemprop="publicationNumber">XX0000005A1</dd>
      <section itemprop="claims" itemscope>
        <div class="claims ocr">
          <div class="claim-text">CLAIMS What is claimed is:</div>
          <div class="claim-text">1. A widget assembly comprising a frame.</div>
          <div class="claim-text">2. The assembly of claim 1, wherein the frame is steel.</div>
        </div>
      </section>
    </article></body></html>
    """
    doc = parse_patent_html(html)
    assert doc.claims == ()
    assert "What is claimed is:" in doc.claims_fallback_text
    assert "wherein the frame is steel" in doc.claims_fallback_text
    # Whitespace is normalized, so the raw newline/indent runs are gone.
    assert "\n" not in doc.claims_fallback_text
    assert "  " not in doc.claims_fallback_text


def test_structured_claims_leave_fallback_text_empty() -> None:
    """When numbered claims are extracted, no fallback text is produced."""
    html = f"""
    <html><body><article>
      <dd itemprop="publicationNumber">XX0000006A1</dd>
      {_SYNTHETIC_CLAIM_SECTION}
    </article></body></html>
    """
    doc = parse_patent_html(html)
    assert len(doc.claims) == 1
    assert doc.claims_fallback_text == ""


# --- Fallback (2): numbered claims present, but no ``claim-ref`` markup.


def _claim_div(number: int, text: str, *, ref: int | None = None) -> str:
    """Return a synthetic ``div.claim[num]`` block, optionally with a claim-ref."""
    body = text if ref is None else text.replace("claim", f'<claim-ref idref="CLM-{ref:05d}">claim')
    if ref is not None:
        body += "</claim-ref>"
    return f'<div num="{number:05d}" class="claim"><div class="claim-text">{body}</div></div>'


def test_dependencies_are_recovered_from_claim_text_without_claim_ref() -> None:
    """Pages without ``claim-ref`` markup get dependencies from the claim text."""
    html = f"""
    <html><body><article>
      <dd itemprop="publicationNumber">XX0000007A1</dd>
      <section itemprop="claims" itemscope>
        <div class="claim">
          {_claim_div(1, "1. A widget assembly.")}
          {_claim_div(2, "2. The assembly of claim 1, wherein it is steel.")}
          {_claim_div(3, "3. The assembly according to claim 2, further comprising a lid.")}
          {_claim_div(4, "4. The assembly of any one of claims 1 to 3, painted red.")}
        </div>
      </section>
    </article></body></html>
    """
    doc = parse_patent_html(html)
    assert [claim.number for claim in doc.claims] == [1, 2, 3, 4]
    assert doc.claims[0].depends_on == ()
    assert doc.claims[1].depends_on == (1,)
    assert doc.claims[2].depends_on == (2,)
    assert doc.claims[3].depends_on == (1, 2, 3)


def test_claim_ref_markup_wins_over_claim_text() -> None:
    """Text recovery does not run for claims whose dependencies came from markup.

    The synthetic claim 3 has a ``claim-ref`` pointing at claim 1 while its
    text mentions claim 2; the structured value must survive untouched.
    """
    html = f"""
    <html><body><article>
      <dd itemprop="publicationNumber">XX0000008A1</dd>
      <section itemprop="claims" itemscope>
        <div class="claim">
          {_claim_div(1, "1. A widget assembly.")}
          {_claim_div(2, "2. The assembly of claim 1, wherein it is steel.", ref=1)}
          {_claim_div(3, "3. The assembly of claim 2, further comprising a lid.", ref=1)}
        </div>
      </section>
    </article></body></html>
    """
    doc = parse_patent_html(html)
    assert doc.claims[1].depends_on == (1,)
    assert doc.claims[2].depends_on == (1,)


# --- Cross-fixture smoke test over every saved page. ---


def test_every_fixture_yields_claims_or_fallback_text() -> None:
    """Every saved page parses and exposes claims either structured or as text.

    This guards the two known Google Patents markup gaps (no ``num``
    attributes, no ``claim-ref`` elements): whichever path is taken, callers
    must never end up with an empty document.
    """
    paths = sorted(FIXTURE_DIR.glob("*.html"))
    if not paths:
        pytest.skip(f"fixtures not available: {FIXTURE_DIR}")

    empty: list[str] = []
    for path in paths:
        doc = parse_patent_html(path.read_text(encoding="utf-8"))
        if not doc.claims and not doc.claims_fallback_text:
            empty.append(path.name)
    assert not empty, f"no claims and no fallback text: {empty}"
