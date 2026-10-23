"""DoveORM models: the ``Model`` base class, its metaclass and ``@table``."""
from .base import Model
from .decorators import table
from .meta import ModelMeta

__all__ = ["Model", "ModelMeta", "table"]
