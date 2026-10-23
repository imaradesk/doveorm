"""Table naming, option validation and DDL (``create_table`` / ``drop_table``)."""
from __future__ import annotations

import re

from ..fields import ForeignKey, IntegerField
from ..utils.sql import validate_identifier



def _column_list(columns, what: str) -> tuple[str, ...]:
    cols = tuple(columns) if isinstance(columns, (list, tuple)) else (columns,)
    if not cols:
        raise ValueError(f"Empty column list in {what}.")
    return cols


class SchemaMixin:
    """Schema management for models."""

    @classmethod
    def _validate_options(cls) -> None:
        """Validate table name and Meta / @table options."""
        validate_identifier(cls._table_name(), "table name")
        for spec, what in ((cls._meta_indexes, "indexes"), (cls._meta_unique_together, "unique_together")):
            for columns in spec:
                for col in _column_list(columns, what):
                    if col not in cls._fields:
                        raise ValueError(f"{cls.__name__}.{what} references unknown field {col!r}.")
        for entry in cls._meta_ordering:
            if not isinstance(entry, str):
                raise TypeError(f"{cls.__name__}.ordering entries must be strings.")
            col = entry[1:] if entry.startswith("-") else entry
            if col not in cls._fields:
                raise ValueError(f"{cls.__name__}.ordering references unknown field {col!r}.")
        version = cls._meta_version_field
        if version is not None:
            field = cls._fields.get(version)
            if not isinstance(field, IntegerField) or field.primary_key:
                raise TypeError(
                    f"{cls.__name__}.version_field must name a non-primary-key "
                    f"IntegerField, got {version!r}."
                )
            field.editable = False
        if cls._meta_engine is not None:
            validate_identifier(cls._meta_engine, "MySQL engine")
        if cls._meta_charset is not None:
            validate_identifier(cls._meta_charset, "MySQL charset")

    @classmethod
    def _table_name(cls) -> str:
        # Allow explicit override via Meta.table_name
        if cls._table_name_override:
            return cls._table_name_override
        # CamelCase → snake_case
        name = re.sub(r"(?<!^)(?=[A-Z])", "_", cls.__name__).lower()
        return cls._pluralize(name)

    @staticmethod
    def _pluralize(word: str) -> str:
        """Smarter English pluralization heuristic."""
        if word.endswith(("s", "x", "z", "ch", "sh")):
            return word + "es"
        if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
            return word[:-1] + "ies"
        return word + "s"

    @classmethod
    def create_table(cls, if_not_exists=True):
        """Emit CREATE TABLE DDL (plus indexes) for this model."""
        if cls._abstract:
            raise TypeError(f"Cannot create a table for abstract model {cls.__name__}.")
        db = cls._ensure_db()
        driver = db.driver
        table = cls._table_name()
        qn = driver.quote_name
        is_mysql = driver.name == "mysql"

        col_defs = []
        fk_clauses = []
        for col_name, field in cls._fields.items():
            col_type = field._resolved_type(driver)
            if is_mysql and (field.unique or field.db_index) and not field.primary_key \
                    and not driver.allows_default_for(col_type):
                raise ValueError(
                    f"{cls.__name__}.{col_name}: MySQL cannot index a {col_type} column "
                    "without a prefix length; use CharField instead."
                )
            col_defs.append(f"{qn(col_name)} {field.sql_definition(driver)}")
            if isinstance(field, ForeignKey):
                ref_cls = field.target_model()
                if ref_cls is None:
                    raise ValueError(
                        f"{cls.__name__}.{col_name} references {field.to!r}, "
                        "which is not defined yet."
                    )
                fk_clauses.append(
                    f"FOREIGN KEY ({qn(col_name)}) REFERENCES "
                    f"{qn(ref_cls._table_name())}({qn(ref_cls._pk_field())}) ON DELETE {field.on_delete}"
                )

        guard = "IF NOT EXISTS " if if_not_exists else ""
        body = ",\n    ".join(col_defs + fk_clauses)
        ddl = f"CREATE TABLE {guard}{qn(table)} (\n    {body}\n)"

        # MySQL: add engine specification
        if is_mysql:
            engine_str = cls._meta_engine or "InnoDB"
            charset_str = cls._meta_charset or "utf8mb4"
            ddl += f" ENGINE={engine_str} DEFAULT CHARSET={charset_str}"

        with db.transaction():
            db.execute_ddl(ddl)

            # Indexes for fields with db_index=True (UNIQUE columns are already indexed)
            for col_name, field in cls._fields.items():
                if field.db_index and not field.primary_key and not field.unique:
                    db.create_index(table, [col_name])

            # Composite indexes from Meta.indexes / @table(indexes=...)
            for columns in cls._meta_indexes:
                db.create_index(table, _column_list(columns, "indexes"))

            # UNIQUE constraints from Meta.unique_together / @table(unique_together=...)
            for columns in cls._meta_unique_together:
                db.create_index(table, _column_list(columns, "unique_together"), unique=True)

    @classmethod
    def drop_table(cls, if_exists=True):
        db = cls._ensure_db()
        guard = "IF EXISTS " if if_exists else ""
        db.execute_ddl(f"DROP TABLE {guard}{db.driver.quote_name(cls._table_name())}")
