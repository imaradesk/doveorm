"""DoveORM file-path fields (paths are validated against traversal)."""
from __future__ import annotations

import posixpath
import re

from ..exceptions import ValidationError
from .base import Field
from .text import CharField


def _reject_unsafe_path(field: Field, value: str) -> None:
    if "\x00" in value:
        raise ValidationError(f"Field '{field.name}': path contains a NUL byte.")
    segments = re.split(r"[\\/]", value)
    if ".." in segments:
        raise ValidationError(f"Field '{field.name}': path traversal ('..') is not allowed.")


class FileField(CharField):
    """Stores a file path as a string in the database.

    The actual file handling (upload, storage) is left to your
    application layer — this field just persists the path/URL.
    Values containing NUL bytes or ``..`` path segments are rejected.

    Usage:
        document = FileField(upload_to="uploads/docs/")
    """

    def __init__(self, upload_to="", max_length=255, **kwargs):
        super().__init__(max_length=max_length, **kwargs)
        self.upload_to = upload_to

    def validate(self, value):
        value = super().validate(value)
        if value is not None:
            _reject_unsafe_path(self, value)
        return value


class ImageField(FileField):
    """Stores an image file path.  Same as FileField with optional
    dimension helpers (width_field / height_field names).

    Usage:
        avatar = ImageField(upload_to="uploads/avatars/")
    """

    def __init__(self, upload_to="", width_field=None, height_field=None, **kwargs):
        super().__init__(upload_to=upload_to, **kwargs)
        self.width_field = width_field
        self.height_field = height_field


class FilePathField(CharField):
    """Stores a filesystem path, restricted to a directory.

    * ``path``      – base directory the value must live in (if set).
    * ``recursive`` – allow files in sub-directories of ``path``.
    * ``match``     – regex the file name must match.

    Validation is lexical (the filesystem is not touched).

    Usage:
        config = FilePathField(path="/etc/app/", match=r".*\\.conf$")
    """

    def __init__(self, path="", match=None, recursive=False, max_length=100, **kwargs):
        super().__init__(max_length=max_length, **kwargs)
        self.path = path
        self.match = match
        self.recursive = recursive
        self._match_re = re.compile(match) if match else None

    def validate(self, value):
        value = super().validate(value)
        if value is None:
            return None
        _reject_unsafe_path(self, value)
        if self.path:
            base = posixpath.normpath(self.path.replace("\\", "/"))
            raw = value.replace("\\", "/")
            full = posixpath.normpath(raw if posixpath.isabs(raw) else posixpath.join(base, raw))
            parent = posixpath.dirname(full)
            inside = parent == base or (self.recursive and parent.startswith(base.rstrip("/") + "/"))
            if not inside:
                raise ValidationError(
                    f"Field '{self.name}': {self._shown(value)} is not inside {self.path!r}"
                    f"{' (recursive)' if self.recursive else ''}."
                )
        if self._match_re is not None and not self._match_re.search(posixpath.basename(value)):
            raise ValidationError(f"Field '{self.name}': {self._shown(value)} does not match {self.match!r}.")
        return value
