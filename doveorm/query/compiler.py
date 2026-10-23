"""SQL compilation for ``QuerySet`` (WHERE / ORDER BY / SELECT)."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .lookups import _COMPARISONS, _LIKE_ESCAPE, _PATTERNS, _SAFE_COL_RE, _escape_like
from .q import Q


class SQLCompilerMixin:
    """Builds parameterised SQL from a QuerySet's state.

    Every value is bound as a parameter; column names have already been
    validated against the model's declared fields.
    """

    def _numeric_cast(self, field, driver) -> bool:
        return driver.name == "sqlite" and getattr(field, "numeric_text_on_sqlite", False)

    def _resolve_filter(self, col: str, op: str, value: Any, driver) -> tuple[str, list]:
        """Turn a single (col, op, value) into a SQL fragment + params,
        routing values through to_db() for proper type conversion."""
        if not _SAFE_COL_RE.match(col):
            raise ValueError(f"Invalid column name: {col!r}")
        field = self._model._fields[col]
        cast = self._numeric_cast(field, driver)
        to_db = field.to_db

        if op in ("eq", "exact"):
            if value is None:
                return f"{col} IS NULL", []
            return f"{col} = ?", [to_db(value)]

        if op == "ne":
            if value is None:
                return f"{col} IS NOT NULL", []
            return f"{col} != ?", [to_db(value)]

        if op == "iexact":
            if value is None:
                return f"{col} IS NULL", []
            return f"LOWER({col}) = LOWER(?)", [to_db(value)]

        if op in _COMPARISONS:
            if value is None:
                raise ValueError(f"Lookup '{op}' on '{col}' does not accept None.")
            sql_op = _COMPARISONS[op]
            if cast:
                return f"CAST({col} AS REAL) {sql_op} CAST(? AS REAL)", [to_db(value)]
            return f"{col} {sql_op} ?", [to_db(value)]

        if op == "range":
            if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
                raise TypeError(f"Lookup 'range' on '{col}' expects a (low, high) pair.")
            bounds = list(value)
            if len(bounds) != 2 or None in bounds:
                raise ValueError(f"Lookup 'range' on '{col}' expects a (low, high) pair of non-None values.")
            params = [to_db(bounds[0]), to_db(bounds[1])]
            if cast:
                return f"CAST({col} AS REAL) BETWEEN CAST(? AS REAL) AND CAST(? AS REAL)", params
            return f"{col} BETWEEN ? AND ?", params

        if op in ("in", "not_in"):
            if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
                raise TypeError(f"Lookup '{op}' on '{col}' expects an iterable of values.")
            values = list(value)
            has_null = any(v is None for v in values)
            converted = [to_db(v) for v in values if v is not None]
            if op == "in":
                parts = []
                if converted:
                    parts.append(f"{col} IN ({', '.join('?' for _ in converted)})")
                if has_null:
                    parts.append(f"{col} IS NULL")
                if not parts:
                    return "1 = 0", []
                return (parts[0] if len(parts) == 1 else f"({' OR '.join(parts)})"), converted
            parts = []
            if converted:
                parts.append(f"{col} NOT IN ({', '.join('?' for _ in converted)})")
            if has_null:
                parts.append(f"{col} IS NOT NULL")
            if not parts:
                return "1 = 1", []
            return (parts[0] if len(parts) == 1 else f"({' AND '.join(parts)})"), converted

        if op in ("is_null", "isnull"):
            if not isinstance(value, bool):
                raise TypeError(f"Lookup '{op}' on '{col}' expects True or False.")
            return f"{col} IS {'' if value else 'NOT '}NULL", []

        if op == "like":
            if not isinstance(value, str):
                raise TypeError(f"Lookup 'like' on '{col}' expects a string pattern.")
            return f"{col} LIKE ?", [value]

        if op in _PATTERNS:
            if value is None:
                raise ValueError(f"Lookup '{op}' on '{col}' does not accept None.")
            text = value if isinstance(value, str) else str(to_db(value))
            template, insensitive = _PATTERNS[op]
            pattern = template.format(_escape_like(text))
            if insensitive:
                return f"LOWER({col}) LIKE LOWER(?) ESCAPE '{_LIKE_ESCAPE}'", [pattern]
            return f"{col} LIKE ? ESCAPE '{_LIKE_ESCAPE}'", [pattern]

        raise ValueError(f"Unsupported lookup '{op}'.")

    def _build_q(self, q: Q, driver) -> tuple[str, list]:
        """Recursively compile a Q object into SQL + params."""
        parts = []
        params = []
        for child in q.children:
            if isinstance(child, Q):
                sql_part, child_params = self._build_q(child, driver)
                if sql_part:
                    parts.append(f"({sql_part})")
                    params.extend(child_params)
            else:
                # child is a (key, value) tuple from Q(**kwargs)
                key, value = child
                col, op = self._parse_key(key)
                sql_frag, frag_params = self._resolve_filter(col, op, value, driver)
                parts.append(sql_frag)
                params.extend(frag_params)

        connector = f" {q.connector} "
        combined = connector.join(parts) if parts else ""
        if q.negated and combined:
            combined = f"NOT ({combined})"
        return combined, params

    def _build_where(self, driver) -> tuple[str, list]:
        all_parts = []
        all_params = []

        # Classic .where() filters (always AND-ed)
        for col, op, value in self._filters:
            sql_frag, frag_params = self._resolve_filter(col, op, value, driver)
            all_parts.append(sql_frag)
            all_params.extend(frag_params)

        # Q-object filters
        for q in self._q_filters:
            q_sql, q_params = self._build_q(q, driver)
            if q_sql:
                all_parts.append(f"({q_sql})")
                all_params.extend(q_params)

        return " AND ".join(all_parts), all_params

    def _effective_order(self) -> list[tuple[str, bool]]:
        if self._order:
            return list(self._order)
        return [
            (o[1:], True) if o.startswith("-") else (o, False)
            for o in self._model._meta_ordering
        ]

    def _order_clause(self, driver) -> str:
        order = self._effective_order()
        if not order:
            return ""
        fields = self._model._fields
        parts = []
        for col, desc in order:
            expr = f"CAST({col} AS REAL)" if self._numeric_cast(fields[col], driver) else col
            parts.append(f"{expr} {'DESC' if desc else 'ASC'}")
        return " ORDER BY " + ", ".join(parts)

    def _build_select(self, driver) -> tuple[str, list]:
        table = self._model._table_name()
        cols = ", ".join(self._select_cols) if self._select_cols else "*"
        sql = f"SELECT {cols} FROM {table}"
        where_sql, params = self._build_where(driver)
        if where_sql:
            sql += f" WHERE {where_sql}"
        sql += self._order_clause(driver)
        lo = driver.limit_offset_sql(self._limit_val, self._offset_val)
        if lo:
            sql += f" {lo}"
        return sql, params
