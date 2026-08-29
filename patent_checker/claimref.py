"""Claim-reference extraction from claim text.

Both data sources need this as a fallback: OPS full-text claims carry no
structured dependency markup at all (plain text only), and some Google
Patents pages lack ``<claim-ref>`` elements. Dependencies are then recovered
from English phrasings such as "according to claim 1", "of claim 1 or 2",
"of any one of claims 1-3", or "any preceding claim".
"""

from __future__ import annotations

import re

# "any preceding claim" / "any (one) of the preceding claims" and similar.
_PRECEDING_RE = re.compile(
    r"\bany\s+(?:one\s+of\s+|of\s+)?the\s+preceding\s+claims\b|\bany\s+preceding\s+claim\b",
    re.IGNORECASE,
)

# "claim(s)" followed by a list of numbers joined by , / or / and / to /
# through / dashes, e.g. "claims 1, 2 or 4", "claims 1-3", "claim 2".
_CLAIM_LIST_RE = re.compile(
    r"\bclaims?\s+(\d+(?:\s*(?:,|or|and|to|through|[-–—])\s*\d+)*)",
    re.IGNORECASE,
)

_TOKEN_RE = re.compile(r"\d+|to|through|[-–—]", re.IGNORECASE)

_RANGE_CONNECTORS = frozenset({"to", "through", "-", "–", "—"})

# Guard against absurd expansions from malformed text such as "claims 1-9999".
_MAX_RANGE_SPAN = 200


def extract_claim_refs_from_text(text: str, *, claim_number: int | None = None) -> tuple[int, ...]:
    """Extract referenced claim numbers from English claim text.

    Args:
        text: Claim text to scan.
        claim_number: Number of the claim the text belongs to, when known.
            Used to expand "preceding claim(s)" phrasings to ``1..N-1`` and to
            drop self-references. With ``None``, preceding-claim phrasings
            yield nothing and self-references are kept.

    Returns:
        Referenced claim numbers in order of first appearance, deduplicated.
    """
    if not isinstance(text, str):
        raise TypeError(f"claim text must be a str, got {type(text).__name__}")

    found: list[int] = []

    def add(num: int) -> None:
        if num == claim_number:
            return
        if num > 0 and num not in found:
            found.append(num)

    if claim_number is not None and _PRECEDING_RE.search(text):
        for num in range(1, claim_number):
            add(num)

    for match in _CLAIM_LIST_RE.finditer(text):
        tokens = _TOKEN_RE.findall(match.group(1))
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.isdigit():
                start = int(token)
                is_range = (
                    index + 2 < len(tokens)
                    and tokens[index + 1].lower() in _RANGE_CONNECTORS
                    and tokens[index + 2].isdigit()
                )
                if is_range:
                    end = int(tokens[index + 2])
                    if start <= end <= start + _MAX_RANGE_SPAN:
                        for num in range(start, end + 1):
                            add(num)
                    else:
                        add(start)
                        add(end)
                    index += 3
                    continue
                add(start)
            index += 1

    return tuple(found)
