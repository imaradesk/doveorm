"""Base class for relationship descriptors."""
from __future__ import annotations

from ..utils.sql import validate_identifier


class RelationshipDescriptor:
    """Base descriptor; resolved lazily when accessed on an instance."""

    attr_name: str | None = None

    def __set_name__(self, owner, name):
        self.attr_name = name
        # Register on the owning class so Model knows about it.  Copy the
        # inherited mapping so subclasses never mutate their parent's dict.
        if "_relationships" not in owner.__dict__:
            owner._relationships = dict(getattr(owner, "_relationships", None) or {})
        owner._relationships[name] = self

    @staticmethod
    def _resolve_model(model_ref, registry):
        if isinstance(model_ref, str):
            try:
                return registry[model_ref]
            except KeyError:
                raise LookupError(f"Related model {model_ref!r} is not defined.") from None
        return model_ref

    @staticmethod
    def _require_field(model, name: str, role: str) -> str:
        validate_identifier(name, role)
        if name not in model._fields:
            raise ValueError(f"{model.__name__} has no field {name!r} (expected {role}).")
        return name

    def _check_prefetch(self, instance) -> tuple[bool, object]:
        """Return (found, value) from the prefetch cache, if available."""
        cache = getattr(instance, "_prefetch_cache", None)
        if cache is not None and self.attr_name in cache:
            return True, cache[self.attr_name]
        return False, None
