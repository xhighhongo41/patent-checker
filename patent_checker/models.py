"""Data models shared across source-specific modules (Google Patents, OPS)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Claim:
    """One claim: its number, flattened text, and dependency targets.

    Attributes:
        number: Claim number (1-based).
        text: Whitespace-normalized full text of the claim (leading
            "N." numbering kept as printed).
        depends_on: Claim numbers this claim references, in order of first
            appearance, deduplicated. Empty for independent claims.
    """

    number: int
    text: str
    depends_on: tuple[int, ...] = ()
