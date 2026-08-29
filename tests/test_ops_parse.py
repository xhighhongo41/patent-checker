"""Tests for EPO OPS XML response parsing (poc/ops_parse.py)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from patent_checker.ops.parse import (
    OpsFamily,
    OpsSearchPage,
    parse_biblio_xml,
    parse_claims_xml,
    parse_family_xml,
    parse_legal_xml,
    parse_search_biblio_xml,
    parse_search_xml,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ops"

_CPC_RE = re.compile(r"^[A-Z]\d{2}[A-Z]\d+/\d+$")


def _load_fixture(name: str) -> bytes:
    """Return the raw bytes of a saved OPS fixture, skipping if unavailable."""
    path = FIXTURE_DIR / name
    if not path.exists():
        pytest.skip(f"fixture not available: {path}")
    return path.read_bytes()


# --- Fixture-dependent tests: values below were confirmed against the raw
# --- OPS XML and must not be adjusted to match implementation output. ---


def test_parse_search_xml_fixture() -> None:
    """The saved search response is parsed into the expected page/hits."""
    xml = _load_fixture("20260827-001753_search_ab40de2f9f.xml")
    page = parse_search_xml(xml)
    assert isinstance(page, OpsSearchPage)
    assert page.total_count == 1958269
    assert page.query == "ta = computer"
    assert page.begin == 1
    assert page.end == 5
    assert len(page.hits) == 5
    assert page.hits[0].pub == "WO.2026170235.A1"
    assert page.hits[0].family_id == "95065549"


def test_parse_biblio_xml_fixture() -> None:
    """The saved biblio response is parsed into the expected bibliographic data."""
    xml = _load_fixture("20260827-001754_biblio_US.11468338.B2.xml")
    biblio = parse_biblio_xml(xml)
    assert biblio.pub == "US.11468338.B2"
    assert biblio.family_id == "69719947"
    assert biblio.title == "COMPILING MODELS FOR DEDICATED HARDWARE"
    assert biblio.abstract != ""
    assert any("rossi" in name.lower() for name in biblio.inventors)
    assert any("apple" in name.lower() for name in biblio.applicants)
    assert len(biblio.cpc) >= 1
    assert all(_CPC_RE.match(code) for code in biblio.cpc)
    assert len(biblio.ipc) >= 1
    assert biblio.npl_citation_count == 4
    assert len(biblio.cited_patents) >= 1
    assert all(pub.count(".") >= 2 for pub in biblio.cited_patents)


def test_parse_legal_xml_fixture() -> None:
    """The saved legal response is parsed into the expected legal events."""
    xml = _load_fixture("20260827-001754_legal_US.11468338.B2.xml")
    events = parse_legal_xml(xml)
    assert len(events) == 10
    assert events[0].code == "AS"
    assert events[0].desc == "ASSIGNMENT"
    assert all(event.code != "" for event in events)
    assert len(events[0].pre_lines) >= 2
    assert "ASSIGNMENT" in events[0].pre_lines[0]


def test_parse_family_xml_fixture() -> None:
    """The saved family response is parsed into the expected family members."""
    xml = _load_fixture("20260827-001755_family_US.11468338.B2.xml")
    family = parse_family_xml(xml)
    assert isinstance(family, OpsFamily)
    assert family.family_id == "69719947"
    assert len(family.members) == 5
    assert "US.2020082274.A1" in family.members


# --- Synthetic XML tests (no fixture dependency; always run). ---

_INVALID_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns="http://www.epo.org/exchange" xmlns:ops="http://ops.epo.org">
    <ops:some-other-element/>
</ops:world-patent-data>
"""


def test_parse_search_xml_not_a_search_response_raises_value_error() -> None:
    """A document without ops:biblio-search is rejected."""
    with pytest.raises(ValueError):
        parse_search_xml(_INVALID_XML)


def test_parse_biblio_xml_not_a_biblio_response_raises_value_error() -> None:
    """A document without exchange-document is rejected."""
    with pytest.raises(ValueError):
        parse_biblio_xml(_INVALID_XML)


def test_parse_legal_xml_not_a_legal_response_raises_value_error() -> None:
    """A document without ops:legal elements is rejected."""
    with pytest.raises(ValueError):
        parse_legal_xml(_INVALID_XML)


def test_parse_family_xml_not_a_family_response_raises_value_error() -> None:
    """A document without ops:family-member elements is rejected."""
    with pytest.raises(ValueError):
        parse_family_xml(_INVALID_XML)


_NAME_PRIORITY_BIBLIO_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns="http://www.epo.org/exchange" xmlns:ops="http://ops.epo.org">
    <exchange-documents>
        <exchange-document country="US" doc-number="1234567" kind="B2" family-id="1">
            <bibliographic-data>
                <publication-reference>
                    <document-id document-id-type="docdb">
                        <country>US</country>
                        <doc-number>1234567</doc-number>
                        <kind>B2</kind>
                        <date>20200101</date>
                    </document-id>
                </publication-reference>
                <invention-title lang="en">TEST INVENTION</invention-title>
                <parties>
                    <applicants>
                        <applicant sequence="1" data-format="epodoc">
                            <applicant-name><name>TEST CORP [US]</name></applicant-name>
                        </applicant>
                        <applicant sequence="1" data-format="original">
                            <applicant-name><name>Test Corp.</name></applicant-name>
                        </applicant>
                    </applicants>
                    <inventors>
                        <inventor sequence="1" data-format="epodoc">
                            <inventor-name><name>DOE JANE [US]</name></inventor-name>
                        </inventor>
                    </inventors>
                </parties>
            </bibliographic-data>
        </exchange-document>
    </exchange-documents>
</ops:world-patent-data>
"""


def test_parse_biblio_xml_prefers_original_data_format() -> None:
    """When both epodoc and original names exist, the original one is used."""
    biblio = parse_biblio_xml(_NAME_PRIORITY_BIBLIO_XML)
    assert biblio.applicants == ("Test Corp.",)
    # No "original" inventor entry is present, so the epodoc one is used as a fallback.
    assert biblio.inventors == ("DOE JANE [US]",)


_LEGAL_NO_GAZETTE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns="http://www.epo.org/exchange" xmlns:ops="http://ops.epo.org">
    <ops:patent-family legal="true">
        <ops:family-member family-id="1">
            <ops:legal code="XX" desc="TEST EVENT">
                <ops:pre line="00001">line one</ops:pre>
                <ops:L001EP desc="Country Code">US</ops:L001EP>
            </ops:legal>
        </ops:family-member>
    </ops:patent-family>
</ops:world-patent-data>
"""


def test_parse_legal_xml_missing_gazette_date_returns_empty_string() -> None:
    """An event without a Gazette DATE child yields an empty gazette_date."""
    events = parse_legal_xml(_LEGAL_NO_GAZETTE_XML)
    assert len(events) == 1
    assert events[0].code == "XX"
    assert events[0].gazette_date == ""
    assert events[0].pre_lines == ("line one",)


_FAMILY_DUP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns="http://www.epo.org/exchange" xmlns:ops="http://ops.epo.org">
    <ops:patent-family legal="false">
        <ops:family-member family-id="42">
            <publication-reference>
                <document-id document-id-type="docdb">
                    <country>US</country>
                    <doc-number>1111111</doc-number>
                    <kind>A1</kind>
                </document-id>
            </publication-reference>
        </ops:family-member>
        <ops:family-member family-id="42">
            <publication-reference>
                <document-id document-id-type="docdb">
                    <country>US</country>
                    <doc-number>1111111</doc-number>
                    <kind>A1</kind>
                </document-id>
            </publication-reference>
        </ops:family-member>
    </ops:patent-family>
</ops:world-patent-data>
"""


def test_parse_family_xml_deduplicates_members() -> None:
    """Duplicate family-member document-ids are collapsed to a single member."""
    family = parse_family_xml(_FAMILY_DUP_XML)
    assert family.family_id == "42"
    assert family.members == ("US.1111111.A1",)


# --- Claims full text -------------------------------------------------------
# --- The claim counts and dependencies asserted below were read off the raw
# --- OPS XML (claim-text elements and their leading "N." numbering) and must
# --- not be adjusted to match implementation output. ---


def test_parse_claims_xml_wo_fixture() -> None:
    """The saved WO claims response yields all 21 claims with their dependencies."""
    xml = _load_fixture("20260828-120540_claims_WO.2026157016.A1.xml")
    claims = parse_claims_xml(xml)
    # The fixture holds 21 claim-text elements numbered "1." to "21.".
    assert len(claims) == 21
    assert tuple(claim.number for claim in claims) == tuple(range(1, 22))
    assert claims[0].text.startswith("1. An apparatus, comprising:")
    assert claims[0].depends_on == ()
    # "2. The apparatus of claim 1, ..."
    assert claims[1].depends_on == (1,)
    # "3. The apparatus of claim 1 or 2, ..."
    assert claims[2].depends_on == (1, 2)
    # "4. The apparatus of any one of claims 1-3, ..."
    assert claims[3].depends_on == (1, 2, 3)
    # "8. One or more non-transitory computer-readable media storing ..." (independent)
    assert claims[7].depends_on == ()
    assert claims[14].text.startswith("15. A method, comprising:")
    # Whitespace inside a claim is normalized to single spaces.
    assert "\n" not in claims[0].text


def test_parse_claims_xml_wo_single_claim_text_fixture() -> None:
    """A whole claim set packed into one claim-text element is split into 25 claims."""
    xml = _load_fixture("20260828-120541_claims_WO.2026142734.A1.xml")
    claims = parse_claims_xml(xml)
    # The single claim-text starts with a stray "1. Claims" heading and then
    # runs from "1. A computing system" to "25. The one or more ...".
    assert len(claims) == 25
    assert tuple(claim.number for claim in claims) == tuple(range(1, 26))
    assert "A computing system, comprising:" in claims[0].text
    assert claims[1].depends_on == (1,)
    assert claims[2].depends_on == (1, 2)


def test_parse_claims_xml_ep_fixture() -> None:
    """The saved EP claims response yields 6 claims spread over 11 claim-text elements."""
    xml = _load_fixture("20260828-130733_claims_EP.4645156.A1.xml")
    claims = parse_claims_xml(xml)
    assert len(claims) == 6
    assert tuple(claim.number for claim in claims) == (1, 2, 3, 4, 5, 6)
    assert claims[0].text.startswith("1. A computer implemented method for text segmentation")
    # Claim 1 continues in the following claim-text elements ("- embedding (4), ...").
    assert "embedding (4)" in claims[0].text
    # "2. The method of claim 1, ..."
    assert claims[1].depends_on == (1,)
    # "3. The method according to any of the preceding claims, ..."
    assert claims[2].depends_on == (1, 2)


_CLAIMS_MULTI_TEXT_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns="http://www.epo.org/exchange" xmlns:ops="http://ops.epo.org">
    <ftxt:fulltext-documents xmlns="http://www.epo.org/fulltext"
                             xmlns:ftxt="http://www.epo.org/fulltext">
        <ftxt:fulltext-document fulltext-format="text-only">
            <claims lang="EN">
                <claim>
                    <claim-text>1. A device comprising:
a widget; and</claim-text>
                    <claim-text>a gadget attached to the widget.</claim-text>
                    <claim-text>2. The device of claim 1, wherein the widget is round.</claim-text>
                </claim>
            </claims>
        </ftxt:fulltext-document>
    </ftxt:fulltext-documents>
</ops:world-patent-data>
"""


def test_parse_claims_xml_joins_claim_text_elements_of_one_claim() -> None:
    """A claim split over several claim-text elements is rejoined into one claim."""
    claims = parse_claims_xml(_CLAIMS_MULTI_TEXT_XML)
    assert len(claims) == 2
    assert claims[0].number == 1
    assert (
        claims[0].text == "1. A device comprising: a widget; and a gadget attached to the widget."
    )
    assert claims[0].depends_on == ()
    assert claims[1].number == 2
    assert claims[1].depends_on == (1,)


_CLAIMS_PACKED_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns="http://www.epo.org/exchange" xmlns:ops="http://ops.epo.org">
    <ftxt:fulltext-documents xmlns="http://www.epo.org/fulltext"
                             xmlns:ftxt="http://www.epo.org/fulltext">
        <ftxt:fulltext-document fulltext-format="text-only">
            <claims lang="EN">
                <claim>
                    <claim-text>1. A method comprising a step.
2. The method of claim 1, fast.3. The method of any one of claims 1-2, slow.
4. The method of claim 3, repeated 2. 5 times.</claim-text>
                </claim>
            </claims>
        </ftxt:fulltext-document>
    </ftxt:fulltext-documents>
</ops:world-patent-data>
"""


def test_parse_claims_xml_splits_several_claims_inside_one_claim_text() -> None:
    """Claims packed into one element split at line starts and after a sentence period."""
    claims = parse_claims_xml(_CLAIMS_PACKED_XML)
    assert tuple(claim.number for claim in claims) == (1, 2, 3, 4)
    assert claims[1].text == "2. The method of claim 1, fast."
    assert claims[2].depends_on == (1, 2)
    # "2." inside claim 4 does not restart the numbering (it is not increasing).
    assert claims[3].text.endswith("repeated 2. 5 times.")
    assert claims[3].depends_on == (3,)


_CLAIMS_LANG_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns="http://www.epo.org/exchange" xmlns:ops="http://ops.epo.org">
    <ftxt:fulltext-documents xmlns="http://www.epo.org/fulltext"
                             xmlns:ftxt="http://www.epo.org/fulltext">
        <ftxt:fulltext-document fulltext-format="text-only">
            <claims lang="DE">
                <claim>
                    <claim-text>1. Eine Vorrichtung.</claim-text>
                </claim>
            </claims>
            <claims lang="EN">
                <claim>
                    <claim-text>1. An apparatus.</claim-text>
                    <claim-text>2. The apparatus of claim 1.</claim-text>
                </claim>
            </claims>
        </ftxt:fulltext-document>
    </ftxt:fulltext-documents>
</ops:world-patent-data>
"""


def test_parse_claims_xml_prefers_the_english_claim_set() -> None:
    """With several language variants present, the English one is used."""
    claims = parse_claims_xml(_CLAIMS_LANG_XML)
    assert len(claims) == 2
    assert claims[0].text == "1. An apparatus."


_CLAIMS_NO_LANG_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns="http://www.epo.org/exchange" xmlns:ops="http://ops.epo.org">
    <ftxt:fulltext-documents xmlns="http://www.epo.org/fulltext"
                             xmlns:ftxt="http://www.epo.org/fulltext">
        <ftxt:fulltext-document fulltext-format="text-only">
            <claims>
                <claim>
                    <claim-text>5. A late-numbered claim.</claim-text>
                    <claim-text>7. The claim of claim 5.</claim-text>
                </claim>
            </claims>
        </ftxt:fulltext-document>
    </ftxt:fulltext-documents>
</ops:world-patent-data>
"""


def test_parse_claims_xml_accepts_non_consecutive_numbering() -> None:
    """Claim numbers are taken as printed, even when they neither start at 1 nor run on."""
    claims = parse_claims_xml(_CLAIMS_NO_LANG_XML)
    assert tuple(claim.number for claim in claims) == (5, 7)
    assert claims[1].depends_on == (5,)


_CLAIMS_UNNUMBERED_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<ops:world-patent-data xmlns="http://www.epo.org/exchange" xmlns:ops="http://ops.epo.org">
    <ftxt:fulltext-documents xmlns="http://www.epo.org/fulltext"
                             xmlns:ftxt="http://www.epo.org/fulltext">
        <ftxt:fulltext-document fulltext-format="text-only">
            <claims lang="EN">
                <claim>
                    <claim-text>An apparatus without any printed numbering.</claim-text>
                </claim>
            </claims>
        </ftxt:fulltext-document>
    </ftxt:fulltext-documents>
</ops:world-patent-data>
"""


def test_parse_claims_xml_without_claims_element_raises_value_error() -> None:
    """A document without a fulltext claims element is rejected."""
    with pytest.raises(ValueError):
        parse_claims_xml(_INVALID_XML)


def test_parse_claims_xml_without_numbered_claims_raises_value_error() -> None:
    """Claim text carrying no "N." numbering cannot be split and is rejected."""
    with pytest.raises(ValueError):
        parse_claims_xml(_CLAIMS_UNNUMBERED_XML)


# --- Zero-hit fault normalization -------------------------------------------


def test_parse_search_xml_entity_not_found_fault_is_an_empty_page() -> None:
    """The 404 body of a zero-hit search parses as a page with no hits."""
    xml = _load_fixture("20260829-capture_search_zero-hit-404.xml")
    page = parse_search_xml(xml)
    assert page.total_count == 0
    assert page.hits == ()
    assert page.query == ""
    assert page.begin == 0
    assert page.end == 0


def test_parse_search_biblio_xml_entity_not_found_fault_is_an_empty_page() -> None:
    """The same 404 body parses as an empty biblio-constituent page."""
    xml = _load_fixture("20260829-capture_search_zero-hit-404.xml")
    page = parse_search_biblio_xml(xml)
    assert page.total_count == 0
    assert page.docs == ()
    assert page.begin == 0
    assert page.end == 0


_OTHER_FAULT_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<fault xmlns="http://ops.epo.org">
    <code>CLIENT.InvalidQuery</code>
    <message>Invalid query</message>
</fault>
"""


def test_parse_search_xml_other_fault_raises_value_error() -> None:
    """Faults other than SERVER.EntityNotFound are real errors."""
    with pytest.raises(ValueError, match="CLIENT.InvalidQuery"):
        parse_search_xml(_OTHER_FAULT_XML)


def test_parse_search_biblio_xml_other_fault_raises_value_error() -> None:
    """Faults other than SERVER.EntityNotFound are real errors (biblio constituent)."""
    with pytest.raises(ValueError, match="CLIENT.InvalidQuery"):
        parse_search_biblio_xml(_OTHER_FAULT_XML)
