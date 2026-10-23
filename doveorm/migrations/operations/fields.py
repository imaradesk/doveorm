"""Field-level operations: add / remove / alter / rename a column."""
from __future__ import annotations

from ...exceptions import MigrationError
from ...utils.sql import validate_identifier
from ..backends.base import NOT_PROVIDED
from .base import Operation, fk_targets, prepare_field
from .models import DeleteModel


class FieldOperation(Operation):
    def __init__(self, model_name: str, name: str):
        self.model_name = validate_identifier(model_name, "model name")
        self.name = validate_identifier(name, "field name")

    def writes(self) -> set[str]:
        return {self.model_name}

    def reads(self) -> set[str]:
        field = getattr(self, "field", None)
        return (fk_targets([field]) - {self.model_name}) if field is not None else set()

    @property
    def structural(self) -> bool:
        # Changing / renaming the primary key changes how FKs point at the model.
        field = getattr(self, "field", None)
        return isinstance(self, RenameField) or (field is not None and field.primary_key)

    def _same_field(self, other) -> bool:
        return (isinstance(other, FieldOperation) and other.model_name == self.model_name
                and other.name == self.name)


def _uses_in_options(ms, field_name: str) -> bool:
    return any(field_name in cols for key in ("indexes", "unique_together")
               for cols in ms.options[key])


class AddField(FieldOperation):
    """Add a column.

    ``backfill`` is a one-off value written into existing rows when the new
    column is NOT NULL without a default; it does not become the default.
    """

    def __init__(self, model_name: str, name: str, field, backfill=NOT_PROVIDED):
        super().__init__(model_name, name)
        self.field = prepare_field(field, name)
        if field.primary_key:
            raise MigrationError(f"{model_name}.{name}: cannot add a primary key field.")
        self.backfill = backfill

    def deconstruct(self):
        out = {"model_name": self.model_name, "name": self.name, "field": self.field}
        if self.backfill is not NOT_PROVIDED:
            out["backfill"] = self.backfill
        return out

    def state_forwards(self, state):
        ms = state.get_model(self.model_name).clone()
        if self.name in ms.fields:
            raise MigrationError(f"{self.model_name} already has a field {self.name!r}.")
        if hasattr(self.field, "to"):
            state.target_of(self.field)
        ms.fields[self.name] = self.field
        state.models[self.model_name] = ms

    def database_forwards(self, editor, before, after):
        editor.add_field(before.get_model(self.model_name), after.get_model(self.model_name),
                         self.name, after, backfill=self.backfill)

    def database_backwards(self, editor, before, after):
        editor.remove_field(after.get_model(self.model_name), before.get_model(self.model_name),
                            self.name, after, before)

    def describe(self):
        return f"Add field {self.name} to {self.model_name}"

    @property
    def migration_name_fragment(self):
        return f"{self.model_name.lower()}_{self.name}"

    def reduce(self, other):
        if isinstance(other, DeleteModel) and other.name == self.model_name:
            return [other]
        if not self._same_field(other):
            return None
        if isinstance(other, AlterField):
            backfill = other.backfill if other.backfill is not NOT_PROVIDED else self.backfill
            return [AddField(self.model_name, self.name, other.field, backfill=backfill)]
        if isinstance(other, RemoveField):
            return []
        if isinstance(other, RenameField):
            return [AddField(self.model_name, other.new_name, self.field, backfill=self.backfill)]
        return None


class RemoveField(FieldOperation):
    """Drop a column."""

    def deconstruct(self):
        return {"model_name": self.model_name, "name": self.name}

    def _pins(self, other) -> bool:
        # The removed field's FK target is unknown here: never move a removal
        # across model creation / deletion / renames.
        from .models import CreateModel, RenameModel
        return isinstance(other, (CreateModel, DeleteModel, RenameModel))

    def state_forwards(self, state):
        ms = state.get_model(self.model_name).clone()
        field = ms.get_field(self.name)
        if field.primary_key:
            raise MigrationError(f"{self.model_name}.{self.name}: cannot remove the primary key.")
        if _uses_in_options(ms, self.name):
            raise MigrationError(
                f"{self.model_name}.{self.name} is still used by indexes / unique_together; "
                "alter those first."
            )
        del ms.fields[self.name]
        state.models[self.model_name] = ms

    def database_forwards(self, editor, before, after):
        editor.remove_field(before.get_model(self.model_name), after.get_model(self.model_name),
                            self.name, before, after)

    def database_backwards(self, editor, before, after):
        editor.add_field(after.get_model(self.model_name), before.get_model(self.model_name),
                         self.name, before)

    def describe(self):
        return f"Remove field {self.name} from {self.model_name}"

    @property
    def migration_name_fragment(self):
        return f"remove_{self.model_name.lower()}_{self.name}"

    def reduce(self, other):
        if isinstance(other, DeleteModel) and other.name == self.model_name:
            return [other]
        return None


class AlterField(FieldOperation):
    """Change a column's type, nullability, default, uniqueness, index or FK.

    ``backfill`` replaces NULLs in existing rows before a column becomes
    NOT NULL.
    """

    def __init__(self, model_name: str, name: str, field, backfill=NOT_PROVIDED):
        super().__init__(model_name, name)
        self.field = prepare_field(field, name)
        self.backfill = backfill

    def deconstruct(self):
        out = {"model_name": self.model_name, "name": self.name, "field": self.field}
        if self.backfill is not NOT_PROVIDED:
            out["backfill"] = self.backfill
        return out

    def state_forwards(self, state):
        ms = state.get_model(self.model_name).clone()
        old = ms.get_field(self.name)
        if old.primary_key != self.field.primary_key:
            raise MigrationError(
                f"{self.model_name}.{self.name}: migrations cannot move the primary key."
            )
        if hasattr(self.field, "to"):
            state.target_of(self.field)
        ms.fields[self.name] = self.field
        state.models[self.model_name] = ms

    def database_forwards(self, editor, before, after):
        editor.alter_field(before.get_model(self.model_name), after.get_model(self.model_name),
                           self.name, before, after, backfill=self.backfill)

    def database_backwards(self, editor, before, after):
        editor.alter_field(after.get_model(self.model_name), before.get_model(self.model_name),
                           self.name, after, before)

    def describe(self):
        return f"Alter field {self.name} on {self.model_name}"

    @property
    def migration_name_fragment(self):
        return f"alter_{self.model_name.lower()}_{self.name}"

    def reduce(self, other):
        if isinstance(other, DeleteModel) and other.name == self.model_name:
            return [other]
        if not self._same_field(other):
            return None
        if isinstance(other, AlterField):
            backfill = other.backfill if other.backfill is not NOT_PROVIDED else self.backfill
            return [AlterField(self.model_name, self.name, other.field, backfill=backfill)]
        if isinstance(other, RemoveField):
            return [other]
        return None


class RenameField(FieldOperation):
    """Rename a column (data, indexes and constraints are kept)."""

    def __init__(self, model_name: str, old_name: str, new_name: str):
        super().__init__(model_name, old_name)
        self.old_name = self.name
        self.new_name = validate_identifier(new_name, "field name")

    def deconstruct(self):
        return {"model_name": self.model_name, "old_name": self.old_name, "new_name": self.new_name}

    def state_forwards(self, state):
        ms = state.get_model(self.model_name)
        field = ms.get_field(self.old_name)
        if self.new_name in ms.fields:
            raise MigrationError(f"{self.model_name} already has a field {self.new_name!r}.")
        renamed = field.clone()
        renamed.name = self.new_name
        new = ms.clone()
        new.fields = {(self.new_name if n == self.old_name else n): (renamed if n == self.old_name else f)
                      for n, f in ms.fields.items()}
        for key in ("indexes", "unique_together"):
            new.options[key] = sorted(tuple(self.new_name if c == self.old_name else c for c in cols)
                                      for cols in ms.options[key])
        state.models[self.model_name] = new

    def database_forwards(self, editor, before, after):
        editor.rename_field(before.get_model(self.model_name), after.get_model(self.model_name),
                            self.old_name, self.new_name, after)

    def database_backwards(self, editor, before, after):
        editor.rename_field(after.get_model(self.model_name), before.get_model(self.model_name),
                            self.new_name, self.old_name, before)

    def describe(self):
        return f"Rename field {self.old_name} on {self.model_name} to {self.new_name}"

    @property
    def migration_name_fragment(self):
        return f"rename_{self.old_name}_{self.model_name.lower()}_{self.new_name}"

    def _same_field(self, other) -> bool:
        return (isinstance(other, FieldOperation) and other.model_name == self.model_name
                and other.name == self.new_name)

    def reduce(self, other):
        if isinstance(other, DeleteModel) and other.name == self.model_name:
            return [other]
        if isinstance(other, RenameField) and self._same_field(other):
            if other.new_name == self.old_name:
                return []
            return [RenameField(self.model_name, self.old_name, other.new_name)]
        return None
