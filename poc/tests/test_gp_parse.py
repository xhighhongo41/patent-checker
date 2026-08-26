"""Tests for Google Patents HTML parsing (poc/gp_fetch.py)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from gp_fetch import GPatentDoc, parse_patent_html

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "開発資料" / "v0.1" / "raw" / "gp"

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
