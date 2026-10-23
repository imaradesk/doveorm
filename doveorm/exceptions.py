"""
DoveORM Exceptions

All ORM-raised errors derive from :class:`ORMError`.  Where an error used
to be a plain built-in (``ValueError``, ``RuntimeError``) the new class
also inherits from that built-in, so existing ``except`` clauses keep working.
"""
from __future__ import annotations


class ORMError(Exception):
    """Base class for every error raised by DoveORM itself."""


class ConfigurationError(ORMError, ValueError):
    """Invalid or incomplete ORM configuration."""


class ValidationError(ORMError, ValueError):
    """A field value failed validation."""


class DoesNotExist(ORMError, RuntimeError):
    """The requested row does not exist (or no longer exists)."""


class UnsafeQueryError(ORMError):
    """A potentially destructive query was refused.

    Raised for bulk ``update()`` / ``delete()`` with no WHERE clause unless
    the QuerySet was obtained from ``Model.all()``, and for bulk writes
    combined with ``limit()`` / ``offset()`` (which would otherwise be
    silently ignored).
    """


class MigrationError(ORMError):
    """A migration could not be applied or rolled back."""


class ConcurrencyError(ORMError):
    """Optimistic-locking conflict: the row was changed by someone else
    since this instance was loaded (its version no longer matches)."""


class TransactionError(ORMError):
    """A connection / transaction operation was used incorrectly
    (e.g. disconnecting while a transaction is open)."""


class QueryTimeout(ORMError, TimeoutError):
    """A statement exceeded the driver's ``statement_timeout``."""
