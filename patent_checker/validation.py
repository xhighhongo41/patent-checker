"""Size and shape checks shared by every front end.

The offline batch helpers (``dedup``, ``verify``) accept whatever a caller
hands them, so the MCP server and the CLI have to agree on what "too much"
means before any of it is processed. This module is that single answer: a
ceiling on the number of elements, on the size of one element, and on the
size of the whole payload, plus the element shapes the helpers can work with
(a publication-number string, or a mapping carrying one under ``"pub"``).

Every violation is a :class:`ValueError` naming the offending element by
index and the limit it broke, so the MCP tool layer can map it to its
``invalid_input`` error and the CLI can print it as-is.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

# Ceiling on one element of a batch. Generous next to a publication number or
# a search hit; it exists so a single element cannot carry a whole document.
MAX_ITEM_CHARS = 4096

# Ceiling on a whole batch. Kept well below what a batch of MAX_ITEM_CHARS
# elements could reach, so the element count alone never bounds the work.
MAX_PAYLOAD_CHARS = 4 * 1024 * 1024


def _item_chars(item: str | Mapping[str, Any]) -> int:
    """Return the size of one element in characters.

    A string is measured by its length; a mapping by its JSON serialization,
    which counts nested values too and so cannot be inflated by hiding bulk
    one level down.
    """
    if isinstance(item, str):
        return len(item)
    return len(json.dumps(item, ensure_ascii=False, default=str))


def _check_item(item: Any, position: str) -> None:
    """Check the shape of one element.

    Raises:
        ValueError: If *item* is neither a string nor a mapping, or is a
            mapping without a string ``"pub"`` entry.
    """
    if isinstance(item, str):
        return
    if not isinstance(item, Mapping):
        raise ValueError(f"{position} must be a string or a mapping, got {type(item).__name__}")
    if "pub" not in item:
        raise ValueError(f'{position} is missing the required key "pub"')
    pub = item["pub"]
    if not isinstance(pub, str):
        raise ValueError(f'{position}["pub"] must be a string, got {type(pub).__name__}')


def validate_batch(
    records: Sequence[Any],
    *,
    label: str,
    max_records: int,
    max_item_chars: int = MAX_ITEM_CHARS,
    max_payload_chars: int = MAX_PAYLOAD_CHARS,
) -> None:
    """Check that a batch payload is one this toolkit is willing to process.

    Checked in this order, so the cheapest rejection comes first: the payload
    is a list, it holds at most *max_records* elements, every element has an
    accepted shape and is at most *max_item_chars* long, and the elements
    together are at most *max_payload_chars* long. All three limits are
    inclusive.

    Args:
        records: The batch to check.
        label: The caller's name for the payload (an argument name), used to
            open every error message.
        max_records: Largest accepted number of elements.
        max_item_chars: Largest accepted size of one element.
        max_payload_chars: Largest accepted size of all elements together.

    Raises:
        ValueError: If *records* is not a list/tuple, or any limit or shape
            rule is broken. The message names the offending element by index.
    """
    if isinstance(records, str) or not isinstance(records, Sequence):
        raise ValueError(f"{label} must be a list, got {type(records).__name__}")
    if len(records) > max_records:
        raise ValueError(
            f"{label} holds {len(records)} entries: at most {max_records} are accepted"
        )

    total = 0
    for index, item in enumerate(records):
        position = f"{label}[{index}]"
        _check_item(item, position)
        size = _item_chars(item)
        if size > max_item_chars:
            raise ValueError(
                f"{position} is {size} characters long: "
                f"at most {max_item_chars} are accepted per entry"
            )
        total += size
        # Checked inside the loop so an oversized payload is refused as soon
        # as it is known to be one, without measuring the rest -- which is
        # why the message names where the ceiling was reached, not the (then
        # unknown) full size.
        if total > max_payload_chars:
            raise ValueError(
                f"{label} is larger than {max_payload_chars} characters in total "
                f"(the limit is reached at {position})"
            )
