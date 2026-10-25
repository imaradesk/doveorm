"""The table that records which migrations have been applied."""
from __future__ import annotations

import datetime

from ..fields import AutoField, CharField, DateTimeField
from ..model import Model

TABLE = "doveorm_migrations"
LEGACY_TABLE = "migrations"         # before 0.6.0; renamed on the next write


class MigrationRecord(Model):
    """One row per applied migration (table ``migrations``)."""
    id         = AutoField()
    name       = CharField(max_length=200, nullable=False, unique=True)
    applied_at = DateTimeField(nullable=True, use_tz=True)

    class Meta:
        table_name = TABLE

    # Internal: never picked up by makemigrations.
    _doveorm_internal = True


class MigrationRecorder:
    def __init__(self, db):
        self.db = db

    def _legacy_table(self) -> bool:
        """True if the pre-0.6 history table ``migrations`` exists and is ours."""
        if not self.db.table_exists(LEGACY_TABLE):
            return False
        from .backends.base import editor_for
        with editor_for(self.db, atomic=False) as editor:
            return {"name", "applied_at"} <= editor.column_names(LEGACY_TABLE)

    def _table(self) -> str:
        """The table to read: the current one, else the legacy one (not yet renamed)."""
        if not self.db.table_exists(TABLE) and self._legacy_table():
            return LEGACY_TABLE
        return TABLE

    def has_table(self) -> bool:
        return self._table() == LEGACY_TABLE or self.db.table_exists(TABLE)

    def ensure_table(self) -> None:
        if not self.db.table_exists(TABLE) and self._legacy_table():
            from .backends.base import editor_for
            try:
                with editor_for(self.db, atomic=False) as editor:
                    editor.rename_table(LEGACY_TABLE, TABLE)
            except Exception:
                if not self.db.table_exists(TABLE):     # lost a rename race: fine
                    raise
            return
        if self.has_table():
            return
        from .backends.base import editor_for
        from .state import ModelState, ProjectState
        ms = ModelState.from_model(MigrationRecord)
        try:
            with editor_for(self.db, atomic=False) as editor:
                editor.create_model(ms, ProjectState([ms]))
        except Exception:
            if not self.has_table():      # lost a creation race: fine
                raise

    def applied(self) -> list[str]:
        """Applied migration names, oldest first."""
        if not self.has_table():
            return []
        return [row[0] for row in self.db.execute(f"SELECT name FROM {self._table()} ORDER BY id")]

    def record_applied(self, name: str) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        self.db.execute_write(
            f"INSERT INTO {self._table()} (name, applied_at) VALUES (?, ?)",
            [name, MigrationRecord._fields["applied_at"].to_db(now)],
        )

    def record_unapplied(self, name: str) -> None:
        self.db.execute_write(f"DELETE FROM {self._table()} WHERE name = ?", [name])
