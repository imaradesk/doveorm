"""SQLite schema editor.

SQLite's ``ALTER TABLE`` can only add and rename columns, so other changes
use the documented *table remake* procedure: create the new table, copy
the rows, drop the old table, rename the new one and recreate its indexes,
all inside the migration's transaction.

Foreign-key enforcement is switched off for the duration of the migration
(it can only be changed outside a transaction) because dropping the old
table would otherwise fire ``ON DELETE CASCADE`` on child rows.  Before
committing, ``PRAGMA foreign_key_check`` verifies every table that was
touched, so a migration can never leave dangling references behind.
"""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from ...exceptions import MigrationError
from ...fields import ForeignKey
from .base import NOT_PROVIDED, BaseSchemaEditor

if TYPE_CHECKING:
    from ..state import ModelState, ProjectState

_RENAME_COLUMN = sqlite3.sqlite_version_info >= (3, 25, 0)


class SQLiteSchemaEditor(BaseSchemaEditor):

    def __init__(self, db, **kwargs):
        super().__init__(db, **kwargs)
        self._restore_fk = None
        self._touched: set[str] = set()

    # ── context: FK enforcement off, checked before commit ──

    def __enter__(self):
        if not self.collect_sql and not self.db.in_transaction():
            enabled = self.db.execute("PRAGMA foreign_keys")[0][0]
            if enabled:
                self.db.execute_ddl("PRAGMA foreign_keys = OFF")
                self._restore_fk = True
        try:
            return super().__enter__()
        except BaseException:
            self._reenable()
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None and self._restore_fk:
                self._check_foreign_keys()
        except BaseException as check_exc:
            super().__exit__(type(check_exc), check_exc, check_exc.__traceback__)
            self._reenable()
            raise
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self._reenable()

    def _reenable(self):
        if self._restore_fk:
            self._restore_fk = None
            self.db.execute_ddl("PRAGMA foreign_keys = ON")

    def _check_foreign_keys(self):
        for table in sorted(self._touched):
            if not self.db.table_exists(table):
                continue
            rows = self.db.execute(f"PRAGMA foreign_key_check({table})")
            if rows:
                raise MigrationError(
                    f"Migration would leave {len(rows)} row(s) in {table!r} with a broken "
                    f"foreign key (first: references {rows[0][2]!r}); nothing was applied."
                )

    def _fk_guard(self):
        """Remaking a table with FK enforcement on would cascade-delete child rows."""
        if self.collect_sql or self._restore_fk:
            return
        if self.db.execute("PRAGMA foreign_keys")[0][0]:
            raise MigrationError(
                "This SQLite migration rebuilds a table and must run outside an open "
                "transaction (foreign key enforcement cannot be suspended inside one)."
            )

    # ── DDL ──

    def create_model(self, ms: "ModelState", state: "ProjectState", *, table: str | None = None,
                     with_indexes: bool = True) -> None:
        table = table or ms.table
        cols = [f"{name} {self.column_sql(field, state)}" for name, field in ms.fields.items()]
        fks = [self.fk_clause(table, name, field, state)
               for name, field in ms.fields.items() if isinstance(field, ForeignKey)]
        body = ",\n    ".join(cols + fks)
        self.execute(f"CREATE TABLE {table} (\n    {body}\n)")
        self._touched.add(ms.table)
        if with_indexes:
            for name, cols_, unique in self.index_specs(ms):
                self.create_index(ms.table, name, cols_, unique)

    def delete_model(self, ms, state) -> None:
        self._fk_guard()
        super().delete_model(ms, state)
        for other, _name, _field in state.referencing_fields(ms.name):
            if other.name != ms.name:
                self._touched.add(other.table)

    def rename_table(self, old_table: str, new_table: str) -> None:
        self.execute(f"ALTER TABLE {old_table} RENAME TO {new_table}")
        self._touched.add(new_table)

    def create_index(self, table, name, columns, unique) -> None:
        kind = "UNIQUE INDEX" if unique else "INDEX"
        self.execute(f"CREATE {kind} IF NOT EXISTS {name} ON {table} ({', '.join(columns)})")

    def drop_index(self, table, name, columns, unique) -> None:
        if not self.collect_sql and not self.db.index_exists(table, name):
            found = self._find_index(table, columns, unique)
            if found is None:
                return
            name = found
        self.execute(f"DROP INDEX IF EXISTS {name}")

    def _find_index(self, table, columns, unique) -> str | None:
        for row in self.db.execute(f"PRAGMA index_list({table})"):
            if row["origin"] != "c" or bool(row["unique"]) != unique:
                continue
            cols = tuple(r["name"] for r in self.db.execute(f"PRAGMA index_info({row['name']})"))
            if cols == tuple(columns):
                return row["name"]
        return None

    def add_field(self, before, after, name, state, backfill=NOT_PROVIDED) -> None:
        field = after.fields[name]
        default = self.default_literal(field, state)
        is_fk = isinstance(field, ForeignKey)
        # ADD COLUMN cannot add PRIMARY KEY / UNIQUE columns, NOT NULL columns
        # without a default, or a REFERENCES column whose default is not NULL.
        needs_remake = (field.primary_key or field.unique
                        or (not field.nullable and default is None)
                        or (is_fk and (default is not None or not field.nullable)))
        if needs_remake:
            fill = {}
            if backfill is not NOT_PROVIDED:
                fill[name] = self.backfill_literal(field, backfill, state)
            self._remake(before, after, state, fill=fill)
            return
        definition = self.column_sql(field, state)
        if is_fk:
            target = state.target_of(field)
            definition += f" REFERENCES {target.table}({target.pk_name}) ON DELETE {field.on_delete}"
        self.execute(f"ALTER TABLE {after.table} ADD COLUMN {name} {definition}")
        if backfill is not NOT_PROVIDED:
            self.execute(f"UPDATE {after.table} SET {name} = "
                         f"{self.backfill_literal(field, backfill, state)}")
        self._touched.add(after.table)
        if field.db_index:
            self.sync_indexes(before, after)

    def remove_field(self, before, after, name, state_before, state_after) -> None:
        self._remake(before, after, state_after)

    def alter_field(self, before, after, name, state_before, state_after, backfill=NOT_PROVIDED) -> None:
        old, new = before.fields[name], after.fields[name]
        fill = {}
        if backfill is not NOT_PROVIDED:
            fill[name] = f"COALESCE({name}, {self.backfill_literal(new, backfill, state_after)})"
        if (not fill and self.column_type(old, state_before) == self.column_type(new, state_after)
                and self._same_except_index(old, new)):
            self.sync_indexes(before, after)     # only db_index changed
            return
        self._remake(before, after, state_after, fill=fill)

    @staticmethod
    def _same_except_index(old, new) -> bool:
        a, b = old.deconstruct(), new.deconstruct()
        strip = lambda kw: {k: v for k, v in kw.items() if k != "db_index"}  # noqa: E731
        return a[0] is b[0] and strip(a[1]) == strip(b[1])

    def rename_field(self, before, after, old, new, state) -> None:
        if _RENAME_COLUMN:
            self.execute(f"ALTER TABLE {after.table} RENAME COLUMN {old} TO {new}")
            self._touched.add(after.table)
        else:  # pragma: no cover - SQLite < 3.25
            self._remake(before, after, state, renames={new: old})

    # ── table remake ──

    def _remake(self, before: "ModelState", after: "ModelState", state: "ProjectState", *,
                fill: dict | None = None, renames: dict | None = None) -> None:
        """Rebuild *before*'s table with *after*'s definition, keeping the rows.

        ``fill`` maps a column to the SQL expression used to populate it;
        ``renames`` maps new column names to old ones.
        """
        self._fk_guard()
        fill = fill or {}
        renames = renames or {}
        tmp = f"doveorm_new_{after.table}"[:64]
        self.create_model(after, state, table=tmp, with_indexes=False)
        targets, sources = [], []
        for name, field in after.fields.items():
            if name in fill:
                expr = fill[name]
            elif renames.get(name, name) in before.fields:
                expr = renames.get(name, name)
            else:
                expr = self.default_literal(field, state)
                if expr is None:
                    if not field.nullable and not field.primary_key:
                        raise MigrationError(
                            f"{after.name}.{name} is NOT NULL without a default; "
                            "existing rows need a backfill value."
                        )
                    continue
            targets.append(name)
            sources.append(expr)
        self.execute(f"INSERT INTO {tmp} ({', '.join(targets)}) "
                     f"SELECT {', '.join(sources)} FROM {before.table}")
        self.execute(f"DROP TABLE {before.table}")
        self.execute(f"ALTER TABLE {tmp} RENAME TO {after.table}")
        for name, cols, unique in self.index_specs(after):
            self.create_index(after.table, name, cols, unique)
        self._touched.add(after.table)

    # ── introspection ──

    def column_names(self, table: str) -> set[str]:
        return {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
