"""Escape hatches: raw SQL and Python code inside a migration."""
from __future__ import annotations

from ...exceptions import MigrationError
from ...utils.sql import split_statements
from .base import Operation


def _statements(sql, driver) -> list[str]:
    if isinstance(sql, str):
        return split_statements(sql, backslash_escapes=driver.backslash_escapes)
    if isinstance(sql, (list, tuple)) and all(isinstance(s, str) for s in sql):
        return [s.strip().rstrip(";") for s in sql if s.strip()]
    raise TypeError("RunSQL expects a SQL string or a list of SQL strings.")


class RunSQL(Operation):
    """Run raw SQL.

    ``reverse_sql`` makes it reversible; ``state_operations`` describe the
    schema change the SQL makes, so the migration state stays accurate::

        RunSQL("CREATE INDEX ix_name ON users (name(20))",
               reverse_sql="DROP INDEX ix_name ON users")

    Never build the SQL from user input.
    """
    barrier = True

    def __init__(self, sql, reverse_sql=None, state_operations=()):
        _statements(sql, _Dialect)              # type-check early
        if reverse_sql is not None:
            _statements(reverse_sql, _Dialect)
        self.sql = sql
        self.reverse_sql = reverse_sql
        self.state_operations = list(state_operations)

    @property
    def reversible(self):
        return self.reverse_sql is not None

    def deconstruct(self):
        out = {"sql": self.sql}
        if self.reverse_sql is not None:
            out["reverse_sql"] = self.reverse_sql
        if self.state_operations:
            out["state_operations"] = self.state_operations
        return out

    def state_forwards(self, state):
        for op in self.state_operations:
            op.state_forwards(state)

    def database_forwards(self, editor, before, after):
        for stmt in _statements(self.sql, editor.driver):
            editor.execute(stmt)

    def database_backwards(self, editor, before, after):
        if self.reverse_sql is None:
            raise MigrationError("RunSQL without reverse_sql is not reversible.")
        for stmt in _statements(self.reverse_sql, editor.driver):
            editor.execute(stmt)

    def describe(self):
        return "Raw SQL operation"


class RunPython(Operation):
    """Run a Python function — typically a data migration.

    ``code(db)`` receives the :class:`~doveorm.Database`.  Use raw SQL or the
    query API on tables as they are *at this point of the history*; avoid
    importing your current model classes, which may already have fields
    that this migration's schema does not.
    """
    barrier = True

    def __init__(self, code, reverse_code=None):
        if not callable(code):
            raise TypeError("RunPython code must be callable.")
        if reverse_code is not None and not callable(reverse_code):
            raise TypeError("RunPython reverse_code must be callable.")
        self.code = code
        self.reverse_code = reverse_code

    @staticmethod
    def noop(db):
        """Use as ``reverse_code`` when undoing needs no work."""

    @property
    def reversible(self):
        return self.reverse_code is not None

    def deconstruct(self):
        out = {"code": self.code}
        if self.reverse_code is not None:
            out["reverse_code"] = self.reverse_code
        return out

    def state_forwards(self, state):
        pass

    def database_forwards(self, editor, before, after):
        if editor.collect_sql:
            editor.collected.append(f"-- RunPython: {_name(self.code)} (not shown as SQL)")
            return
        self.code(editor.db)

    def database_backwards(self, editor, before, after):
        if self.reverse_code is None:
            raise MigrationError("RunPython without reverse_code is not reversible.")
        if editor.collect_sql:
            editor.collected.append(f"-- RunPython: {_name(self.reverse_code)} (not shown as SQL)")
            return
        self.reverse_code(editor.db)

    def describe(self):
        return f"Run Python {_name(self.code)}"


def _name(fn) -> str:
    return getattr(fn, "__qualname__", None) or repr(fn)


class _Dialect:
    backslash_escapes = False
