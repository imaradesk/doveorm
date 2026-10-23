"""``@table()`` – class decorator alternative to ``class Meta``."""
from __future__ import annotations


def table(
    name: str | None = None,
    *,
    indexes: list | None = None,
    unique_together: list | None = None,
    ordering: list[str] | None = None,
    engine: str | None = None,
    charset: str | None = None,
    version_field: str | None = None,
):
    """
    Class decorator — an alternative to ``class Meta`` for configuring
    a Model's table options.  Inspired by Spring Boot / JPA ``@Entity``
    and ``@Table`` annotations.

    Usage::

        from doveorm import Model, CharField, table

        @table(name="app_users", indexes=[("email",)], ordering=["-id"])
        class User(Model):
            username = CharField(max_length=100)
            email    = CharField(max_length=200)

    Is equivalent to::

        class User(Model):
            username = CharField(max_length=100)
            email    = CharField(max_length=200)

            class Meta:
                table_name = "app_users"
                indexes = [("email",)]
                ordering = ["-id"]

    Supported parameters:

    * **name** – explicit table name (overrides auto-pluralized snake_case).
    * **indexes** – list of column tuples for CREATE INDEX.
    * **unique_together** – list of column tuples for UNIQUE constraints.
    * **ordering** – default ORDER BY columns (prefix ``-`` for DESC).
    * **engine** – MySQL storage engine (default ``InnoDB``).
    * **charset** – MySQL charset (default ``utf8mb4``).
    * **version_field** – IntegerField used for optimistic locking.

    If both ``@table()`` and ``class Meta`` are present on the same model,
    the ``@table()`` values take precedence.
    """

    def decorator(cls):
        # The decorator runs *after* the metaclass, so we update directly.
        if name is not None:
            cls._table_name_override = name
        if indexes is not None:
            cls._meta_indexes = list(indexes)
        if unique_together is not None:
            cls._meta_unique_together = list(unique_together)
        if ordering is not None:
            cls._meta_ordering = list(ordering)
        if engine is not None:
            cls._meta_engine = engine
        if charset is not None:
            cls._meta_charset = charset
        if version_field is not None:
            cls._meta_version_field = version_field
        if not cls._abstract:
            cls._validate_options()
        return cls

    return decorator
