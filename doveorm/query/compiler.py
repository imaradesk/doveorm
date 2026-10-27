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

    # Lookups whose SQL is UNKNOWN (not FALSE) when the column is NULL.  Inside a
    # negation they get an explicit ``col IS NOT NULL`` so NOT(...) keeps NULL rows,
    # so that exclude(name="x") also returns rows whose name is NULL.
    _NULL_SENSITIVE = frozenset({"eq", "exact", "iexact", "in", "range", "like"}
                                | set(_COMPARISONS) | set(_PATTERNS))

    def _resolve_filter(self, col: str, op: str, value: Any, driver,
                        negated: bool = False) -> tuple[str, list]:
        """Turn a single (col, op, value) into a SQL fragment + params.

        Values go through ``field.get_prep_value()`` so a value the column
        cannot hold raises instead of matching the wrong rows.
        """
        if not _SAFE_COL_RE.match(col):
            raise ValueError(f"Invalid column name: {col!r}")
        field = self._model._fields[col]
        if op in ("in", "not_in") and isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            value = list(value)                  # iterated twice below
        sql, params = self._lookup_sql(col, field, op, value, driver)
        matches_null = op == "in" and any(v is None for v in value)    # already NULL-safe
        if (negated and field.nullable and not field.primary_key
                and op in self._NULL_SENSITIVE and value is not None and not matches_null):
            sql = f"({sql} AND {driver.quote_name(col)} IS NOT NULL)"
        return sql, params

    def _lookup_sql(self, col: str, field, op: str, value: Any, driver) -> tuple[str, list]:
        qcol = driver.quote_name(col)
        cast = self._numeric_cast(field, driver)
        prep = field.get_prep_value
        is_json = field.sql_type() == "JSON"

        def param(v):
            return driver.json_param() if is_json else "?"

        if op in ("eq", "exact"):
            if value is None:
                return f"{qcol} IS NULL", []
            return f"{qcol} = {param(value)}", [prep(value)]

        if op == "ne":
            if value is None:
                return f"{qcol} IS NOT NULL", []
            sql = f"{qcol} != {param(value)}"
            if field.nullable and not field.primary_key:
                sql = f"({sql} OR {qcol} IS NULL)"     # NULL is "not equal" too, as in Python
            return sql, [prep(value)]

        if op == "iexact":
            if value is None:
                return f"{qcol} IS NULL", []
            target = driver.lookup_cast(qcol, field, op)
            return f"LOWER({target}) = LOWER(?)", [self._pattern_text(field, value)]

        if op in _COMPARISONS:
            if value is None:
                raise ValueError(f"Lookup '{op}' on '{col}' does not accept None.")
            sql_op = _COMPARISONS[op]
            if cast:
                return f"CAST({qcol} AS REAL) {sql_op} CAST(? AS REAL)", [prep(value)]
            return f"{qcol} {sql_op} ?", [prep(value)]

        if op == "range":
            if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
                raise TypeError(f"Lookup 'range' on '{col}' expects a (low, high) pair.")
            bounds = list(value)
            if len(bounds) != 2 or None in bounds:
                raise ValueError(f"Lookup 'range' on '{col}' expects a (low, high) pair of non-None values.")
            params = [prep(bounds[0]), prep(bounds[1])]
            if cast:
                return f"CAST({qcol} AS REAL) BETWEEN CAST(? AS REAL) AND CAST(? AS REAL)", params
            return f"{qcol} BETWEEN ? AND ?", params

        if op in ("in", "not_in"):
            if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
                raise TypeError(f"Lookup '{op}' on '{col}' expects an iterable of values.")
            values = list(value)
            has_null = any(v is None for v in values)
            converted = [prep(v) for v in values if v is not None]
            marks = ", ".join(param(v) for v in converted)
            if op == "in":
                parts = []
                if converted:
                    parts.append(f"{qcol} IN ({marks})")
                if has_null:
                    parts.append(f"{qcol} IS NULL")
                if not parts:
                    return "1 = 0", []
                return (parts[0] if len(parts) == 1 else f"({' OR '.join(parts)})"), converted
            if not converted:
                return (f"{qcol} IS NOT NULL", []) if has_null else ("1 = 1", [])
            sql = f"{qcol} NOT IN ({marks})"
            if has_null:
                return f"({sql} AND {qcol} IS NOT NULL)", converted
            if field.nullable and not field.primary_key:
                sql = f"({sql} OR {qcol} IS NULL)"     # a NULL is not one of the values
            return sql, converted

        if op in ("is_null", "isnull"):
            if not isinstance(value, bool):
                raise TypeError(f"Lookup '{op}' on '{col}' expects True or False.")
            return f"{qcol} IS {'' if value else 'NOT '}NULL", []

        if op == "like":
            if not isinstance(value, str):
                raise TypeError(f"Lookup 'like' on '{col}' expects a string pattern.")
            return f"{driver.lookup_cast(qcol, field, op)} LIKE ?", [value]

        if op in _PATTERNS:
            if value is None:
                raise ValueError(f"Lookup '{op}' on '{col}' does not accept None.")
            template, insensitive = _PATTERNS[op]
            pattern = template.format(_escape_like(self._pattern_text(field, value)))
            target = driver.lookup_cast(qcol, field, op)
            if insensitive:
                return f"LOWER({target}) LIKE LOWER(?) ESCAPE '{_LIKE_ESCAPE}'", [pattern]
            return f"{target} LIKE ? ESCAPE '{_LIKE_ESCAPE}'", [pattern]

        raise ValueError(f"Unsupported lookup '{op}'.")

    @staticmethod
    def _pattern_text(field, value) -> str:
        """Text to search for: strings as given, other values in their stored form."""
        if isinstance(value, str):
            if "\x00" in value:
                raise field._prep_error(value, "text without NUL characters")
            return value
        return str(field.get_prep_value(value))

    def _build_q(self, q: Q, driver, negated: bool = False) -> tuple[str, list]:
        """Recursively compile a Q object into SQL + params."""
        parts = []
        params = []
        inside_not = negated or q.negated
        for child in q.children:
            if isinstance(child, Q):
                sql_part, child_params = self._build_q(child, driver, inside_not)
                if sql_part:
                    parts.append(f"({sql_part})")
                    params.extend(child_params)
            else:
                # child is a (key, value) tuple from Q(**kwargs)
                key, value = child
                col, op = self._parse_key(key)
                sql_frag, frag_params = self._resolve_filter(col, op, value, driver, inside_not)
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
            qcol = driver.quote_name(col)
            expr = f"CAST({qcol} AS REAL)" if self._numeric_cast(fields[col], driver) else qcol
            parts.append(f"{expr} {'DESC' if desc else 'ASC'}")
        return " ORDER BY " + ", ".join(parts)

    def _build_select(self, driver) -> tuple[str, list]:
        qn = driver.quote_name
        table = qn(self._model._table_name())
        cols = ", ".join(qn(c) for c in self._select_cols) if self._select_cols else "*"
        sql = f"SELECT {cols} FROM {table}"
        where_sql, params = self._build_where(driver)
        if where_sql:
            sql += f" WHERE {where_sql}"
        sql += self._order_clause(driver)
        lo = driver.limit_offset_sql(self._limit_val, self._offset_val)
        if lo:
            sql += f" {lo}"
        return sql, params
