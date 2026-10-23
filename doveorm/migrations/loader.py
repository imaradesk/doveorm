"""Find migration files on disk and build the dependency graph.

Files named ``NNNN_description.py`` in the migrations directory are loaded.
Two styles are understood:

* **New style** – the module defines ``class Migration(migrations.Migration)``.
* **Legacy style** – the module defines ``up()`` (and optionally ``down()``
  and ``name``); it becomes a ``RunPython`` step that depends on the file
  before it.  This keeps projects that used hand-written migration
  functions working.

Loaded modules are cached by path and content hash, so repeated loads
(e.g. ``showmigrations`` then ``migrate``) do not re-execute unchanged
files, while an edited file is always reloaded.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import types

from ..exceptions import MigrationError
from .graph import MigrationGraph
from .migration import Migration
from .operations import RunPython

MIGRATION_FILE_RE = re.compile(r"^(\d{4,})_[A-Za-z0-9_]+\.py$")

_cache: dict[str, tuple[str, object]] = {}
_cache_lock = threading.Lock()


def _load_module(path: str):
    with open(path, "rb") as fh:
        source = fh.read()
    digest = hashlib.sha256(source).hexdigest()
    with _cache_lock:
        hit = _cache.get(path)
        if hit is not None and hit[0] == digest:
            return hit[1]
    stem = os.path.splitext(os.path.basename(path))[0]
    package = "doveorm_migrations_" + hashlib.sha1(os.path.dirname(path).encode()).hexdigest()[:12]
    module = types.ModuleType(f"{package}.{stem}")
    module.__file__ = path
    try:
        exec(compile(source, path, "exec"), module.__dict__)
    except Exception as exc:
        raise MigrationError(f"Could not load migration file {path}: {exc}") from exc
    with _cache_lock:
        _cache[path] = (digest, module)
    return module


def _legacy(name: str, module, previous: str | None) -> Migration:
    up = module.up
    down = getattr(module, "down", None)

    class LegacyMigration(Migration):
        dependencies = [previous] if previous else []
        operations = [RunPython(lambda db: up(), (lambda db: down()) if down else None)]

    return LegacyMigration(name)


class MigrationLoader:
    def __init__(self, directory: str):
        self.directory = os.path.abspath(directory)
        self.disk: dict[str, Migration] = {}
        self.files: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.isdir(self.directory):
            return
        previous = None
        for filename in sorted(os.listdir(self.directory)):
            if not MIGRATION_FILE_RE.match(filename):
                continue
            path = os.path.join(self.directory, filename)
            stem = filename[:-3]
            module = _load_module(path)
            cls = getattr(module, "Migration", None)
            if isinstance(cls, type) and issubclass(cls, Migration):
                migration = cls(stem)
            elif callable(getattr(module, "up", None)):
                migration = _legacy(str(getattr(module, "name", stem)), module, previous)
            else:
                raise MigrationError(
                    f"{path} defines neither a Migration class nor an up() function."
                )
            if migration.name in self.disk:
                raise MigrationError(f"Duplicate migration name {migration.name!r} ({path}).")
            self.disk[migration.name] = migration
            self.files[migration.name] = path
            previous = migration.name

    def numbers(self) -> list[int]:
        return sorted(int(MIGRATION_FILE_RE.match(os.path.basename(p)).group(1))
                      for p in self.files.values())

    def build_graph(self, applied=()) -> tuple[MigrationGraph, set[str]]:
        """The graph to run, plus the effective applied set.

        A squashed migration is used instead of the ones it replaces when
        either all or none of those are applied; if only some are, the
        originals are used so the remaining ones can still run.
        """
        applied = set(applied)
        graph = MigrationGraph()
        for m in self.disk.values():
            graph.add_node(m)
        for m in self.disk.values():
            for dep in m.dependencies:
                if dep not in self.disk and not any(dep in s.replaces for s in self.disk.values()):
                    raise MigrationError(f"Migration {m.name} depends on unknown migration {dep!r}.")
                if dep in self.disk:
                    graph.add_dependency(m.name, dep)

        effective = set(applied)
        for squash in [m for m in self.disk.values() if m.replaces]:
            done = [r for r in squash.replaces if r in applied]
            if len(done) in (0, len(squash.replaces)):
                for r in squash.replaces:
                    if r not in graph:
                        continue
                    for child in list(graph.children[r]):
                        if child != squash.name and child not in squash.replaces:
                            graph.add_dependency(child, squash.name)
                    graph.remove_node(r)
                # Migrations naming a replaced one as a dependency now follow the squash.
                for m in self.disk.values():
                    if m.name in graph and m.name != squash.name and set(m.dependencies) & set(squash.replaces):
                        graph.add_dependency(m.name, squash.name)
                if done:
                    effective.add(squash.name)
            else:
                missing = [r for r in squash.replaces if r not in self.disk]
                if missing:
                    raise MigrationError(
                        f"{squash.name} replaces partially-applied migrations, but "
                        f"{', '.join(missing)} are no longer on disk."
                    )
                for child in list(graph.children[squash.name]):
                    graph.add_dependency(child, squash.replaces[-1])
                graph.remove_node(squash.name)
        graph.order()          # raises on cycles
        return graph, effective
