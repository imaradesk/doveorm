"""DoveORM date / time fields (with ``auto_now`` / ``auto_now_add`` support)."""
from __future__ import annotations

import datetime

from ..exceptions import ValidationError
from .base import Field

_UTC = datetime.timezone.utc


def _use_tz_setting() -> bool:
    from ..config import get_setting
    return bool(get_setting("USE_TZ", False))


class _AutoNowMixin:
    """Shared ``auto_now`` / ``auto_now_add`` handling."""

    def _now(self):
        raise NotImplementedError

    def pre_save(self, instance, add: bool):
        if self.auto_now or (self.auto_now_add and add and getattr(instance, self.name) is None):
            value = self._now()
            setattr(instance, self.name, value)
            return value
        return getattr(instance, self.name)


class DateField(_AutoNowMixin, Field):
    """Date (YYYY-MM-DD).  SQLite: ``TEXT``, MySQL: ``DATE``.

    ``auto_now`` sets the value on every save; ``auto_now_add`` sets it on
    insert when no value was provided.
    """

    def __init__(self, auto_now=False, auto_now_add=False, **kwargs):
        super().__init__(**kwargs)
        self.auto_now = auto_now
        self.auto_now_add = auto_now_add
        if auto_now or auto_now_add:
            self.editable = False

    def sql_type(self): return "DATE"

    def _now(self):
        return datetime.date.today()

    def _parse(self, value):
        if isinstance(value, datetime.datetime):
            return value.date()
        if isinstance(value, datetime.date):
            return value
        if isinstance(value, str):
            try:
                return datetime.date.fromisoformat(value.strip()[:10])
            except ValueError:
                raise ValidationError(f"Field '{self.name}': {self._shown(value)} is not a valid date.") from None
        raise TypeError(f"Field '{self.name}' expects date, got {type(value).__name__}.")

    def validate(self, value):
        value = super().validate(value)
        return None if value is None else self._parse(value)

    def to_python(self, value):
        if isinstance(value, (datetime.date, str)):
            try:
                return self._parse(value)
            except ValidationError:
                return value
        return value

    def to_db(self, value):
        if value is None: return None
        return self._parse(value).isoformat()

    def from_db(self, value):
        if value is None: return None
        return self._parse(value if not isinstance(value, bytes) else value.decode())


class DateTimeField(_AutoNowMixin, Field):
    """DateTime.  SQLite: ``TEXT`` (``YYYY-MM-DD HH:MM:SS[.ffffff]``),
    MySQL: ``DATETIME(6)``, PostgreSQL: ``TIMESTAMP`` (microseconds).

    Aware datetimes are converted to UTC before storage.  With
    ``use_tz=True`` (or the global ``USE_TZ`` setting) values read back are
    aware UTC datetimes and naive input is interpreted as UTC.
    """

    def __init__(self, auto_now=False, auto_now_add=False, use_tz=None, **kwargs):
        super().__init__(**kwargs)
        self.auto_now = auto_now
        self.auto_now_add = auto_now_add
        if auto_now or auto_now_add:
            self.editable = False
        self.use_tz = use_tz

    def sql_type(self): return "DATETIME"

    def _resolved_type(self, driver=None) -> str:
        if driver is not None and driver.name == "mysql":
            return "DATETIME(6)"
        return super()._resolved_type(driver)

    def _tz_enabled(self) -> bool:
        return self.use_tz if self.use_tz is not None else _use_tz_setting()

    def _now(self):
        if self._tz_enabled():
            return datetime.datetime.now(_UTC)
        return datetime.datetime.now()

    def _parse(self, value) -> datetime.datetime:
        if isinstance(value, datetime.datetime):
            return value
        if isinstance(value, datetime.date):
            return datetime.datetime(value.year, value.month, value.day)
        if isinstance(value, bytes):
            value = value.decode()
        if isinstance(value, str):
            text = value.strip()
            if text.endswith(("Z", "z")):
                text = text[:-1] + "+00:00"
            try:
                return datetime.datetime.fromisoformat(text)
            except ValueError:
                raise ValidationError(f"Field '{self.name}': {self._shown(value)} is not a valid datetime.") from None
        raise TypeError(f"Field '{self.name}' expects datetime, got {type(value).__name__}.")

    def validate(self, value):
        value = super().validate(value)
        return None if value is None else self._parse(value)

    def to_python(self, value):
        if value is None:
            return None
        try:
            dt = self._parse(value)
        except (ValidationError, TypeError):
            return value
        if self._tz_enabled() and dt.tzinfo is None:
            dt = dt.replace(tzinfo=_UTC)
        return dt

    def to_db(self, value):
        if value is None: return None
        dt = self._parse(value)
        if dt.tzinfo is not None and dt.utcoffset() is not None:
            dt = dt.astimezone(_UTC).replace(tzinfo=None)
        return dt.isoformat(sep=" ", timespec="microseconds" if dt.microsecond else "seconds")

    def from_db(self, value):
        if value is None: return None
        dt = self._parse(value)
        if self._tz_enabled():
            dt = dt.replace(tzinfo=_UTC) if dt.tzinfo is None else dt.astimezone(_UTC)
        return dt


class TimeField(_AutoNowMixin, Field):
    """Time of day (HH:MM:SS[.ffffff]).  SQLite: ``TEXT``, MySQL: ``TIME(6)``."""

    def __init__(self, auto_now=False, auto_now_add=False, **kwargs):
        super().__init__(**kwargs)
        self.auto_now = auto_now
        self.auto_now_add = auto_now_add
        if auto_now or auto_now_add:
            self.editable = False

    def sql_type(self): return "TIME"

    def _resolved_type(self, driver=None) -> str:
        if driver is not None and driver.name == "mysql":
            return "TIME(6)"
        return super()._resolved_type(driver)

    def _now(self):
        return datetime.datetime.now().time()

    def _parse(self, value) -> datetime.time:
        if isinstance(value, datetime.time):
            return value
        if isinstance(value, datetime.timedelta):        # MySQLdb returns TIME as timedelta
            total = int(value.total_seconds())
            if not 0 <= total < 86400:
                raise ValidationError(f"Field '{self.name}': {self._shown(value)} is outside a single day.")
            return (datetime.datetime.min + value).time()
        if isinstance(value, bytes):
            value = value.decode()
        if isinstance(value, str):
            try:
                return datetime.time.fromisoformat(value.strip())
            except ValueError:
                raise ValidationError(f"Field '{self.name}': {self._shown(value)} is not a valid time.") from None
        raise TypeError(f"Field '{self.name}' expects time, got {type(value).__name__}.")

    def validate(self, value):
        value = super().validate(value)
        return None if value is None else self._parse(value)

    def to_python(self, value):
        if value is None:
            return None
        try:
            return self._parse(value)
        except (ValidationError, TypeError):
            return value

    def to_db(self, value):
        if value is None: return None
        return self._parse(value).isoformat()

    def from_db(self, value):
        if value is None: return None
        return self._parse(value)


class DurationField(Field):
    """Duration / timedelta.  Stored as total microseconds (BIGINT).

    SQLite: ``INTEGER``, MySQL: ``BIGINT``.
    """
    def sql_type(self): return "BIGINT"

    def validate(self, value):
        value = super().validate(value)
        if value is not None and not isinstance(value, datetime.timedelta):
            raise TypeError(f"Field '{self.name}' expects timedelta, got {type(value).__name__}.")
        return value

    def to_python(self, value):
        if value is None or isinstance(value, datetime.timedelta):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return datetime.timedelta(microseconds=value)
        return value

    def to_db(self, value):
        if value is None: return None
        if isinstance(value, datetime.timedelta):
            # exact integer arithmetic — total_seconds() is a float and loses precision
            return (value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds
        return int(value)

    def from_db(self, value):
        if value is None: return None
        if isinstance(value, datetime.timedelta):
            return value
        return datetime.timedelta(microseconds=int(value))
