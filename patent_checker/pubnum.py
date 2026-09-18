"""Publication-number normalization.

Converts between the three publication-number spellings used by our data
sources, keeping the parts (country / number / kind) explicit:

- docdb:  ``US.11468338.B2`` (dot-separated, used by OPS docdb endpoints)
- epodoc: ``US11468338B2``   (concatenated, used by OPS epodoc endpoints)
- Google Patents: ``US20240111636A1`` (concatenated, used in ``/patent/`` URLs)

docdb/epodoc numbers observed from OPS mix two lengths for the digit run:
10 digits (4-digit year + 6-digit serial) and 11 digits (4-digit year +
7-digit serial), depending on office and document kind. Google Patents,
however, always spells *US published applications* with an 11-digit run,
padding a 10-digit docdb/epodoc number by inserting a ``0`` right after the
4-digit year (e.g. docdb ``US.2024111636.A1`` -> Google
``US20240111636A1``). ``google()`` applies this padding only for that case;
docdb() and epodoc() are never padded, and non-US offices and US granted
patents (whose serial is not year-prefixed) are passed through unchanged.

For US A-kind (published-application) numbers, this padding is
year-dependent and reversible: DOCDB spells them with 10 digits through
publication year 2025 and with 11 digits from 2026 onward (verified against
EPO OPS, 2026-09). ``parse_pubnum`` normalizes an 11-digit, year-prefixed,
zero-padded US A-kind number back down to the 10-digit docdb form whenever
the year is before 2026, so a Google Patents spelling round-trips through
``parse_pubnum(...).docdb()`` back to the original docdb spelling for those
years. From 2026 onward, docdb/epodoc and Google Patents already agree on
11 digits, so no shrinking happens.

The number part is not always digits. OPS reports publication numbers whose
number part carries letters, and since v1.2 they parse and round-trip like
any other: JP era-based numbers (``JP.H1051684.A``, ``JP.S58196141.A``; S =
Showa, H = Heisei), Indian application numbers with an office code inside
(``IN.985DE2013.A``), and series prefixes such as ``TW.I707812.B``,
``HU.P0304100.A2`` and Google Patents' ``BRPI0410768B1``. Nothing is
converted (no era-to-Gregorian arithmetic): the letters are part of the
number and travel with it, so only the three spellings have to agree. Inputs
this module cannot parse raise ``ValueError`` so the caller can record the
case (implementation plan section 4.2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Longest number part accepted in a publication number, counted in
# characters (letters included, see the module docstring). Real numbers use
# at most 11 characters (e.g. a 4-digit year + 7-digit serial); the ceiling
# leaves one spare character and keeps an over-long input from reaching the
# file system, where the docdb spelling becomes a cache path (ENAMETOOLONG).
MAX_NUMBER_DIGITS: int = 12

# Offices whose numbers may start with letters in a spelling that carries no
# separators (``JPH0218652A``, ``BRPI0410768B1``). Only these are listed,
# because without a separator a leading letter is indistinguishable from a
# third letter of the country code: allowing it everywhere would make
# ``USA11468338B2`` parse as country US, number A11468338. The list holds the
# offices seen in real OPS data and is extended when a new one shows up; a
# separated spelling (``US.A1234567.B2``) is accepted for any office.
LEADING_LETTER_COUNTRIES: frozenset[str] = frozenset({"JP", "TW", "HU", "BR"})

# Matches: 2-letter country + optional 1-char separator + number part +
# optional (1-char separator + kind letter + optional 1 digit).
# Separator may be "", ".", "-" or a single space, independently at each
# position; the pattern is applied to an already upper-cased, stripped string.
#
# The number part is one of two branches:
#
# - a digit run that may contain a single "/" (the USPTO citation style
#   "2007/0016547"); the slash is stripped out before further normalization.
#   Each side of the slash is bounded by MAX_NUMBER_DIGITS; their sum is
#   checked in parse_pubnum.
# - 0-2 leading letters, a digit run, and optionally an inner letter block of
#   1-4 letters followed by at least 4 more digits, so the number part always
#   ends with a digit. The 4-digit minimum after the inner letters is what
#   keeps "US11468338B22" out: "B22" cannot be read as an inner block, and a
#   kind code takes at most one digit.
#
# Whether the leading letters are allowed also depends on the separator and
# the office, which the pattern cannot express; parse_pubnum checks it
# against LEADING_LETTER_COUNTRIES. The digit-run branch comes first so a
# digit-only number is matched by the simpler, unchanged rule.
_PUBNUM_RE = re.compile(
    rf"^(?P<country>[A-Z]{{2}})(?P<separator>[.\- ]?)"
    rf"(?P<number>"
    rf"\d{{1,{MAX_NUMBER_DIGITS}}}(?:/\d{{1,{MAX_NUMBER_DIGITS}}})?"
    rf"|(?P<leading>[A-Z]{{1,2}})?\d{{1,{MAX_NUMBER_DIGITS}}}"
    rf"(?:[A-Z]{{1,4}}\d{{4,{MAX_NUMBER_DIGITS}}})?"
    rf")"
    rf"(?:[.\- ]?(?P<kind>[A-Z]\d?))?$"
)

# DOCDB spells US A-kind publications with 10 digits through 2025 and 11
# digits from 2026 (verified against EPO OPS, 2026-09).
US_APPLICATION_ELEVEN_DIGITS_FROM_YEAR: int = 2026


@dataclass(frozen=True)
class PubNumber:
    """A publication number split into its three parts.

    Attributes:
        country: Two-letter country/office code, upper case (e.g. ``US``).
        number: Number part as given, leading zeros preserved. Usually
            digits, but it may carry letters (``H1051684``, ``985DE2013``).
        kind: Kind code such as ``A1`` or ``B2``; empty string when absent.
    """

    country: str
    number: str
    kind: str = ""

    def docdb(self) -> str:
        """Return the dot-separated docdb spelling (kind part omitted when empty)."""
        if self.kind:
            return f"{self.country}.{self.number}.{self.kind}"
        return f"{self.country}.{self.number}"

    def epodoc(self) -> str:
        """Return the concatenated epodoc spelling."""
        return f"{self.country}{self.number}{self.kind}"

    def google(self) -> str:
        """Return the spelling used in Google Patents ``/patent/`` URLs.

        US published applications are spelled with an 11-digit number on
        Google Patents (4-digit year + 7-digit serial), while docdb/epodoc
        may carry the same publication as a 10-digit number (4-digit year +
        6-digit serial). When that 10-digit, year-prefixed shape is
        detected, a ``0`` is inserted right after the year to produce the
        11-digit Google Patents spelling. All other cases (11-digit numbers,
        US granted patents, non-US offices, and 10-digit numbers that are
        not year-prefixed) are passed through as-is. Publications from 2026
        onward already arrive as 11 digits (see ``parse_pubnum``), so they
        never reach this padding step. A number part carrying letters is
        never padded either: the year/serial reading only applies to a
        digit-only number.
        """
        if (
            self.country == "US"
            and len(self.number) == 10
            and self.number.isdigit()
            and self.number[:2] in ("19", "20")
        ):
            padded_number = f"{self.number[:4]}0{self.number[4:]}"
            return f"{self.country}{padded_number}{self.kind}"
        return self.epodoc()


def parse_pubnum(text: str) -> PubNumber:
    """Parse a publication number given in any of the three spellings.

    Input is case-insensitive and tolerates surrounding whitespace and a
    single ``.``, ``-`` or space between the parts. The number part may carry
    letters (``JP.H1051684.A``, ``IN.985DE2013.A``); without separators a
    number starting with letters is only accepted for the offices in
    :data:`LEADING_LETTER_COUNTRIES`, because otherwise the letter cannot be
    told apart from a third letter of the country code.

    Raises:
        ValueError: If ``text`` is not a recognizable publication number,
            including a number part longer than :data:`MAX_NUMBER_DIGITS`
            characters.
        TypeError: If ``text`` is not a string.
    """
    if not isinstance(text, str):
        raise TypeError(f"pubnum text must be a str, got {type(text).__name__}")

    normalized = text.strip().upper()
    match = _PUBNUM_RE.match(normalized)
    if match is None:
        raise ValueError(f"cannot parse publication number: {text!r}")

    country = match["country"]
    number = match["number"].replace("/", "")
    kind = match["kind"] or ""

    if match["leading"] and not match["separator"] and country not in LEADING_LETTER_COUNTRIES:
        raise ValueError(
            f"cannot parse publication number: {text!r} "
            f"(a number starting with letters needs a separator, "
            f"as in {country}.{match['number']}.{kind or 'A'})"
        )

    # The slash form and the letter-bearing form are bounded per run by the
    # pattern, so a sum of runs is the only remaining way past the ceiling.
    if len(number) > MAX_NUMBER_DIGITS:
        raise ValueError(
            f"publication number has {len(number)} characters, "
            f"at most {MAX_NUMBER_DIGITS} are accepted: {text!r}"
        )

    # Reading the number as a year plus a serial only makes sense for a
    # digit-only number part.
    if (
        country == "US"
        and kind.startswith("A")
        and len(number) == 11
        and number.isdigit()
        and number[:2] in ("19", "20")
        and number[4] == "0"
        and int(number[:4]) < US_APPLICATION_ELEVEN_DIGITS_FROM_YEAR
    ):
        number = number[:4] + number[5:]

    return PubNumber(country=country, number=number, kind=kind)
