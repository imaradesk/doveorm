"""The ``Migration`` base class that migration files subclass."""
from __future__ import annotations

from ..exceptions import MigrationError
from .operations.base import Operation


class Migration:
    """One migration file.

    ::

        from doveorm import fields, migrations

        class Migration(migrations.Migration):
            dependencies = ["0001_initial"]
            operations = [
                migrations.AddField("User", "phone", fields.CharField(max_length=20)),
            ]

    * ``dependencies`` – names of migrations that must run first.
    * ``replaces`` – set by ``squashmigrations``: the migrations this one replaces.
    * ``initial`` – first migration that creates tables (enables ``fake_initial``).
    * ``atomic`` – run in one transaction (SQLite; MySQL DDL is never atomic).
    """
    dependencies: list[str] = []
    operations: list[Operation] = []
    replaces: list[str] = []
    initial = False
    atomic = True

    def __init__(self, name: str):
        self.name = name
        # Copy class-level lists so instances never share mutable state.
        self.dependencies = list(type(self).dependencies)
        self.operations = list(type(self).operations)
        self.replaces = list(type(self).replaces)
        for op in self.operations:
            if not isinstance(op, Operation):
                raise MigrationError(f"Migration {name}: {op!r} is not an operation.")
        for dep in self.dependencies:
            if not isinstance(dep, str):
                raise MigrationError(f"Migration {name}: dependencies must be migration names.")

    def mutate_state(self, state, *, in_place: bool = False):
        """The project state after this migration."""
        new = state if in_place else state.clone()
        for op in self.operations:
            op.state_forwards(new)
        return new

    @property
    def reversible(self) -> bool:
        return all(op.reversible for op in self.operations)

    def __repr__(self):
        return f"<Migration {self.name}>"
