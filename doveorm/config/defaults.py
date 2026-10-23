"""Default values for every DoveORM setting.

Only *library behaviour* has real defaults here.  Where the data lives —
path, host, database name, user, password — is deployment configuration
owned by the application: those settings default to ``None`` and must be
supplied explicitly (see :mod:`doveorm.config.builders` for what is required).
"""
from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    # -- database engine: "sqlite" | "mysql"
    "DATABASE_ENGINE": "sqlite",
    # -- SQLite: file path, or ":memory:" for an explicit temporary database
    "DATABASE_PATH": None,
    # -- MySQL (required: NAME, USER, PASSWORD; HOST unless OPTIONS["unix_socket"])
    "DATABASE_NAME": None,
    "DATABASE_HOST": None,
    "DATABASE_PORT": 3306,        # MySQL protocol default
    "DATABASE_USER": None,
    "DATABASE_PASSWORD": None,    # use "" explicitly for passwordless auth
    "DATABASE_CHARSET": "utf8mb4",
    "DATABASE_OPTIONS": {},       # extra kwargs forwarded to the driver
    # -- ORM behaviour
    "LOG_QUERIES": False,
    "LOG_QUERY_PARAMS": False,    # log bound values (may expose secrets / PII)
    "AUTO_WIRE_MODELS": True,     # auto-call Model.use(db) on every model
    "USE_TZ": False,              # DateTimeField returns aware UTC datetimes
    # -- Django-style dict (takes precedence if set)
    "DATABASES": None,
}
