"""
DoveORM Database
Driver-agnostic façade for database operations.

Supports SQLite (default), MySQL and PostgreSQL via pluggable drivers.
Thread-safe: each thread gets its own connection (handled by the driver).
Includes optional query logging for debugging.  Bound parameter values
are *not* logged unless ``log_params=True`` — they routinely contain
passwords, tokens and personal data.

Usage – SQLite:
    db = Database("app.db")
    db = Database(":memory:")

Usage – MySQL:
    db = MySQLDatabase(host="localhost", user="app",
                       password="secret", database="myapp")

Usage – PostgreSQL:
    db = PostgreSQLDatabase(host="localhost", user="app",
                            password="secret", database="myapp")
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterable, Sequence

from .drivers.base import DatabaseDriver
from .exceptions import ConfigurationError
from .drivers.sqlite import SQLiteDriver
from .drivers.mysql import MySQLDriver
from .drivers.postgresql import PostgreSQLDriver

log = logging.getLogger("doveorm")


class Database:
    """
    Primary ORM entry-point.

    Wraps a :class:`DatabaseDriver` and exposes a unified API that
    the rest of the ORM (Model, QuerySet, Fields) programmes against.

    By default uses SQLite.  Pass a driver explicitly for MySQL / PostgreSQL::

        from doveorm.drivers import MySQLDriver, PostgreSQLDriver
        db = Database(driver=MySQLDriver(host="…", …))
        db = Database(driver=PostgreSQLDriver(host="…", …))
    """

    def __init__(
        self,
        path: str | None = None,
        *,
        driver: DatabaseDriver | None = None,
        log_queries: bool = False,
        log_params: bool = False,
    ):
        if driver is not None:
            if path is not None:
                raise ConfigurationError("Pass either a SQLite path or a driver, not both.")
            self._driver = driver
        elif path is None:
            raise ConfigurationError(
                "Database() needs a SQLite path (e.g. 'app.db', or ':memory:' explicitly) "
                "or driver=…."
            )
        else:
            # positional ``path`` → SQLite
            self._driver = SQLiteDriver(path)
        self.log_queries = log_queries
        self.log_params = log_params

    # ── expose driver info ──

    @property
    def driver(self) -> DatabaseDriver:
        return self._driver

    @property
    def driver_name(self) -> str:
        return self._driver.name

    @property
    def placeholder(self) -> str:
        return self._driver.placeholder

    @property
    def integrity_errors(self) -> tuple:
        return self._driver.integrity_errors

    # ── context manager ──

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False

    # ── connection lifecycle ──

    def connect(self):
        self._driver.connect()

    def disconnect(self, *, force: bool = False):
        """Close the current thread's connection.

        Refuses while a transaction is open unless ``force=True``.
        """
        self._driver.disconnect(force=force)

    def close_all(self):
        """Close every connection in every thread (call at shutdown)."""
        self._driver.close_all()

    # ── transactions ──

    @contextmanager
    def transaction(self):
        with self._driver.transaction() as conn:
            yield conn

    atomic = transaction

    def in_transaction(self) -> bool:
        return self._driver.in_transaction()

    def run_in_transaction(self, fn, *, retries: int = 3, backoff: float = 0.05,
                           max_backoff: float = 1.0):
        """Run ``fn()`` in a transaction, retrying deadlocks / lock timeouts.

        ``fn`` may run more than once, so it must not have side effects
        outside the database.  See :meth:`DatabaseDriver.run_in_transaction`.
        """
        return self._driver.run_in_transaction(
            fn, retries=retries, backoff=backoff, max_backoff=max_backoff)

    def is_transient_error(self, exc: BaseException) -> bool:
        return self._driver.is_transient_error(exc)

    # ── query execution ──

    def _log(self, sql: str, params, elapsed: float) -> None:
        if not self.log_queries:
            return
        if params is None:
            shown = "[]"
        elif self.log_params:
            shown = repr(list(params))
        else:
            shown = f"<{len(params)} redacted>"
        log.debug(
            "SQL [%s] (%.3fms): %s  params=%s",
            self.driver_name, elapsed * 1000, sql.strip(), shown,
        )

    def _timed(self, fn, sql, params):
        t0 = time.perf_counter()
        try:
            return fn()
        finally:
            self._log(sql, params, time.perf_counter() - t0)

    def execute(self, sql: str, params: Sequence | None = None) -> list:
        """Run a SELECT and return rows."""
        return self._timed(lambda: self._driver.execute(sql, params), sql, params)

    def execute_write(self, sql: str, params: Sequence | None = None) -> int:
        """Run INSERT/UPDATE/DELETE.  Returns lastrowid (INSERT) or rows matched."""
        return self._timed(lambda: self._driver.execute_write(sql, params), sql, params)

    def execute_many(self, sql: str, seq_of_params: Iterable[Sequence]) -> int:
        """Run one statement for many parameter sets, atomically."""
        rows = [list(p) for p in seq_of_params]
        t0 = time.perf_counter()
        try:
            return self._driver.execute_many(sql, rows)
        finally:
            if self.log_queries:
                log.debug("SQL [%s] (%.3fms): %s  x%d",
                          self.driver_name, (time.perf_counter() - t0) * 1000,
                          sql.strip(), len(rows))

    def execute_script(self, sql: str, *, atomic: bool = True):
        """Run a multi-statement script (CREATE TABLE …; CREATE INDEX …)."""
        return self._timed(lambda: self._driver.execute_script(sql, atomic=atomic), sql, None)

    def execute_ddl(self, sql: str):
        """Run exactly one DDL statement."""
        return self._timed(lambda: self._driver.execute_ddl(sql), sql, None)

    def create_index(self, table: str, columns: Sequence[str], *,
                     unique: bool = False, name: str | None = None) -> str:
        return self._driver.create_index(table, list(columns), unique=unique, name=name)

    def table_exists(self, table_name: str) -> bool:
        return self._driver.table_exists(table_name)

    def index_exists(self, table_name: str, index: str) -> bool:
        return self._driver.index_exists(table_name, index)

    def raw(self, sql: str, params: Sequence | None = None) -> list:
        """Run an arbitrary SQL query and return rows.

        Use this for complex queries (JOINs, sub-selects, UNIONs, etc.)
        that cannot be expressed through the QuerySet API.  Always pass
        user input through *params*, never by string formatting.
        """
        return self.execute(sql, params)

    def __repr__(self):
        connected = self._driver.is_connected()
        return f"<Database driver={self.driver_name!r} connected={connected}>"

    def __reduce_ex__(self, protocol):
        # Holds live connections and credentials: never serialise or copy.
        raise TypeError("Database objects cannot be pickled or copied.")


class MySQLDatabase(Database):
    """
    Convenience constructor for MySQL connections.

    Usage::

        db = MySQLDatabase(
            host="localhost",
            port=3306,
            user="app",
            password="secret",
            database="myapp",
        )
    """

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int = 3306,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
        charset: str = "utf8mb4",
        log_queries: bool = False,
        log_params: bool = False,
        **kwargs,
    ):
        driver = MySQLDriver(
            host=host, port=port, user=user,
            password=password, database=database,
            charset=charset, **kwargs,
        )
        super().__init__(driver=driver, log_queries=log_queries, log_params=log_params)


class PostgreSQLDatabase(Database):
    """
    Convenience constructor for PostgreSQL connections.

    Usage::

        db = PostgreSQLDatabase(
            host="localhost",
            port=5432,
            user="app",
            password="secret",
            database="myapp",
        )
    """

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int = 5432,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
        log_queries: bool = False,
        log_params: bool = False,
        **kwargs,
    ):
        driver = PostgreSQLDriver(
            host=host, port=port, user=user,
            password=password, database=database, **kwargs,
        )
        super().__init__(driver=driver, log_queries=log_queries, log_params=log_params)
