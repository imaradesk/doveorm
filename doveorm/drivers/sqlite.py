"""
DoveORM – SQLite driver.

Wraps the stdlib ``sqlite3`` module.  This is the default backend.

* Connections run in autocommit mode (``isolation_level=None``); the
  driver issues ``BEGIN`` / ``COMMIT`` itself inside :meth:`transaction`,
  so transaction boundaries are exactly the ones you write.
* ``":memory:"`` databases are opened as a *named shared-cache* in-memory
  database, so every thread sees the same data.  An internal anchor
  connection keeps it alive until :meth:`close_all` is called.  In-memory
  databases are not fork-safe; use a file database with multiple processes.

Hardening defaults
------------------
* New database files are created with mode ``0600`` (``file_mode``).
* ``PRAGMA foreign_keys = ON`` and ``PRAGMA trusted_schema = OFF`` (schema
  objects such as views/triggers cannot call risky SQL functions).
* ``statement_timeout`` (seconds, optional) interrupts runaway statements
  and raises :class:`~doveorm.exceptions.QueryTimeout`.
* "database is locked" errors are classified as transient, so
  :meth:`run_in_transaction` retries them.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Iterable, Sequence

from ..exceptions import QueryTimeout
from ..utils.sql import index_name, split_statements, validate_identifier
from .base import DatabaseDriver

_PRAGMA_VALUE_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_BEGIN_MODES = ("DEFERRED", "IMMEDIATE", "EXCLUSIVE")
_PROGRESS_INTERVAL = 1000          # SQLite VM instructions between timeout checks
_TRANSIENT_MESSAGES = ("database is locked", "database table is locked", "database is busy")


class SQLiteDriver(DatabaseDriver):
    name = "sqlite"
    placeholder = "?"
    autoincrement_sql = "AUTOINCREMENT"
    max_limit = "-1"

    def __init__(
        self,
        path: str,
        *,
        timeout: float = 5.0,
        pragmas: dict | None = None,
        begin_mode: str = "DEFERRED",
        statement_timeout: float | None = None,
        file_mode: int | None = 0o600,
        max_lifetime: float | None = None,
    ):
        super().__init__(max_lifetime=max_lifetime)
        if not isinstance(path, str):
            raise TypeError("SQLite path must be a string.")
        if "\x00" in path:
            raise ValueError("SQLite path must not contain NUL bytes.")
        self.path = path
        self.timeout = float(timeout)
        if self.timeout < 0:
            raise ValueError("timeout must be >= 0.")
        begin_mode = str(begin_mode).upper()
        if begin_mode not in _BEGIN_MODES:
            raise ValueError(f"begin_mode must be one of {_BEGIN_MODES}, got {begin_mode!r}.")
        self.begin_mode = begin_mode
        if statement_timeout is not None:
            if isinstance(statement_timeout, bool) or float(statement_timeout) <= 0:
                raise ValueError("statement_timeout must be a positive number of seconds or None.")
            statement_timeout = float(statement_timeout)
        self.statement_timeout = statement_timeout
        if file_mode is not None and (isinstance(file_mode, bool) or not isinstance(file_mode, int)
                                      or not 0 <= file_mode <= 0o777):
            raise ValueError("file_mode must be an int permission mask (e.g. 0o600) or None.")
        self.file_mode = file_mode

        self._memory = path in (":memory:", "")
        if self._memory:
            self._target = f"file:doveorm-mem-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
        else:
            self._target = path
            self._uri = path.startswith("file:")

        base_pragmas: dict = {"foreign_keys": "ON", "trusted_schema": "OFF"}
        if not self._memory:
            base_pragmas["journal_mode"] = "WAL"
        base_pragmas.update(pragmas or {})
        for key, value in base_pragmas.items():
            validate_identifier(key, "PRAGMA name")
            if not _PRAGMA_VALUE_RE.match(str(value)):
                raise ValueError(f"Invalid PRAGMA value for {key}: {value!r}")
        self._pragmas = base_pragmas

        self._anchor: sqlite3.Connection | None = None
        self._anchor_lock = threading.Lock()

    def __repr__(self):
        target = ":memory:" if self._memory else self.path
        return f"<SQLiteDriver path={target!r} connected={self.is_connected()}>"

    # ── connection hooks ──

    def _ensure_file(self) -> None:
        """Create a new database file with restrictive permissions."""
        if self.file_mode is None or self._memory or self._uri:
            return
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, self.file_mode)
        except FileExistsError:
            return
        os.close(fd)

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._target,
            uri=self._uri,
            timeout=self.timeout,
            isolation_level=None,          # autocommit; transactions are explicit
            check_same_thread=False,       # only so close_all() can close it
        )
        conn.row_factory = sqlite3.Row
        for key, value in self._pragmas.items():
            conn.execute(f"PRAGMA {key} = {value}")
        if self.statement_timeout is not None:
            conn.set_progress_handler(self._deadline_passed, _PROGRESS_INTERVAL)
        return conn

    def _open(self) -> sqlite3.Connection:
        if self._memory:
            with self._anchor_lock:
                if self._anchor is None:
                    self._anchor = self._new_connection()
        else:
            self._ensure_file()
        return self._new_connection()

    def close_all(self) -> None:
        """Close all connections.  For ``:memory:`` this discards the database."""
        super().close_all()
        with self._anchor_lock:
            if self._anchor is not None:
                self._anchor.close()
                self._anchor = None

    def _exec_control(self, conn, sql: str) -> None:
        conn.execute(sql)

    def _begin(self, conn) -> None:
        conn.execute(f"BEGIN {self.begin_mode}")

    def _commit(self, conn) -> None:
        if conn.in_transaction:
            conn.execute("COMMIT")

    def _rollback(self, conn) -> None:
        if conn.in_transaction:
            conn.execute("ROLLBACK")

    @property
    def integrity_errors(self):
        return (sqlite3.IntegrityError,)

    def is_transient_error(self, exc: BaseException) -> bool:
        if isinstance(exc, sqlite3.OperationalError):
            msg = str(exc).lower()
            return any(m in msg for m in _TRANSIENT_MESSAGES)
        return False

    # ── statement timeout ──

    def _deadline_passed(self) -> int:
        deadline = getattr(self._local, "deadline", None)
        return 1 if deadline is not None and time.monotonic() > deadline else 0

    @contextmanager
    def _deadline(self):
        if self.statement_timeout is None:
            yield
            return
        previous = getattr(self._local, "deadline", None)
        self._local.deadline = time.monotonic() + self.statement_timeout
        try:
            yield
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise QueryTimeout(
                    f"SQLite statement exceeded statement_timeout={self.statement_timeout}s."
                ) from exc
            raise
        finally:
            self._local.deadline = previous

    # ── query execution ──

    def execute(self, sql: str, params: Sequence | None = None) -> list:
        self.connect()
        with self._deadline():
            cur = self._conn.execute(sql, self._params(params))
            try:
                return cur.fetchall()           # sqlite3.Row objects
            finally:
                cur.close()

    def execute_write(self, sql: str, params: Sequence | None = None) -> int:
        self.connect()
        with self._deadline():
            cur = self._conn.execute(sql, self._params(params))
            try:
                if self._is_insert(sql):
                    return cur.lastrowid
                return cur.rowcount
            finally:
                cur.close()

    def execute_many(self, sql: str, seq_of_params: Iterable[Sequence]) -> int:
        rows = [self._params(p) for p in seq_of_params]
        if not rows:
            return 0
        with self.transaction() as conn, self._deadline():
            cur = conn.executemany(sql, rows)
            try:
                return cur.rowcount
            finally:
                cur.close()

    def _execute_statement(self, sql: str) -> None:
        self.connect()
        with self._deadline():
            self._conn.execute(sql).close()

    def _split_script(self, sql: str) -> list[str]:
        # Re-join pieces until SQLite agrees the statement is complete, so
        # CREATE TRIGGER … BEGIN …; …; END is kept as one statement.
        out: list[str] = []
        buf = ""
        for piece in split_statements(sql):
            buf = f"{buf};\n{piece}" if buf else piece
            if sqlite3.complete_statement(buf + ";"):
                out.append(buf)
                buf = ""
        if buf:
            out.append(buf)
        return out

    # ── introspection ──

    def table_exists(self, table_name: str) -> bool:
        rows = self.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            [table_name],
        )
        return len(rows) > 0

    def index_exists(self, table_name: str, index: str) -> bool:
        rows = self.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND name=?",
            [table_name, index],
        )
        return len(rows) > 0

    def create_index(self, table, columns, *, unique=False, name=None) -> str:
        validate_identifier(table, "table name")
        for col in columns:
            validate_identifier(col, "index column")
        if name is None:
            name = index_name("uq" if unique else "idx", table, columns)
        validate_identifier(name, "index name")
        kind = "UNIQUE INDEX" if unique else "INDEX"
        qn = self.quote_name
        self._execute_statement(
            f"CREATE {kind} IF NOT EXISTS {qn(name)} ON {qn(table)} ({', '.join(qn(c) for c in columns)})"
        )
        return name

    # ── dialect overrides ──

    def insert_ignore_prefix(self) -> str:
        return "INSERT OR IGNORE"

    def foreign_key_pragma(self) -> str | None:
        return "PRAGMA foreign_keys = ON"

    def sql_type_map(self) -> dict[str, str]:
        return {
            "INTEGER":  "INTEGER",
            "BIGINT":   "INTEGER",
            "SMALLINT": "INTEGER",
            "REAL":     "REAL",
            "DECIMAL":  "TEXT",
            "TEXT":     "TEXT",
            "VARCHAR":  "TEXT",
            "BOOLEAN":  "INTEGER",
            "DATETIME": "TEXT",
            "DATE":     "TEXT",
            "TIME":     "TEXT",
            "JSON":     "TEXT",
            "UUID":     "TEXT",
            "BLOB":     "BLOB",
        }

    def autoincrement_definition(self) -> str:
        return "PRIMARY KEY AUTOINCREMENT"
