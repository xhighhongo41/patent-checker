"""Publication-number normalization for the v0.1 PoC.

Converts between the three publication-number spellings used by our data
sources, keeping the parts (country / number / kind) explicit:

- docdb:  ``US.11468338.B2`` (dot-separated, used by OPS docdb endpoints)
- epodoc: ``US11468338B2``   (concatenated, used by OPS epodoc endpoints)
- Google Patents: ``US11468338B2`` (concatenated, used in ``/patent/`` URLs)

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
_PUBNUM_RE = re.compile(r"^([A-Z]{2})[.\- ]?(\d+)(?:[.\- ]?([A-Z]\d?))?$")


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
        """Return the spelling used in Google Patents ``/patent/`` URLs."""
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
    return PubNumber(country=country, number=number, kind=kind or "")
