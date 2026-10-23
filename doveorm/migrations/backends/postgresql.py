"""PostgreSQL schema editor.

PostgreSQL DDL is transactional, so every migration runs inside one
transaction: if a statement fails, the whole migration is rolled back.

Column changes use ``ALTER COLUMN … TYPE / SET NOT NULL / SET DEFAULT``
(there is no ``MODIFY``).  Constraint and index names are looked up in the
system catalogs when they are not the ones the ORM would generate, so
tables created by ``Model.create_table()`` (``<table>_<col>_key`` /
``<table>_<col>_fkey`` constraints) can be migrated too.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ...exceptions import MigrationError
from ...fields import ForeignKey
from ...utils.sql import index_name
from .base import NOT_PROVIDED, BaseSchemaEditor

if TYPE_CHECKING:
    from ..state import ModelState, ProjectState


_INDEXES_SQL = """
SELECT i.relname AS index_name, ix.indisunique AS is_unique, ix.indisprimary AS is_primary,
       c.conname AS constraint_name,
       array_to_string(ARRAY(
           SELECT a.attname FROM unnest(ix.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
           JOIN pg_catalog.pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
           ORDER BY k.ord), ',') AS columns
FROM pg_catalog.pg_index ix
JOIN pg_catalog.pg_class t ON t.oid = ix.indrelid
JOIN pg_catalog.pg_class i ON i.oid = ix.indexrelid
JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
LEFT JOIN pg_catalog.pg_constraint c ON c.conindid = ix.indexrelid AND c.contype IN ('u', 'p')
WHERE n.nspname = current_schema() AND t.relname = ?
"""

_FKS_SQL = """
SELECT c.conname
FROM pg_catalog.pg_constraint c
JOIN pg_catalog.pg_class t ON t.oid = c.conrelid
JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
JOIN pg_catalog.pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey)
WHERE c.contype = 'f' AND n.nspname = current_schema() AND t.relname = ? AND a.attname = ?
"""


def _quoted(name: str) -> str:
    """Quote a name read from the catalog (exact case, may need quoting)."""
    return '"' + name.replace('"', '""') + '"'


class PostgreSQLSchemaEditor(BaseSchemaEditor):

    # ── tables ──

    def create_model(self, ms: "ModelState", state: "ProjectState") -> None:
        cols, fks = [], []
        for name, field in ms.fields.items():
            cols.append(f"{self.qn(name)} {self.column_sql(field, state)}")
            if isinstance(field, ForeignKey):
                fks.append(self.fk_clause(ms.table, name, field, state))
        body = ",\n    ".join(cols + fks)
        self.execute(f"CREATE TABLE {self.qn(ms.table)} (\n    {body}\n)")
        for name, cols_, unique in self.index_specs(ms):
            self.create_index(ms.table, name, cols_, unique)

    def rename_table(self, old_table: str, new_table: str) -> None:
        self.execute(f"ALTER TABLE {self.qn(old_table)} RENAME TO {self.qn(new_table)}")

    # ── indexes / constraints ──

    def drop_index(self, table, name, columns, unique) -> None:
        if self.collect_sql:
            self.execute(f"DROP INDEX IF EXISTS {self.qn(name)}")
            return
        stored = self.driver.stored_name
        indexes = self._indexes(table)
        match = next((ix for ix in indexes if ix["index_name"] == stored(name)), None)
        if match is None:
            wanted = tuple(stored(c) for c in columns)
            match = next((ix for ix in indexes
                          if not ix["is_primary"] and bool(ix["is_unique"]) == unique
                          and tuple(ix["columns"].split(",")) == wanted), None)
        if match is None:
            return
        # Catalog names are exact, so they are always quoted when reused.
        if match["constraint_name"]:
            # UNIQUE constraints own their index; drop the constraint instead.
            self.execute(f"ALTER TABLE {self.qn(table)} DROP CONSTRAINT {_quoted(match['constraint_name'])}")
        else:
            self.execute(f"DROP INDEX {_quoted(match['index_name'])}")

    def _indexes(self, table: str) -> list:
        return self.db.execute(_INDEXES_SQL, [self.driver.stored_name(table)])

    def _fk_names(self, table: str, column: str) -> list[str]:
        if self.collect_sql:
            return [self.qn(self.fk_constraint_name(table, column))]
        stored = self.driver.stored_name
        return [_quoted(row[0]) for row in self.db.execute(_FKS_SQL, [stored(table), stored(column)])]

    def _drop_fks(self, table: str, column: str) -> None:
        for name in self._fk_names(table, column):
            self.execute(f"ALTER TABLE {self.qn(table)} DROP CONSTRAINT {name}")

    def _add_fk(self, table: str, name: str, field: ForeignKey, state) -> None:
        self.execute(f"ALTER TABLE {self.qn(table)} ADD {self.fk_clause(table, name, field, state)}")

    # ── columns ──

    def add_field(self, before, after, name, state, backfill=NOT_PROVIDED) -> None:
        field = after.fields[name]
        definition = self.column_sql(field, state)
        temp_default = backfill is not NOT_PROVIDED and self.default_literal(field, state) is None
        if temp_default:
            # Existing rows take the backfill through a temporary DEFAULT.
            definition += f" DEFAULT {self.backfill_literal(field, backfill, state)}"
        table, col = self.qn(after.table), self.qn(name)
        self.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
        if temp_default:
            self.execute(f"ALTER TABLE {table} ALTER COLUMN {col} DROP DEFAULT")
        elif backfill is not NOT_PROVIDED:
            self.execute(f"UPDATE {table} SET {col} = "
                         f"{self.backfill_literal(field, backfill, state)}")
        if isinstance(field, ForeignKey):
            self._add_fk(after.table, name, field, state)
        if field.db_index:
            self.sync_indexes(before, after)

    def remove_field(self, before, after, name, state_before, state_after) -> None:
        # Indexes and constraints on this column disappear with it.
        self.execute(f"ALTER TABLE {self.qn(before.table)} DROP COLUMN {self.qn(name)}")

    def _alter_type(self, table: str, name: str, sql_type: str) -> None:
        table, name = self.qn(table), self.qn(name)
        self.execute(f"ALTER TABLE {table} ALTER COLUMN {name} TYPE {sql_type} "
                     f"USING {name}::{sql_type}")

    def alter_field(self, before, after, name, state_before, state_after, backfill=NOT_PROVIDED) -> None:
        old, new = before.fields[name], after.fields[name]
        table = after.table
        if old.primary_key != new.primary_key:
            raise MigrationError(
                f"{after.name}.{name}: changing which field is the primary key is not "
                "supported by migrations; write a RunSQL operation."
            )
        old_fk = isinstance(old, ForeignKey)
        new_fk = isinstance(new, ForeignKey)
        fk_changed = old_fk and (not new_fk or (old.to, old.on_delete) != (new.to, new.on_delete))
        if fk_changed:
            self._drop_fks(table, name)
        if old.unique and not new.unique and not old.primary_key:
            self.drop_index(table, index_name("uq", table, [name]), (name,), True)
        old_idx = old.db_index and not old.unique and not old.primary_key
        new_idx = new.db_index and not new.unique and not new.primary_key
        if old_idx and not new_idx:
            self.drop_index(table, index_name("idx", table, [name]), (name,), False)

        old_type = self.column_type(old, state_before)
        new_type = self.column_type(new, state_after)
        type_changed = old_type != new_type
        # A primary key whose type changes drags its foreign keys along.
        children = state_after.referencing_fields(after.name) if new.primary_key and type_changed else []
        for child_ms, child_name, _ in children:
            self._drop_fks(child_ms.table, child_name)

        old_default = self.default_literal(old, state_before)
        new_default = self.default_literal(new, state_after)
        default_changed = type_changed or old_default != new_default
        qt, qc = self.qn(table), self.qn(name)
        if old_default is not None and default_changed:
            # An old-typed default can block the type change.
            self.execute(f"ALTER TABLE {qt} ALTER COLUMN {qc} DROP DEFAULT")

        if backfill is not NOT_PROVIDED:
            self.execute(f"UPDATE {qt} SET {qc} = ? WHERE {qc} IS NULL",
                         [self._backfill_param(new, backfill, state_after)])
        if type_changed:
            self._alter_type(table, name, new_type)
        if not new.primary_key and old.nullable != new.nullable:
            action = "DROP NOT NULL" if new.nullable else "SET NOT NULL"
            self.execute(f"ALTER TABLE {qt} ALTER COLUMN {qc} {action}")
        if new_default is not None and default_changed:
            self.execute(f"ALTER TABLE {qt} ALTER COLUMN {qc} SET DEFAULT {new_default}")

        for child_ms, child_name, child in children:
            self._alter_type(child_ms.table, child_name, self.column_type(child, state_after))
            self._add_fk(child_ms.table, child_name, child, state_after)

        if new.unique and not old.unique and not new.primary_key:
            self.create_index(table, index_name("uq", table, [name]), (name,), True)
        if new_idx and not old_idx:
            self.create_index(table, index_name("idx", table, [name]), (name,), False)
        if new_fk and (fk_changed or not old_fk):
            self._add_fk(table, name, new, state_after)

    def _backfill_param(self, field, value, state):
        if isinstance(field, ForeignKey):
            return state.target_of(field).pk_field.to_db(value)
        return field.to_db(field.to_python(value))

    def rename_field(self, before, after, old, new, state) -> None:
        self.execute(f"ALTER TABLE {self.qn(after.table)} RENAME COLUMN {self.qn(old)} TO {self.qn(new)}")

    # ── introspection ──

    def column_names(self, table: str) -> set[str]:
        rows = self.db.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            [self.driver.stored_name(table)],
        )
        return {row[0] for row in rows}
