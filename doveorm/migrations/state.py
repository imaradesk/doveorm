"""In-memory description of the schema that migrations build up.

A :class:`ProjectState` is a set of :class:`ModelState` objects – plain data
(model name, table, fields, table options) with no live model classes
behind it.  It is produced two ways:

* by replaying the operations of the migration files (the "history"), and
* from the live model classes (``ProjectState.from_models``).

The autodetector diffs the two.  States are cheap to copy: ``clone()`` is
shallow and operations *replace* the ``ModelState`` they change instead of
mutating it, so earlier snapshots stay valid.
"""
from __future__ import annotations

from typing import Iterable

from ..exceptions import MigrationError
from ..fields import Field, ForeignKey
from ..utils.sql import validate_identifier

# Table-level options tracked by migrations (everything else in Meta is
# Python-side behaviour and never needs a migration).
OPTION_KEYS = ("indexes", "unique_together", "engine", "charset")


def normalize_column_sets(spec) -> list[tuple[str, ...]]:
    """``["a", ("b", "c")]`` → ``[("a",), ("b", "c")]`` (sorted, de-duplicated)."""
    out = set()
    for entry in spec or ():
        cols = tuple(entry) if isinstance(entry, (list, tuple)) else (entry,)
        if not cols:
            raise MigrationError("Empty column list in indexes / unique_together.")
        for col in cols:
            validate_identifier(col, "index column")
        out.add(cols)
    return sorted(out)


def normalize_options(options: dict | None) -> dict:
    options = dict(options or {})
    unknown = set(options) - set(OPTION_KEYS)
    if unknown:
        raise MigrationError(f"Unknown model option(s): {', '.join(sorted(unknown))}.")
    out = {
        "indexes": normalize_column_sets(options.get("indexes")),
        "unique_together": normalize_column_sets(options.get("unique_together")),
        "engine": options.get("engine"),
        "charset": options.get("charset"),
    }
    for key in ("engine", "charset"):
        if out[key] is not None:
            validate_identifier(out[key], f"MySQL {key}")
    return out


def field_signature(field: Field) -> tuple:
    """Hashable schema signature used to detect changed fields."""
    cls, kwargs = field.deconstruct()
    return cls, tuple(sorted((k, _freeze(v)) for k, v in kwargs.items()))


def _freeze(value):
    if isinstance(value, dict):
        return ("dict", tuple(sorted((k, _freeze(v)) for k, v in value.items())))
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, tuple(_freeze(v) for v in value))
    if isinstance(value, set):
        return ("set", tuple(sorted(_freeze(v) for v in value)))
    return (type(value).__name__, value)


class ModelState:
    """Schema of one model: name, table, ordered fields and table options."""

    __slots__ = ("name", "table", "fields", "options")

    def __init__(self, name: str, table: str, fields, options: dict | None = None):
        validate_identifier(name, "model name")
        validate_identifier(table, "table name")
        self.name = name
        self.table = table
        self.fields: dict[str, Field] = {}
        for fname, field in (fields.items() if isinstance(fields, dict) else fields):
            validate_identifier(fname, "field name")
            if not isinstance(field, Field):
                raise MigrationError(f"{name}.{fname}: expected a Field, got {field!r}.")
            if fname in self.fields:
                raise MigrationError(f"{name}: duplicate field {fname!r}.")
            field.name = fname
            self.fields[fname] = field
        self.options = normalize_options(options)
        pks = [n for n, f in self.fields.items() if f.primary_key]
        if len(pks) != 1:
            raise MigrationError(f"{name} must have exactly one primary key, found {len(pks)}.")

    @classmethod
    def from_model(cls, model) -> "ModelState":
        return cls(
            model.__name__,
            model._table_name(),
            [(n, f.clone()) for n, f in model._fields.items()],
            {
                "indexes": model._meta_indexes,
                "unique_together": model._meta_unique_together,
                "engine": model._meta_engine,
                "charset": model._meta_charset,
            },
        )

    def clone(self) -> "ModelState":
        new = object.__new__(ModelState)
        new.name = self.name
        new.table = self.table
        new.fields = dict(self.fields)
        new.options = {k: (list(v) if isinstance(v, list) else v) for k, v in self.options.items()}
        return new

    @property
    def pk_name(self) -> str:
        return next(n for n, f in self.fields.items() if f.primary_key)

    @property
    def pk_field(self) -> Field:
        return self.fields[self.pk_name]

    def get_field(self, name: str) -> Field:
        try:
            return self.fields[name]
        except KeyError:
            raise MigrationError(f"Model {self.name} has no field {name!r}.") from None

    def references(self, model_name: str) -> bool:
        return any(isinstance(f, ForeignKey) and f.to == model_name for f in self.fields.values())

    def __eq__(self, other):
        if not isinstance(other, ModelState):
            return NotImplemented
        return (self.name == other.name and self.table == other.table
                and self.options == other.options
                and self.fields.keys() == other.fields.keys()      # column order is irrelevant
                and all(field_signature(self.fields[n]) == field_signature(other.fields[n])
                        for n in self.fields))

    def __repr__(self):
        return f"<ModelState {self.name} table={self.table!r} fields={list(self.fields)}>"


class ProjectState:
    """All models at one point of the migration history."""

    __slots__ = ("models",)

    def __init__(self, models: Iterable[ModelState] = ()):
        self.models: dict[str, ModelState] = {}
        for ms in models:
            self.add_model(ms)

    @classmethod
    def from_models(cls, models: Iterable[type]) -> "ProjectState":
        state = cls(ModelState.from_model(m) for m in models)
        state.check_references()
        return state

    def clone(self) -> "ProjectState":
        new = object.__new__(ProjectState)
        new.models = dict(self.models)
        return new

    def add_model(self, ms: ModelState) -> None:
        if ms.name in self.models:
            raise MigrationError(f"Model {ms.name} already exists in the migration state.")
        clash = self.model_for_table(ms.table)
        if clash is not None:
            raise MigrationError(f"Models {clash.name} and {ms.name} both use table {ms.table!r}.")
        self.models[ms.name] = ms

    def get_model(self, name: str) -> ModelState:
        try:
            return self.models[name]
        except KeyError:
            raise MigrationError(f"Model {name!r} does not exist in the migration state.") from None

    def model_for_table(self, table: str) -> ModelState | None:
        return next((m for m in self.models.values() if m.table == table), None)

    def target_of(self, field: ForeignKey) -> ModelState:
        """The model a foreign key points to (must exist in this state)."""
        try:
            return self.models[field.to]
        except KeyError:
            raise MigrationError(
                f"Field {field.name!r} references model {field.to!r}, which does not exist "
                "at this point of the migration history."
            ) from None

    def referencing_fields(self, model_name: str) -> list[tuple[ModelState, str, ForeignKey]]:
        """Every foreign key (in any model) pointing at *model_name*."""
        return [(ms, n, f) for ms in self.models.values() for n, f in ms.fields.items()
                if isinstance(f, ForeignKey) and f.to == model_name]

    def check_references(self) -> None:
        for ms in self.models.values():
            for field in ms.fields.values():
                if isinstance(field, ForeignKey) and field.to not in self.models:
                    raise MigrationError(
                        f"{ms.name}.{field.name} references model {field.to!r}, which is not "
                        "part of the migrated models."
                    )

    def __eq__(self, other):
        if not isinstance(other, ProjectState):
            return NotImplemented
        return self.models == other.models

    def __repr__(self):
        return f"<ProjectState models={sorted(self.models)}>"
