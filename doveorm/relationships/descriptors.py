"""One-to-many / one-to-one / many-to-one descriptors."""
from __future__ import annotations

from ..utils.sql import validate_identifier
from .base import RelationshipDescriptor


class HasMany(RelationshipDescriptor):
    """
    One-to-many: this model has many of the related model.

    class User(Model):
        posts = HasMany("Post", foreign_key="user_id")

    user.posts  →  [Post, Post, ...]
    """

    def __init__(self, to, foreign_key: str = None, back_populates: str = None):
        if foreign_key is not None:
            validate_identifier(foreign_key, "foreign key")
        self.to = to
        self._fk = foreign_key
        self.back_populates = back_populates

    def resolve(self, owner) -> tuple[type, str]:
        related_cls = self._resolve_model(self.to, owner._registry)
        fk = self._fk or f"{owner.__name__.lower()}_id"
        return related_cls, self._require_field(related_cls, fk, "foreign key")

    def __get__(self, instance, owner):
        if instance is None:
            return self
        found, cached = self._check_prefetch(instance)
        if found:
            return list(cached)
        related_cls, fk = self.resolve(owner)
        pk_val = getattr(instance, owner._pk_field())
        if pk_val is None:
            return []
        return related_cls.where(**{fk: pk_val}).get()


class HasOne(RelationshipDescriptor):
    """
    One-to-one: this model has one of the related model.

    class User(Model):
        profile = HasOne("Profile", foreign_key="user_id")

    user.profile  →  Profile | None
    """

    def __init__(self, to, foreign_key: str = None):
        if foreign_key is not None:
            validate_identifier(foreign_key, "foreign key")
        self.to = to
        self._fk = foreign_key

    def resolve(self, owner) -> tuple[type, str]:
        related_cls = self._resolve_model(self.to, owner._registry)
        fk = self._fk or f"{owner.__name__.lower()}_id"
        return related_cls, self._require_field(related_cls, fk, "foreign key")

    def __get__(self, instance, owner):
        if instance is None:
            return self
        found, cached = self._check_prefetch(instance)
        if found:
            return cached
        related_cls, fk = self.resolve(owner)
        pk_val = getattr(instance, owner._pk_field())
        if pk_val is None:
            return None
        return related_cls.where(**{fk: pk_val}).first()


class BelongsTo(RelationshipDescriptor):
    """
    Many-to-one: this model belongs to the related model.

    class Post(Model):
        user_id = ForeignKey("User")
        user    = BelongsTo("User", foreign_key="user_id")

    post.user  →  User instance | None
    """

    def __init__(self, to, foreign_key: str = None):
        if foreign_key is not None:
            validate_identifier(foreign_key, "foreign key")
        self.to = to
        self._fk = foreign_key

    def resolve(self, owner) -> tuple[type, str]:
        related_cls = self._resolve_model(self.to, owner._registry)
        fk = self._fk or f"{related_cls.__name__.lower()}_id"
        return related_cls, self._require_field(owner, fk, "foreign key")

    def __get__(self, instance, owner):
        if instance is None:
            return self
        related_cls, fk = self.resolve(owner)
        fk_val = getattr(instance, fk, None)
        related_pk = related_cls._pk_field()

        found, cached = self._check_prefetch(instance)
        if found and (cached is None and fk_val is None
                      or cached is not None and getattr(cached, related_pk) == fk_val):
            return cached
        if fk_val is None:
            return None

        cache = instance.__dict__.get("_relation_cache")
        if cache is None:
            cache = {}
            object.__setattr__(instance, "_relation_cache", cache)
        hit = cache.get(self.attr_name)
        if hit is not None and hit[0] == fk_val:
            return hit[1]
        obj = related_cls.where(**{related_pk: fk_val}).first()
        cache[self.attr_name] = (fk_val, obj)
        return obj
