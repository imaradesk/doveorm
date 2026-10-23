"""DoveORM string fields: char, text, slug, email, URL and IP addresses."""
from __future__ import annotations

import ipaddress
import re

from ..exceptions import ValidationError
from .base import Field


class CharField(Field):
    """Fixed-max-length string.  SQLite: ``TEXT``, MySQL: ``VARCHAR(n)``."""

    def __init__(self, max_length=255, **kwargs):
        super().__init__(**kwargs)
        if not isinstance(max_length, int) or isinstance(max_length, bool) or max_length < 1:
            raise ValueError("CharField.max_length must be a positive integer.")
        self.max_length = max_length

    def sql_type(self): return "VARCHAR"

    def _resolved_type(self, driver=None) -> str:
        if driver is not None and driver.name == "mysql":
            return f"VARCHAR({self.max_length})"
        return "TEXT"

    def validate(self, value):
        value = super().validate(value)
        if value is not None:
            if not isinstance(value, str):
                raise TypeError(f"Field '{self.name}' expects str, got {type(value).__name__}.")
            if len(value) > self.max_length:
                raise ValidationError(
                    f"Field '{self.name}' value exceeds max_length={self.max_length}."
                )
        return value


class TextField(Field):
    """Unlimited-length text.  SQLite: ``TEXT``, MySQL: ``LONGTEXT``."""
    def sql_type(self): return "TEXT"

    def validate(self, value):
        value = super().validate(value)
        if value is not None and not isinstance(value, str):
            raise TypeError(f"Field '{self.name}' expects str, got {type(value).__name__}.")
        return value


class SlugField(CharField):
    """URL-friendly slug (letters, numbers, hyphens, underscores).

    Usage:
        slug = SlugField(max_length=100, unique=True)
    """

    def __init__(self, max_length=50, **kwargs):
        kwargs.setdefault("db_index", True)
        super().__init__(max_length=max_length, **kwargs)

    _SLUG_RE = re.compile(r"^[-a-zA-Z0-9_]+$")

    def validate(self, value):
        value = super().validate(value)
        if value is not None and not self._SLUG_RE.match(value):
            raise ValidationError(
                f"Field '{self.name}': {self._shown(value)} is not a valid slug. "
                "Only letters, numbers, hyphens and underscores are allowed."
            )
        return value


class EmailField(CharField):
    """Email address – stored as VARCHAR, validated with a basic regex.

    Usage:
        email = EmailField(max_length=254, unique=True)
    """

    def __init__(self, max_length=254, **kwargs):
        super().__init__(max_length=max_length, **kwargs)

    _EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    def validate(self, value):
        value = super().validate(value)
        if value is not None and not self._EMAIL_RE.match(value):
            raise ValidationError(f"Field '{self.name}': {self._shown(value)} is not a valid email address.")
        return value


class URLField(CharField):
    """URL – stored as VARCHAR, validated with a basic check.

    Usage:
        website = URLField(max_length=500)
    """

    def __init__(self, max_length=200, **kwargs):
        super().__init__(max_length=max_length, **kwargs)

    _URL_RE = re.compile(r"^https?://[^\s/?#]+\S*$", re.IGNORECASE)

    def validate(self, value):
        value = super().validate(value)
        if value is not None and not self._URL_RE.match(value):
            raise ValidationError(f"Field '{self.name}': {self._shown(value)} is not a valid URL.")
        return value


class IPAddressField(CharField):
    """IPv4 address stored as a string.

    Usage:
        ip = IPAddressField()
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("max_length", 15)
        super().__init__(**kwargs)

    def validate(self, value):
        value = super().validate(value)
        if value is not None:
            try:
                ipaddress.IPv4Address(value)
            except ValueError:
                raise ValidationError(
                    f"Field '{self.name}': {self._shown(value)} is not a valid IPv4 address."
                ) from None
        return value


class GenericIPAddressField(CharField):
    """IPv4 or IPv6 address stored as a string.

    Usage:
        ip = GenericIPAddressField()
    """

    _PROTOCOLS = ("both", "IPv4", "IPv6")

    def __init__(self, protocol="both", **kwargs):
        kwargs.setdefault("max_length", 45)  # max textual IPv6 (incl. mapped IPv4)
        super().__init__(**kwargs)
        if protocol not in self._PROTOCOLS:
            raise ValueError(f"protocol must be one of {self._PROTOCOLS}, got {protocol!r}.")
        self.protocol = protocol

    def validate(self, value):
        value = super().validate(value)
        if value is not None:
            try:
                addr = ipaddress.ip_address(value)
            except ValueError:
                addr = None
            ok = addr is not None and (
                self.protocol == "both"
                or (self.protocol == "IPv4" and isinstance(addr, ipaddress.IPv4Address))
                or (self.protocol == "IPv6" and isinstance(addr, ipaddress.IPv6Address))
            )
            if not ok:
                raise ValidationError(
                    f"Field '{self.name}': {self._shown(value)} is not a valid {self.protocol} address."
                )
        return value
