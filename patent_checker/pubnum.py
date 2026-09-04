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

JP-specific quirks (era-based numbering etc.) are out of scope for v0.1:
whatever OPS returns is carried around as-is, and inputs this module cannot
parse raise ``ValueError`` so the caller can record the case (implementation
plan section 4.2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Matches: 2-letter country + optional 1-char separator + digit run +
# optional (1-char separator + kind letter + optional 1 digit).
# Separator may be "", ".", "-" or a single space, independently at each
# position; the pattern is applied to an already upper-cased, stripped string.
# The digit run may contain a single "/" (e.g. the USPTO citation style
# "2007/0016547"); it is stripped out before further normalization.
_PUBNUM_RE = re.compile(r"^([A-Z]{2})[.\- ]?(\d+(?:/\d+)?)(?:[.\- ]?([A-Z]\d?))?$")

# DOCDB spells US A-kind publications with 10 digits through 2025 and 11
# digits from 2026 (verified against EPO OPS, 2026-09).
US_APPLICATION_ELEVEN_DIGITS_FROM_YEAR: int = 2026


@dataclass(frozen=True)
class PubNumber:
    """A publication number split into its three parts.

    Attributes:
        country: Two-letter country/office code, upper case (e.g. ``US``).
        number: Digit string as given, leading zeros preserved.
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
        never reach this padding step.
        """
        if self.country == "US" and len(self.number) == 10 and self.number[:2] in ("19", "20"):
            padded_number = f"{self.number[:4]}0{self.number[4:]}"
            return f"{self.country}{padded_number}{self.kind}"
        return self.epodoc()


def parse_pubnum(text: str) -> PubNumber:
    """Parse a publication number given in any of the three spellings.

    Input is case-insensitive and tolerates surrounding whitespace and a
    single ``.``, ``-`` or space between the parts.

    Raises:
        ValueError: If ``text`` is not a recognizable publication number.
        TypeError: If ``text`` is not a string.
    """
    if not isinstance(text, str):
        raise TypeError(f"pubnum text must be a str, got {type(text).__name__}")

    normalized = text.strip().upper()
    match = _PUBNUM_RE.match(normalized)
    if match is None:
        raise ValueError(f"cannot parse publication number: {text!r}")

    country, number, kind = match.groups()
    number = number.replace("/", "")
    kind = kind or ""

    if (
        country == "US"
        and kind.startswith("A")
        and len(number) == 11
        and number[:2] in ("19", "20")
        and number[4] == "0"
        and int(number[:4]) < US_APPLICATION_ELEVEN_DIGITS_FROM_YEAR
    ):
        number = number[:4] + number[5:]

    return PubNumber(country=country, number=number, kind=kind)
