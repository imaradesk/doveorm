"""DoveORM numeric fields: integers, auto-increment keys, floats and decimals."""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation

from ..exceptions import ValidationError
from .base import Field


class IntegerField(Field):
    """32-bit integer.  SQLite: ``INTEGER``, MySQL: ``INT``."""
    MIN_VALUE = -2 ** 31
    MAX_VALUE = 2 ** 31 - 1

    def sql_type(self): return "INTEGER"

    def validate(self, value):
        value = super().validate(value)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"Field '{self.name}' expects int, got {type(value).__name__}.")
            if not (self.MIN_VALUE <= value <= self.MAX_VALUE):
                raise ValidationError(
                    f"Field '{self.name}' must be between {self.MIN_VALUE} and "
                    f"{self.MAX_VALUE}, got {self._shown(value)}."
                )
        return value


class BigIntegerField(IntegerField):
    """64-bit integer.  SQLite: ``INTEGER``, MySQL: ``BIGINT``."""
    MIN_VALUE = -2 ** 63
    MAX_VALUE = 2 ** 63 - 1

    def sql_type(self): return "BIGINT"


class SmallIntegerField(IntegerField):
    """16-bit integer.  SQLite: ``INTEGER``, MySQL: ``SMALLINT``."""
    MIN_VALUE = -2 ** 15
    MAX_VALUE = 2 ** 15 - 1

    def sql_type(self): return "SMALLINT"


class PositiveIntegerField(IntegerField):
    """Integer that must be ≥ 0."""
    MIN_VALUE = 0


class PositiveSmallIntegerField(SmallIntegerField):
    """Small integer that must be ≥ 0."""
    MIN_VALUE = 0


class PositiveBigIntegerField(BigIntegerField):
    """Big integer that must be ≥ 0."""
    MIN_VALUE = 0


class FloatField(Field):
    """Floating-point column.  SQLite: ``REAL``, MySQL: ``DOUBLE``."""
    def sql_type(self): return "REAL"

    def validate(self, value):
        value = super().validate(value)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"Field '{self.name}' expects float, got {type(value).__name__}.")
        value = float(value)
        if not math.isfinite(value):
            raise ValidationError(f"Field '{self.name}' must be a finite number, got {self._shown(value)}.")
        return value


class DecimalField(Field):
    """Fixed-precision decimal.  SQLite: ``TEXT`` (stored as a quantized
    string; range comparisons and ordering cast to REAL),
    MySQL: ``DECIMAL(max_digits, decimal_places)``."""

    numeric_text_on_sqlite = True

    def __init__(self, max_digits=10, decimal_places=2, **kwargs):
        super().__init__(**kwargs)
        if not isinstance(max_digits, int) or not isinstance(decimal_places, int) \
                or max_digits < 1 or decimal_places < 0 or decimal_places > max_digits:
            raise ValueError("DecimalField requires 1 <= max_digits and 0 <= decimal_places <= max_digits.")
        self.max_digits = max_digits
        self.decimal_places = decimal_places
        self._quantum = Decimal(1).scaleb(-decimal_places)

    def sql_type(self): return "DECIMAL"

    def _resolved_type(self, driver=None) -> str:
        if driver is not None and driver.name == "mysql":
            return f"DECIMAL({self.max_digits},{self.decimal_places})"
        return "TEXT"  # SQLite: store as text to preserve precision

    def _to_decimal(self, value) -> Decimal:
        if isinstance(value, bool):
            raise TypeError(f"Field '{self.name}' expects a decimal-compatible value, got bool.")
        if isinstance(value, Decimal):
            return value
        if isinstance(value, float):
            return Decimal(repr(value))
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            raise TypeError(
                f"Field '{self.name}' expects a decimal-compatible value, got {type(value).__name__}."
            ) from None

    def validate(self, value):
        value = super().validate(value)
        if value is None:
            return None
        dec = self._to_decimal(value)
        if not dec.is_finite():
            raise ValidationError(f"Field '{self.name}' must be a finite decimal.")
        _sign, digits, exponent = dec.normalize().as_tuple()
        places = max(0, -exponent)
        whole = max(0, len(digits) + exponent) if exponent < 0 else len(digits) + exponent
        if places > self.decimal_places:
            raise ValidationError(
                f"Field '{self.name}' allows at most {self.decimal_places} decimal places, got {self._shown(value)}."
            )
        if whole > self.max_digits - self.decimal_places:
            raise ValidationError(
                f"Field '{self.name}' allows at most {self.max_digits - self.decimal_places} "
                f"digits before the decimal point, got {self._shown(value)}."
            )
        return dec

    def to_python(self, value):
        if value is None:
            return None
        try:
            return self._to_decimal(value)
        except TypeError:
            return value            # let validate() report it

    def to_db(self, value):
        if value is None:
            return None
        return str(self._to_decimal(value).quantize(self._quantum))

    def from_db(self, value):
        if value is None:
            return None
        return Decimal(str(value))


class AutoField(IntegerField):
    """Auto-incrementing integer primary key (added automatically if none declared)."""
    def __init__(self):
        super().__init__(primary_key=True, nullable=False)

    @property
    def auto_increment(self) -> bool:
        return True


class BigAutoField(BigIntegerField):
    """Auto-incrementing 64-bit primary key."""
    def __init__(self):
        super().__init__(primary_key=True, nullable=False)

    @property
    def auto_increment(self) -> bool:
        return True


class SmallAutoField(SmallIntegerField):
    """Auto-incrementing 16-bit primary key."""
    def __init__(self):
        super().__init__(primary_key=True, nullable=False)

    @property
    def auto_increment(self) -> bool:
        return True
