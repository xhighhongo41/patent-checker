"""Tests for the publication-number normalization module (poc/pubnum.py)."""

from __future__ import annotations

import dataclasses

import pytest

from patent_checker.pubnum import PubNumber, parse_pubnum

# (input text, expected country, expected number, expected kind)
VALID_CASES = [
    ("US.11468338.B2", "US", "11468338", "B2"),  # docdb spelling
    ("US11468338B2", "US", "11468338", "B2"),  # epodoc / Google spelling
    ("US20200104750A1", "US", "20200104750", "A1"),  # published application
    ("US.11468338", "US", "11468338", ""),  # docdb, no kind
    ("US11468338", "US", "11468338", ""),  # epodoc, no kind
    ("JP2006187606A", "JP", "2006187606", "A"),  # single-letter kind
    ("WO2020104750A1", "WO", "2020104750", "A1"),  # PCT publication
    ("us11468338b2", "US", "11468338", "B2"),  # lower case input
    ("US-11468338-B2", "US", "11468338", "B2"),  # dash separators
    ("US 11468338 B2", "US", "11468338", "B2"),  # space separators
    ("  US11468338B2  ", "US", "11468338", "B2"),  # surrounding whitespace
    ("EP.0123456.B1", "EP", "0123456", "B1"),  # leading zero preserved
]


@pytest.mark.parametrize(("text", "country", "number", "kind"), VALID_CASES)
def test_parse_pubnum_valid(text: str, country: str, number: str, kind: str) -> None:
    """parse_pubnum accepts docdb/epodoc/Google spellings and normalizes them."""
    result = parse_pubnum(text)
    assert result.country == country
    assert result.number == number
    assert result.kind == kind


# (input text, expected docdb spelling, expected epodoc/google spelling)
ROUNDTRIP_CASES = [
    ("US.11468338.B2", "US.11468338.B2", "US11468338B2"),
    ("US11468338B2", "US.11468338.B2", "US11468338B2"),
    ("US.11468338", "US.11468338", "US11468338"),
    ("US11468338", "US.11468338", "US11468338"),
    ("EP.0123456.B1", "EP.0123456.B1", "EP0123456B1"),
]


@pytest.mark.parametrize(("text", "docdb", "epodoc"), ROUNDTRIP_CASES)
def test_parse_pubnum_roundtrip_formats(text: str, docdb: str, epodoc: str) -> None:
    """docdb()/epodoc()/google() reproduce the canonical spellings, kind or not."""
    result = parse_pubnum(text)
    assert result.docdb() == docdb
    assert result.epodoc() == epodoc
    assert result.google() == epodoc


INVALID_VALUES = [
    "",
    "US",
    "11468338B2",  # no country code
    "USA11468338B2",  # three-letter country code
    "US11468338B22",  # kind with two digits
    "US11468338BB",  # kind with two letters
    "US..11468338..B2",  # doubled separators
]


@pytest.mark.parametrize("text", INVALID_VALUES)
def test_parse_pubnum_invalid_raises_value_error(text: str) -> None:
    """Unparseable strings raise ValueError."""
    with pytest.raises(ValueError):
        parse_pubnum(text)


@pytest.mark.parametrize("value", [None, b"US11468338B2"])
def test_parse_pubnum_non_string_raises_type_error(value: object) -> None:
    """Non-string input raises TypeError."""
    with pytest.raises(TypeError):
        parse_pubnum(value)  # type: ignore[arg-type]


def test_pubnumber_is_frozen() -> None:
    """PubNumber instances are immutable."""
    pubnum = PubNumber(country="US", number="11468338", kind="B2")
    with pytest.raises(dataclasses.FrozenInstanceError):
        pubnum.country = "JP"  # type: ignore[misc]
