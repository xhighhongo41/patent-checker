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


# (docdb input, expected google() spelling)
# US published applications: docdb's 10-digit number (4-digit year +
# 6-digit serial) is padded to Google Patents' 11-digit spelling by
# inserting a "0" right after the year.
GOOGLE_ZFILL_CASES = [
    ("US.2024111636.A1", "US20240111636A1"),
    ("US.2020349465.A1", "US20200349465A1"),
    ("US.2017302753.A1", "US20170302753A1"),
    ("US.2007016547.A1", "US20070016547A1"),
    ("US.2009172824.A1", "US20090172824A1"),
    ("US.2023153438.A1", "US20230153438A1"),
]


@pytest.mark.parametrize(("text", "expected_google"), GOOGLE_ZFILL_CASES)
def test_google_pads_us_published_application_number(text: str, expected_google: str) -> None:
    """google() inserts a "0" after the year for 10-digit US publication numbers."""
    result = parse_pubnum(text)
    assert result.google() == expected_google


# (docdb input, expected google() spelling)
# Already-11-digit US publication numbers pass through google() unchanged.
GOOGLE_PASSTHROUGH_ELEVEN_DIGIT_CASES = [
    ("US.20260099470.A1", "US20260099470A1"),
    ("US.20260228482.A1", "US20260228482A1"),
]


@pytest.mark.parametrize(("text", "expected_google"), GOOGLE_PASSTHROUGH_ELEVEN_DIGIT_CASES)
def test_google_passes_through_eleven_digit_us_number(text: str, expected_google: str) -> None:
    """google() does not touch numbers that are already 11 digits."""
    result = parse_pubnum(text)
    assert result.google() == expected_google


# (docdb input, expected google() spelling)
# US granted patents have short, non-year-prefixed serials and must not be
# zero-padded.
GOOGLE_PASSTHROUGH_US_GRANT_CASES = [
    ("US.11468338.B2", "US11468338B2"),
    ("US.9098446.B1", "US9098446B1"),
    ("US.10749545.B1", "US10749545B1"),
]


@pytest.mark.parametrize(("text", "expected_google"), GOOGLE_PASSTHROUGH_US_GRANT_CASES)
def test_google_passes_through_us_granted_patent(text: str, expected_google: str) -> None:
    """google() does not zero-pad US granted-patent numbers."""
    result = parse_pubnum(text)
    assert result.google() == expected_google


# (docdb input, expected google() spelling)
# Non-US offices are never zero-padded, even a KR 11-digit publication
# number that superficially resembles the US year-prefixed shape.
GOOGLE_PASSTHROUGH_NON_US_CASES = [
    ("KR.20160004285.A", "KR20160004285A"),
    ("CN.101414299.A", "CN101414299A"),
    ("KR.102480287.B1", "KR102480287B1"),
    ("WO.2026157016.A1", "WO2026157016A1"),
    ("GB.2469308.A", "GB2469308A"),
    ("EP.1672502.A1", "EP1672502A1"),
]


@pytest.mark.parametrize(("text", "expected_google"), GOOGLE_PASSTHROUGH_NON_US_CASES)
def test_google_passes_through_non_us_office(text: str, expected_google: str) -> None:
    """google() never zero-pads publication numbers from non-US offices."""
    result = parse_pubnum(text)
    assert result.google() == expected_google


def test_google_differs_from_epodoc_for_us_published_application() -> None:
    """epodoc() keeps the 10-digit number while google() pads it to 11 digits."""
    result = parse_pubnum("US.2024111636.A1")
    assert result.epodoc() == "US2024111636A1"
    assert result.google() == "US20240111636A1"
    assert result.epodoc() != result.google()


def test_google_does_not_pad_non_year_looking_ten_digit_number() -> None:
    """A 10-digit US number not prefixed by "19"/"20" is left untouched."""
    pubnum = PubNumber(country="US", number="1234567890", kind="A1")
    assert pubnum.google() == "US1234567890A1"


def test_docdb_roundtrip_keeps_google_style_eleven_digit_number() -> None:
    """Parsing a Google-style 11-digit number keeps 11 digits in docdb().

    This is the documented, one-directional asymmetry: parse_pubnum does not
    shrink an 11-digit Google Patents number back down to the 10-digit
    docdb form.
    """
    result = parse_pubnum("US20240111636A1")
    assert result.docdb() == "US.20240111636.A1"
