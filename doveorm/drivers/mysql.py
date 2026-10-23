"""
DoveORM – MySQL driver.

Uses ``mysqlclient`` (C-based, fast MySQLdb interface).

Install:
    pip install mysqlclient

Usage:
    from doveorm import MySQLDatabase

    db = MySQLDatabase(
        host="localhost",
        port=3306,
        user="app",
        password="secret",
        database="myapp",
    )

Behaviour
---------
* Connections run with ``autocommit=True`` so plain reads always see the
  latest committed data (no long-lived REPEATABLE READ snapshot).
  :meth:`transaction` issues ``START TRANSACTION`` / ``COMMIT`` explicitly.
* ``CLIENT.FOUND_ROWS`` is enabled so UPDATE returns the number of rows
  *matched*, consistent with SQLite.
* A dropped connection (error 2006 / 2013) is re-established and the
  statement retried once — only outside a transaction, and writes are only
  retried for 2006 (statement never reached the server).
* DDL statements cause an implicit commit in MySQL; they are not
  transactional.

Hardening defaults
------------------
* Every session runs with a strict ``sql_mode`` (:data:`DEFAULT_SQL_MODE`),
  so out-of-range / truncated / invalid values raise errors instead of
  being silently altered.  Pass ``sql_mode=None`` to keep the server's.
* ``isolation_level`` (optional) is validated against the four SQL levels.
* A warning is logged for remote connections without TLS (``ssl`` /
  ``ssl_mode`` options) and for empty passwords.
* Deadlocks (1213), lock-wait timeouts (1205) and "server has gone away"
  (2006) are transient, so :meth:`run_in_transaction` retries them.
* ``repr()`` never includes the password.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, Sequence

from ..exceptions import ConfigurationError
from ..utils.sql import validate_identifier
from .base import DatabaseDriver, _DictRow

log = logging.getLogger("doveorm")

_SERVER_GONE = 2006        # CR_SERVER_GONE_ERROR  – query never sent
_SERVER_LOST = 2013        # CR_SERVER_LOST        – lost during query
_RETRY_READ = (_SERVER_GONE, _SERVER_LOST)
_RETRY_WRITE = (_SERVER_GONE,)
_LOCK_WAIT_TIMEOUT = 1205
_DEADLOCK = 1213
_TRANSIENT = (_DEADLOCK, _LOCK_WAIT_TIMEOUT, _SERVER_GONE)

DEFAULT_SQL_MODE = (
    "STRICT_ALL_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
    "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION"
)
_SQL_MODE_RE = re.compile(r"^([A-Z_]+(,[A-Z_]+)*)?$")
ISOLATION_LEVELS = ("READ UNCOMMITTED", "READ COMMITTED", "REPEATABLE READ", "SERIALIZABLE")
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")

_NO_DEFAULT_TYPES = (
    "TINYTEXT", "TEXT", "MEDIUMTEXT", "LONGTEXT",
    "TINYBLOB", "BLOB", "MEDIUMBLOB", "LONGBLOB",
    "JSON", "GEOMETRY",
)


class MySQLDriver(DatabaseDriver):
    name = "mysql"
    placeholder = "%s"
    autoincrement_sql = "AUTO_INCREMENT"
    backslash_escapes = True
    supports_transactional_ddl = False
    identifier_quote = "`"
    max_limit = "18446744073709551615"

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int = 3306,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
        charset: str = "utf8mb4",
        autoreconnect: bool = True,
        sql_mode: str | None = DEFAULT_SQL_MODE,
        isolation_level: str | None = None,
        max_lifetime: float | None = None,
        **extra_kwargs,
    ):
        super().__init__(max_lifetime=max_lifetime)
        # Where the data lives is the application's decision: never guess it.
        missing = [label for label, value in (("database", database), ("user", user)) if not value]
        if password is None:
            missing.append("password (use '' explicitly for passwordless auth)")
        if not host and "unix_socket" not in extra_kwargs:
            missing.append("host (or unix_socket)")
        if missing:
            raise ConfigurationError("MySQL driver is missing: " + "; ".join(missing) + ".")
        validate_identifier(charset, "MySQL charset")
        if sql_mode is not None:
            sql_mode = str(sql_mode).replace(" ", "").upper()
            if not _SQL_MODE_RE.match(sql_mode):
                raise ValueError(f"Invalid sql_mode: {sql_mode!r}")
            # Literal quoting must match how the server parses backslashes.
            self.backslash_escapes = "NO_BACKSLASH_ESCAPES" not in sql_mode.split(",")
        if isolation_level is not None:
            isolation_level = " ".join(str(isolation_level).upper().replace("_", " ").split())
            if isolation_level not in ISOLATION_LEVELS:
                raise ValueError(f"isolation_level must be one of {ISOLATION_LEVELS}, got {isolation_level!r}.")
        self.sql_mode = sql_mode
        self.isolation_level = isolation_level

        extra_kwargs.setdefault("connect_timeout", 10)
        # mysqlclient uses 'passwd' and 'db' instead of 'password' and 'database'
        self._cfg = dict(
            port=int(port),
            user=user,
            passwd=password,
            db=database,
            charset=charset,
            **extra_kwargs,
        )
        if host:
            self._cfg["host"] = host
        # Keep the logical names for introspection (table_exists, etc.)
        self._database_name = database
        location = f"{host}:{int(port)}" if host else f"socket:{extra_kwargs['unix_socket']}"
        self._display = f"{user}@{location}/{database}"
        self.autoreconnect = autoreconnect
        if not password:
            log.warning("MySQL driver configured for user %r with an empty password.", user)
        is_local = host in _LOCAL_HOSTS or "unix_socket" in extra_kwargs
        if not is_local and not ({"ssl", "ssl_mode"} & set(extra_kwargs)):
            log.warning("MySQL connection to %s is not configured for TLS (pass ssl= or ssl_mode=).", host)

    def __repr__(self):
        return f"<MySQLDriver {self._display} connected={self.is_connected()}>"

    # ── connection hooks ──

    def _open(self):
        import MySQLdb
        from MySQLdb.constants import CLIENT

        cfg = dict(self._cfg)
        cfg["client_flag"] = int(cfg.get("client_flag", 0)) | CLIENT.FOUND_ROWS
        conn = MySQLdb.connect(**cfg)
        try:
            conn.autocommit(True)
            cur = conn.cursor()
            try:
                if self.sql_mode is not None:
                    cur.execute("SET SESSION sql_mode = %s", (self.sql_mode,))
                if self.isolation_level is not None:
                    # value comes from the ISOLATION_LEVELS whitelist
                    cur.execute(f"SET SESSION TRANSACTION ISOLATION LEVEL {self.isolation_level}")
            finally:
                cur.close()
        except BaseException:
            conn.close()
            raise
        return conn

    def is_connected(self) -> bool:
        if not super().is_connected():
            return False
        conn = self._conn
        try:
            conn.ping()
            return True
        except Exception:
            return False

    def _exec_control(self, conn, sql: str) -> None:
        cur = conn.cursor()
        try:
            cur.execute(sql)
        finally:
            cur.close()

    def _begin(self, conn) -> None:
        self._exec_control(conn, "START TRANSACTION")

    def _commit(self, conn) -> None:
        conn.commit()

    def _rollback(self, conn) -> None:
        conn.rollback()

    @property
    def integrity_errors(self):
        import MySQLdb
        return (MySQLdb.IntegrityError,)

    def is_transient_error(self, exc: BaseException) -> bool:
        return self._error_code(exc) in _TRANSIENT

    def _is_disconnect_error(self, exc: BaseException) -> bool:
        return self._error_code(exc) in _RETRY_READ

    # ── execution helpers ──

    @staticmethod
    def _error_code(exc: BaseException) -> int | None:
        try:
            import MySQLdb
        except ImportError:  # pragma: no cover
            return None
        error_base = getattr(MySQLdb, "Error", MySQLdb.OperationalError)
        if isinstance(exc, error_base) and exc.args and isinstance(exc.args[0], int):
            return exc.args[0]
        return None

    def _run(self, fn, retry_codes: tuple[int, ...]):
        self.connect()
        try:
            return fn(self._conn)
        except Exception as exc:
            if not (self.autoreconnect and self._nesting == 0
                    and self._error_code(exc) in retry_codes):
                raise
            log.warning("MySQL connection lost (%s); reconnecting and retrying once.", exc)
            self.disconnect()
            self.connect()
            return fn(self._conn)

    def _prepare(self, sql: str, params: Sequence | None):
        params = self._params(params)
        if params:
            return self.adapt_sql(sql), params
        return sql, None          # None → mysqlclient does not %-format the query

    # ── query execution ──

    def execute(self, sql: str, params: Sequence | None = None) -> list:
        query, args = self._prepare(sql, params)

        def run(conn):
            cur = conn.cursor()
            try:
                cur.execute(query, args)
                if cur.description is None:
                    return []
                col_names = [desc[0] for desc in cur.description]
                return [_DictRow(col_names, row) for row in cur.fetchall()]
            finally:
                cur.close()

        return self._run(run, _RETRY_READ)

    def execute_write(self, sql: str, params: Sequence | None = None) -> int:
        query, args = self._prepare(sql, params)
        is_insert = self._is_insert(sql)

        def run(conn):
            cur = conn.cursor()
            try:
                cur.execute(query, args)
                return cur.lastrowid if is_insert else cur.rowcount
            finally:
                cur.close()

        return self._run(run, _RETRY_WRITE)

    def execute_many(self, sql: str, seq_of_params: Iterable[Sequence]) -> int:
        rows = [self._params(p) for p in seq_of_params]
        if not rows:
            return 0
        query = self.adapt_sql(sql)
        with self.transaction() as conn:
            cur = conn.cursor()
            try:
                cur.executemany(query, rows)
                return cur.rowcount
            finally:
                cur.close()

    def _execute_statement(self, sql: str) -> None:
        def run(conn):
            cur = conn.cursor()
            try:
                cur.execute(sql)
            finally:
                cur.close()

        self._run(run, _RETRY_WRITE)

    # ── introspection ──

    def table_exists(self, table_name: str) -> bool:
        if self._database_name:
            rows = self.execute(
                "SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
                [self._database_name, table_name],
            )
        else:
            rows = self.execute(
                "SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ?",
                [table_name],
            )
        return len(rows) > 0

    def index_exists(self, table_name: str, index: str) -> bool:
        validate_identifier(index, "index name")
        rows = self.execute(
            "SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ? AND INDEX_NAME = ?",
            [table_name, index],
        )
        return len(rows) > 0

    # ── dialect overrides ──

    def insert_ignore_prefix(self) -> str:
        return "INSERT IGNORE"

    def json_param(self, placeholder: str = "?") -> str:
        # A JSON column compared with a plain string is never equal.
        return f"CAST({placeholder} AS JSON)"

    def foreign_key_pragma(self) -> str | None:
        return None  # MySQL enforces FKs by default with InnoDB

    def allows_default_for(self, sql_type: str) -> bool:
        base = sql_type.split("(", 1)[0].strip().upper()
        return base not in _NO_DEFAULT_TYPES

    def sql_type_map(self) -> dict[str, str]:
        return {
            "INTEGER":  "INT",
            "BIGINT":   "BIGINT",
            "SMALLINT": "SMALLINT",
            "REAL":     "DOUBLE",
            "DECIMAL":  "DECIMAL",
            "TEXT":     "LONGTEXT",
            "VARCHAR":  "VARCHAR",
            "BOOLEAN":  "TINYINT(1)",
            "DATETIME": "DATETIME",
            "DATE":     "DATE",
            "TIME":     "TIME",
            "JSON":     "JSON",
            "UUID":     "CHAR(32)",
            "BLOB":     "LONGBLOB",
        }

    def autoincrement_definition(self) -> str:
        return "PRIMARY KEY AUTO_INCREMENT"
