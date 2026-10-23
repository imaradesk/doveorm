"""
DoveORM Relationships
Descriptors that provide lazy-loaded related objects on model instances.
Supports eager loading via _prefetch_cache (populated by QuerySet.prefetch_related).

Caching
-------
* ``BelongsTo`` caches the loaded object per instance and re-queries only
  when the foreign-key value changes.
* ``HasMany`` / ``HasOne`` query on every access (the related rows can
  change at any time); use ``prefetch_related()`` to avoid N+1 queries.
* ``ManyToMany`` returns a manager that loads lazily on first use.
"""
from .base import RelationshipDescriptor
from .descriptors import BelongsTo, HasMany, HasOne
from .many_to_many import ManyToMany, ManyToManyManager

__all__ = ["RelationshipDescriptor", "HasMany", "HasOne", "BelongsTo", "ManyToMany", "ManyToManyManager"]
