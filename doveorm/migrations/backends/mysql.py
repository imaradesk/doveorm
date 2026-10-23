"""MySQL / MariaDB schema editor.

MySQL commits every DDL statement implicitly, so a migration is not
atomic: if a statement fails half-way, earlier statements of that
migration stay applied (the history row is only written on success).
Keep one logical change per migration on MySQL.

Constraint and index names are looked up in ``information_schema`` when
they are not the ones the ORM would generate, so tables created by
``Model.create_table()`` (anonymous FK constraints, inline UNIQUE) can be
migrated too.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ...exceptions import MigrationError
from ...fields import ForeignKey
from ...utils.sql import index_name
from .base import NOT_PROVIDED, BaseSchemaEditor

if TYPE_CHECKING:
    from ..state import ModelState, ProjectState


class MySQLSchemaEditor(BaseSchemaEditor):

    def __init__(self, db, **kwargs):
        # DDL cannot be rolled back on MySQL; wrapping it in a transaction
        # would only hide the implicit commits.
        kwargs["atomic"] = False
        super().__init__(db, **kwargs)

    @property
    def _schema(self) -> str:
        return self.driver._database_name

    def check_column(self, ms, name, field, state) -> None:
        col_type = self.column_type(field, state)
        if (field.unique or field.db_index) and not field.primary_key \
                and not self.driver.allows_default_for(col_type):
            raise MigrationError(
                f"{ms.name}.{name}: MySQL cannot index a {col_type} column without a "
                "prefix length; use CharField instead."
            )

    # ── tables ──

    def create_model(self, ms: "ModelState", state: "ProjectState") -> None:
        cols, fks = [], []
        for name, field in ms.fields.items():
            self.check_column(ms, name, field, state)
            cols.append(f"{name} {self.column_sql(field, state)}")
            if isinstance(field, ForeignKey):
                fks.append(self.fk_clause(ms.table, name, field, state))
        body = ",\n    ".join(cols + fks)
        engine = ms.options["engine"] or "InnoDB"
        charset = ms.options["charset"] or "utf8mb4"
        self.execute(f"CREATE TABLE {ms.table} (\n    {body}\n) ENGINE={engine} DEFAULT CHARSET={charset}")
        for name, cols_, unique in self.index_specs(ms):
            self.create_index(ms.table, name, cols_, unique)

    def rename_table(self, old_table: str, new_table: str) -> None:
        self.execute(f"RENAME TABLE {old_table} TO {new_table}")

    def alter_table_options(self, before, after) -> None:
        if (before.options["engine"] or "InnoDB") != (after.options["engine"] or "InnoDB"):
            self.execute(f"ALTER TABLE {after.table} ENGINE={after.options['engine'] or 'InnoDB'}")
        if (before.options["charset"] or "utf8mb4") != (after.options["charset"] or "utf8mb4"):
            self.execute(f"ALTER TABLE {after.table} CONVERT TO CHARACTER SET "
                         f"{after.options['charset'] or 'utf8mb4'}")

    # ── indexes / constraints ──

    def drop_index(self, table, name, columns, unique) -> None:
        if not self.collect_sql and not self.db.index_exists(table, name):
            found = self._find_index(table, columns, unique)
            if found is None:
                return
            name = found
        self.execute(f"DROP INDEX {name} ON {table}")

    def _find_index(self, table, columns, unique) -> str | None:
        rows = self.db.execute(
            "SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND INDEX_NAME <> 'PRIMARY' "
            "ORDER BY INDEX_NAME, SEQ_IN_INDEX",
            [self._schema, table],
        )
        indexes: dict[str, tuple[bool, list[str]]] = {}
        for row in rows:
            entry = indexes.setdefault(row["INDEX_NAME"], (not int(row["NON_UNIQUE"]), []))
            entry[1].append(row["COLUMN_NAME"])
        for name, (is_unique, cols) in indexes.items():
            if is_unique == unique and tuple(cols) == tuple(columns):
                return name
        return None

    def _fk_names(self, table: str, column: str) -> list[str]:
        if self.collect_sql:
            return [self.fk_constraint_name(table, column)]
        rows = self.db.execute(
            "SELECT CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? AND COLUMN_NAME = ? "
            "AND REFERENCED_TABLE_NAME IS NOT NULL",
            [self._schema, table, column],
        )
        return [row["CONSTRAINT_NAME"] for row in rows]

    def _drop_fks(self, table: str, column: str) -> None:
        for name in self._fk_names(table, column):
            self.execute(f"ALTER TABLE {table} DROP FOREIGN KEY {name}")

    def _add_fk(self, table: str, name: str, field: ForeignKey, state) -> None:
        self.execute(f"ALTER TABLE {table} ADD {self.fk_clause(table, name, field, state)}")

    # ── columns ──

    def add_field(self, before, after, name, state, backfill=NOT_PROVIDED) -> None:
        field = after.fields[name]
        self.check_column(after, name, field, state)
        definition = self.column_sql(field, state)
        temp_default = backfill is not NOT_PROVIDED and self.default_literal(field, state) is None
        if temp_default:
            # Existing rows take the backfill through a temporary DEFAULT.
            definition += f" DEFAULT {self.backfill_literal(field, backfill, state)}"
        self.execute(f"ALTER TABLE {after.table} ADD COLUMN {name} {definition}")
        if temp_default:
            self.execute(f"ALTER TABLE {after.table} ALTER COLUMN {name} DROP DEFAULT")
        elif backfill is not NOT_PROVIDED:
            self.execute(f"UPDATE {after.table} SET {name} = "
                         f"{self.backfill_literal(field, backfill, state)}")
        if isinstance(field, ForeignKey):
            self._add_fk(after.table, name, field, state)
        if field.db_index:
            self.sync_indexes(before, after)

    def remove_field(self, before, after, name, state_before, state_after) -> None:
        if isinstance(before.fields[name], ForeignKey):
            self._drop_fks(before.table, name)
        # Indexes made only of this column disappear with it.
        self.execute(f"ALTER TABLE {before.table} DROP COLUMN {name}")

    def alter_field(self, before, after, name, state_before, state_after, backfill=NOT_PROVIDED) -> None:
        old, new = before.fields[name], after.fields[name]
        table = after.table
        if old.primary_key != new.primary_key:
            raise MigrationError(
                f"{after.name}.{name}: changing which field is the primary key is not "
                "supported by migrations; write a RunSQL operation."
            )
        self.check_column(after, name, new, state_after)
        old_fk = isinstance(old, ForeignKey)
        new_fk = isinstance(new, ForeignKey)
        fk_changed = old_fk and (not new_fk or (old.to, old.on_delete) != (new.to, new.on_delete))
        if fk_changed:
            self._drop_fks(table, name)
        if old.unique and not new.unique and not old.primary_key:
            self.drop_index(table, name, (name,), True)
        old_idx = old.db_index and not old.unique and not old.primary_key
        new_idx = new.db_index and not new.unique and not new.primary_key
        if old_idx and not new_idx:
            self.drop_index(table, index_name("idx", table, [name]), (name,), False)

        # A primary key whose type changes drags its foreign keys along.
        type_changed = self.column_type(old, state_before) != self.column_type(new, state_after)
        children = state_after.referencing_fields(after.name) if new.primary_key and type_changed else []
        for child_ms, child_name, _ in children:
            self._drop_fks(child_ms.table, child_name)

        if backfill is not NOT_PROVIDED:
            self.execute(f"UPDATE {table} SET {name} = ? WHERE {name} IS NULL",
                         [self._backfill_param(new, backfill, state_after)])
        self.execute(f"ALTER TABLE {table} MODIFY COLUMN {name} "
                     f"{self.column_sql(new, state_after, inline_unique=False, inline_primary_key=False)}")

        for child_ms, child_name, child in children:
            self.execute(f"ALTER TABLE {child_ms.table} MODIFY COLUMN {child_name} "
                         f"{self.column_sql(child, state_after, inline_unique=False)}")
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
        field = after.fields[new]
        self.execute(f"ALTER TABLE {after.table} CHANGE {old} {new} "
                     f"{self.column_sql(field, state, inline_unique=False, inline_primary_key=False)}")

    # ── introspection ──

    def column_names(self, table: str) -> set[str]:
        rows = self.db.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
            [self._schema, table],
        )
        return {row["COLUMN_NAME"] for row in rows}
