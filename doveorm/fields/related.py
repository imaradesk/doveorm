"""DoveORM relationship fields: ``ForeignKey`` and ``OneToOneField``."""
from __future__ import annotations

from ..exceptions import ValidationError
from .base import Field

# The constants are the plain strings, so ``on_delete=CASCADE`` and
# ``on_delete="CASCADE"`` are the same value (and migrations write the string).
CASCADE = "CASCADE"
SET_NULL = "SET NULL"
SET_DEFAULT = "SET DEFAULT"
RESTRICT = "RESTRICT"
NO_ACTION = "NO ACTION"

ON_DELETE_ACTIONS = (CASCADE, SET_NULL, SET_DEFAULT, RESTRICT, NO_ACTION)


class ForeignKey(Field):
    """Many-to-one relationship.

    The column type and value conversion follow the referenced model's
    primary key (INTEGER for AutoField, BIGINT for BigAutoField, CHAR(32)
    for UUIDField …).  A model instance may be assigned; its PK is stored.

    Usage:
        class Post(Model):
            author_id = ForeignKey("User", on_delete="CASCADE")
            editor_id = ForeignKey("User", on_delete=SET_NULL, nullable=True)

    ``on_delete`` is a string or one of the constants ``CASCADE``, ``SET_NULL``,
    ``SET_DEFAULT``, ``RESTRICT``, ``NO_ACTION`` (also ``fields.CASCADE`` …).
    """

    def __init__(self, to, on_delete=CASCADE, related_name=None, **kwargs):
        super().__init__(**kwargs)
        action = " ".join(str(on_delete).upper().replace("_", " ").split())     # "set_null" works too
        if action not in ON_DELETE_ACTIONS:
            raise ValueError(f"on_delete must be one of {ON_DELETE_ACTIONS}, got {on_delete!r}.")
        if action == "SET NULL" and not self.nullable:
            raise ValueError("on_delete='SET NULL' requires nullable=True.")
        self.to = to                     # model class or string name
        self.on_delete = action
        self.related_name = related_name

    def target_model(self):
        """Return the referenced model class, or None if not (yet) defined."""
        if isinstance(self.to, str):
            from ..model import ModelMeta
            return ModelMeta._registry.get(self.to)
        return self.to

    def target_field(self) -> Field | None:
        model = self.target_model()
        if model is None:
            return None
        return model._fields.get(model._pk_field())

    def sql_type(self): return "INTEGER"

    def _resolved_type(self, driver=None) -> str:
        target = self.target_field()
        if target is None:
            return super()._resolved_type(driver)
        return target._resolved_type(driver)

    def _unwrap(self, value):
        from ..model import Model
        if isinstance(value, Model):
            pk = getattr(value, type(value)._pk_field())
            if pk is None:
                raise ValidationError(f"Field '{self.name}': related instance has not been saved.")
            return pk
        return value

    def validate(self, value):
        value = super().validate(self._unwrap(value))
        target = self.target_field()
        if value is not None and target is not None:
            value = target.validate(value)      # type-check with the target PK's rules
        return value

    def to_python(self, value):
        value = self._unwrap(value)
        target = self.target_field()
        return target.to_python(value) if target is not None and value is not None else value

    def to_db(self, value):
        value = self._unwrap(value)
        target = self.target_field()
        return target.to_db(value) if target is not None and value is not None else value

    def get_prep_value(self, value):
        value = self._unwrap(value)
        target = self.target_field()
        return target.get_prep_value(value) if target is not None and value is not None else value

    def from_db(self, value):
        target = self.target_field()
        return target.from_db(value) if target is not None and value is not None else value


class OneToOneField(ForeignKey):
    """One-to-one relationship — a ForeignKey with ``unique=True``.

    Usage:
        class Profile(Model):
            user_id = OneToOneField("User", on_delete="CASCADE")
    """

    def __init__(self, to, on_delete=CASCADE, related_name=None, **kwargs):
        kwargs["unique"] = True
        super().__init__(to, on_delete=on_delete, related_name=related_name, **kwargs)
