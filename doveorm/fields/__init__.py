"""
DoveORM Fields
All column types that can be used on a Model.

Fields are driver-aware: sql_definition() accepts an optional ``driver``
argument to produce correct DDL for SQLite vs MySQL.

Value pipeline
--------------
* ``to_python(value)`` – coerce a value assigned by user code
  (``Model(field=value)``) into the field's Python type.
* ``validate(value)``  – check the value; raise on bad input.
* ``to_db(value)``     – convert a Python value into a DB parameter.
* ``from_db(value)``   – convert a DB value back into Python.
* ``pre_save(instance, add)`` – hook run by ``Model.save()`` (auto_now …).

Full Django-compatible field set:
    AutoField, BigAutoField, SmallAutoField,
    IntegerField, BigIntegerField, SmallIntegerField,
    PositiveIntegerField, PositiveSmallIntegerField, PositiveBigIntegerField,
    FloatField, DecimalField,
    CharField, TextField, SlugField, EmailField, URLField,
    IPAddressField, GenericIPAddressField,
    BooleanField,
    DateField, DateTimeField, TimeField, DurationField,
    UUIDField, JSONField, BinaryField,
    FileField, ImageField, FilePathField,
    ForeignKey, OneToOneField,
"""
from .base import Field
from .numeric import (
    AutoField, BigAutoField, SmallAutoField,
    IntegerField, BigIntegerField, SmallIntegerField,
    PositiveIntegerField, PositiveSmallIntegerField, PositiveBigIntegerField,
    FloatField, DecimalField,
)
from .text import (
    CharField, TextField, SlugField, EmailField, URLField,
    IPAddressField, GenericIPAddressField,
)
from .temporal import DateField, DateTimeField, TimeField, DurationField
from .special import BooleanField, UUIDField, JSONField, BinaryField
from .files import FileField, ImageField, FilePathField
from .related import ON_DELETE_ACTIONS, ForeignKey, OneToOneField

__all__ = [
    "Field",
    "AutoField", "BigAutoField", "SmallAutoField",
    "IntegerField", "BigIntegerField", "SmallIntegerField",
    "PositiveIntegerField", "PositiveSmallIntegerField", "PositiveBigIntegerField",
    "FloatField", "DecimalField",
    "CharField", "TextField", "SlugField", "EmailField", "URLField",
    "IPAddressField", "GenericIPAddressField",
    "BooleanField",
    "DateField", "DateTimeField", "TimeField", "DurationField",
    "UUIDField", "JSONField", "BinaryField",
    "FileField", "ImageField", "FilePathField",
    "ON_DELETE_ACTIONS", "ForeignKey", "OneToOneField",
]
