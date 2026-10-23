"""Operation base class.

Every operation knows how to

* change a :class:`ProjectState` (``state_forwards``),
* change the database (``database_forwards`` / ``database_backwards``,
  given the states before and after the operation),
* describe itself for humans and for the migration writer
  (``describe`` / ``deconstruct``),
* merge with a later operation (``reduce``) for the optimizer.
"""
from __future__ import annotations

from ...fields import Field, ForeignKey
from ..state import field_signature


class Operation:
    reversible = True
    # Barrier operations (RunSQL / RunPython) can do anything, so the
    # optimizer never moves other operations across them.
    barrier = False

    def state_forwards(self, state) -> None:
        raise NotImplementedError

    def database_forwards(self, editor, before, after) -> None:
        raise NotImplementedError

    def database_backwards(self, editor, before, after) -> None:
        raise NotImplementedError(f"{type(self).__name__} is not reversible.")

    def describe(self) -> str:
        return type(self).__name__

    @property
    def migration_name_fragment(self) -> str | None:
        return None

    def deconstruct(self) -> dict:
        """Keyword arguments that rebuild this operation (in writing order)."""
        raise NotImplementedError

    # ── optimizer support ──

    # True if the change affects how other tables reference this model
    # (creating, dropping or renaming it, its table, or its primary key).
    structural = False

    def writes(self) -> set[str]:
        """Model names whose definition this operation changes."""
        return set()

    def reads(self) -> set[str]:
        """Model names this operation depends on without changing (FK targets)."""
        return set()

    def reduce(self, other: "Operation") -> list["Operation"] | None:
        """Combine ``self`` followed by *other* into a shorter list, or None."""
        return None

    def can_pass(self, other: "Operation") -> bool:
        """True if ``self`` and *other* are independent, so their order can be swapped."""
        if self.barrier or other.barrier or self._pins(other) or other._pins(self):
            return False
        if self.writes() & other.writes():
            return False
        if self.structural and self.writes() & other.reads():
            return False
        if other.structural and other.writes() & self.reads():
            return False
        return True

    def _pins(self, other: "Operation") -> bool:
        """Extra ordering constraints beyond shared model names."""
        return False

    # ── comparison ──

    def _key(self):
        return type(self), tuple((k, _comparable(v)) for k, v in self.deconstruct().items())

    def __eq__(self, other):
        if not isinstance(other, Operation):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self):
        return hash(type(self))

    def __repr__(self):
        args = ", ".join(f"{k}={v!r}" for k, v in self.deconstruct().items())
        return f"{type(self).__name__}({args})"


def _comparable(value):
    if isinstance(value, Field):
        return ("field", field_signature(value))
    if isinstance(value, (list, tuple)):
        return tuple(_comparable(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _comparable(v)) for k, v in value.items()))
    return value


def fk_targets(fields) -> set[str]:
    return {f.to for f in fields if isinstance(f, ForeignKey)}


def prepare_field(field: Field, name: str) -> Field:
    """A private, schema-only copy of *field* named *name* (FK targets as names)."""
    if not isinstance(field, Field):
        raise TypeError(f"Expected a Field for {name!r}, got {field!r}.")
    clone = field.clone()
    clone.name = name
    return clone
