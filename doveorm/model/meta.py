"""``ModelMeta`` – the metaclass that turns a class body into a model."""
from __future__ import annotations

import threading
import warnings
from typing import TYPE_CHECKING

from ..fields import AutoField, Field
from ..utils.sql import validate_identifier

if TYPE_CHECKING:
    from .base import Model

# Module that defines the root ``Model`` class (which is not registered).
_ROOT_MODULE = __name__.rpartition(".")[0] + ".base"


class ModelMeta(type):
    """
    Metaclass that:
    1. Collects Field instances declared on the class body.
    2. Injects an ``id = AutoField()`` if no primary key was declared.
    3. Registers every concrete Model subclass in a global registry.
    4. Validates table options (names, indexes, ordering).
    5. Provides dynamic ``findBy<Field>`` methods.
    """
    _registry: dict[str, "Model"] = {}
    _registry_lock = threading.RLock()
    # Public attribute names of the root Model (methods etc.); fields may not shadow them.
    _reserved_names: frozenset = frozenset()

    def __new__(mcs, name, bases, namespace, **kwargs):
        is_root = name == "Model" and namespace.get("__module__") == _ROOT_MODULE
        meta = namespace.get("Meta")
        _abstract = bool(getattr(meta, "abstract", False))
        _migrate = bool(getattr(meta, "migrate", True))

        fields: dict[str, Field] = {}

        # Inherit fields from base models
        for base in bases:
            if hasattr(base, "_fields"):
                fields.update(base._fields)

        # Collect fields declared in this class body
        for attr_name, value in list(namespace.items()):
            if isinstance(value, Field):
                if attr_name.startswith("_"):
                    raise TypeError(
                        f"Model '{name}': field name {attr_name!r} is invalid; "
                        "names starting with '_' are reserved for ORM internals."
                    )
                if attr_name in mcs._reserved_names:
                    raise TypeError(
                        f"Model '{name}': field name {attr_name!r} would shadow "
                        f"Model.{attr_name}; choose another name."
                    )
                validate_identifier(attr_name, "field name")
                value.name = attr_name
                fields[attr_name] = value

        pk_names = [n for n, f in fields.items() if f.primary_key]
        if len(pk_names) > 1:
            raise TypeError(f"Model '{name}' declares more than one primary key: {', '.join(pk_names)}.")
        if not pk_names and not is_root and not _abstract:
            if "id" in fields:
                raise TypeError(
                    f"Model '{name}' has a non-primary-key field named 'id'; "
                    "declare a primary key explicitly."
                )
            auto = AutoField()
            auto.name = "id"
            fields = {"id": auto, **fields}
            namespace["id"] = auto
            pk_names = ["id"]

        namespace["_fields"] = fields
        namespace["_pk_name"] = pk_names[0] if pk_names else None
        namespace["_db"] = None          # set later via Model.use(db)
        namespace["_registry"] = mcs._registry
        namespace["_abstract"] = _abstract
        namespace["_migrate"] = _migrate
        namespace["_finder_cache"] = {}

        # @table() overrides Meta if both are present (applied by the decorator)
        namespace["_table_name_override"] = getattr(meta, "table_name", None) if meta else None
        namespace["_meta_indexes"] = list(getattr(meta, "indexes", []) or []) if meta else []
        namespace["_meta_unique_together"] = list(getattr(meta, "unique_together", []) or []) if meta else []
        namespace["_meta_ordering"] = list(getattr(meta, "ordering", []) or []) if meta else []
        namespace["_meta_engine"] = getattr(meta, "engine", None) if meta else None
        namespace["_meta_charset"] = getattr(meta, "charset", None) if meta else None
        namespace["_meta_version_field"] = getattr(meta, "version_field", None) if meta else None

        cls = super().__new__(mcs, name, bases, namespace, **kwargs)

        if is_root:
            mcs._reserved_names = frozenset(n for n in dir(cls) if not n.startswith("_"))

        if not is_root and not _abstract:
            cls._validate_options()
            with mcs._registry_lock:
                existing = mcs._registry.get(name)
                if existing is not None and existing.__module__ != cls.__module__:
                    warnings.warn(
                        f"Model name {name!r} is already registered by "
                        f"{existing.__module__}.{existing.__qualname__}; string references "
                        f"to {name!r} now resolve to {cls.__module__}.{cls.__qualname__}.",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                mcs._registry[name] = cls
            # Auto-wire to the configured database if available
            from ..config import is_configured, get_database
            if is_configured() and cls._db is None:
                cls._db = get_database()

        return cls

    # ──────────────────────────────────────────
    # Dynamic findBy<Field>
    # ──────────────────────────────────────────

    def __getattr__(cls, name: str):
        """
        Intercept attribute access on the *class* to support dynamic finders.

        Patterns supported::

            User.findByUsername("titus")            → first match or None
            User.findAllByActive(True)              → list of all matches
            User.findByAuthorIdAndPublished(1, True) → first with both
            User.findAllByActiveAndAge(True, 28)    → list with both
            User.countByActive(True)                → int count
            User.existsByEmail("x@y.com")           → bool

        The field name portion uses CamelCase which is auto-converted to
        snake_case to match actual model field names::

            findByAuthorId  → author_id
            findByCreatedAt → created_at
        """
        if name.startswith("_"):
            raise AttributeError(f"type object {cls.__name__!r} has no attribute {name!r}")

        cache = cls.__dict__.get("_finder_cache")
        if cache is not None and name in cache:
            return cache[name]

        if name.startswith("findAllBy"):
            fn = cls._make_dynamic_finder(name[9:], many=True)
        elif name.startswith("findBy"):
            fn = cls._make_dynamic_finder(name[6:], many=False)
        elif name.startswith("countBy"):
            fn = cls._make_dynamic_counter(name[7:])
        elif name.startswith("existsBy"):
            fn = cls._make_dynamic_exists(name[8:])
        else:
            raise AttributeError(
                f"type object {cls.__name__!r} has no attribute {name!r}"
            )
        if cache is not None:
            cache[name] = fn
        return fn
