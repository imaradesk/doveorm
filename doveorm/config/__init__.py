"""
DoveORM Configuration
======================

Provides a centralised, declarative way to configure the ORM —
similar to Django's ``DATABASES`` setting — so that users never need
to instantiate ``Database`` or driver objects by hand.

Quick start
-----------

.. code-block:: python

    from doveorm import configure, Model, CharField

    # SQLite (simple)
    configure(DATABASE_PATH="app.db", LOG_QUERIES=True)

    # MySQL
    configure(
        DATABASE_ENGINE="mysql",
        DATABASE_NAME="myapp",
        DATABASE_HOST="127.0.0.1",
        DATABASE_PORT=3306,
        DATABASE_USER="app",
        DATABASE_PASSWORD="secret",
        LOG_QUERIES=False,
    )

    # PostgreSQL
    configure(
        DATABASE_ENGINE="postgresql",
        DATABASE_NAME="myapp",
        DATABASE_HOST="127.0.0.1",
        DATABASE_USER="app",
        DATABASE_PASSWORD="secret",
    )

    # Or use a dict (Django-style)
    configure(DATABASES={
        "default": {
            "ENGINE": "sqlite",
            "NAME": "app.db",
        }
    })

    class User(Model):
        name = CharField(max_length=100)

    # No need to call User.use(db) — it's done automatically.

After ``configure()`` is called every *existing* and *future* ``Model``
subclass will automatically be wired to the configured database.

Advanced
--------
You can also configure from a Python file, environment variables, or a dict:

    configure_from_file("settings.py")     # deprecated – executes the file
    configure_from_env()                   # reads DOVEORM_* env vars
    configure_from_dict({...})

Unknown setting names raise :class:`~doveorm.exceptions.ConfigurationError`
so typos are caught immediately.
"""
from __future__ import annotations

import logging
import threading
import warnings
from typing import TYPE_CHECKING, Any

from ..exceptions import ConfigurationError
from .builders import build_database
from .defaults import DEFAULTS
from .loaders import load_env_settings, load_file_settings

if TYPE_CHECKING:
    from ..database import Database

log = logging.getLogger("doveorm")

# Sentinel for "not set"
_UNSET = object()

# ──────────────────────────────────────────────
# Internal state
# ──────────────────────────────────────────────

_lock = threading.RLock()
_settings: dict[str, Any] = dict(DEFAULTS)
_database_instance = None    # lazily created Database / MySQLDatabase / PostgreSQLDatabase
_configured = False


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────

def configure(**overrides) -> "Database":
    """
    Configure the ORM.

    Accepts keyword arguments matching the setting names (see DEFAULTS).
    Returns the configured :class:`Database` instance.

    Can be called multiple times — each call replaces the previous config.
    Models bound to the previous global database are re-bound to the new
    one and the previous database's connections are closed.  Models bound
    explicitly to another database with ``Model.use(db)`` are left alone.
    """
    global _settings, _database_instance, _configured

    unknown = sorted(set(overrides) - set(DEFAULTS))
    if unknown:
        raise ConfigurationError(
            f"Unknown setting(s): {', '.join(unknown)}. "
            f"Valid settings: {', '.join(sorted(DEFAULTS))}."
        )

    with _lock:
        settings = dict(DEFAULTS)
        settings.update(overrides)
        new_db = build_database(settings)

        old_db = _database_instance
        _settings = settings
        _database_instance = new_db
        _configured = True

        _rewire_models(old_db, new_db, auto_wire=bool(settings["AUTO_WIRE_MODELS"]))
        if old_db is not None and old_db is not new_db:
            old_db.close_all()

    return new_db


def configure_from_dict(d: dict) -> "Database":
    """Configure from a plain dict.

    If the dict has a ``DATABASES`` key containing a ``default`` entry,
    that takes precedence (Django-style).
    """
    return configure(**d)


def configure_from_env(prefix: str = "DOVEORM_") -> "Database":
    """
    Read configuration from environment variables.

    Env var names are ``{prefix}{SETTING_NAME}``, e.g.
    ``DOVEORM_DATABASE_ENGINE=mysql``.  Dict-valued settings
    (``DATABASE_OPTIONS``, ``DATABASES``) are parsed as JSON.
    """
    return configure(**load_env_settings(prefix))


def configure_from_file(filepath: str) -> "Database":
    """
    Read settings from a Python file (e.g. ``settings.py``).

    The file should define module-level variables whose names match
    the supported setting keys.

    .. warning:: The file is *executed*.  Only pass trusted paths — never
       a path derived from user input.
    """
    warnings.warn(
        "configure_from_file() is deprecated: it executes the given file. "
        "Import your settings module and call configure(**settings) instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return configure(**load_file_settings(filepath))


def get_setting(name: str, default: Any = _UNSET) -> Any:
    """Retrieve a single setting value."""
    if default is _UNSET:
        return _settings[name]
    return _settings.get(name, default)


def get_database() -> "Database":
    """
    Return the currently configured Database instance.

    Raises :class:`ConfigurationError` if ``configure()`` has not been
    called — the ORM never invents a database on the application's behalf.
    """
    with _lock:
        if _database_instance is None:
            raise ConfigurationError(
                "DoveORM is not configured. Call doveorm.configure(...) at application "
                "start-up (e.g. configure(DATABASE_PATH=':memory:') in tests)."
            )
        return _database_instance


def is_configured() -> bool:
    return _configured


def reset():
    """Reset all configuration to defaults (useful for testing)."""
    global _settings, _database_instance, _configured
    with _lock:
        old_db = _database_instance
        _settings = dict(DEFAULTS)
        _database_instance = None
        _configured = False
        if old_db is not None:
            _rewire_models(old_db, None, auto_wire=False)
            old_db.close_all()


def _rewire_models(old_db, new_db, *, auto_wire: bool) -> None:
    """Point models at *new_db*.

    Models that were unbound or bound to the previous global database are
    re-bound; models explicitly bound to some other database are untouched.
    """
    from ..model import ModelMeta
    for cls in list(ModelMeta._registry.values()):
        current = cls.__dict__.get("_db")
        if current is None or (old_db is not None and current is old_db):
            cls._db = new_db if auto_wire else None
