"""Shrink a list of operations without changing its effect.

Examples: ``CreateModel`` + ``AddField`` on the same model becomes one
``CreateModel``; ``AddField`` followed by ``RemoveField`` disappears;
consecutive ``AlterField``s collapse to the last one.

Two operations A … B are merged only if one can be moved next to the
other: either A is independent of every operation in between (the result
takes B's place) or B is (the result takes A's place).  Independent means
no shared model writes, no foreign key onto a model whose identity or
primary key changes, and no ``RunSQL`` / ``RunPython`` barrier — so the
optimized list always has the same effect.  Used by ``makemigrations``
and ``squashmigrations``.
"""
from __future__ import annotations

from .operations.base import Operation


def optimize(operations: list[Operation]) -> list[Operation]:
    ops = list(operations)
    while True:
        result = _optimize_once(ops)
        if result is None:
            return ops
        ops = result


def _optimize_once(ops: list[Operation]) -> list[Operation] | None:
    for i, op in enumerate(ops):
        right = True          # can op still move right past everything so far?
        for j in range(i + 1, len(ops)):
            other = ops[j]
            reduced = op.reduce(other)
            if reduced is not None:
                between = ops[i + 1:j]
                if right:
                    # op moves right, past ops it is independent of.
                    return ops[:i] + between + reduced + ops[j + 1:]
                if all(other.can_pass(b) for b in between):
                    # other moves left instead.
                    return ops[:i] + reduced + between + ops[j + 1:]
            if right and not op.can_pass(other):
                right = False
    return None
