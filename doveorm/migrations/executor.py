"""Plan and run migrations against a database.

Performance notes
-----------------
* The applied history is read once per run.
* The schema state is built in a single pass over the graph: applied
  migrations only replay their (in-memory) state changes, pending ones
  also run their DDL.  States are copy-on-write, so this is linear in the
  number of operations.
* Each migration is one transaction on SQLite (DDL included) together with
  its history row; on MySQL a named lock (``GET_LOCK``) stops two
  deployments migrating at the same time.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

from ..exceptions import MigrationError
from .backends.base import editor_for
from .loader import MigrationLoader
from .operations import AddField, CreateModel
from .recorder import MigrationRecorder
from .state import ProjectState

log = logging.getLogger("doveorm.migrations")

ZERO = "zero"
_LOCK_NAME = "doveorm_migrate"


class MigrationExecutor:
    def __init__(self, db, loader: MigrationLoader | str, *, lock_timeout: int = 60):
        self.db = db
        self.loader = loader if isinstance(loader, MigrationLoader) else MigrationLoader(loader)
        self.recorder = MigrationRecorder(db)
        self.lock_timeout = int(lock_timeout)

    # ── planning ──

    def _graph(self):
        recorded = self.recorder.applied()
        graph, applied = self.loader.build_graph(recorded)
        self._check_consistency(graph, applied)
        return graph, applied

    @staticmethod
    def _check_consistency(graph, applied):
        for name in graph.order():
            if name in applied:
                missing = [p for p in graph.parents[name] if p not in applied]
                if missing:
                    raise MigrationError(
                        f"Inconsistent history: {name} is applied but its dependency "
                        f"{missing[0]} is not."
                    )

    def resolve(self, graph, target: str) -> str:
        """Accept a full name or a unique prefix such as ``0003``."""
        if target in graph:
            return target
        matches = [n for n in graph.nodes if n.startswith(target)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise MigrationError(f"No migration matches {target!r}.")
        raise MigrationError(f"{target!r} is ambiguous: {', '.join(sorted(matches))}.")

    def plan(self, target: str | None = None) -> list[tuple[str, bool]]:
        """``[(name, backwards), …]`` needed to reach *target*.

        ``None`` means "latest"; ``"zero"`` unapplies everything; a name
        means: apply it and its dependencies, or – if it is already applied –
        unapply everything that depends on it.
        """
        graph, applied = self._graph()
        return self._plan(graph, applied, target)

    def _plan(self, graph, applied, target):
        order = graph.order()
        if target is None:
            leaves = graph.leaves()
            if len(leaves) > 1:
                raise MigrationError(
                    f"Conflicting migrations (several latest: {', '.join(leaves)}); "
                    "run makemigrations with merge=True."
                )
            wanted = set().union(*(graph.ancestors(l) for l in leaves)) if leaves else set()
            return [(n, False) for n in order if n in wanted and n not in applied]
        if target == ZERO:
            return [(n, True) for n in reversed(order) if n in applied]
        target = self.resolve(graph, target)
        if target not in applied:
            wanted = graph.ancestors(target)
            return [(n, False) for n in order if n in wanted and n not in applied]
        undo = graph.descendants(target) - {target}
        return [(n, True) for n in reversed(order) if n in undo and n in applied]

    # ── running ──

    def migrate(self, target: str | None = None, *, fake: bool = False,
                fake_initial: bool = False) -> list[tuple[str, bool]]:
        """Bring the database to *target*.  Returns the plan that ran."""
        self.recorder.ensure_table()
        with self._lock():
            graph, applied = self._graph()
            plan = self._plan(graph, applied, target)
            if not plan:
                log.info("No migrations to apply.")
                return []
            if all(backwards for _, backwards in plan):
                self._unapply(graph, applied, [n for n, _ in plan], fake=fake)
            elif not any(backwards for _, backwards in plan):
                self._apply(graph, applied, [n for n, _ in plan], fake=fake, fake_initial=fake_initial)
            else:  # pragma: no cover - _plan never mixes directions
                raise MigrationError("Mixed forwards/backwards plan.")
        return plan

    def _apply(self, graph, applied, names, *, fake, fake_initial):
        todo = set(names)
        state = ProjectState()
        for name in graph.order():
            if not todo:
                break
            migration = graph.nodes[name]
            if name in todo:
                todo.discard(name)
                state = self._apply_one(migration, state, fake=fake, fake_initial=fake_initial)
            elif name in applied:
                state = migration.mutate_state(state)

    def _apply_one(self, migration, state, *, fake, fake_initial):
        if not fake and fake_initial and self._already_in_db(migration, state):
            log.info("Faking %s (tables already exist).", migration.name)
            fake = True
        log.info("%s %s", "Faking" if fake else "Applying", migration.name)
        try:
            with editor_for(self.db, atomic=migration.atomic) as editor:
                for op in migration.operations:
                    after = state.clone()
                    op.state_forwards(after)
                    if not fake:
                        op.database_forwards(editor, state, after)
                    state = after
                for replaced in migration.replaces:
                    if replaced not in self._recorded():
                        self.recorder.record_applied(replaced)
                self.recorder.record_applied(migration.name)
        except MigrationError:
            raise
        except Exception as exc:
            hint = ""
            if not self.db.driver.supports_transactional_ddl:
                hint = " (MySQL cannot roll back DDL: statements before the failure stay applied)"
            raise MigrationError(f"Migration {migration.name} failed{hint}: {exc}") from exc
        self._recorded_cache = None
        return state

    def _recorded(self) -> set[str]:
        cache = getattr(self, "_recorded_cache", None)
        if cache is None:
            cache = self._recorded_cache = set(self.recorder.applied())
        return cache

    def _already_in_db(self, migration, state) -> bool:
        """For ``fake_initial``: every table / column the migration creates exists."""
        ops = migration.operations
        if not migration.initial or not ops or not all(isinstance(o, (CreateModel, AddField)) for o in ops):
            return False
        editor = editor_for(self.db, collect_sql=True)
        after = migration.mutate_state(state)
        for op in ops:
            if isinstance(op, CreateModel):
                ms = after.get_model(op.name)
                if not editor.table_exists(ms.table):
                    return False
                if not set(ms.fields) <= editor.column_names(ms.table):
                    return False
            else:
                ms = after.get_model(op.model_name)
                if not editor.table_exists(ms.table) or op.name not in editor.column_names(ms.table):
                    return False
        return True

    def _unapply(self, graph, applied, names, *, fake):
        targets = set(names)
        for name in names:
            if not fake and not graph.nodes[name].reversible:
                raise MigrationError(f"Migration {name} is not reversible; nothing was unapplied.")
        # State before each migration to undo, in one pass.
        before: dict[str, ProjectState] = {}
        state = ProjectState()
        for name in graph.order():
            if name in targets:
                before[name] = state
            if name in applied:
                state = graph.nodes[name].mutate_state(state)
        for name in names:                       # already newest-first
            migration = graph.nodes[name]
            log.info("%s %s", "Faking unapply of" if fake else "Unapplying", name)
            steps, s = [], before[name]
            for op in migration.operations:
                after = s.clone()
                op.state_forwards(after)
                steps.append((op, s, after))
                s = after
            try:
                with editor_for(self.db, atomic=migration.atomic) as editor:
                    if not fake:
                        for op, b, a in reversed(steps):
                            op.database_backwards(editor, b, a)
                    for replaced in migration.replaces:
                        self.recorder.record_unapplied(replaced)
                    self.recorder.record_unapplied(name)
            except MigrationError:
                raise
            except Exception as exc:
                raise MigrationError(f"Unapplying {name} failed: {exc}") from exc

    # ── SQL preview ──

    def sql_for(self, name: str, *, backwards: bool = False) -> list[str]:
        """The SQL a migration would run (nothing is executed)."""
        graph, _applied = self.loader.build_graph(self.recorder.applied())
        name = self.resolve(graph, name)
        ancestors = graph.ancestors(name)
        state = ProjectState()
        for n in graph.order():
            if n in ancestors and n != name:
                state = graph.nodes[n].mutate_state(state)
        migration = graph.nodes[name]
        editor = editor_for(self.db, collect_sql=True)
        steps, s = [], state
        for op in migration.operations:
            after = s.clone()
            op.state_forwards(after)
            steps.append((op, s, after))
            s = after
        if backwards:
            for op, b, a in reversed(steps):
                if not op.reversible:
                    raise MigrationError(f"{op.describe()} is not reversible.")
                op.database_backwards(editor, b, a)
        else:
            for op, b, a in steps:
                op.database_forwards(editor, b, a)
        return editor.collected

    # ── state of the history ──

    def history_state(self) -> ProjectState:
        """The schema described by all migration files (applied or not)."""
        graph, _ = self.loader.build_graph(self.recorder.applied())
        state = ProjectState()
        for name in graph.order():
            graph.nodes[name].mutate_state(state, in_place=True)
        return state

    # ── locking ──

    @contextmanager
    def _lock(self):
        if self.db.driver.name != "mysql":
            yield            # SQLite: the UNIQUE history row + per-migration transaction guard
            return
        got = self.db.execute("SELECT GET_LOCK(?, ?)", [_LOCK_NAME, self.lock_timeout])
        if not got or got[0][0] != 1:
            raise MigrationError(
                f"Another process is running migrations (waited {self.lock_timeout}s)."
            )
        try:
            yield
        finally:
            self.db.execute("SELECT RELEASE_LOCK(?)", [_LOCK_NAME])
