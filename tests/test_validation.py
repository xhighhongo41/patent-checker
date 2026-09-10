"""Tests for :mod:`patent_checker.validation`."""

from __future__ import annotations

import pytest

from patent_checker.validation import (
    MAX_ITEM_CHARS,
    MAX_PAYLOAD_CHARS,
    validate_batch,
)


def _validate(records: list, **overrides) -> None:
    """Call ``validate_batch`` with generous defaults, overriding one limit at a time."""
    limits = {
        "max_records": 10,
        "max_item_chars": MAX_ITEM_CHARS,
        "max_payload_chars": MAX_PAYLOAD_CHARS,
    }
    limits.update(overrides)
    validate_batch(records, label="records", **limits)


# --- accepted payloads ---------------------------------------------------


def test_an_empty_batch_is_accepted() -> None:
    """Nothing to check is not an error: the offline helpers return empty results."""
    _validate([])


def test_strings_and_mappings_are_accepted() -> None:
    """Both accepted element shapes pass: a bare string and a mapping carrying ``pub``."""
    _validate(["US11468338B2", {"pub": "US11468338B2", "family_id": "1"}])


# --- count ----------------------------------------------------------------


def test_too_many_records_are_rejected() -> None:
    """More elements than ``max_records`` is a ``ValueError`` naming the limit."""
    with pytest.raises(ValueError, match=r"records holds 11 entries.*at most 10"):
        _validate(["US11468338B2"] * 11)


def test_the_record_count_limit_is_inclusive() -> None:
    """Exactly ``max_records`` elements are still accepted (boundary)."""
    _validate(["US11468338B2"] * 10)


# --- element type ----------------------------------------------------------


def test_a_non_string_non_mapping_element_is_rejected() -> None:
    """An element that is neither a string nor a mapping names its index and type."""
    with pytest.raises(ValueError, match=r"records\[1\].*string or a mapping.*int"):
        _validate(["US11468338B2", 5])


def test_a_mapping_without_a_pub_key_is_rejected() -> None:
    """A mapping element must carry a publication number under ``pub``."""
    with pytest.raises(ValueError, match=r"records\[0\].*\"?pub\"?"):
        _validate([{"family_id": "1"}])


def test_a_mapping_with_a_non_string_pub_is_rejected() -> None:
    """``pub`` must be a string; a number would fail deeper in with an opaque error."""
    with pytest.raises(ValueError, match=r"records\[0\].*pub.*int"):
        _validate([{"pub": 5, "family_id": "1"}])


def test_a_non_sequence_payload_is_rejected() -> None:
    """A payload that is not a list is the caller's mistake, not an internal error."""
    with pytest.raises(ValueError, match=r"records must be a list"):
        validate_batch(
            "US11468338B2",  # type: ignore[arg-type]
            label="records",
            max_records=10,
            max_item_chars=MAX_ITEM_CHARS,
            max_payload_chars=MAX_PAYLOAD_CHARS,
        )


# --- element size ----------------------------------------------------------


def test_an_oversized_string_element_is_rejected() -> None:
    """A single huge string is refused with its index, its size and the limit."""
    with pytest.raises(ValueError, match=r"records\[1\] is 11 characters long.*at most 10"):
        _validate(["ok", "x" * 11], max_item_chars=10)


def test_an_element_of_exactly_the_size_limit_is_accepted() -> None:
    """The element size limit is inclusive (boundary)."""
    _validate(["x" * 10], max_item_chars=10)


def test_an_oversized_mapping_element_is_rejected() -> None:
    """A mapping is measured by its serialized size, so bulky values are caught too."""
    with pytest.raises(ValueError, match=r"records\[0\] is \d+ characters long"):
        _validate([{"pub": "US11468338B2", "abstract": "x" * 200}], max_item_chars=50)


# --- total payload size -----------------------------------------------------


def test_an_oversized_payload_is_rejected() -> None:
    """Many individually acceptable elements can still exceed the total limit."""
    with pytest.raises(ValueError, match=r"records is larger than 25 characters in total"):
        _validate(["x" * 10] * 3, max_item_chars=10, max_payload_chars=25)


def test_a_payload_of_exactly_the_total_limit_is_accepted() -> None:
    """The total size limit is inclusive (boundary)."""
    _validate(["x" * 10] * 3, max_item_chars=10, max_payload_chars=30)


# --- limits -----------------------------------------------------------------


def test_the_default_limits_are_the_documented_ones() -> None:
    """The two shared ceilings are the ones the documentation and tools rely on."""
    assert MAX_ITEM_CHARS == 4096
    assert MAX_PAYLOAD_CHARS == 4 * 1024 * 1024
