"""Dependency graph of migrations."""
from __future__ import annotations

import heapq

from ..exceptions import MigrationError


class MigrationGraph:
    def __init__(self):
        self.nodes: dict = {}
        self.parents: dict[str, set[str]] = {}
        self.children: dict[str, set[str]] = {}
        self._order: list[str] | None = None

    def add_node(self, migration) -> None:
        self.nodes[migration.name] = migration
        self.parents.setdefault(migration.name, set())
        self.children.setdefault(migration.name, set())
        self._order = None

    def add_dependency(self, child: str, parent: str) -> None:
        if parent not in self.nodes:
            raise MigrationError(f"Migration {child} depends on unknown migration {parent!r}.")
        self.parents[child].add(parent)
        self.children[parent].add(child)
        self._order = None

    def remove_node(self, name: str) -> None:
        for p in self.parents.pop(name, ()):
            self.children[p].discard(name)
        for c in self.children.pop(name, ()):
            self.parents[c].discard(name)
        self.nodes.pop(name, None)
        self._order = None

    def order(self) -> list[str]:
        """All migrations, dependencies first (ties broken by name)."""
        if self._order is None:
            indegree = {n: len(p) for n, p in self.parents.items()}
            heap = [n for n, d in indegree.items() if d == 0]
            heapq.heapify(heap)
            out = []
            while heap:
                n = heapq.heappop(heap)
                out.append(n)
                for c in self.children[n]:
                    indegree[c] -= 1
                    if indegree[c] == 0:
                        heapq.heappush(heap, c)
            if len(out) != len(self.nodes):
                cycle = sorted(n for n, d in indegree.items() if d > 0)
                raise MigrationError(f"Circular migration dependencies among: {', '.join(cycle)}.")
            self._order = out
        return self._order

    def leaves(self) -> list[str]:
        return sorted(n for n, c in self.children.items() if not c)

    def _walk(self, start: str, edges) -> set[str]:
        seen, stack = set(), [start]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(edges[n])
        return seen

    def ancestors(self, name: str) -> set[str]:
        """*name* and everything it depends on."""
        return self._walk(name, self.parents)

    def descendants(self, name: str) -> set[str]:
        """*name* and everything that depends on it."""
        return self._walk(name, self.children)

    def __contains__(self, name):
        return name in self.nodes

    def __len__(self):
        return len(self.nodes)
