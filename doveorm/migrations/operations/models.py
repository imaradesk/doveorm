"""Model-level operations: create / delete / rename models and table options."""
from __future__ import annotations

import re

from ...exceptions import MigrationError
from ...fields import ForeignKey
from ...utils.sql import validate_identifier
from ..state import ModelState, normalize_column_sets, normalize_options
from .base import Operation, fk_targets, prepare_field


def default_table_name(model_name: str) -> str:
    """Same rule as ``Model._table_name()``: CamelCase → snake_case plural."""
    from ...model.schema import SchemaMixin
    return SchemaMixin._pluralize(re.sub(r"(?<!^)(?=[A-Z])", "_", model_name).lower())


class ModelOperation(Operation):
    def __init__(self, name: str):
        self.name = validate_identifier(name, "model name")

    structural = True

    def writes(self) -> set[str]:
        return {self.name}


class CreateModel(ModelOperation):
    """Create a table for a new model."""

    def __init__(self, name: str, fields, table: str | None = None, options: dict | None = None):
        super().__init__(name)
        self.table = validate_identifier(table or default_table_name(name), "table name")
        items = fields.items() if isinstance(fields, dict) else fields
        self.fields = [(fname, prepare_field(f, fname)) for fname, f in items]
        self.options = {k: v for k, v in normalize_options(options).items() if v}
        ModelState(self.name, self.table, self.fields, self.options)     # validate early

    def deconstruct(self):
        out = {"name": self.name, "table": self.table, "fields": self.fields}
        if self.options:
            out["options"] = self.options
        return out

    def state_forwards(self, state):
        state.add_model(ModelState(self.name, self.table, self.fields, self.options))

    def database_forwards(self, editor, before, after):
        editor.create_model(after.get_model(self.name), after)

    def database_backwards(self, editor, before, after):
        editor.delete_model(after.get_model(self.name), after)

    def describe(self):
        return f"Create model {self.name}"

    @property
    def migration_name_fragment(self):
        return f"create_{self.name.lower()}"

    def reads(self):
        return fk_targets(f for _, f in self.fields) - {self.name}

    def _with(self, **changes) -> "CreateModel":
        kwargs = {"name": self.name, "fields": self.fields, "table": self.table,
                  "options": self.options}
        kwargs.update(changes)
        return CreateModel(**kwargs)

    def _rename_in_options(self, old, new):
        def ren(sets):
            return [tuple(new if c == old else c for c in cols) for cols in sets]
        return {k: (ren(v) if k in ("indexes", "unique_together") else v)
                for k, v in self.options.items()}

    def _uses_in_options(self, field_name) -> bool:
        return any(field_name in cols for key in ("indexes", "unique_together")
                   for cols in self.options.get(key, ()))

    def reduce(self, other):
        from .fields import AddField, AlterField, RemoveField, RenameField
        if isinstance(other, DeleteModel) and other.name == self.name:
            return []
        if isinstance(other, RenameModel) and other.old_name == self.name:
            return [self._with(name=other.new_name)]
        if isinstance(other, AlterModelTable) and other.name == self.name:
            return [self._with(table=other.table)]
        if isinstance(other, (AlterIndexes, AlterUniqueTogether)) and other.name == self.name:
            return [self._with(options={**self.options, other.option_name: other.value})]
        if isinstance(other, AlterTableOptions) and other.name == self.name:
            return [self._with(options={**self.options, **other.values})]
        if getattr(other, "model_name", None) != self.name:
            return None
        if isinstance(other, AddField):
            return [self._with(fields=self.fields + [(other.name, other.field)])]
        if isinstance(other, AlterField):
            return [self._with(fields=[(n, other.field if n == other.name else f)
                                       for n, f in self.fields])]
        removed = dict(self.fields).get(other.name) if isinstance(other, RemoveField) else None
        if removed is not None and not removed.primary_key and not self._uses_in_options(other.name):
            return [self._with(fields=[(n, f) for n, f in self.fields if n != other.name])]
        if isinstance(other, RenameField):
            return [self._with(
                fields=[(other.new_name if n == other.old_name else n, f) for n, f in self.fields],
                options=self._rename_in_options(other.old_name, other.new_name),
            )]
        return None


class DeleteModel(ModelOperation):
    """Drop a model's table."""

    def deconstruct(self):
        return {"name": self.name}

    def _pins(self, other) -> bool:
        # The deleted model's own foreign keys are unknown here, so keep its
        # position relative to other structural changes and field removals.
        from .fields import RemoveField
        return other.structural or isinstance(other, RemoveField)

    def state_forwards(self, state):
        state.get_model(self.name)
        refs = [f"{ms.name}.{n}" for ms, n, _ in state.referencing_fields(self.name)
                if ms.name != self.name]
        if refs:
            raise MigrationError(
                f"Cannot delete model {self.name}: still referenced by {', '.join(refs)}."
            )
        del state.models[self.name]

    def database_forwards(self, editor, before, after):
        editor.delete_model(before.get_model(self.name), before)

    def database_backwards(self, editor, before, after):
        editor.create_model(before.get_model(self.name), before)

    def describe(self):
        return f"Delete model {self.name}"

    @property
    def migration_name_fragment(self):
        return f"delete_{self.name.lower()}"


class RenameModel(Operation):
    """Rename a model class.  The table is unchanged (see ``AlterModelTable``)."""

    def __init__(self, old_name: str, new_name: str):
        self.old_name = validate_identifier(old_name, "model name")
        self.new_name = validate_identifier(new_name, "model name")

    def deconstruct(self):
        return {"old_name": self.old_name, "new_name": self.new_name}

    def state_forwards(self, state):
        ms = state.get_model(self.old_name).clone()
        if self.new_name in state.models:
            raise MigrationError(f"Cannot rename {self.old_name}: model {self.new_name} exists.")
        del state.models[self.old_name]
        ms.name = self.new_name
        state.models[self.new_name] = ms
        # Foreign keys refer to models by name: repoint them.
        for other in list(state.models.values()):
            if not other.references(self.old_name):
                continue
            changed = other.clone()
            for fname, field in other.fields.items():
                if isinstance(field, ForeignKey) and field.to == self.old_name:
                    cls, kwargs = field.deconstruct()
                    kwargs["to"] = self.new_name
                    new_field = cls(**kwargs)
                    new_field.name = fname
                    changed.fields[fname] = new_field
            state.models[changed.name] = changed

    def database_forwards(self, editor, before, after):
        pass

    def database_backwards(self, editor, before, after):
        pass

    def describe(self):
        return f"Rename model {self.old_name} to {self.new_name}"

    @property
    def migration_name_fragment(self):
        return f"rename_{self.old_name.lower()}_{self.new_name.lower()}"

    structural = True

    def writes(self):
        return {self.old_name, self.new_name}

    def reduce(self, other):
        if isinstance(other, RenameModel) and other.old_name == self.new_name:
            if other.new_name == self.old_name:
                return []
            return [RenameModel(self.old_name, other.new_name)]
        return None


class AlterModelTable(ModelOperation):
    """Rename a model's table."""

    def __init__(self, name: str, table: str):
        super().__init__(name)
        self.table = validate_identifier(table, "table name")

    def deconstruct(self):
        return {"name": self.name, "table": self.table}

    def state_forwards(self, state):
        ms = state.get_model(self.name).clone()
        clash = state.model_for_table(self.table)
        if clash is not None and clash.name != self.name:
            raise MigrationError(f"Table {self.table!r} is already used by model {clash.name}.")
        ms.table = self.table
        state.models[self.name] = ms

    def database_forwards(self, editor, before, after):
        old, new = before.get_model(self.name).table, after.get_model(self.name).table
        if old != new:
            editor.rename_table(old, new)

    def database_backwards(self, editor, before, after):
        old, new = before.get_model(self.name).table, after.get_model(self.name).table
        if old != new:
            editor.rename_table(new, old)

    def describe(self):
        return f"Rename table for {self.name} to {self.table}"

    @property
    def migration_name_fragment(self):
        return f"alter_{self.name.lower()}_table"

    def reduce(self, other):
        if isinstance(other, (AlterModelTable, DeleteModel)) and other.name == self.name:
            return [other]
        return None


class _AlterColumnSets(ModelOperation):
    option_name = ""
    structural = False

    def __init__(self, name: str, value):
        super().__init__(name)
        self.value = normalize_column_sets(value)

    def deconstruct(self):
        return {"name": self.name, self.option_name: self.value}

    def state_forwards(self, state):
        ms = state.get_model(self.name).clone()
        for cols in self.value:
            for col in cols:
                ms.get_field(col)
        ms.options[self.option_name] = list(self.value)
        state.models[self.name] = ms

    def database_forwards(self, editor, before, after):
        editor.sync_indexes(before.get_model(self.name), after.get_model(self.name))

    def database_backwards(self, editor, before, after):
        editor.sync_indexes(after.get_model(self.name), before.get_model(self.name))

    def reduce(self, other):
        if (isinstance(other, (type(self), DeleteModel))) and other.name == self.name:
            return [other]
        return None


class AlterIndexes(_AlterColumnSets):
    """Set a model's composite indexes (``Meta.indexes``)."""
    option_name = "indexes"

    def __init__(self, name: str, indexes):
        super().__init__(name, indexes)

    def describe(self):
        return f"Alter indexes for {self.name} ({len(self.value)} index(es))"

    @property
    def migration_name_fragment(self):
        return f"alter_{self.name.lower()}_indexes"


class AlterUniqueTogether(_AlterColumnSets):
    """Set a model's composite unique constraints (``Meta.unique_together``)."""
    option_name = "unique_together"

    def __init__(self, name: str, unique_together):
        super().__init__(name, unique_together)

    def describe(self):
        return f"Alter unique_together for {self.name} ({len(self.value)} constraint(s))"

    @property
    def migration_name_fragment(self):
        return f"alter_{self.name.lower()}_unique_together"


class AlterTableOptions(ModelOperation):
    """Change MySQL ``engine`` / ``charset`` (no-op on SQLite)."""
    structural = False

    def __init__(self, name: str, engine: str | None = None, charset: str | None = None):
        super().__init__(name)
        self.values = {k: v for k, v in normalize_options(
            {"engine": engine, "charset": charset}).items() if k in ("engine", "charset")}

    def deconstruct(self):
        return {"name": self.name, **self.values}

    def state_forwards(self, state):
        ms = state.get_model(self.name).clone()
        ms.options.update(self.values)
        state.models[self.name] = ms

    def database_forwards(self, editor, before, after):
        editor.alter_table_options(before.get_model(self.name), after.get_model(self.name))

    def database_backwards(self, editor, before, after):
        editor.alter_table_options(after.get_model(self.name), before.get_model(self.name))

    def describe(self):
        return f"Alter table options for {self.name}"

    @property
    def migration_name_fragment(self):
        return f"alter_{self.name.lower()}_options"

    def reduce(self, other):
        if isinstance(other, (AlterTableOptions, DeleteModel)) and other.name == self.name:
            return [other]
        return None
