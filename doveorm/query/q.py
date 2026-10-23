"""``Q`` objects – composable filter expressions."""
from __future__ import annotations

from typing import Any


class Q:
    """
    Composable filter node supporting AND (``&``), OR (``|``) and NOT (``~``).

    Usage:
        User.all().filter(Q(age__gte=18) | Q(name="admin"))
        User.all().filter(Q(active=True) & (Q(role="editor") | Q(role="admin")))
    """
    AND = "AND"
    OR  = "OR"

    def __init__(self, **kwargs):
        # Leaf node: list of (key, value) pairs
        self.children: list[Q | tuple[str, Any]] = list(kwargs.items())
        self.connector: str = self.AND
        self.negated: bool = False

    def _combine(self, other, connector: str) -> "Q":
        if not isinstance(other, Q):
            return NotImplemented
        node = Q()
        node.connector = connector
        node.children = [self, other]
        return node

    def __or__(self, other: "Q") -> "Q":
        return self._combine(other, self.OR)

    def __and__(self, other: "Q") -> "Q":
        return self._combine(other, self.AND)

    def __invert__(self) -> "Q":
        clone = Q()
        clone.children = self.children[:]
        clone.connector = self.connector
        clone.negated = not self.negated
        return clone

    def leaves(self):
        """Yield every ``(key, value)`` leaf in this tree."""
        for child in self.children:
            if isinstance(child, Q):
                yield from child.leaves()
            else:
                yield child

    def __repr__(self):
        prefix = "NOT " if self.negated else ""
        return f"<Q {prefix}{self.connector} {self.children!r}>"
