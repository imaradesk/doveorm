"""Construct ``Database`` instances from resolved settings.

Connection details are never guessed:

* SQLite needs ``DATABASE_PATH`` (``":memory:"`` must be asked for explicitly).
* MySQL needs ``DATABASE_NAME``, ``DATABASE_USER`` and ``DATABASE_PASSWORD``
  (``""`` is accepted but must be explicit), plus ``DATABASE_HOST`` unless
  ``DATABASE_OPTIONS["unix_socket"]`` is given.
* PostgreSQL needs ``DATABASE_NAME``, ``DATABASE_USER``, ``DATABASE_PASSWORD``
  and ``DATABASE_HOST`` (a socket directory such as ``/var/run/postgresql``
  is a valid host).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..exceptions import ConfigurationError

DEFAULT_PORTS = {"mysql": 3306, "postgresql": 5432}

if TYPE_CHECKING:
    from ..database import Database


def build_database(settings: dict) -> "Database":
    """Construct a ``Database`` instance from the resolved settings dict."""
    db_dict = settings.get("DATABASES")
    if db_dict is not None:
        if not isinstance(db_dict, dict) or not isinstance(db_dict.get("default"), dict):
            raise ConfigurationError("DATABASES must be a dict with a 'default' entry.")
        return _build_from_db_dict(db_dict["default"], settings)

    return _construct(
        engine=str(settings["DATABASE_ENGINE"]),
        path=settings.get("DATABASE_PATH") or settings.get("DATABASE_NAME"),
        host=settings.get("DATABASE_HOST"),
        port=settings.get("DATABASE_PORT"),
        user=settings.get("DATABASE_USER"),
        password=settings.get("DATABASE_PASSWORD"),
        name=settings.get("DATABASE_NAME"),
        charset=settings.get("DATABASE_CHARSET", "utf8mb4"),
        options=settings.get("DATABASE_OPTIONS") or {},
        settings=settings,
    )


def _build_from_db_dict(d: dict, settings: dict) -> "Database":
    """Build a Database from a DATABASES.default dict."""
    return _construct(
        engine=str(d.get("ENGINE", "sqlite")),
        path=d.get("NAME"),
        host=d.get("HOST"),
        port=d.get("PORT"),
        user=d.get("USER"),
        password=d.get("PASSWORD"),
        name=d.get("NAME"),
        charset=d.get("CHARSET", "utf8mb4"),
        options=d.get("OPTIONS") or {},
        settings=settings,
    )


def _construct(*, engine, path, host, port, user, password, name, charset,
               options, settings) -> "Database":
    from ..database import Database, MySQLDatabase, PostgreSQLDatabase
    from ..drivers.sqlite import SQLiteDriver

    engine = engine.lower()
    # Normalise dotted engine paths  (e.g. "django.db.backends.sqlite3")
    if "sqlite" in engine:
        engine = "sqlite"
    elif "mysql" in engine:
        engine = "mysql"
    elif "postgres" in engine or engine in ("pg", "pgsql", "psycopg", "psycopg2"):
        engine = "postgresql"
    if port in (None, "") and engine in DEFAULT_PORTS:
        port = DEFAULT_PORTS[engine]

    if not isinstance(options, dict):
        raise ConfigurationError("DATABASE_OPTIONS / OPTIONS must be a dict.")

    log_queries = bool(settings.get("LOG_QUERIES", False))
    log_params = bool(settings.get("LOG_QUERY_PARAMS", False))

    if engine == "sqlite":
        if not path:
            raise ConfigurationError(
                "DATABASE_PATH is required for the SQLite engine "
                "(use ':memory:' explicitly for a temporary database)."
            )
        return Database(
            driver=SQLiteDriver(str(path), **options),
            log_queries=log_queries,
            log_params=log_params,
        )

    if engine == "mysql":
        missing = [label for label, value in (("DATABASE_NAME", name), ("DATABASE_USER", user))
                   if not value]
        if password is None:
            missing.append("DATABASE_PASSWORD (use '' explicitly for passwordless auth)")
        if not host and "unix_socket" not in options:
            missing.append("DATABASE_HOST (or DATABASE_OPTIONS['unix_socket'])")
        if missing:
            raise ConfigurationError(
                "Missing required MySQL setting(s): " + "; ".join(missing) + "."
            )
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise ConfigurationError(f"DATABASE_PORT must be an integer, got {port!r}.") from None
        return MySQLDatabase(
            host=host,
            port=port,
            user=user,
            password=password,
            database=name,
            charset=charset,
            log_queries=log_queries,
            log_params=log_params,
            **options,
        )

    if engine == "postgresql":
        missing = [label for label, value in (("DATABASE_NAME", name), ("DATABASE_USER", user),
                                              ("DATABASE_HOST", host)) if not value]
        if password is None:
            missing.append("DATABASE_PASSWORD (use '' explicitly for passwordless auth)")
        if missing:
            raise ConfigurationError(
                "Missing required PostgreSQL setting(s): " + "; ".join(missing) + "."
            )
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise ConfigurationError(f"DATABASE_PORT must be an integer, got {port!r}.") from None
        return PostgreSQLDatabase(
            host=host,
            port=port,
            user=user,
            password=password,
            database=name,
            log_queries=log_queries,
            log_params=log_params,
            **options,
        )

    raise ConfigurationError(
        f"Unknown DATABASE_ENGINE {engine!r}.  Supported: 'sqlite', 'mysql', 'postgresql'."
    )
