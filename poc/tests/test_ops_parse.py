"""Tests for EPO OPS XML response parsing (poc/ops_parse.py)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from ops_parse import (
    OpsFamily,
    OpsSearchPage,
    parse_biblio_xml,
    parse_family_xml,
    parse_legal_xml,
    parse_search_xml,
)

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "開発資料" / "v0.1" / "raw" / "ops"

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
