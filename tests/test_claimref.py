"""Tests for claim-reference extraction from claim text."""

import pytest

from patent_checker.claimref import extract_claim_refs_from_text


@pytest.mark.parametrize(
    ("text", "claim_number", "expected"),
    [
        # Single references, GP and OPS phrasings.
        ("The apparatus according to claim 1, wherein...", 2, (1,)),
        ("The apparatus of claim 1, wherein the rotation...", 3, (1,)),
        ("A method as claimed in claim 7.", 9, (7,)),
        # Alternatives and lists.
        ("The apparatus of claim 1 or 2, wherein...", 3, (1, 2)),
        ("The system of claims 1, 3 or 5.", 6, (1, 3, 5)),
        ("The system of claims 2, 4 and 6.", 7, (2, 4, 6)),
        # Ranges with several connectors.
        ("The apparatus of any one of claims 1-3, wherein...", 4, (1, 2, 3)),
        ("A device according to any of claims 2 to 4.", 5, (2, 3, 4)),
        ("A device according to claims 1 through 3.", 4, (1, 2, 3)),
        ("The method of any one of claims 1–3.", 4, (1, 2, 3)),
        # Preceding-claim phrasings expand to all previous claims.
        ("A system according to any preceding claim.", 4, (1, 2, 3)),
        ("A system according to any one of the preceding claims.", 3, (1, 2)),
        ("A system according to any of the preceding claims.", 2, (1,)),
        # Independent claims: no references.
        ("An apparatus, comprising: one or more data processing units.", 1, ()),
        # Self-references are dropped when claim_number is known.
        ("The method of claim 2, wherein claim 2 language repeats.", 2, ()),
        # Order of first appearance, deduplicated.
        ("Combining claim 5 with the device of claim 1 or claim 5.", 7, (5, 1)),
    ],
)
def test_extraction(text: str, claim_number: int, expected: tuple[int, ...]) -> None:
    assert extract_claim_refs_from_text(text, claim_number=claim_number) == expected


def test_without_claim_number_keeps_self_and_skips_preceding() -> None:
    text = "The method of claim 2, according to any preceding claim."
    assert extract_claim_refs_from_text(text) == (2,)


def test_absurd_range_is_not_expanded() -> None:
    text = "The method of claims 1-9999."
    assert extract_claim_refs_from_text(text, claim_number=5) == (1, 9999)


def test_zero_is_ignored() -> None:
    assert extract_claim_refs_from_text("See claim 0.", claim_number=2) == ()


def test_non_string_raises() -> None:
    with pytest.raises(TypeError):
        extract_claim_refs_from_text(123)  # type: ignore[arg-type]
