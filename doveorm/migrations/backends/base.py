"""Schema editor base: turns migration operations into DDL.

A schema editor is used as a context manager around one migration::

    with editor_for(db) as editor:
        operation.database_forwards(editor, before, after)

Inside it, every statement goes through :meth:`execute`, which logs it and
(unless ``collect_sql=True``) runs it.  ``collect_sql=True`` is how
``sqlmigrate`` prints the SQL of a migration without touching the schema.

All identifiers come from validated model states, so they are safe to
interpolate.  Values (defaults, backfills) are rendered with
``quote_literal`` or passed as bound parameters.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Sequence

from ...exceptions import MigrationError
from ...fields import Field, ForeignKey
from ...utils.sql import index_name, quote_literal

if TYPE_CHECKING:
    from ..state import ModelState, ProjectState

log = logging.getLogger("doveorm.migrations")

NOT_PROVIDED = object()


class BaseSchemaEditor:
    """Backend-independent parts of DDL generation."""

    def __init__(self, db, *, atomic: bool = True, collect_sql: bool = False):
        self.db = db
        self.driver = db.driver
        self.atomic = atomic
        self.collect_sql = collect_sql
        self.collected: list[str] = []
        self._tx = None

    # ── context management ───────────────────────

    def __enter__(self):
        if self.atomic and not self.collect_sql:
            self._tx = self.db.transaction()
            self._tx.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._tx is not None:
            tx, self._tx = self._tx, None
            return tx.__exit__(exc_type, exc, tb)
        return False

    # ── execution ────────────────────────────────

    def execute(self, sql: str, params: Sequence | None = None) -> None:
        if params:
            # Only used for data backfills; shown with placeholders in collected SQL.
            self.collected.append(f"{sql};  -- params: {len(params)}")
        else:
            self.collected.append(sql + ";")
        if self.collect_sql:
            return
        log.debug("migration SQL: %s", sql)
        if params:
            self.db.execute_write(sql, params)
        else:
            self.db.execute_ddl(sql)

    def quote_value(self, value) -> str:
        literal = quote_literal(value, backslash_escapes=self.driver.backslash_escapes)
        if literal is None:
            raise MigrationError(f"Cannot render {value!r} as a SQL literal.")
        return literal

    # ── column rendering ─────────────────────────

    def column_type(self, field: Field, state: "ProjectState") -> str:
        """Backend column type; foreign keys follow the *state's* target primary key."""
        if isinstance(field, ForeignKey):
            return state.target_of(field).pk_field._resolved_type(self.driver)
        return field._resolved_type(self.driver)

    def column_sql(self, field: Field, state: "ProjectState", **opts) -> str:
        return field.sql_definition(self.driver, sql_type=self.column_type(field, state), **opts)

    def fk_constraint_name(self, table: str, column: str) -> str:
        return index_name("fk", table, [column])

    def fk_clause(self, table: str, name: str, field: ForeignKey, state: "ProjectState") -> str:
        target = state.target_of(field)
        return (f"CONSTRAINT {self.fk_constraint_name(table, name)} FOREIGN KEY ({name}) "
                f"REFERENCES {target.table}({target.pk_name}) ON DELETE {field.on_delete}")

    def backfill_literal(self, field: Field, value, state: "ProjectState") -> str:
        """SQL literal for a one-off value used to fill existing rows."""
        if isinstance(field, ForeignKey):
            db_value = state.target_of(field).pk_field.to_db(value)
        else:
            db_value = field.to_db(field.to_python(value))
        return self.quote_value(db_value)

    def default_literal(self, field: Field, state: "ProjectState") -> str | None:
        """The field's DDL default as a literal, or None."""
        return field._default_sql(self.driver, self.column_type(field, state))

    def check_column(self, ms: "ModelState", name: str, field: Field, state: "ProjectState") -> None:
        """Hook: reject column definitions the backend cannot index etc."""

    # ── indexes (shared naming with Model.create_table) ──

    @staticmethod
    def index_specs(ms: "ModelState") -> list[tuple[str, tuple[str, ...], bool]]:
        """``(name, columns, unique)`` for every secondary index the model needs."""
        specs = []
        for name, field in ms.fields.items():
            if field.db_index and not field.primary_key and not field.unique:
                specs.append((index_name("idx", ms.table, [name]), (name,), False))
        for cols in ms.options["indexes"]:
            specs.append((index_name("idx", ms.table, cols), tuple(cols), False))
        for cols in ms.options["unique_together"]:
            specs.append((index_name("uq", ms.table, cols), tuple(cols), True))
        seen, out = set(), []
        for spec in specs:
            if spec[0] not in seen:
                seen.add(spec[0])
                out.append(spec)
        return out

    def create_index(self, table: str, name: str, columns: Sequence[str], unique: bool) -> None:
        kind = "UNIQUE INDEX" if unique else "INDEX"
        self.execute(f"CREATE {kind} {name} ON {table} ({', '.join(columns)})")

    def drop_index(self, table: str, name: str, columns: Sequence[str], unique: bool) -> None:
        raise NotImplementedError

    def sync_indexes(self, old: "ModelState | None", new: "ModelState") -> None:
        """Drop indexes *old* had but *new* lacks, then create new ones."""
        old_specs = {s[0]: s for s in (self.index_specs(old) if old is not None else [])}
        new_specs = {s[0]: s for s in self.index_specs(new)}
        old_cols = {(s[1], s[2]) for s in old_specs.values()}
        new_cols = {(s[1], s[2]) for s in new_specs.values()}
        for name, (_, cols, unique) in old_specs.items():
            if (cols, unique) not in new_cols:
                self.drop_index(old.table if old is not None else new.table, name, cols, unique)
        for name, (_, cols, unique) in new_specs.items():
            if (cols, unique) not in old_cols:
                self.create_index(new.table, name, cols, unique)

    # ── operations (implemented per backend) ─────

    def create_model(self, ms: "ModelState", state: "ProjectState") -> None:
        raise NotImplementedError

    def delete_model(self, ms: "ModelState", state: "ProjectState") -> None:
        self.execute(f"DROP TABLE {ms.table}")

    def rename_table(self, old_table: str, new_table: str) -> None:
        raise NotImplementedError

    def add_field(self, before: "ModelState", after: "ModelState", name: str,
                  state: "ProjectState", backfill=NOT_PROVIDED) -> None:
        raise NotImplementedError

    def remove_field(self, before: "ModelState", after: "ModelState", name: str,
                     state_before: "ProjectState", state_after: "ProjectState") -> None:
        raise NotImplementedError

    def alter_field(self, before: "ModelState", after: "ModelState", name: str,
                    state_before: "ProjectState", state_after: "ProjectState",
                    backfill=NOT_PROVIDED) -> None:
        raise NotImplementedError

    def rename_field(self, before: "ModelState", after: "ModelState", old: str, new: str,
                     state: "ProjectState") -> None:
        raise NotImplementedError

    def alter_table_options(self, before: "ModelState", after: "ModelState") -> None:
        """Engine / charset changes (MySQL only)."""

    # ── introspection (used by --fake-initial) ───

    def table_exists(self, table: str) -> bool:
        return self.db.table_exists(table)

    def column_names(self, table: str) -> set[str]:
        raise NotImplementedError


def editor_for(db, **kwargs) -> BaseSchemaEditor:
    """The schema editor for *db*'s backend."""
    name = db.driver.name
    if name == "sqlite":
        from .sqlite import SQLiteSchemaEditor
        return SQLiteSchemaEditor(db, **kwargs)
    if name == "mysql":
        from .mysql import MySQLSchemaEditor
        return MySQLSchemaEditor(db, **kwargs)
    raise MigrationError(f"Migrations are not supported for the {name!r} backend.")
