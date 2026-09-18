"""Tests for the publication-number normalization module (poc/pubnum.py)."""

from __future__ import annotations

import dataclasses

import pytest

from patent_checker.pubnum import (
    LEADING_LETTER_COUNTRIES,
    MAX_NUMBER_DIGITS,
    PubNumber,
    parse_pubnum,
)

# (input text, expected country, expected number, expected kind)
VALID_CASES = [
    ("US.11468338.B2", "US", "11468338", "B2"),  # docdb spelling
    ("US11468338B2", "US", "11468338", "B2"),  # epodoc / Google spelling
    # Published application, pre-2026: parse_pubnum shrinks the Google-style
    # 11-digit number back to the 10-digit docdb form.
    ("US20200104750A1", "US", "2020104750", "A1"),
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


def test_docdb_roundtrip_shrinks_pre_2026_google_style_number() -> None:
    """Parsing a pre-2026 Google-style 11-digit number shrinks it back to 10 digits.

    DOCDB spells US A-kind publications with 10 digits through 2025, so
    parse_pubnum removes the "0" Google Patents inserts after the year,
    restoring the 10-digit docdb form.
    """
    result = parse_pubnum("US20240111636A1")
    assert result.docdb() == "US.2024111636.A1"


# (Google-style input, expected docdb spelling)
# DOCDB spells US A-kind publications with 10 digits through 2025 and 11
# digits from 2026 (verified against EPO OPS, 2026-09), so parse_pubnum's
# 11-to-10-digit shrink only applies to years before 2026.
US_APPLICATION_YEAR_ROUNDTRIP_CASES = [
    ("US20070016547A1", "US.2007016547.A1", "US2007016547A1", "US20070016547A1"),
    ("US20260024003A1", "US.20260024003.A1", "US20260024003A1", "US20260024003A1"),
    ("US20250000001A1", "US.2025000001.A1", "US2025000001A1", "US20250000001A1"),
    ("US20260000001A1", "US.20260000001.A1", "US20260000001A1", "US20260000001A1"),
]


@pytest.mark.parametrize(("text", "docdb", "epodoc", "google"), US_APPLICATION_YEAR_ROUNDTRIP_CASES)
def test_parse_pubnum_shrinks_us_application_number_by_year(
    text: str, docdb: str, epodoc: str, google: str
) -> None:
    """parse_pubnum shrinks 11-digit US A-kind numbers only for years before 2026."""
    result = parse_pubnum(text)
    assert result.docdb() == docdb
    assert result.epodoc() == epodoc
    assert result.google() == google


def test_parse_pubnum_roundtrips_through_google_for_pre_2026_us_application() -> None:
    """Feeding google()'s output back through parse_pubnum restores the docdb form."""
    original = parse_pubnum("US.2025131256.A1")
    roundtripped = parse_pubnum(original.google())
    assert roundtripped.docdb() == original.docdb()


# (input text, expected docdb spelling)
# Each case fails exactly one of the conditions required to shrink an
# 11-digit US number, so the number is passed through unchanged.
US_APPLICATION_SHRINK_SKIPPED_CASES = [
    ("US20260024003B2", "US.20260024003.B2"),  # kind is B-series, not A-series
    ("US20070016547", "US.20070016547"),  # no kind at all
    ("CN20070016547A", "CN.20070016547.A"),  # non-US office
    ("US20071016547A1", "US.20071016547.A1"),  # 5th digit is not "0"
    ("US18070016547A1", "US.18070016547.A1"),  # leading two digits not "19"/"20"
    ("US.202418774328.A", "US.202418774328.A"),  # 12 digits, not 11
]


@pytest.mark.parametrize(("text", "docdb"), US_APPLICATION_SHRINK_SKIPPED_CASES)
def test_parse_pubnum_does_not_shrink_when_a_condition_is_not_met(text: str, docdb: str) -> None:
    """parse_pubnum leaves the number untouched unless every shrink condition holds."""
    result = parse_pubnum(text)
    assert result.docdb() == docdb


# (input text, expected docdb spelling)
# The USPTO citation style splits the digit run with a single "/"; parse_pubnum
# accepts it and strips the slash before applying the year-based normalization.
SLASH_FORM_VALID_CASES = [
    ("US 2007/0016547 A1", "US.2007016547.A1"),
    ("US2007/0016547A1", "US.2007016547.A1"),
    ("US 2026/0024003 A1", "US.20260024003.A1"),
]


@pytest.mark.parametrize(("text", "docdb"), SLASH_FORM_VALID_CASES)
def test_parse_pubnum_accepts_single_slash_in_digit_run(text: str, docdb: str) -> None:
    """parse_pubnum accepts the USPTO citation style with one "/" in the digits."""
    result = parse_pubnum(text)
    assert result.docdb() == docdb


SLASH_FORM_INVALID_CASES = [
    "US 2007/0016/547 A1",  # two slashes
    "US /20070016547 A1",  # leading slash
    "US 20070016547/ A1",  # trailing slash
    "US//20070016547A1",  # doubled slash right after country
]


@pytest.mark.parametrize("text", SLASH_FORM_INVALID_CASES)
def test_parse_pubnum_rejects_malformed_slash_forms(text: str) -> None:
    """More than one slash, or a slash with nothing on one side, still raises."""
    with pytest.raises(ValueError):
        parse_pubnum(text)


# --- Digit-count ceiling (v1.0) ---------------------------------------------

TOO_MANY_DIGITS_CASES = [
    "US" + "1" * 13,  # 13 digits: one past the ceiling
    "US." + "9" * 40 + ".A1",  # a pasted paragraph of digits
    "US 2007/00165470000 A1",  # slash form whose digits add up past the ceiling
]


@pytest.mark.parametrize("text", TOO_MANY_DIGITS_CASES)
def test_parse_pubnum_rejects_numbers_past_the_digit_ceiling(text: str) -> None:
    """A digit run longer than MAX_NUMBER_DIGITS is rejected, not carried around.

    An unbounded digit run reaches file-system paths (cache keys are built from
    the docdb spelling) and can only fail there, with ENAMETOOLONG.
    """
    with pytest.raises(ValueError):
        parse_pubnum(text)


def test_parse_pubnum_accepts_the_longest_allowed_digit_run() -> None:
    """The ceiling itself is accepted, so real long numbers keep parsing."""
    text = "JP" + "1" * MAX_NUMBER_DIGITS + "A"
    result = parse_pubnum(text)
    assert result.number == "1" * MAX_NUMBER_DIGITS
    assert result.kind == "A"


# --- Letter-bearing number parts (v1.2) -------------------------------------

# (input text, country, number, kind, docdb spelling, epodoc/google spelling)
# OPS reports publication numbers whose number part carries letters: JP
# era-based numbers (S = Showa, H = Heisei), an office code inside an IN
# application number, and the TW/HU/BR series prefixes. No era conversion
# happens here; only the spellings round-trip.
LETTER_NUMBER_CASES = [
    ("JP.H1051684.A", "JP", "H1051684", "A", "JP.H1051684.A", "JPH1051684A"),
    ("JP.H11184933.A", "JP", "H11184933", "A", "JP.H11184933.A", "JPH11184933A"),
    ("JP.S58196141.A", "JP", "S58196141", "A", "JP.S58196141.A", "JPS58196141A"),
    ("JPH0218652A", "JP", "H0218652", "A", "JP.H0218652.A", "JPH0218652A"),
    ("JPH0317142B2", "JP", "H0317142", "B2", "JP.H0317142.B2", "JPH0317142B2"),
    ("JPS59160899A", "JP", "S59160899", "A", "JP.S59160899.A", "JPS59160899A"),
    ("IN.985DE2013.A", "IN", "985DE2013", "A", "IN.985DE2013.A", "IN985DE2013A"),
    ("IN985DE2013A", "IN", "985DE2013", "A", "IN.985DE2013.A", "IN985DE2013A"),
    ("TW.I707812.B", "TW", "I707812", "B", "TW.I707812.B", "TWI707812B"),
    ("TWI707812B", "TW", "I707812", "B", "TW.I707812.B", "TWI707812B"),
    ("HU.P0304100.A2", "HU", "P0304100", "A2", "HU.P0304100.A2", "HUP0304100A2"),
    ("BRPI0410768B1", "BR", "PI0410768", "B1", "BR.PI0410768.B1", "BRPI0410768B1"),
    # Separators make the leading letter block unambiguous, so they are
    # accepted with any spelling of the separators.
    ("JP-H0218652-A", "JP", "H0218652", "A", "JP.H0218652.A", "JPH0218652A"),
    ("JP H0218652 A", "JP", "H0218652", "A", "JP.H0218652.A", "JPH0218652A"),
    ("jph0218652a", "JP", "H0218652", "A", "JP.H0218652.A", "JPH0218652A"),
]


@pytest.mark.parametrize(
    ("text", "country", "number", "kind", "docdb", "epodoc"),
    LETTER_NUMBER_CASES,
    ids=[case[0] for case in LETTER_NUMBER_CASES],
)
def test_parse_pubnum_accepts_letter_bearing_number_part(
    text: str, country: str, number: str, kind: str, docdb: str, epodoc: str
) -> None:
    """A number part with letters parses and round-trips through all three spellings."""
    result = parse_pubnum(text)
    assert result.country == country
    assert result.number == number
    assert result.kind == kind
    assert result.docdb() == docdb
    assert result.epodoc() == epodoc
    # google() is the epodoc spelling: the US ten-digit padding applies to
    # digit-only numbers only.
    assert result.google() == epodoc


LETTER_NUMBER_INVALID_CASES = [
    "USA11468338B2",  # three-letter country code: US is not a leading-letter office
    "INDEL2013985A",  # leading letters without separators, and IN is not listed
    "US11468338B22",  # kind with two digits, not an inner letter block
    "IN.985DE201.A",  # inner letter block followed by only 3 digits
    "JP.H.A",  # a letter block with no digits at all
    "JP.H0218652B.A",  # number part ending in a letter
    "IN.985DEFGH2013.A",  # inner letter block of 5 letters
    "JP.H" + "1" * 12 + ".A",  # 13-character number part: one past the ceiling
]


@pytest.mark.parametrize("text", LETTER_NUMBER_INVALID_CASES, ids=LETTER_NUMBER_INVALID_CASES)
def test_parse_pubnum_rejects_number_parts_outside_the_grammar(text: str) -> None:
    """Spellings the letter grammar must keep out still raise ValueError."""
    with pytest.raises(ValueError):
        parse_pubnum(text)


def test_parse_pubnum_reports_an_over_long_letter_bearing_number_part_by_characters() -> None:
    """The ceiling counts characters of the number part, letters included."""
    with pytest.raises(ValueError, match="13 characters"):
        parse_pubnum("JP.H" + "1" * 12 + ".A")


def test_parse_pubnum_accepts_the_longest_allowed_letter_bearing_number_part() -> None:
    """A number part of exactly MAX_NUMBER_DIGITS characters is still accepted."""
    result = parse_pubnum("JP.H" + "1" * (MAX_NUMBER_DIGITS - 1) + ".A")
    assert result.number == "H" + "1" * (MAX_NUMBER_DIGITS - 1)
    assert len(result.number) == MAX_NUMBER_DIGITS


def test_parse_pubnum_accepts_a_separated_leading_letter_for_an_unlisted_country() -> None:
    """Separators disambiguate, so a leading letter block is accepted for any office.

    ``US.A1234567.B2`` parses although US is not in
    ``LEADING_LETTER_COUNTRIES``: the dots show where the number part begins,
    so the letter cannot be mistaken for a third letter of the country code.
    The same string without separators (``USA1234567B2``) stays invalid.
    """
    result = parse_pubnum("US.A1234567.B2")
    assert result.number == "A1234567"
    assert result.docdb() == "US.A1234567.B2"
    with pytest.raises(ValueError):
        parse_pubnum("USA1234567B2")


def test_leading_letter_countries_lists_the_offices_seen_in_ops_data() -> None:
    """The unseparated leading-letter allowance is limited to the observed offices."""
    assert LEADING_LETTER_COUNTRIES == frozenset({"JP", "TW", "HU", "BR"})


def test_google_does_not_pad_a_letter_bearing_us_number() -> None:
    """The US ten-digit padding is limited to digit-only numbers.

    The number used here is 10 characters long and starts with "20", so it
    meets every other condition of the padding rule; only the letters inside
    rule it out, because a year-plus-serial reading makes no sense for it.
    """
    pubnum = PubNumber(country="US", number="2020DE1234", kind="A1")
    assert pubnum.google() == "US2020DE1234A1"


def test_parse_pubnum_does_not_shrink_a_letter_bearing_us_application_number() -> None:
    """The 2026 year rule applies to digit-only numbers, so letters pass through."""
    result = parse_pubnum("US.20A00123456.A1")
    assert result.number == "20A00123456"
    assert result.docdb() == "US.20A00123456.A1"
