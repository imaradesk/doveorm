"""Compare two project states and produce the operations between them.

Operations are emitted in an order that is always valid:

1. model renames, table renames, field renames
2. indexes / unique_together entries that are going away
3. new models (dependency order; FK cycles are broken with a later AddField)
4. removed fields, added fields, altered fields, MySQL table options
5. new indexes / unique_together entries
6. deleted models (referencing models first)

The result is then optimized and *verified*: replaying it on the old
state must give exactly the new state, otherwise detection fails loudly
instead of writing a wrong migration.
"""
from __future__ import annotations

from ..exceptions import MigrationError
from ..fields import ForeignKey
from .backends.base import NOT_PROVIDED
from .operations import (
    AddField, AlterField, AlterIndexes, AlterModelTable, AlterTableOptions,
    AlterUniqueTogether, CreateModel, DeleteModel, RemoveField, RenameField, RenameModel,
)
from .optimizer import optimize
from .questioner import Questioner
from .state import ProjectState, field_signature

_SET_OPS = (("indexes", AlterIndexes), ("unique_together", AlterUniqueTogether))


class Autodetector:
    def __init__(self, from_state: ProjectState, to_state: ProjectState,
                 questioner: Questioner | None = None):
        self.from_state = from_state
        self.to_state = to_state
        self.questioner = questioner or Questioner()

    def changes(self) -> list:
        self.working = self.from_state.clone()
        phases = {name: [] for name in (
            "renames", "drop_sets", "create", "remove", "add", "alter", "options",
            "add_sets", "delete")}
        self.phases = phases

        self._detect_model_renames()
        self._detect_table_renames()
        self._detect_field_renames()

        old, new = self.working.models, self.to_state.models
        created = sorted(set(new) - set(old))
        deleted = sorted(set(old) - set(new))
        common = sorted(set(old) & set(new))

        for name in common:
            self._diff_sets(old[name], new[name])
            self._diff_fields(old[name], new[name])
            self._diff_table_options(old[name], new[name])
        self._create_models(created)
        self._delete_models(deleted)

        ops = [op for phase in phases.values() for op in phase]
        ops = optimize(ops)
        self._verify(ops)
        return ops

    # ── renames ──

    def _apply(self, phase: str, op) -> None:
        op.state_forwards(self.working)
        self.phases[phase].append(op)

    def _detect_model_renames(self):
        old, new = self.working.models, self.to_state.models
        removed = [n for n in sorted(old) if n not in new]
        added = [n for n in sorted(new) if n not in old]
        for new_name in added:
            new_ms = new[new_name]
            for old_name in removed:
                old_ms = self.working.models.get(old_name)
                if old_ms is None or not _same_fields(old_ms, new_ms):
                    continue
                if self.questioner.ask_rename_model(old_name, new_name):
                    self._apply("renames", RenameModel(old_name, new_name))
                    removed.remove(old_name)
                    break

    def _detect_table_renames(self):
        for name in sorted(set(self.working.models) & set(self.to_state.models)):
            old_table = self.working.models[name].table
            new_table = self.to_state.models[name].table
            if old_table != new_table:
                self._apply("renames", AlterModelTable(name, new_table))

    def _detect_field_renames(self):
        for name in sorted(set(self.working.models) & set(self.to_state.models)):
            old_ms, new_ms = self.working.models[name], self.to_state.models[name]
            removed = [f for f in old_ms.fields if f not in new_ms.fields]
            added = [f for f in new_ms.fields if f not in old_ms.fields]
            for new_f in added:
                for old_f in removed:
                    if field_signature(old_ms.fields[old_f]) != field_signature(new_ms.fields[new_f]):
                        continue
                    if self.questioner.ask_rename_field(name, old_f, new_f):
                        self._apply("renames", RenameField(name, old_f, new_f))
                        removed.remove(old_f)
                        break

    # ── per-model diffs ──

    def _diff_sets(self, old_ms, new_ms):
        for key, op_cls in _SET_OPS:
            old_v, new_v = old_ms.options[key], new_ms.options[key]
            if old_v == new_v:
                continue
            kept = [cols for cols in old_v if cols in new_v]
            if kept != old_v:
                self.phases["drop_sets"].append(op_cls(new_ms.name, kept))
            if kept != new_v:
                self.phases["add_sets"].append(op_cls(new_ms.name, new_v))

    def _diff_fields(self, old_ms, new_ms):
        name = new_ms.name
        for fname in old_ms.fields:
            if fname not in new_ms.fields:
                self.phases["remove"].append(RemoveField(name, fname))
        for fname, field in new_ms.fields.items():
            if fname not in old_ms.fields:
                self.phases["add"].append(self._add_field(name, fname, field))
                continue
            old = old_ms.fields[fname]
            if field_signature(old) == field_signature(field):
                continue
            backfill = NOT_PROVIDED
            if old.nullable and not field.nullable and not field.primary_key:
                backfill = self.questioner.ask_not_null_alteration(name, fname)
            self.phases["alter"].append(AlterField(name, fname, field, backfill=backfill))

    def _add_field(self, model, fname, field):
        backfill = NOT_PROVIDED
        if not field.nullable and field.default is None:
            backfill = self.questioner.ask_not_null_addition(model, fname)
        return AddField(model, fname, field, backfill=backfill)

    def _diff_table_options(self, old_ms, new_ms):
        changes = {k: new_ms.options[k] for k in ("engine", "charset")
                   if old_ms.options[k] != new_ms.options[k]}
        if changes:
            self.phases["options"].append(AlterTableOptions(
                new_ms.name, engine=new_ms.options["engine"], charset=new_ms.options["charset"]))

    # ── created / deleted models ──

    def _create_models(self, created):
        pending = {n: self.to_state.models[n] for n in created}
        while pending:
            ready = [n for n in sorted(pending)
                     if not (_fk_targets(pending[n]) & (set(pending) - {n}))]
            deferred: list[str] = []
            if not ready:
                # Cycle: create one model without its FKs into the cycle, add them later.
                n = sorted(pending)[0]
                deferred = [f for f, field in pending[n].fields.items()
                            if isinstance(field, ForeignKey) and field.to in pending and field.to != n]
                ready = [n]
            for n in ready:
                ms = pending.pop(n)
                fields = [(f, field) for f, field in ms.fields.items() if f not in deferred]
                kept = {k: [cols for cols in ms.options[k] if not set(cols) & set(deferred)]
                        for k, _ in _SET_OPS}
                options = {**ms.options, **kept}
                self.phases["create"].append(CreateModel(n, fields, table=ms.table, options=options))
                for f in deferred:
                    self.phases["add"].append(AddField(n, f, ms.fields[f]))
                for key, op_cls in _SET_OPS:
                    if kept[key] != ms.options[key]:
                        self.phases["add_sets"].append(op_cls(n, ms.options[key]))
                deferred = []

    def _delete_models(self, deleted):
        pending = {n: self.working.models[n] for n in deleted}
        while pending:
            # A model can go once no other pending model points at it.
            ready = [n for n in sorted(pending)
                     if not any(n in _fk_targets(pending[o]) for o in pending if o != n)]
            if not ready:
                n = sorted(pending)[0]
                for other in sorted(pending):
                    for f, field in pending[other].fields.items():
                        if isinstance(field, ForeignKey) and field.to == n and other != n:
                            self.phases["delete"].append(RemoveField(other, f))
                            pending[other] = pending[other].clone()
                            del pending[other].fields[f]
                ready = [n]
            for n in ready:
                pending.pop(n)
                self.phases["delete"].append(DeleteModel(n))

    # ── safety net ──

    def _verify(self, ops):
        state = self.from_state.clone()
        for op in ops:
            op.state_forwards(state)
        if state != self.to_state:
            diff = sorted(n for n in set(state.models) | set(self.to_state.models)
                          if state.models.get(n) != self.to_state.models.get(n))
            raise MigrationError(
                "Internal error: detected operations do not reproduce the model state "
                f"(differences in: {', '.join(diff)}). Please report this; no file was written."
            )


def _fk_targets(ms) -> set[str]:
    return {f.to for f in ms.fields.values() if isinstance(f, ForeignKey)}


def _same_fields(a, b) -> bool:
    return a.fields.keys() == b.fields.keys() and all(
        field_signature(a.fields[n]) == field_signature(b.fields[n]) for n in a.fields)
