"""Many-to-many relationships via a join table."""
from __future__ import annotations

from typing import Callable

from ..utils.sql import validate_identifier
from .base import RelationshipDescriptor


class ManyToManyManager:
    """
        Proxy returned by ManyToMany.__get__() that wraps the related objects
        (loaded lazily on first use) and provides .add() / .remove() / .clear()
        helpers for managing the join table.
    """

    def __init__(self, loader: Callable[[], list], *, db, through: str, src_fk: str,
                 tgt_fk: str, src_pk_val, src_field, related_cls, items: list | None = None):
        self._loader = loader
        self._loaded: list | None = items
        self._db = db
        self._through = through
        self._src_fk = src_fk
        self._tgt_fk = tgt_fk
        self._src_pk_val = src_pk_val
        self._src_field = src_field
        self._related_cls = related_cls
        self._tgt_pk = related_cls._pk_field()
        self._tgt_field = related_cls._fields[self._tgt_pk]

    @property
    def _items(self) -> list:
        if self._loaded is None:
            self._loaded = self._loader()
        return self._loaded

    # list-like behaviour
    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def __getitem__(self, index):
        return self._items[index]

    def __contains__(self, obj):
        return obj in self._items

    def __bool__(self):
        return bool(self._items)

    def __repr__(self):
        if self._loaded is None:
            return "<ManyToManyManager (not loaded)>"
        return f"<ManyToManyManager count={len(self._loaded)}>"

    def all(self) -> list:
        return list(self._items)

    def _target_pks(self, instances) -> list:
        pks = []
        for inst in instances:
            if not isinstance(inst, self._related_cls):
                raise TypeError(
                    f"Expected {self._related_cls.__name__} instances, got {type(inst).__name__}."
                )
            pk = getattr(inst, self._tgt_pk)
            if pk is None:
                raise ValueError(f"Cannot relate an unsaved {self._related_cls.__name__}.")
            if pk not in pks:
                pks.append(pk)
        return pks

    def _require_source(self) -> None:
        if self._src_pk_val is None:
            raise ValueError("Save the instance before changing its many-to-many relations.")

    def add(self, *instances) -> None:
        """Insert rows into the join table for the given related instances."""
        self._require_source()
        pks = self._target_pks(instances)
        if not pks:
            return
        items = self._items                      # load current state first
        ignore = self._db.driver.insert_ignore_prefix()
        src_db = self._src_field.to_db(self._src_pk_val)
        self._db.execute_many(
            f"{ignore} INTO {self._through} ({self._src_fk}, {self._tgt_fk}) VALUES (?, ?)",
            [[src_db, self._tgt_field.to_db(pk)] for pk in pks],
        )
        # Avoid duplicates in the in-memory list
        existing = {getattr(i, self._tgt_pk) for i in items}
        for inst in instances:
            pk = getattr(inst, self._tgt_pk)
            if pk not in existing:
                items.append(inst)
                existing.add(pk)

    def remove(self, *instances) -> None:
        """Delete rows from the join table for the given related instances."""
        self._require_source()
        pks = self._target_pks(instances)
        if not pks:
            return
        src_db = self._src_field.to_db(self._src_pk_val)
        with self._db.transaction():
            for i in range(0, len(pks), 500):
                chunk = pks[i:i + 500]
                placeholders = ", ".join("?" for _ in chunk)
                self._db.execute_write(
                    f"DELETE FROM {self._through} WHERE {self._src_fk} = ? "
                    f"AND {self._tgt_fk} IN ({placeholders})",
                    [src_db] + [self._tgt_field.to_db(pk) for pk in chunk],
                )
        if self._loaded is not None:
            removed = set(pks)
            self._loaded = [i for i in self._loaded if getattr(i, self._tgt_pk) not in removed]

    def clear(self) -> None:
        """Remove every relation for this instance."""
        self._require_source()
        self._db.execute_write(
            f"DELETE FROM {self._through} WHERE {self._src_fk} = ?",
            [self._src_field.to_db(self._src_pk_val)],
        )
        self._loaded = []


class ManyToMany(RelationshipDescriptor):
    """
    Many-to-many via a join table.

    class Student(Model):
        courses = ManyToMany("Course", through="student_courses",
                             source_fk="student_id", target_fk="course_id")

    student.courses       →  ManyToManyManager (iterable, len-able)
    student.courses.add(course)
    student.courses.remove(course)
    student.courses.clear()
    """

    def __init__(self, to, through: str, source_fk: str = None, target_fk: str = None):
        validate_identifier(through, "join table name")
        if source_fk is not None:
            validate_identifier(source_fk, "source foreign key")
        if target_fk is not None:
            validate_identifier(target_fk, "target foreign key")
        self.to = to
        self.through = through   # name of the join table
        self._src_fk = source_fk
        self._tgt_fk = target_fk

    def resolve(self, owner) -> tuple[type, str, str, str]:
        related_cls = self._resolve_model(self.to, owner._registry)
        src_fk = validate_identifier(self._src_fk or f"{owner.__name__.lower()}_id", "source foreign key")
        tgt_fk = validate_identifier(self._tgt_fk or f"{related_cls.__name__.lower()}_id", "target foreign key")
        return related_cls, self.through, src_fk, tgt_fk

    def __get__(self, instance, owner):
        if instance is None:
            return self
        related_cls, through, src_fk, tgt_fk = self.resolve(owner)
        pk_name = owner._pk_field()
        pk_val = getattr(instance, pk_name)
        src_field = owner._fields[pk_name]
        db = owner._ensure_db()

        found, cached = self._check_prefetch(instance)

        def loader() -> list:
            if pk_val is None:
                return []
            sql = (
                f"SELECT r.* FROM {related_cls._table_name()} r "
                f"JOIN {through} j ON j.{tgt_fk} = r.{related_cls._pk_field()} "
                f"WHERE j.{src_fk} = ?"
            )
            rows = db.execute(sql, [src_field.to_db(pk_val)])
            return [related_cls._from_row(row) for row in rows]

        return ManyToManyManager(
            loader,
            items=list(cached) if found else None,
            db=db,
            through=through,
            src_fk=src_fk,
            tgt_fk=tgt_fk,
            src_pk_val=pk_val,
            src_field=src_field,
            related_cls=related_cls,
        )
