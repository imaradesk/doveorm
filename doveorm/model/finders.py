"""Dynamic finders: ``findBy…``, ``findAllBy…``, ``countBy…``, ``existsBy…``."""
from __future__ import annotations

import re



def _camel_to_snake(name: str) -> str:
    """Convert CamelCase to snake_case.  E.g. 'Username' → 'username',
    'AuthorId' → 'author_id', 'CreatedAt' → 'created_at'."""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


class FinderMixin:
    """Factories for the callables returned by ``ModelMeta.__getattr__``."""

    @classmethod
    def _parse_findby_fields(cls, compound_name: str) -> list[str]:
        """Parse a compound CamelCase name split by 'And' into snake_case field names.
        Example:
            'UsernameAndActive' → ['username', 'active']
            'AuthorIdAndPublished' → ['author_id', 'published']
        """
        parts = re.split(r"And(?=[A-Z])", compound_name)
        fields = []
        for part in parts:
            snake = _camel_to_snake(part)
            if snake not in cls._fields:
                raise AttributeError(
                    f"{cls.__name__} has no field '{snake}' "
                    f"(parsed from 'findBy...{part}'). "
                    f"Available fields: {', '.join(cls._fields.keys())}"
                )
            fields.append(snake)
        return fields

    @classmethod
    def _make_dynamic_finder(cls, compound_name: str, *, many: bool):
        """Return a callable that performs a findBy query.

        ``many=False`` → returns first match or None (like ``findByX``).
        ``many=True``  → returns list (like ``findAllByX``).
        """
        field_names = cls._parse_findby_fields(compound_name)

        def finder(*args):
            if len(args) != len(field_names):
                raise TypeError(
                    f"{cls.__name__}.findBy{compound_name}() expects "
                    f"{len(field_names)} argument(s) ({', '.join(field_names)}), "
                    f"got {len(args)}."
                )
            kwargs = dict(zip(field_names, args))
            qs = cls.where(**kwargs)
            return qs.get() if many else qs.first()

        finder.__name__ = f"{'findAllBy' if many else 'findBy'}{compound_name}"
        finder.__doc__ = (
            f"Auto-generated finder: query {cls.__name__} "
            f"by {', '.join(field_names)}."
        )
        return finder

    @classmethod
    def _make_dynamic_counter(cls, compound_name: str):
        """Return a callable that counts matching rows."""
        field_names = cls._parse_findby_fields(compound_name)

        def counter(*args):
            if len(args) != len(field_names):
                raise TypeError(
                    f"{cls.__name__}.countBy{compound_name}() expects "
                    f"{len(field_names)} argument(s), got {len(args)}."
                )
            kwargs = dict(zip(field_names, args))
            return cls.where(**kwargs).count()

        counter.__name__ = f"countBy{compound_name}"
        return counter

    @classmethod
    def _make_dynamic_exists(cls, compound_name: str):
        """Return a callable that checks existence."""
        field_names = cls._parse_findby_fields(compound_name)

        def checker(*args):
            if len(args) != len(field_names):
                raise TypeError(
                    f"{cls.__name__}.existsBy{compound_name}() expects "
                    f"{len(field_names)} argument(s), got {len(args)}."
                )
            kwargs = dict(zip(field_names, args))
            return cls.where(**kwargs).exists()

        checker.__name__ = f"existsBy{compound_name}"
        return checker
