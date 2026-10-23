"""
DoveORM – DatabaseDriver base class.

Every backend driver must implement this interface.  The ORM core
(Database, Model, QuerySet) programmes against these methods only.
Connection handling and transactions live in :mod:`.connection`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable, Sequence

from ..utils.sql import first_keyword, index_name, split_statements, translate_qmark, validate_identifier
from .connection import ConnectionManagerMixin


class DatabaseDriver(ConnectionManagerMixin, ABC):
    """Abstract base for all DoveORM database drivers."""

    # ── dialect identifiers ──────────────────────

    name: str = "base"            # "sqlite" | "mysql"
    placeholder: str = "?"        # native parameter placeholder
    autoincrement_sql: str = "AUTOINCREMENT"  # keyword after PRIMARY KEY
    backslash_escapes: bool = False           # backslash escapes inside string literals
    supports_transactional_ddl: bool = True


    # ── backend hooks (implemented by subclasses) ──

    @abstractmethod
    def _open(self):
        """Open and return a new DB-API connection."""

    def _close(self, conn) -> None:
        conn.close()

    @abstractmethod
    def _exec_control(self, conn, sql: str) -> None:
        """Execute a parameterless control statement (BEGIN, SAVEPOINT …)."""

    @abstractmethod
    def _begin(self, conn) -> None: ...

    @abstractmethod
    def _commit(self, conn) -> None: ...

    @abstractmethod
    def _rollback(self, conn) -> None: ...

    @property
    def integrity_errors(self) -> tuple[type[BaseException], ...]:
        """Exception classes the backend raises for constraint violations."""
        return ()

    def __reduce_ex__(self, protocol):
        # Drivers hold live connections and credentials: never serialise or copy them.
        raise TypeError(f"{type(self).__name__} cannot be pickled or copied.")


    # ── query execution ──────────────────────────

    @abstractmethod
    def execute(self, sql: str, params: Sequence | None = None) -> list:
        """Run a SELECT.  Must return a list of rows supporting ``row["col"]``,
        ``row[0]`` and ``row.keys()``."""

    @abstractmethod
    def execute_write(self, sql: str, params: Sequence | None = None) -> int:
        """Run INSERT/UPDATE/DELETE.

        Returns ``lastrowid`` for INSERT/REPLACE statements, otherwise the
        number of rows matched.
        """

    @abstractmethod
    def execute_many(self, sql: str, seq_of_params: Iterable[Sequence]) -> int:
        """Run one statement for each parameter set, atomically.  Returns rowcount."""

    @abstractmethod
    def _execute_statement(self, sql: str) -> None:
        """Execute a single parameterless statement (used for DDL)."""

    def execute_ddl(self, sql: str) -> None:
        """Execute exactly one DDL statement."""
        self._execute_statement(sql)

    def execute_script(self, sql: str, *, atomic: bool = True) -> None:
        """Execute a multi-statement script.

        Statements are split on top-level ``;`` (semicolons inside string
        literals and comments are respected).  With ``atomic=True`` (the
        default) the script runs inside a transaction — joining the caller's
        transaction if one is open.  Note that on MySQL DDL statements
        commit implicitly regardless.
        """
        statements = self._split_script(sql)
        if not atomic:
            for stmt in statements:
                self._execute_statement(stmt)
            return
        with self.transaction():
            for stmt in statements:
                self._execute_statement(stmt)

    def _split_script(self, sql: str) -> list[str]:
        return split_statements(sql, backslash_escapes=self.backslash_escapes)

    @staticmethod
    def _is_insert(sql: str) -> bool:
        return first_keyword(sql) in ("INSERT", "REPLACE")


    # ── introspection ────────────────────────────

    @abstractmethod
    def table_exists(self, table_name: str) -> bool: ...

    @abstractmethod
    def index_exists(self, table_name: str, index: str) -> bool: ...

    # ── SQL dialect helpers ──────────────────────

    def adapt_sql(self, sql: str) -> str:
        """Rewrite ``?`` placeholders if the driver uses something else.

        The ORM always builds SQL with ``?``.  ``?`` characters inside
        string literals and comments are left untouched, and literal ``%``
        signs are escaped for pyformat drivers.
        """
        if self.placeholder != "?":
            return translate_qmark(sql, self.placeholder, backslash_escapes=self.backslash_escapes)
        return sql

    def limit_offset_sql(self, limit: int | None, offset: int | None) -> str:
        """Return the LIMIT/OFFSET clause (values must already be validated ints)."""
        parts = []
        if limit is not None:
            parts.append(f"LIMIT {limit}")
        elif offset:
            parts.append(f"LIMIT {self.max_limit}")
        if offset:
            parts.append(f"OFFSET {offset}")
        return " ".join(parts)

    max_limit: str = "-1"

    def sql_type_map(self) -> dict[str, str]:
        """Return a mapping of DoveORM generic type → backend SQL type.

        Subclasses override to customise types.
        """
        return {
            "INTEGER": "INTEGER",
            "REAL":    "REAL",
            "TEXT":    "TEXT",
        }

    def allows_default_for(self, sql_type: str) -> bool:
        """Whether a literal DEFAULT is permitted on a column of *sql_type*."""
        return True

    def autoincrement_definition(self) -> str:
        """Return the full PK + auto-increment clause for this backend."""
        return f"PRIMARY KEY {self.autoincrement_sql}"

    def insert_ignore_prefix(self) -> str:
        """Return the INSERT variant that silently ignores duplicate keys."""
        return "INSERT OR IGNORE"

    def foreign_key_pragma(self) -> str | None:
        """Return a statement to enable FK enforcement, or None."""
        return None

    def create_index(self, table: str, columns: Sequence[str], *,
                     unique: bool = False, name: str | None = None) -> str:
        """Create an index if it does not already exist.  Returns its name."""
        validate_identifier(table, "table name")
        for col in columns:
            validate_identifier(col, "index column")
        if name is None:
            name = index_name("uq" if unique else "idx", table, columns)
        validate_identifier(name, "index name")
        if not self.index_exists(table, name):
            kind = "UNIQUE INDEX" if unique else "INDEX"
            self._execute_statement(f"CREATE {kind} {name} ON {table} ({', '.join(columns)})")
        return name

    @staticmethod
    def _params(params: Sequence | None) -> tuple:
        if params is None:
            return ()
        if isinstance(params, (str, bytes)):
            raise TypeError("SQL params must be a sequence, not a string.")
        return tuple(params)

    def __repr__(self):
        return f"<{type(self).__name__} connected={self.is_connected()}>"


__all__ = ["DatabaseDriver"]
