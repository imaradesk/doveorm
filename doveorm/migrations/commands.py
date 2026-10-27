"""High-level API: ``makemigrations``, ``migrate``, ``showmigrations`` …

These are what the CLI calls; applications can call them directly too::

    from doveorm import migrations
    migrations.makemigrations("migrations", models=[User, Post])
    migrations.migrate("migrations")
"""
from __future__ import annotations

import datetime
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field

from ..exceptions import MigrationError
from .autodetector import Autodetector
from .executor import MigrationExecutor
from .loader import MigrationLoader
from .operations import CreateModel
from .optimizer import optimize
from .questioner import Questioner
from .state import ProjectState
from .writer import write_migration

log = logging.getLogger("doveorm.migrations")

_NAME_RE = re.compile(r"^[a-z0-9_]{1,80}$")


@dataclass
class MigrationFile:
    name: str
    path: str
    source: str
    operations: list = field(default_factory=list)
    written: bool = False


def project_models(models=None, *, include_unmigrated: bool = False) -> list[type]:
    """Concrete models to migrate (default: every registered model).

    Models declared with ``migrate = False`` are left out unless *include_unmigrated*.
    """
    if models is None:
        from ..model import ModelMeta
        models = list(ModelMeta._registry.values())
    out = []
    for m in models:
        if getattr(m, "_abstract", False) or getattr(m, "_doveorm_internal", False):
            continue
        if not include_unmigrated and not getattr(m, "_migrate", True):
            continue
        out.append(m)
    return sorted(out, key=lambda m: m.__name__)


def _version() -> str:
    from .. import __version__
    return __version__


def _history(loader: MigrationLoader):
    graph, _ = loader.build_graph()
    state = ProjectState()
    for name in graph.order():
        graph.nodes[name].mutate_state(state, in_place=True)
    return graph, state


def _next_number(loader: MigrationLoader) -> int:
    numbers = loader.numbers()
    return (numbers[-1] + 1) if numbers else 1


def _auto_name(ops, initial: bool) -> str:
    if initial:
        return "initial"
    fragments = [op.migration_name_fragment for op in ops]
    if fragments and all(fragments):
        name = "_".join(fragments)
        if len(name) <= 52:
            return name
        return fragments[0][:40] + "_and_more"
    return "auto_" + datetime.datetime.now().strftime("%Y%m%d_%H%M")


def _write(directory: str, filename: str, source: str) -> str:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, filename)
    if os.path.exists(path):
        raise MigrationError(f"{path} already exists.")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(source)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def makemigrations(directory: str, *, models=None, name: str | None = None,
                   empty: bool = False, dry_run: bool = False, merge: bool = False,
                   check: bool = False, questioner: Questioner | None = None,
                   defaults: dict | None = None) -> MigrationFile | None:
    """Detect model changes and write the next migration file.

    Returns the :class:`MigrationFile` (``written=False`` for ``dry_run``),
    or ``None`` when nothing changed.  ``check=True`` raises
    :class:`MigrationError` if changes are missing a migration (for CI).
    No database connection is needed.
    """
    if name is not None and not _NAME_RE.match(name):
        raise MigrationError("Migration name must match [a-z0-9_]{1,80}.")
    loader = MigrationLoader(directory)
    graph, history = _history(loader)
    leaves = graph.leaves()

    if merge:
        if len(leaves) < 2:
            log.debug("No conflicts to merge.")
            return None
        return _emit(directory, loader, name or "merge_" + "_".join(l.split("_", 1)[0] for l in leaves),
                     [], leaves, dry_run=dry_run)
    if len(leaves) > 1:
        raise MigrationError(
            f"Conflicting migrations ({', '.join(leaves)}); run makemigrations with merge=True."
        )

    if empty:
        return _emit(directory, loader, name or "custom", [], leaves, dry_run=dry_run)

    target = ProjectState.from_models(project_models(models))
    if questioner is None:
        questioner = Questioner(defaults=defaults)
    ops = Autodetector(history, target, questioner).changes()
    if not ops:
        log.debug("No changes detected.")
        return None
    if check:
        raise MigrationError(
            "Model changes are not covered by a migration: "
            + "; ".join(op.describe() for op in ops)
        )
    initial = not history.models and all(isinstance(op, CreateModel) for op in ops)
    return _emit(directory, loader, name or _auto_name(ops, initial), ops, leaves,
                 initial=initial, dry_run=dry_run)


def _emit(directory, loader, name, ops, dependencies, *, initial=False, replaces=(),
          dry_run=False, number=None) -> MigrationFile:
    number = number if number is not None else _next_number(loader)
    full = f"{number:04d}_{name}"
    source = write_migration(operations=ops, dependencies=dependencies, initial=initial,
                             replaces=replaces, version=_version())
    path = os.path.join(directory, full + ".py")
    result = MigrationFile(full, path, source, list(ops))
    if not dry_run:
        _write(directory, full + ".py", source)
        result.written = True
        log.debug("Created %s", path)
    return result


def migrate(directory: str, target: str | None = None, *, db=None, fake: bool = False,
            fake_initial: bool = False) -> list[tuple[str, bool]]:
    """Apply (or with a *target*, unapply) migrations.  Returns ``[(name, backwards)]``."""
    return _executor(directory, db).migrate(target, fake=fake, fake_initial=fake_initial)


def rollback(directory: str, steps: int = 1, *, db=None, fake: bool = False):
    """Unapply the last *steps* applied migrations (newest first)."""
    if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
        raise ValueError("steps must be a positive integer.")
    executor = _executor(directory, db)
    graph, applied = executor._graph()
    done = [n for n in graph.order() if n in applied]
    if not done:
        return []
    keep = done[:-steps]
    target = keep[-1] if keep else "zero"
    return executor.migrate(target, fake=fake)


def plan(directory: str, target: str | None = None, *, db=None) -> list[tuple[str, bool]]:
    return _executor(directory, db).plan(target)


def showmigrations(directory: str, *, db=None) -> list[tuple[str, bool]]:
    """``[(name, applied)]`` in dependency order."""
    executor = _executor(directory, db)
    graph, applied = executor.loader.build_graph(executor.recorder.applied())
    return [(n, n in applied) for n in graph.order()]


def sqlmigrate(directory: str, name: str, *, db=None, backwards: bool = False) -> list[str]:
    return _executor(directory, db).sql_for(name, backwards=backwards)


def squashmigrations(directory: str, end: str, *, start: str | None = None,
                     name: str | None = None, dry_run: bool = False) -> MigrationFile:
    """Combine migrations ``start``…``end`` into one optimized migration.

    The new file lists them in ``replaces``; keep the old files until every
    database has applied the squashed range, then delete them.
    """
    loader = MigrationLoader(directory)
    graph, _ = loader.build_graph()
    resolve = MigrationExecutor.resolve
    end = resolve(None, graph, end)
    members = graph.ancestors(end)
    if start is not None:
        start = resolve(None, graph, start)
        if start not in members:
            raise MigrationError(f"{start} is not an ancestor of {end}.")
        members -= graph.ancestors(start) - {start}
    ordered = [n for n in graph.order() if n in members]
    if len(ordered) < 2:
        raise MigrationError("Nothing to squash (need at least two migrations).")
    outside = {c for n in ordered for c in graph.children[n]} - members
    for child in outside:
        if graph.ancestors(child) & members and not (graph.ancestors(child) >= members):
            raise MigrationError(f"{child} depends on part of the range; cannot squash it.")
    ops = []
    for n in ordered:
        if graph.nodes[n].replaces:
            raise MigrationError(f"{n} is already a squashed migration.")
        ops.extend(graph.nodes[n].operations)
    deps = sorted({p for n in ordered for p in graph.parents[n]} - members)
    first = int(ordered[0].split("_", 1)[0])
    last = ordered[-1].split("_", 1)[0]
    initial = any(graph.nodes[n].initial for n in ordered)
    label = name or f"squashed_{last}"
    return _emit(directory, loader, label, optimize(ops), deps, initial=initial,
                 replaces=ordered, dry_run=dry_run, number=first)


def _executor(directory, db) -> MigrationExecutor:
    if db is None:
        from ..config import get_database
        db = get_database()
    return MigrationExecutor(db, MigrationLoader(directory))
