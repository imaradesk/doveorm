"""
Legacy ``Migrator``: register Python functions as ordered migration steps.

Kept for projects that already use it.  New code should use migration
files (``makemigrations`` / ``migrate``), which share the same history
table, so both styles see each other's applied rows.

Each step runs inside a transaction together with the insert of its
history record, so on SQLite a failing step leaves neither schema changes
nor a history row behind.  (MySQL commits DDL implicitly, so a failure
part-way through a multi-statement step can leave partial schema
changes; the history row is still only written on success.)
"""
from __future__ import annotations

import datetime
import logging
from typing import Callable

from ..exceptions import MigrationError
from .recorder import MigrationRecord

log = logging.getLogger("doveorm.migrations")

# Backwards-compatible names for the history model
DoveORMMigration = MigrationRecord
Migration = MigrationRecord


class Migrator:
    """
    Run ordered migration functions and track them.

    Usage:
        m = Migrator(db)
        m.register("0001_create_users",
                    lambda: User.create_table(),
                    rollback_fn=lambda: User.drop_table())
        m.migrate()
    """

    def __init__(self, db):
        self.db = db
        self._steps: list[tuple[str, Callable, Callable | None]] = []
        # Bootstrap the migrations table itself
        self._bind()
        Migration.create_table()

    def _bind(self) -> None:
        # The history model is shared; bind it to this migrator's database
        # before every operation so several Migrators can coexist.
        Migration.use(self.db)

    def register(self, name: str, fn: Callable, rollback_fn: Callable | None = None):
        """Add a migration step.  Optionally provide a rollback function."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Migration name must be a non-empty string.")
        if len(name) > Migration._fields["name"].max_length:
            raise ValueError(f"Migration name {name!r} is too long.")
        if not callable(fn):
            raise TypeError(f"Migration {name!r}: fn must be callable.")
        if rollback_fn is not None and not callable(rollback_fn):
            raise TypeError(f"Migration {name!r}: rollback_fn must be callable.")
        if any(existing == name for existing, _fn, _rb in self._steps):
            raise ValueError(f"Migration {name!r} is already registered.")
        self._steps.append((name, fn, rollback_fn))

    def applied(self) -> list[str]:
        """Names of applied migrations, oldest first."""
        self._bind()
        return [m.name for m in Migration.all().order_by("id").get()]

    def pending(self) -> list[str]:
        """Names of registered migrations that have not been applied."""
        done = set(self.applied())
        return [name for name, _fn, _rb in self._steps if name not in done]

    def migrate(self) -> list[str]:
        """Apply all pending migrations in registration order.  Returns their names."""
        self._bind()
        done = set(self.applied())
        ran: list[str] = []
        for name, fn, _rollback_fn in self._steps:
            if name in done:
                continue
            log.info("Running migration: %s", name)
            try:
                with self.db.transaction():
                    fn()
                    self._bind()
                    Migration.create(
                        name=name,
                        applied_at=datetime.datetime.now(datetime.timezone.utc),
                    )
            except Exception as exc:
                raise MigrationError(f"Migration {name!r} failed: {exc}") from exc
            ran.append(name)
        log.info("Migrations complete (%d applied).", len(ran))
        return ran

    def rollback(self, steps: int = 1, *, force: bool = False) -> list[str]:
        """Roll back the last *steps* migrations (newest first).

        Every migration being rolled back must have a ``rollback_fn``;
        otherwise nothing is done and :class:`MigrationError` is raised.
        Pass ``force=True`` to delete history rows that have no rollback
        function (the schema is then left unchanged).
        """
        if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
            raise ValueError("steps must be a positive integer.")
        self._bind()
        applied = Migration.all().order_by("-id").limit(steps).get()
        # Build a lookup of rollback functions by migration name
        rollback_map: dict[str, Callable | None] = {
            name: rb for name, _fn, rb in self._steps
        }
        missing = [m.name for m in applied if rollback_map.get(m.name) is None]
        if missing and not force:
            raise MigrationError(
                "No rollback function for: " + ", ".join(missing)
                + ". Pass force=True to remove the history rows without reverting the schema."
            )

        rolled: list[str] = []
        for m in applied:
            rb_fn = rollback_map.get(m.name)
            try:
                with self.db.transaction():
                    if rb_fn is not None:
                        log.info("Rolling back: %s", m.name)
                        rb_fn()
                    else:
                        log.warning("No rollback function for %s — history row removed, schema unchanged.", m.name)
                    self._bind()
                    m.delete()
            except Exception as exc:
                raise MigrationError(f"Rollback of {m.name!r} failed: {exc}") from exc
            rolled.append(m.name)
        return rolled
