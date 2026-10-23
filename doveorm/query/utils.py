"""Small helpers shared by the query package."""
from __future__ import annotations

import operator

IN_CHUNK_SIZE = 500


def _chunks(values: list, size: int = IN_CHUNK_SIZE):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _check_non_negative_int(value, what: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{what} must be an integer, got bool.")
    try:
        n = operator.index(value)
    except TypeError:
        raise TypeError(f"{what} must be an integer, got {type(value).__name__}.") from None
    if n < 0:
        raise ValueError(f"{what} must be >= 0, got {n}.")
    return n
