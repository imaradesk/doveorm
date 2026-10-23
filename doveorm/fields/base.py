"""
DoveORM Fields – the ``Field`` base class.

Value pipeline
--------------
* ``to_python(value)`` – coerce a value assigned by user code
  (``Model(field=value)``) into the field's Python type.
* ``validate(value)``  – check the value; raise on bad input.
* ``to_db(value)``     – convert a Python value into a DB parameter.
* ``from_db(value)``   – convert a DB value back into Python.
* ``pre_save(instance, add)`` – hook run by ``Model.save()`` (auto_now …).
"""
from __future__ import annotations

import copy
import functools
import inspect
from typing import TYPE_CHECKING

from ..exceptions import ValidationError
from ..utils.sql import quote_literal

if TYPE_CHECKING:
    from ..drivers.base import DatabaseDriver


# Constructor arguments with no effect on the database schema.  They are left
# out of migration files, so changing them never produces a migration.
NON_SCHEMA_ARGS = frozenset({
    "verbose_name", "help_text", "choices", "sensitive", "editable",
    "related_name", "upload_to", "width_field", "height_field",
    "path", "match", "recursive", "protocol", "use_tz",
    "auto_now", "auto_now_add",
})


@functools.lru_cache(maxsize=None)
def _init_signature(cls) -> tuple[tuple[str, ...], dict]:
    """(positional parameter names, merged keyword defaults) for ``cls.__init__``.

    Defaults are merged along the MRO so arguments forwarded through
    ``**kwargs`` (e.g. ``nullable``) are known too; subclasses win.
    """
    defaults: dict = {}
    for klass in reversed(cls.__mro__):
        init = klass.__dict__.get("__init__")
        if init is None or not issubclass(klass, Field):
            continue
        for p in inspect.signature(init).parameters.values():
            if p.default is not p.empty and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
                defaults[p.name] = p.default
    params = list(inspect.signature(cls.__init__).parameters.values())[1:]   # skip self
    positional = tuple(p.name for p in params
                       if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
    return positional, defaults


def _same(a, b) -> bool:
    # 1 == True and 0 == 0.0 in Python; a schema change from 1 to True is still a change
    return type(a) is type(b) and a == b


class Field:
    """Base class for all field types."""

    def __new__(cls, *args, **kwargs):
        # Remember the constructor call so migrations can rebuild the field.
        obj = super().__new__(cls)
        obj._init_args = (args, dict(kwargs))
        return obj

    def __init__(self, primary_key=False, nullable=True, default=None, unique=False,
                 db_index=False, verbose_name=None, help_text="", choices=None,
                 sensitive=False, editable=True):
        self.primary_key = primary_key
        self.nullable = nullable
        self.default = default
        self.unique = unique
        self.db_index = db_index
        self.verbose_name = verbose_name
        self.help_text = help_text
        self.choices = list(choices) if choices is not None else None  # (db_value, display) tuples
        # sensitive: excluded from Model.to_dict() and never echoed in error messages
        self.sensitive = bool(sensitive)
        # editable: may be set through Model.assign() / Model.from_input()
        self.editable = bool(editable) and not primary_key
        self.name = None                # set by ModelMeta when class is built

    def __repr__(self):
        return f"<{type(self).__name__} name={self.name!r}>"

    def deconstruct(self) -> tuple[type, dict]:
        """Return ``(field class, kwargs)`` describing this field's *schema*.

        Only arguments that affect the database are kept (see
        ``NON_SCHEMA_ARGS``), arguments equal to their default are dropped,
        callable defaults are dropped (they never reach the DDL) and a
        model class passed as ``to`` is replaced by its name.
        ``cls(**kwargs)`` rebuilds an equivalent column definition.
        """
        args, kwargs = getattr(self, "_init_args", ((), {}))
        positional, defaults = _init_signature(type(self))
        explicit = dict(zip(positional, args))
        explicit.update(kwargs)
        out = {}
        for key, value in explicit.items():
            if key in NON_SCHEMA_ARGS:
                continue
            if key == "default" and (callable(value) or not self._default_is_schema(value)):
                continue
            if key == "to" and isinstance(value, type):
                value = value.__name__
            if key in defaults and _same(defaults[key], value):
                continue
            out[key] = value
        return type(self), out

    def _default_is_schema(self, value) -> bool:
        """True if *value* can be rendered as a DDL ``DEFAULT`` literal."""
        if value is None:
            return False
        try:
            return quote_literal(self.to_db(value)) is not None
        except Exception:
            return False

    def clone(self) -> "Field":
        """A fresh, unbound field with the same schema (see :meth:`deconstruct`)."""
        cls, kwargs = self.deconstruct()
        field = cls(**kwargs)
        field.name = self.name
        return field

    def _shown(self, value) -> str:
        """Representation of *value* for error messages (redacted if sensitive)."""
        return "<redacted>" if self.sensitive else repr(value)

    def sql_type(self) -> str:
        """Return the *generic* DoveORM type name (e.g. ``INTEGER``).

        The actual backend type is resolved via
        ``driver.sql_type_map()[self.sql_type()]`` at DDL time.
        """
        raise NotImplementedError

    def _resolved_type(self, driver: "DatabaseDriver | None" = None) -> str:
        """Resolve the generic type into a backend-specific type string."""
        generic = self.sql_type()
        if driver is not None:
            return driver.sql_type_map().get(generic, generic)
        return generic

    @property
    def auto_increment(self) -> bool:
        """Return True if this field is an auto-incrementing primary key."""
        return False

    def get_default(self):
        """Return a fresh default value (callables are called, mutables copied)."""
        default = self.default
        if callable(default):
            return default()
        if isinstance(default, (list, dict, set)):
            return copy.deepcopy(default)
        return default

    def _default_sql(self, driver: "DatabaseDriver | None", sql_type: str) -> str | None:
        """Render the DDL DEFAULT literal, or None if it should be omitted."""
        if self.default is None or callable(self.default):
            return None
        if driver is not None and not driver.allows_default_for(sql_type):
            return None
        db_val = self.to_db(self.default)
        return quote_literal(
            db_val,
            backslash_escapes=bool(driver is not None and driver.backslash_escapes),
        )

    def sql_definition(self, driver: "DatabaseDriver | None" = None, *,
                       sql_type: str | None = None, inline_unique: bool = True,
                       inline_primary_key: bool = True) -> str:
        """Return the column definition fragment for CREATE TABLE.

        *sql_type* overrides the resolved column type (used by migrations
        for foreign keys).  ``inline_unique=False`` leaves out ``UNIQUE`` and
        ``inline_primary_key=False`` leaves out ``PRIMARY KEY`` (keeping
        ``NOT NULL`` / ``AUTO_INCREMENT``) — both for ``ALTER … MODIFY``.
        """
        if sql_type is None:
            sql_type = self._resolved_type(driver)
        parts = [sql_type]

        if self.primary_key and not inline_primary_key:
            parts.append("NOT NULL")
            if self.auto_increment and driver is not None and driver.name == "mysql":
                parts.append(driver.autoincrement_sql)
        elif self.primary_key:
            if self.auto_increment:
                # Only auto-increment integer PKs get AUTOINCREMENT
                if driver is not None:
                    parts.append(driver.autoincrement_definition())
                else:
                    parts.append("PRIMARY KEY AUTOINCREMENT")
            else:
                parts.append("PRIMARY KEY")
        if not self.nullable and not self.primary_key:
            parts.append("NOT NULL")
        if self.unique and not self.primary_key and inline_unique:
            parts.append("UNIQUE")
        default_sql = self._default_sql(driver, sql_type)
        if default_sql is not None:
            parts.append(f"DEFAULT {default_sql}")
        return " ".join(parts)

    def validate(self, value):
        if value is None:
            if not self.nullable:
                raise ValidationError(f"Field '{self.name}' cannot be null.")
            return None
        if self.choices:
            valid_values = [c[0] for c in self.choices]
            if value not in valid_values:
                raise ValidationError(
                    f"Field '{self.name}': {self._shown(value)} is not a valid choice. "
                    f"Valid: {valid_values}"
                )
        return value

    def to_python(self, value):
        return self.from_db(value)

    def to_db(self, value):
        return value

    def from_db(self, value):
        return value

    def pre_save(self, instance, add: bool):
        """Return the value to persist for this field (may update *instance*)."""
        return getattr(instance, self.name)
