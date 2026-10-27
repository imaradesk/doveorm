"""Find and load a project's DoveORM settings.

The ``doveorm`` command reads its project settings from, in order:

1. command-line flags (``--settings``, ``--models``, ``--migrations``);
2. ``doveorm.toml`` (top-level keys), or the ``[tool.doveorm]`` table of
   ``pyproject.toml`` — the first one found in the current directory or a
   parent;
3. ``DOVEORM_*`` environment variables for the database connection.

Example ``doveorm.toml``::

    models     = ["myapp.models",       # modules that define your models
                  "users",              # a package: it and its ``models`` submodule
                  "billing.*.models"]   # glob over modules in the project folder
                                        # (empty: models.py files are discovered)
    discover     = true                 # also discover models.py files next to the list
    entry_points = true                 # also load installed packages' "doveorm.models" entry points
    migrations = "migrations"           # relative to this file
    settings   = "myapp.settings:ORM"   # optional; else DOVEORM_* env vars

.. warning:: Loading a project imports the modules it names (settings and
   models), exactly like running the application would.  Only run the
   command in projects you trust.
"""
from __future__ import annotations

import fnmatch
import importlib
import importlib.util
import os
import sys
import types
from dataclasses import dataclass, field
from typing import Any

from ..config.defaults import DEFAULTS
from ..exceptions import ConfigurationError

CONFIG_FILE = "doveorm.toml"
PYPROJECT = "pyproject.toml"
DEFAULT_MIGRATIONS = "migrations"
_KEYS = {"models", "migrations", "settings", "env_prefix", "discover", "entry_points"}
ENTRY_POINT_GROUP = "doveorm.models"

# Directories never searched for models (they hold dependencies, tests or build output).
_SKIP_DIRS = {"tests", "test", "migrations", "build", "dist", "node_modules", "site-packages",
              "__pycache__", "venv", "env"}


def discover_models(root: str) -> list[str]:
    """Dotted names of the ``models.py`` modules / ``models`` packages under *root*.

    Only looks at file names — nothing is imported here.  Hidden folders,
    virtual environments, tests and migrations are skipped.
    """
    found = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith((".", "_")) and d not in _SKIP_DIRS
            and not d.endswith(".egg-info")
            and not os.path.isfile(os.path.join(current, d, "pyvenv.cfg"))
        )
        parts = os.path.relpath(current, root).split(os.sep)
        parts = [] if parts == ["."] else parts
        if not all(p.isidentifier() for p in parts):
            dirs[:] = []
            continue
        if "models.py" in files:
            found.append(".".join([*parts, "models"]))
        if "models" in dirs and os.path.isfile(os.path.join(current, "models", "__init__.py")):
            found.append(".".join([*parts, "models"]))
            dirs.remove("models")           # the package imports its own submodules
    return sorted(set(found))


def _project_modules(root: str) -> list[str]:
    """Dotted names of every module and package under *root* (file names only, nothing imported)."""
    found = []
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith((".", "_")) and d not in _SKIP_DIRS
            and not d.endswith(".egg-info")
            and not os.path.isfile(os.path.join(current, d, "pyvenv.cfg"))
        )
        parts = os.path.relpath(current, root).split(os.sep)
        parts = [] if parts == ["."] else parts
        if not all(p.isidentifier() for p in parts):
            dirs[:] = []
            continue
        if parts and "__init__.py" in files:
            found.append(".".join(parts))
        for f in files:
            stem, ext = os.path.splitext(f)
            if ext == ".py" and stem.isidentifier() and not stem.startswith("_"):
                found.append(".".join([*parts, stem]))
    return sorted(set(found))


def expand_models(entries: list[str], root: str) -> list[str]:
    """Expand glob entries (``*``, ``?``, ``[…]``) against the modules under *root*.

    Plain names pass through untouched; duplicates are dropped, order is kept.
    """
    out: list[str] = []
    available = None
    for entry in entries:
        if any(c in entry for c in "*?["):
            if available is None:
                available = _project_modules(root)
            matches = [m for m in available if fnmatch.fnmatchcase(m, entry)]
            if not matches:
                raise ConfigurationError(f"'models' pattern {entry!r} matches no module under {root}.")
            out.extend(matches)
        else:
            out.append(entry)
    return list(dict.fromkeys(out))


def entry_point_models() -> list[str]:
    """Model modules that installed packages register under the ``doveorm.models`` entry point group."""
    from importlib import metadata
    eps = metadata.entry_points()
    selected = eps.select(group=ENTRY_POINT_GROUP) if hasattr(eps, "select") else eps.get(ENTRY_POINT_GROUP, [])
    return sorted({ep.value.partition(":")[0].strip() for ep in selected})


def _load_toml(path: str) -> dict:
    try:
        import tomllib                      # Python 3.11+
    except ModuleNotFoundError:             # pragma: no cover - depends on Python version
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            raise ConfigurationError(
                f"Reading {os.path.basename(path)} needs Python 3.11+ or the 'tomli' "
                "package (pip install tomli)."
            ) from None
    with open(path, "rb") as fh:
        try:
            return tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(f"{path}: invalid TOML ({exc}).") from None


def _mentions_doveorm(path: str) -> bool:
    """Cheap pre-check, so unrelated pyproject.toml files are never parsed."""
    with open(path, "rb") as fh:
        return b"tool.doveorm" in fh.read()


def find_config(start: str | None = None) -> tuple[str | None, dict]:
    """``(path, settings)`` of the nearest project config, or ``(None, {})``."""
    directory = os.path.abspath(start or os.getcwd())
    while True:
        candidate = os.path.join(directory, CONFIG_FILE)
        if os.path.isfile(candidate):
            return candidate, _load_toml(candidate)
        candidate = os.path.join(directory, PYPROJECT)
        if os.path.isfile(candidate) and _mentions_doveorm(candidate):
            table = _load_toml(candidate).get("tool", {}).get("doveorm")
            if table is not None:
                return candidate, table
        parent = os.path.dirname(directory)
        if parent == directory:
            return None, {}
        directory = parent


@dataclass
class Project:
    root: str
    migrations: str
    models: list[str] = field(default_factory=list)
    settings: str | None = None
    env_prefix: str = "DOVEORM_"
    config_path: str | None = None
    discovered: bool = False                # models were found by searching, not listed
    discover: bool = False                  # search in addition to the listed models
    entry_points: bool = False              # also load the "doveorm.models" entry points
    _ready: bool = field(default=False, repr=False)

    # ── loading ──

    @classmethod
    def load(cls, *, config: str | None = None, settings: str | None = None,
             models: list[str] | None = None, migrations: str | None = None,
             discover: bool | None = None, entry_points: bool | None = None) -> "Project":
        if config:
            path = os.path.abspath(config)
            if not os.path.isfile(path):
                raise ConfigurationError(f"Config file not found: {config}")
            data = _load_toml(path)
            if os.path.basename(path) == PYPROJECT:
                data = data.get("tool", {}).get("doveorm", {})
        else:
            path, data = find_config()
        if not isinstance(data, dict):
            raise ConfigurationError(f"{path}: the DoveORM settings must be a table.")
        unknown = sorted(set(data) - _KEYS)
        if unknown:
            raise ConfigurationError(
                f"{path}: unknown key(s) {', '.join(unknown)}; expected {', '.join(sorted(_KEYS))}."
            )
        root = os.path.dirname(path) if path else os.getcwd()

        file_models = data.get("models", [])
        if isinstance(file_models, str):
            file_models = [file_models]
        if not isinstance(file_models, list) or not all(isinstance(m, str) for m in file_models):
            raise ConfigurationError(f"{path}: 'models' must be a list of module names.")
        for key in ("migrations", "settings", "env_prefix"):
            if key in data and not isinstance(data[key], str):
                raise ConfigurationError(f"{path}: '{key}' must be a string.")
        for key in ("discover", "entry_points"):
            if key in data and not isinstance(data[key], bool):
                raise ConfigurationError(f"{path}: '{key}' must be true or false.")

        if migrations:                      # flags are relative to the working directory
            mig_dir = os.path.abspath(migrations)
        else:
            mig_dir = os.path.join(root, data.get("migrations", DEFAULT_MIGRATIONS))
        return cls(
            root=root,
            migrations=os.path.normpath(mig_dir),
            models=list(models) if models else list(file_models),
            settings=settings or data.get("settings"),
            env_prefix=data.get("env_prefix", "DOVEORM_"),
            config_path=path,
            discover=data.get("discover", False) if discover is None else discover,
            entry_points=data.get("entry_points", False) if entry_points is None else entry_points,
        )

    # ── setup ──

    def setup(self) -> None:
        """Configure the database and import the model modules (once)."""
        if self._ready:
            return
        if self.config_path:
            # Relative paths in the settings (e.g. DATABASE_PATH = "app.db") mean the
            # same thing from any subfolder: they are relative to the project root.
            os.chdir(self.root)
        if self.root not in sys.path:
            sys.path.insert(0, self.root)
        self._configure()
        names = expand_models(self.models, self.root)
        if self.discover or not names:
            found = [m for m in discover_models(self.root) if m not in names]
            self.discovered = bool(found)
            if found:
                print(f"Discovered models: {', '.join(found)}", file=sys.stderr)
            names += found
        if self.entry_points:
            names += [m for m in entry_point_models() if m not in names]
        self.models = names
        for name in names:
            module = _import(name, "models module")
            # A package entry ("users") also brings in its ``models`` submodule.
            if hasattr(module, "__path__") and not name.rpartition(".")[2] == "models" \
                    and importlib.util.find_spec(f"{name}.models") is not None:
                _import(f"{name}.models", "models module")
        self._ready = True

    def _configure(self) -> None:
        from .. import config as cfg
        if self.settings:
            value = _resolve(self.settings)
            if callable(value) and not isinstance(value, type):
                value = value()
            if isinstance(value, dict):
                cfg.configure(**value)
            elif isinstance(value, types.ModuleType):
                found = {k: getattr(value, k) for k in DEFAULTS if hasattr(value, k)}
                if not found:
                    raise ConfigurationError(
                        f"Settings module {self.settings!r} defines none of the DoveORM settings."
                    )
                cfg.configure(**found)
            elif value is not None and not _is_database(value):
                raise ConfigurationError(
                    f"{self.settings!r} must be a dict of settings, a settings module or a "
                    "function that returns one (or calls doveorm.configure() itself)."
                )
            elif not cfg.is_configured():
                raise ConfigurationError(
                    f"{self.settings!r} returned without calling doveorm.configure()."
                )
            return
        from ..config.loaders import load_env_settings
        env = load_env_settings(self.env_prefix)
        if env:
            cfg.configure(**env)

    # ── helpers ──

    @property
    def display_migrations(self) -> str:
        try:
            rel = os.path.relpath(self.migrations)
        except ValueError:                  # pragma: no cover - other drive on Windows
            return self.migrations
        return self.migrations if rel.startswith("..") else rel

    def database(self):
        """The configured database (raises :class:`ConfigurationError` otherwise)."""
        from ..config import get_database
        try:
            return get_database()
        except ConfigurationError:
            raise ConfigurationError(
                "No database configured. Set 'settings' in doveorm.toml, pass --settings, "
                f"or export {self.env_prefix}DATABASE_ENGINE / {self.env_prefix}DATABASE_PATH …"
            ) from None


def _is_database(value: Any) -> bool:
    from ..database import Database
    return isinstance(value, Database)


def _import(name: str, what: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name and (name == exc.name or name.startswith(exc.name + ".")):
            raise ConfigurationError(f"Cannot import {what} {name!r}: module not found.") from None
        raise


def _resolve(spec: str):
    """``"pkg.mod"`` → the module; ``"pkg.mod:attr"`` → that attribute."""
    module, _, attr = spec.partition(":")
    mod = _import(module, "settings")
    if not attr:
        return mod
    try:
        return getattr(mod, attr)
    except AttributeError:
        raise ConfigurationError(f"{module!r} has no attribute {attr!r}.") from None
