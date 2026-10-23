"""
DoveORM QuerySet
The chainable query builder returned by Model.where(), .all(), etc.

Security model
--------------
* Column names come only from the model's declared fields (validated).
* All values are bound parameters.
* LIMIT / OFFSET are validated non-negative integers.
* Bulk ``update()`` / ``delete()`` without a WHERE clause are refused
  unless the QuerySet came from ``Model.all()``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..exceptions import UnsafeQueryError
from .compiler import SQLCompilerMixin
from .lookups import LOOKUPS, _SAFE_COL_RE
from .prefetch import PrefetchMixin
from .q import Q
from .utils import _check_non_negative_int

if TYPE_CHECKING:
    from ..model import Model


class QuerySet(SQLCompilerMixin, PrefetchMixin):
    """
    Lazy, chainable query builder.

    Every method returns a *new* QuerySet so calls can be chained:

        User.where(active=True).order_by("name").limit(10).get()

    Results are cached once evaluated by iteration, ``len()``, ``bool()``
    or indexing.  ``get()`` always runs the query and refreshes the cache.
    """

    def __init__(self, model_class):
        if getattr(model_class, "_abstract", False) or getattr(model_class, "_pk_name", None) is None:
            raise TypeError(f"Cannot query abstract model {model_class.__name__}.")
        self._model = model_class
        self._filters: list[tuple[str, str, Any]] = []   # (col, op, value)
        self._q_filters: list[Q] = []
        self._order: list[tuple[str, bool]] = []         # (col, descending)
        self._limit_val: int | None = None
        self._offset_val: int | None = None
        self._select_cols: list[str] | None = None
        self._prefetch: list[str] = []
        self._allow_unfiltered = False
        self._result_cache: list | None = None

    # ──────────────────────────────────────────────
    # Validation helpers
    # ──────────────────────────────────────────────

    def _validate_column(self, col: str) -> None:
        """Raise ValueError if col is not a safe identifier or known field."""
        if not isinstance(col, str) or not _SAFE_COL_RE.match(col):
            raise ValueError(
                f"Invalid column name: {col!r}. "
                "Column names must be alphanumeric identifiers."
            )
        known = self._model._fields
        if col not in known:
            raise ValueError(
                f"Unknown field '{col}' on {self._model.__name__}. "
                f"Valid fields: {', '.join(known.keys())}"
            )

    def _parse_key(self, key: str) -> tuple[str, str]:
        """Split ``field__lookup`` into ``(field, lookup)`` and validate both."""
        if "__" in key:
            col, op = key.rsplit("__", 1)
            if op in LOOKUPS:
                self._validate_column(col)
                return col, op
            if col in self._model._fields:
                raise ValueError(
                    f"Unsupported lookup '{op}' on field '{col}'. "
                    f"Supported: {', '.join(sorted(LOOKUPS))}"
                )
        self._validate_column(key)
        return key, "eq"

    # ──────────────────────────────────────────────
    # Chainable filter methods
    # ──────────────────────────────────────────────

    def where(self, **kwargs) -> "QuerySet":
        """
        Filter rows; multiple conditions are AND-ed.

        User.where(name="Titus", age=20)

        Lookup suffixes:
            eq / exact, iexact, ne, lt, lte, gt, gte, range,
            in, not_in, is_null / isnull,
            like (raw pattern), contains, icontains,
            startswith, istartswith, endswith, iendswith

        ``field=None`` matches NULL; unknown fields or lookups raise ValueError.
        ``contains`` / ``startswith`` / ``endswith`` follow the backend's
        default case sensitivity; the ``i``-variants are always case-insensitive.
        """
        qs = self._clone()
        for key, value in kwargs.items():
            col, op = self._parse_key(key)
            qs._filters.append((col, op, value))
        return qs

    def exclude(self, **kwargs) -> "QuerySet":
        """Exclude rows matching *all* the given conditions."""
        return self.filter(~Q(**kwargs))

    def order_by(self, *fields) -> "QuerySet":
        """
        Order results. Prefix with '-' for DESC.

            .order_by("name")       → ORDER BY name ASC
            .order_by("-created_at") → ORDER BY created_at DESC
        """
        qs = self._clone()
        for f in fields:
            if not isinstance(f, str):
                raise TypeError(f"order_by() expects field names, got {type(f).__name__}.")
            desc = f.startswith("-")
            col = f[1:] if desc else f
            self._validate_column(col)
            qs._order.append((col, desc))
        return qs

    def limit(self, n: int) -> "QuerySet":
        qs = self._clone()
        qs._limit_val = _check_non_negative_int(n, "limit")
        return qs

    def offset(self, n: int) -> "QuerySet":
        qs = self._clone()
        qs._offset_val = _check_non_negative_int(n, "offset")
        return qs

    def select(self, *cols) -> "QuerySet":
        """Load only *cols* (the primary key is always included).

        Unselected fields hold their defaults and are excluded from
        ``save()`` unless you assign them.
        """
        if not cols:
            raise ValueError("select() requires at least one column.")
        qs = self._clone()
        for c in cols:
            self._validate_column(c)
        pk = self._model._pk_field()
        always = [pk] + ([self._model._meta_version_field] if self._model._meta_version_field else [])
        ordered = always + [c for c in dict.fromkeys(cols) if c not in always]
        qs._select_cols = ordered
        return qs

    def prefetch_related(self, *relationship_names) -> "QuerySet":
        """Eager-load relationships to avoid N+1 queries."""
        from ..relationships import RelationshipDescriptor
        qs = self._clone()
        for name in relationship_names:
            descriptor = self._model.__dict__.get(name)
            if descriptor is None:
                for klass in self._model.__mro__[1:]:
                    if name in klass.__dict__:
                        descriptor = klass.__dict__[name]
                        break
            if not isinstance(descriptor, RelationshipDescriptor):
                raise ValueError(f"{self._model.__name__} has no relationship named {name!r}.")
            if name not in qs._prefetch:
                qs._prefetch.append(name)
        return qs

    def filter(self, *q_objects: Q, **kwargs) -> "QuerySet":
        """Apply one or more Q objects (supports OR / AND / NOT composition).

            User.all().filter(Q(age__gte=18) | Q(name="admin"))

        Keyword arguments behave like :meth:`where`.
        """
        qs = self._clone()
        for q in q_objects:
            if not isinstance(q, Q):
                raise TypeError(f"filter() expects Q objects, got {type(q).__name__}.")
            for key, _value in q.leaves():
                self._parse_key(key)
            qs._q_filters.append(q)
        if kwargs:
            qs = qs.where(**kwargs)
        return qs

    # ──────────────────────────────────────────────
    # Terminal / execution methods
    # ──────────────────────────────────────────────

    def _db(self):
        return self._model._ensure_db()

    def get(self) -> list["Model"]:
        """Execute and return a list of model instances."""
        db = self._db()
        sql, params = self._build_select(db.driver)
        rows = db.execute(sql, params)
        instances = [self._model._from_row(row) for row in rows]

        # Eager-load prefetched relationships
        if self._prefetch and instances:
            self._do_prefetch(instances)

        self._result_cache = instances
        return list(instances)

    def _fetch_all(self) -> list["Model"]:
        if self._result_cache is None:
            self.get()
        return self._result_cache

    def first(self) -> "Model | None":
        """Return the first matching record, or None.

        Without an explicit or ``Meta`` ordering, rows are ordered by PK.
        """
        qs = self if self._effective_order() else self.order_by(self._model._pk_field())
        results = qs.limit(1).get()
        return results[0] if results else None

    def last(self) -> "Model | None":
        """Return the last matching record (reverse of the current ordering,
        or by primary key), or None."""
        order = self._effective_order() or [(self._model._pk_field(), False)]
        qs = self._clone()
        qs._order = [(col, not desc) for col, desc in order]
        results = qs.limit(1).get()
        return results[0] if results else None

    def count(self) -> int:
        """Return number of matching rows (respecting limit/offset)."""
        if self._result_cache is not None:
            return len(self._result_cache)
        db = self._db()
        driver = db.driver
        table = self._model._table_name()
        where_sql, params = self._build_where(driver)
        where = f" WHERE {where_sql}" if where_sql else ""
        if self._limit_val is None and not self._offset_val:
            sql = f"SELECT COUNT(*) FROM {table}{where}"
        else:
            inner = f"SELECT 1 FROM {table}{where}{self._order_clause(driver)}"
            lo = driver.limit_offset_sql(self._limit_val, self._offset_val)
            sql = f"SELECT COUNT(*) FROM ({inner} {lo}) AS doveorm_sub"
        rows = db.execute(sql, params)
        return int(rows[0][0]) if rows else 0

    def exists(self) -> bool:
        if self._result_cache is not None:
            return bool(self._result_cache)
        if self._limit_val == 0:
            return False
        db = self._db()
        driver = db.driver
        table = self._model._table_name()
        where_sql, params = self._build_where(driver)
        sql = f"SELECT 1 FROM {table}"
        if where_sql:
            sql += f" WHERE {where_sql}"
        if self._offset_val:
            sql += self._order_clause(driver)
        sql += " " + driver.limit_offset_sql(1, self._offset_val)
        return len(db.execute(sql, params)) > 0

    def _guard_bulk(self, action: str, where_sql: str) -> None:
        if self._limit_val is not None or self._offset_val:
            raise UnsafeQueryError(
                f"{action}() cannot be combined with limit()/offset(); "
                "select the rows first and filter by primary key."
            )
        if not where_sql and not self._allow_unfiltered:
            name = self._model.__name__
            raise UnsafeQueryError(
                f"Refusing to {action} every row of {name} without a filter. "
                f"Use {name}.all().{action}(...) if that is intended."
            )

    def delete(self) -> int:
        """Delete all matching rows. Returns number of deleted rows."""
        db = self._db()
        where_sql, params = self._build_where(db.driver)
        self._guard_bulk("delete", where_sql)
        sql = f"DELETE FROM {self._model._table_name()}"
        if where_sql:
            sql += f" WHERE {where_sql}"
        self._result_cache = None
        return db.execute_write(sql, params)

    def update(self, **kwargs) -> int:
        """
        Bulk-update matching rows (values are validated).  Returns the
        number of rows matched.

            User.where(active=False).update(active=True)

        Note: ``auto_now`` fields are not touched by bulk updates.
        """
        if not kwargs:
            return 0
        fields = self._model._fields
        set_parts = []
        set_params = []
        for col, value in kwargs.items():
            self._validate_column(col)
            field = fields[col]
            set_parts.append(f"{col} = ?")
            set_params.append(field.to_db(field.validate(field.to_python(value))))
        version = self._model._meta_version_field
        if version is not None and version not in kwargs:
            # keep optimistic locking sound: concurrent holders must see a new version
            set_parts.append(f"{version} = {version} + 1")
        db = self._db()
        where_sql, where_params = self._build_where(db.driver)
        self._guard_bulk("update", where_sql)
        sql = f"UPDATE {self._model._table_name()} SET {', '.join(set_parts)}"
        if where_sql:
            sql += f" WHERE {where_sql}"
        self._result_cache = None
        return db.execute_write(sql, set_params + where_params)

    def __iter__(self):
        return iter(self._fetch_all())

    def __len__(self):
        return len(self._fetch_all())

    def __bool__(self):
        return bool(self._fetch_all())

    def __getitem__(self, key):
        if self._result_cache is not None:
            return self._result_cache[key]
        if isinstance(key, slice):
            if key.step not in (None, 1):
                raise ValueError("QuerySet slicing does not support a step.")
            start = 0 if key.start is None else _check_non_negative_int(key.start, "slice start")
            if self._limit_val is not None or self._offset_val:
                return self._fetch_all()[key]
            qs = self.offset(start) if start else self._clone()
            if key.stop is not None:
                stop = _check_non_negative_int(key.stop, "slice stop")
                qs = qs.limit(max(0, stop - start))
            return qs.get()
        index = _check_non_negative_int(key, "index")
        if self._limit_val is not None or self._offset_val:
            return self._fetch_all()[index]
        results = self.offset(index).limit(1).get()
        if not results:
            raise IndexError("QuerySet index out of range")
        return results[0]

    def __repr__(self):
        return f"<QuerySet model={self._model.__name__} filters={self._filters}>"


    def _clone(self) -> "QuerySet":
        qs = QuerySet(self._model)
        qs._filters = self._filters.copy()
        qs._q_filters = self._q_filters.copy()
        qs._order = self._order.copy()
        qs._limit_val = self._limit_val
        qs._offset_val = self._offset_val
        qs._select_cols = self._select_cols.copy() if self._select_cols else None
        qs._prefetch = self._prefetch.copy()
        qs._allow_unfiltered = self._allow_unfiltered
        return qs
