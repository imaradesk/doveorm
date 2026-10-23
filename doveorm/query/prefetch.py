"""Eager loading for ``QuerySet.prefetch_related()`` (avoids N+1 queries)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .utils import _chunks

if TYPE_CHECKING:
    from ..model import Model


class PrefetchMixin:
    """Batch-loads relationships in chunked ``IN (...)`` queries."""

    @classmethod
    def _fetch_in(cls, model_cls, col: str, values: list) -> list:
        results = []
        for chunk in _chunks(values):
            results.extend(cls(model_cls).where(**{f"{col}__in": chunk}).get())
        return results

    def _do_prefetch(self, instances: list["Model"]) -> None:
        """Batch-load relationships and attach to a _prefetch_cache on each instance."""
        from ..relationships import HasMany, HasOne, BelongsTo, ManyToMany

        model = self._model
        pk_field = model._pk_field()
        pk_values = list(dict.fromkeys(
            getattr(inst, pk_field) for inst in instances if getattr(inst, pk_field) is not None
        ))

        # Initialise cache on every instance
        for inst in instances:
            if getattr(inst, "_prefetch_cache", None) is None:
                object.__setattr__(inst, "_prefetch_cache", {})

        for rel_name in self._prefetch:
            descriptor = getattr(model, rel_name)

            if isinstance(descriptor, (HasMany, HasOne)):
                related_cls, fk = descriptor.resolve(model)
                related_rows = self._fetch_in(related_cls, fk, pk_values) if pk_values else []
                if isinstance(descriptor, HasMany):
                    grouped: dict[Any, list] = {pk: [] for pk in pk_values}
                    for row in related_rows:
                        fk_val = getattr(row, fk)
                        if fk_val in grouped:
                            grouped[fk_val].append(row)
                    for inst in instances:
                        inst._prefetch_cache[rel_name] = list(grouped.get(getattr(inst, pk_field), []))
                else:
                    fk_map = {getattr(row, fk): row for row in related_rows}
                    for inst in instances:
                        inst._prefetch_cache[rel_name] = fk_map.get(getattr(inst, pk_field))

            elif isinstance(descriptor, BelongsTo):
                related_cls, fk = descriptor.resolve(model)
                fk_values = list(dict.fromkeys(
                    getattr(inst, fk) for inst in instances if getattr(inst, fk, None) is not None
                ))
                related_pk = related_cls._pk_field()
                related_map = (
                    {getattr(r, related_pk): r for r in self._fetch_in(related_cls, related_pk, fk_values)}
                    if fk_values else {}
                )
                for inst in instances:
                    inst._prefetch_cache[rel_name] = related_map.get(getattr(inst, fk, None))

            elif isinstance(descriptor, ManyToMany):
                related_cls, through, src_fk, tgt_fk = descriptor.resolve(model)
                src_field = model._fields[pk_field]
                related_pk = related_cls._pk_field()
                tgt_field = related_cls._fields[related_pk]
                db = model._ensure_db()

                pairs: list[tuple[Any, Any]] = []
                for chunk in _chunks(pk_values):
                    placeholders = ", ".join("?" for _ in chunk)
                    join_rows = db.execute(
                        f"SELECT {src_fk}, {tgt_fk} FROM {through} WHERE {src_fk} IN ({placeholders})",
                        [src_field.to_db(v) for v in chunk],
                    )
                    pairs.extend(
                        (src_field.from_db(row[src_fk]), tgt_field.from_db(row[tgt_fk]))
                        for row in join_rows
                    )

                target_ids = list(dict.fromkeys(t for _, t in pairs))
                related_map = (
                    {getattr(r, related_pk): r for r in self._fetch_in(related_cls, related_pk, target_ids)}
                    if target_ids else {}
                )
                grouped = {pk: [] for pk in pk_values}
                for src, tgt in pairs:
                    obj = related_map.get(tgt)
                    if obj is not None and src in grouped:
                        grouped[src].append(obj)
                for inst in instances:
                    inst._prefetch_cache[rel_name] = list(grouped.get(getattr(inst, pk_field), []))
