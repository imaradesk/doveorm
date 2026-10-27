"""
DoveORM Model
Base class for all ORM models.

Supports:
  - ``class Meta`` for table name / indexes
  - ``@table()`` decorator as an alternative to ``class Meta``
  - Dynamic finders:  ``User.findByUsername("titus")``
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from ..exceptions import ConcurrencyError, DoesNotExist, ValidationError
from ..fields import Field
from ..query import QuerySet
from .finders import FinderMixin
from .meta import ModelMeta
from .schema import SchemaMixin

# Avoid circular import – Database is only used for type hints and .use()
if TYPE_CHECKING:
    from ..database import Database


class Model(FinderMixin, SchemaMixin, metaclass=ModelMeta):
    """
    Base class for all ORM models.

    Quick reference
    ───────────────
    Defining a model:
        class User(Model):
            name = CharField(max_length=100, nullable=False)
            age  = IntegerField(default=0)

    CRUD:
        user = User.create(name="Titus", age=25)
        user.age = 26
        user.save()
        user.delete()

    Querying:
        User.where(name="Titus").get()
        User.where(age__gte=18).order_by("-name").limit(5).get()
        User.all()
        User.find(1)
    """

    # Populated by ModelMeta
    _fields: dict[str, Field] = {}
    _pk_name: str | None = None
    _db: Database | None = None
    _registry: dict[str, "Model"] = {}
    _table_name_override: str | None = None
    _meta_indexes: list = []
    _meta_unique_together: list = []
    _meta_ordering: list = []
    _meta_engine: str | None = None
    _meta_charset: str | None = None
    _meta_version_field: str | None = None
    _abstract: bool = False
    _migrate: bool = True          # False: the table is not created or changed by migrations

    def __init__(self, **kwargs):
        cls = type(self)
        if cls._abstract or cls is Model:
            raise TypeError(f"Cannot instantiate abstract model {cls.__name__}.")
        fields = cls._fields
        unknown = [k for k in kwargs if k not in fields]
        if unknown:
            raise TypeError(
                f"{cls.__name__}() got unexpected field(s): {', '.join(sorted(unknown))}. "
                f"Valid fields: {', '.join(fields)}"
            )
        object.__setattr__(self, "_persisted", False)
        object.__setattr__(self, "_deferred", set())
        object.__setattr__(self, "_pk_snapshot", None)
        for field_name, field in fields.items():
            if field_name in kwargs:
                value = kwargs[field_name]
            else:
                value = field.get_default()
            object.__setattr__(self, field_name, None if value is None else field.to_python(value))

    def __setattr__(self, name, value):
        cls = type(self)
        if name not in cls._fields and not name.startswith("_"):
            attr = getattr(cls, name, None)
            if not hasattr(attr, "__set__"):
                from ..relationships import RelationshipDescriptor
                hint = (" (relationships are read-only; assign the foreign-key field instead)"
                        if isinstance(attr, RelationshipDescriptor) else "")
                raise AttributeError(f"{cls.__name__} has no field {name!r}{hint}.")
        deferred = self.__dict__.get("_deferred")
        if deferred and name in deferred:
            deferred.discard(name)
        object.__setattr__(self, name, value)

    # ──────────────────────────────────────────────
    # Class-level helpers
    # ──────────────────────────────────────────────

    @classmethod
    def use(cls, db: Database):
        """Attach a database to this model class.

        Each model class holds its own binding; subclasses are not affected.
        """
        cls._db = db

    @classmethod
    def _ensure_db(cls):
        """Ensure a database is attached. Falls back to the global config."""
        if cls._db is None:
            from ..config import get_database, is_configured
            if is_configured():
                cls._db = get_database()
            else:
                raise RuntimeError(
                    f"No database configured for {cls.__name__}. "
                    f"Either call {cls.__name__}.use(db) or use doveorm.configure(...)."
                )
        return cls._db


    @classmethod
    def _pk_field(cls) -> str:
        if cls._pk_name is None:
            raise TypeError(f"{cls.__name__} has no primary key (abstract model?).")
        return cls._pk_name

    @classmethod
    def _get_fields(cls) -> dict[str, Field]:
        return cls._fields

    @classmethod
    def _get_driver(cls):
        """Return the underlying DatabaseDriver."""
        return cls._ensure_db().driver

    @classmethod
    def _ph(cls) -> str:
        """Placeholder used when building SQL.  The ORM always writes ``?``;
        drivers translate it to their native paramstyle."""
        return "?"


    # ──────────────────────────────────────────────
    # Query interface
    # ──────────────────────────────────────────────

    @classmethod
    def where(cls, **kwargs) -> QuerySet:
        """Return a QuerySet filtered by the given kwargs."""
        return QuerySet(cls).where(**kwargs)

    @classmethod
    def filter(cls, *q_objects, **kwargs) -> QuerySet:
        """Return a QuerySet filtered by Q objects and/or keyword lookups."""
        return QuerySet(cls).filter(*q_objects, **kwargs)

    @classmethod
    def exclude(cls, **kwargs) -> QuerySet:
        """Return a QuerySet excluding rows that match the given lookups."""
        return QuerySet(cls).exclude(**kwargs)

    @classmethod
    def all(cls) -> QuerySet:
        """Return a QuerySet for all rows.

        ``Meta.ordering`` / ``@table(ordering=...)`` applies to every query
        that has no explicit ``order_by()``.  A QuerySet obtained from
        ``all()`` may be bulk-updated or deleted without a filter.
        """
        qs = QuerySet(cls)
        qs._allow_unfiltered = True
        return qs

    @classmethod
    def find(cls, pk_value) -> "Model | None":
        """Fetch a single record by primary key (``None`` for a ``None`` key)."""
        if pk_value is None:
            return None
        return cls.where(**{cls._pk_field(): pk_value}).first()

    # ──────────────────────────────────────────────
    # CRUD
    # ──────────────────────────────────────────────

    @classmethod
    def create(cls, **kwargs) -> "Model":
        """INSERT a new row and return the saved instance."""
        instance = cls(**kwargs)
        instance.save()
        return instance

    @classmethod
    def bulk_create(cls, instances) -> list["Model"]:
        """Insert many instances in a single transaction (one commit)."""
        instances = list(instances)
        for inst in instances:
            if not isinstance(inst, cls):
                raise TypeError(f"bulk_create() expects {cls.__name__} instances, got {type(inst).__name__}.")
        with cls._ensure_db().transaction():
            for inst in instances:
                inst.save()
        return instances

    @classmethod
    def get_or_create(cls, defaults: dict = None, **kwargs) -> tuple["Model", bool]:
        """
        Fetch the first row matching kwargs, or create it.
        Returns (instance, created_bool).

        Safe under concurrency when a UNIQUE constraint covers *kwargs*:
        if another writer wins the race, the existing row is returned.
        """
        instance = cls.where(**kwargs).first()
        if instance:
            return instance, False
        create_kwargs = {**kwargs, **(defaults or {})}
        db = cls._ensure_db()
        try:
            with db.transaction():
                return cls.create(**create_kwargs), True
        except db.integrity_errors:
            instance = cls.where(**kwargs).first()
            if instance:
                return instance, False
            raise

    @classmethod
    def from_input(cls, data: Mapping, *, allowed: Iterable[str] | None = None,
                   ignore_unknown: bool = False) -> "Model":
        """Build an unsaved instance from untrusted input (e.g. a request body).

        See :meth:`assign` for the mass-assignment rules.
        """
        return cls().assign(data, allowed=allowed, ignore_unknown=ignore_unknown)

    def assign(self, data: Mapping, *, allowed: Iterable[str] | None = None,
               ignore_unknown: bool = False) -> "Model":
        """Copy values from untrusted *data* onto this instance, safely.

        Only *allowed* fields may be set (default: every ``editable`` field —
        never the primary key, the version field or ``auto_now`` fields).
        Any other key raises :class:`ValidationError` unless
        ``ignore_unknown=True``, in which case it is skipped.  Values are
        validated when the instance is saved.
        """
        cls = type(self)
        fields = cls._fields
        if not isinstance(data, Mapping):
            raise TypeError(f"assign() expects a mapping, got {type(data).__name__}.")
        if allowed is None:
            permitted = {n for n, f in fields.items() if f.editable}
        else:
            if isinstance(allowed, str):
                raise TypeError("allowed must be an iterable of field names, not a string.")
            permitted = set(allowed)
            unknown = permitted - set(fields)
            if unknown:
                raise ValueError(f"allowed names unknown field(s): {', '.join(sorted(unknown))}.")
            protected = permitted & {cls._pk_field(), cls._meta_version_field}
            if protected:
                raise ValueError(f"{', '.join(sorted(protected))} cannot be mass-assigned.")
        rejected = [k for k in data if k not in permitted]
        if rejected and not ignore_unknown:
            shown = ", ".join(repr(str(k))[:40] for k in rejected[:10])
            raise ValidationError(f"{cls.__name__}: field(s) not assignable: {shown}.")
        for key, value in data.items():
            if key in permitted:
                setattr(self, key, None if value is None else fields[key].to_python(value))
        return self

    def clean(self) -> None:
        """Hook for cross-field validation; override and raise ValidationError.

        Called by :meth:`full_clean` and before every :meth:`save`.
        """

    def full_clean(self) -> None:
        """Validate every field, then run :meth:`clean`; raises ValidationError / TypeError."""
        for name, field in type(self)._fields.items():
            if field.primary_key and field.auto_increment and getattr(self, name) is None:
                continue
            object.__setattr__(self, name, field.validate(getattr(self, name)))
        self.clean()

    def _clean_for_db(self, name: str):
        """Validate one field, store the normalised value, return its DB form."""
        field = type(self)._fields[name]
        value = field.validate(getattr(self, name))
        object.__setattr__(self, name, value)
        return field.to_db(value)

    def save(self, *, update_fields: Iterable[str] | None = None) -> "Model":
        """INSERT or UPDATE depending on whether the instance is persisted.

        ``update_fields`` limits an UPDATE to the named columns.  With
        ``Meta.version_field`` set, an UPDATE only succeeds if the row's
        version still matches; otherwise :class:`ConcurrencyError` is raised.
        """
        cls = type(self)
        db = cls._ensure_db()
        fields = cls._fields
        pk = cls._pk_field()
        pk_field = fields[pk]
        version = cls._meta_version_field
        add = not self._persisted

        if update_fields is not None:
            if add:
                raise ValueError("update_fields cannot be used when inserting a new row.")
            if isinstance(update_fields, str):
                raise TypeError("update_fields must be an iterable of field names, not a string.")
            update_fields = list(dict.fromkeys(update_fields))
            for name in update_fields:
                if name not in fields:
                    raise ValueError(f"{cls.__name__} has no field {name!r}.")
                if name in (pk, version):
                    raise ValueError(f"{name!r} cannot be listed in update_fields.")

        for name, field in fields.items():
            if update_fields is None or name in update_fields:
                field.pre_save(self, add)
        self.clean()

        pk_val = getattr(self, pk)
        if add:
            if version is not None and getattr(self, version) is None:
                object.__setattr__(self, version, 1)
            if pk_val is None:
                if not pk_field.auto_increment:
                    raise ValidationError(
                        f"{cls.__name__}.{pk} must be set before saving "
                        "(it is not auto-incrementing)."
                    )
                col_names = [n for n in fields if n != pk]
            else:
                col_names = list(fields)        # explicit PK value is inserted as-is
            values = [self._clean_for_db(n) for n in col_names]
            qn = db.driver.quote_name
            table = qn(cls._table_name())
            if col_names:
                sql = (f"INSERT INTO {table} ({', '.join(qn(n) for n in col_names)}) "
                       f"VALUES ({', '.join('?' for _ in col_names)})")
            elif db.driver_name == "mysql":
                sql = f"INSERT INTO {table} () VALUES ()"
            else:
                sql = f"INSERT INTO {table} DEFAULT VALUES"
            if pk_val is None:
                sql += db.driver.returning_sql(pk)
            new_id = db.execute_write(sql, values)
            if pk_val is None:
                object.__setattr__(self, pk, pk_field.from_db(new_id))
            elif pk_field.auto_increment:
                db.driver.sync_auto_increment(cls._table_name(), pk, pk_field.to_db(pk_val))
            object.__setattr__(self, "_persisted", True)
            self._deferred.clear()
        else:
            original_pk = self._pk_snapshot if self._pk_snapshot is not None else pk_val
            if original_pk is None:
                raise ValidationError(f"Cannot update {cls.__name__}: primary key is None.")
            if update_fields is not None:
                col_names = list(update_fields)
            else:
                col_names = [n for n in fields
                             if n not in self._deferred and n != version
                             and (n != pk or pk_val != original_pk)]
            if col_names:
                qn = db.driver.quote_name
                values = [self._clean_for_db(n) for n in col_names]
                where = f"{qn(pk)} = ?"
                where_params = [pk_field.to_db(original_pk)]
                new_version = None
                if version is not None:
                    current = getattr(self, version)
                    if current is None or version in self._deferred:
                        raise ValidationError(
                            f"{cls.__name__}.{version} (version field) is not loaded; cannot save safely."
                        )
                    new_version = current + 1
                    col_names.append(version)
                    values.append(fields[version].to_db(fields[version].validate(new_version)))
                    where += f" AND {qn(version)} = ?"
                    where_params.append(fields[version].to_db(current))
                set_clause = ", ".join(f"{qn(n)} = ?" for n in col_names)
                sql = f"UPDATE {qn(cls._table_name())} SET {set_clause} WHERE {where}"
                matched = db.execute_write(sql, values + where_params)
                if matched == 0:
                    self._raise_missing_or_conflict(original_pk)
                if new_version is not None:
                    object.__setattr__(self, version, new_version)
        object.__setattr__(self, "_pk_snapshot", getattr(self, pk))
        return self

    def _raise_missing_or_conflict(self, pk_val):
        cls = type(self)
        pk = cls._pk_field()
        if cls._meta_version_field is not None and cls.where(**{pk: pk_val}).exists():
            raise ConcurrencyError(
                f"{cls.__name__} with {pk}={pk_val!r} was modified by another writer; "
                "reload it and retry."
            )
        raise DoesNotExist(f"{cls.__name__} with {pk}={pk_val!r} no longer exists in the database.")

    def delete(self) -> int:
        """DELETE this row from the database.  Returns the number of rows deleted."""
        cls = type(self)
        pk = cls._pk_field()
        pk_val = self._pk_snapshot if self._pk_snapshot is not None else getattr(self, pk)
        if pk_val is None:
            raise ValueError(f"Cannot delete an unsaved {cls.__name__}.")
        db = cls._ensure_db()
        qn = db.driver.quote_name
        sql = f"DELETE FROM {qn(cls._table_name())} WHERE {qn(pk)} = ?"
        params = [cls._fields[pk].to_db(pk_val)]
        version = cls._meta_version_field
        checked = (version is not None and version not in self._deferred
                   and getattr(self, version) is not None)
        if checked:
            sql += f" AND {qn(version)} = ?"
            params.append(cls._fields[version].to_db(getattr(self, version)))
        deleted = db.execute_write(sql, params)
        if deleted == 0 and checked:
            self._raise_missing_or_conflict(pk_val)
        object.__setattr__(self, pk, None)
        object.__setattr__(self, "_persisted", False)
        object.__setattr__(self, "_pk_snapshot", None)
        return deleted

    def refresh(self) -> "Model":
        """Reload this instance's data from the database."""
        cls = type(self)
        pk = cls._pk_field()
        pk_val = self._pk_snapshot if self._pk_snapshot is not None else getattr(self, pk)
        fresh = cls.find(pk_val) if pk_val is not None else None
        if fresh is None:
            raise DoesNotExist(
                f"{cls.__name__} with {pk}={pk_val!r} no longer exists in the database."
            )
        for name in cls._fields:
            object.__setattr__(self, name, getattr(fresh, name))
        object.__setattr__(self, "_persisted", True)
        object.__setattr__(self, "_pk_snapshot", getattr(fresh, pk))
        self._deferred.clear()
        self.__dict__.pop("_prefetch_cache", None)
        self.__dict__.pop("_relation_cache", None)
        return self

    def to_dict(self, *, fields: Iterable[str] | None = None,
                include_sensitive: bool = False) -> dict:
        """Serialize this instance to a plain dict.

        Fields declared ``sensitive=True`` (password hashes, tokens …) are
        omitted unless ``include_sensitive=True``.
        """
        cls_fields = type(self)._fields
        names = list(cls_fields) if fields is None else list(fields)
        unknown = [n for n in names if n not in cls_fields]
        if unknown:
            raise ValueError(f"{type(self).__name__} has no field(s): {', '.join(unknown)}.")
        return {
            name: getattr(self, name)
            for name in names
            if include_sensitive or not cls_fields[name].sensitive
        }

    # ──────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────

    @classmethod
    def _from_row(cls, row) -> "Model":
        instance = cls.__new__(cls)
        object.__setattr__(instance, "_persisted", True)
        row_keys = set(row.keys())
        deferred = set()
        for name, field in cls._fields.items():
            if name in row_keys:
                value = field.from_db(row[name])
            else:
                # Column wasn't selected — use field default; never written back by save()
                default = field.get_default()
                value = None if default is None else field.to_python(default)
                deferred.add(name)
            object.__setattr__(instance, name, value)
        object.__setattr__(instance, "_deferred", deferred)
        object.__setattr__(instance, "_pk_snapshot", getattr(instance, cls._pk_field()))
        return instance

    def __repr__(self):
        pk = type(self)._pk_field()
        return f"<{type(self).__name__} {pk}={getattr(self, pk)!r}>"

    def __eq__(self, other):
        if not isinstance(other, Model):
            return NotImplemented
        if type(self) is not type(other):
            return False
        pk = type(self)._pk_field()
        mine = getattr(self, pk)
        if mine is None:
            return self is other
        return mine == getattr(other, pk)

    def __hash__(self):
        pk = type(self)._pk_field()
        pk_val = getattr(self, pk)
        if pk_val is None:
            raise TypeError("Model instances without a primary key value are unhashable.")
        return hash((type(self).__name__, pk_val))
