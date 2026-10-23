"""DoveORM boolean, UUID, JSON and binary fields."""
from __future__ import annotations

import json
import uuid

from ..exceptions import ValidationError
from .base import Field


class BooleanField(Field):
    """Boolean.  SQLite: ``INTEGER`` (0/1), MySQL: ``TINYINT(1)``."""
    def sql_type(self): return "BOOLEAN"

    def validate(self, value):
        value = super().validate(value)
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise TypeError(f"Field '{self.name}' expects bool, got {type(value).__name__}.")

    def to_python(self, value):
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        return value                    # None, or invalid → validate() reports it

    def to_db(self, value):
        if value is None: return None
        return 1 if value else 0

    def from_db(self, value):
        if value is None: return None
        return bool(value)


class UUIDField(Field):
    """UUID stored as a 32-char hex string (no dashes).

    SQLite: ``TEXT``, MySQL: ``CHAR(32)``.

    Usage:
        import uuid
        id = UUIDField(primary_key=True, default=uuid.uuid4)
    """

    def sql_type(self): return "UUID"

    def _resolved_type(self, driver=None) -> str:
        if driver is not None and driver.name == "mysql":
            return "CHAR(32)"
        return "TEXT"

    def _coerce(self, value) -> uuid.UUID:
        if isinstance(value, uuid.UUID):
            return value
        if isinstance(value, bytes) and len(value) == 16:
            return uuid.UUID(bytes=value)
        if isinstance(value, bytes):
            value = value.decode()
        if isinstance(value, str):
            try:
                return uuid.UUID(value.strip())
            except ValueError:
                raise ValidationError(f"Field '{self.name}': {self._shown(value)} is not a valid UUID.") from None
        raise TypeError(f"Field '{self.name}' expects UUID or str, got {type(value).__name__}.")

    def validate(self, value):
        value = super().validate(value)
        return None if value is None else self._coerce(value)

    def to_python(self, value):
        if value is None:
            return None
        try:
            return self._coerce(value)
        except (ValidationError, TypeError):
            return value

    def to_db(self, value):
        if value is None: return None
        return self._coerce(value).hex

    def from_db(self, value):
        if value is None: return None
        return self._coerce(value)


class JSONField(Field):
    """Stores JSON-serialisable Python values as JSON text.

    SQLite: ``TEXT``, MySQL: ``JSON``.
    """
    def sql_type(self): return "JSON"

    def validate(self, value):
        value = super().validate(value)
        if value is not None:
            try:
                json.dumps(value)
            except (TypeError, ValueError) as exc:
                raise TypeError(f"Field '{self.name}' value is not JSON-serialisable: {exc}") from None
        return value

    def to_python(self, value):
        return value

    def to_db(self, value):
        if value is None: return None
        return json.dumps(value)

    def from_db(self, value):
        if value is None: return None
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-8")
        if isinstance(value, str):
            return json.loads(value)
        return value  # driver already decoded it


class BinaryField(Field):
    """Raw binary data (bytes).  SQLite: ``BLOB``, MySQL: ``LONGBLOB``."""

    def __init__(self, max_length=None, **kwargs):
        super().__init__(**kwargs)
        self.max_length = max_length

    def sql_type(self): return "BLOB"

    def validate(self, value):
        value = super().validate(value)
        if value is not None:
            if not isinstance(value, (bytes, bytearray, memoryview)):
                raise TypeError(
                    f"Field '{self.name}' expects bytes, got {type(value).__name__}."
                )
            if self.max_length and len(value) > self.max_length:
                raise ValidationError(
                    f"Field '{self.name}' exceeds max_length={self.max_length}."
                )
        return value

    def to_python(self, value):
        return value

    def to_db(self, value):
        if value is None: return None
        return bytes(value) if not isinstance(value, bytes) else value

    def from_db(self, value):
        if value is None: return None
        return bytes(value) if not isinstance(value, bytes) else value
