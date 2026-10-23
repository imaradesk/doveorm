"""Lookup names (``field__lookup``) and LIKE-pattern helpers."""
from __future__ import annotations

from ..utils.sql import IDENTIFIER_RE

# Column names must be simple identifiers — defence-in-depth against injection.
_SAFE_COL_RE = IDENTIFIER_RE

_LIKE_ESCAPE = "!"
_COMPARISONS = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
_PATTERNS = {
    "contains":    ("%{}%", False),
    "icontains":   ("%{}%", True),
    "startswith":  ("{}%",  False),
    "istartswith": ("{}%",  True),
    "endswith":    ("%{}",  False),
    "iendswith":   ("%{}",  True),
}
LOOKUPS = frozenset(
    {"eq", "exact", "iexact", "ne", "like", "in", "not_in", "is_null", "isnull", "range"}
    | set(_COMPARISONS) | set(_PATTERNS)
)


def _escape_like(text: str) -> str:
    return (text.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
                .replace("%", _LIKE_ESCAPE + "%")
                .replace("_", _LIKE_ESCAPE + "_"))
