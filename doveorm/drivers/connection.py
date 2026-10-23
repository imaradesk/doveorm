"""
DoveORM – per-thread connection management and transactions.

Each thread gets its own connection (stored in a ``threading.local``).
A connection is opened lazily on first use and closed by
:meth:`disconnect` (current thread) or :meth:`close_all` (every thread).
When a thread exits, its connection is released with the thread-local
storage and closed by the garbage collector.

Safety rules enforced here:

* **Fork safety** – a connection opened in a parent process is never used
  (or closed) by a child; the child transparently opens its own.
* **Lifetime** – with ``max_lifetime`` set, connections older than that
  many seconds are recycled before the next transaction / statement
  (never in the middle of a transaction).
* **No disconnect mid-transaction** – :meth:`disconnect` refuses while a
  transaction is open (pass ``force=True`` to roll back and close).

:meth:`transaction` is implemented once for all backends: the outermost
block issues BEGIN/COMMIT/ROLLBACK, nested blocks use SAVEPOINTs.
Drivers only provide the primitive hooks (``_open``, ``_close``,
``_begin``, ``_commit``, ``_rollback``, ``_exec_control``) plus the error
classifiers ``is_transient_error`` / ``_is_disconnect_error``.
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
import weakref
from contextlib import contextmanager
from typing import Callable, TypeVar

from ..exceptions import TransactionError

log = logging.getLogger("doveorm")

T = TypeVar("T")


class _ConnectionHolder:
    """Per-thread box for a connection.

    Driver connection objects (e.g. ``sqlite3.Connection``) are not always
    weak-referenceable, so the driver tracks these holders in a WeakSet.
    When a thread dies its holder is collected and drops out of the set.
    """
    __slots__ = ("conn", "nesting", "pid", "created_at", "__weakref__")

    def __init__(self, conn):
        self.conn = conn
        self.nesting = 0
        self.pid = os.getpid()
        self.created_at = time.monotonic()


class ConnectionManagerMixin:
    """Thread-local connection lifecycle and nested transactions."""

    def __init__(self, *, max_lifetime: float | None = None):
        if max_lifetime is not None and (isinstance(max_lifetime, bool) or float(max_lifetime) <= 0):
            raise ValueError("max_lifetime must be a positive number of seconds or None.")
        self.max_lifetime = float(max_lifetime) if max_lifetime is not None else None
        self._local = threading.local()
        self._holders: "weakref.WeakSet[_ConnectionHolder]" = weakref.WeakSet()
        self._holders_lock = threading.Lock()

    # ── per-thread state ─────────────────────────

    @property
    def _holder(self) -> _ConnectionHolder | None:
        return getattr(self._local, "holder", None)

    @property
    def _conn(self):
        holder = self._holder
        return holder.conn if holder is not None else None

    @property
    def _nesting(self) -> int:
        holder = self._holder
        return holder.nesting if holder is not None else 0

    @_nesting.setter
    def _nesting(self, value: int) -> None:
        holder = self._holder
        if holder is not None:
            holder.nesting = value

    def _forget(self, holder: _ConnectionHolder) -> None:
        """Drop *holder* from tracking without touching its connection."""
        if getattr(self._local, "holder", None) is holder:
            self._local.holder = None
        with self._holders_lock:
            self._holders.discard(holder)

    # ── connection lifecycle ─────────────────────

    def connect(self) -> None:
        holder = self._holder
        if holder is not None:
            if holder.pid != os.getpid():
                # Inherited across fork(): the socket/file handle belongs to the
                # parent.  Never use or close it here — just open a fresh one.
                self._forget(holder)
                holder = None
            elif (self.max_lifetime is not None and holder.nesting == 0
                    and time.monotonic() - holder.created_at > self.max_lifetime):
                log.debug("Recycling %s connection older than %ss", self.name, self.max_lifetime)
                self.disconnect()
                holder = None
        if holder is None:
            holder = _ConnectionHolder(self._open())
            self._local.holder = holder
            with self._holders_lock:
                self._holders.add(holder)

    def disconnect(self, *, force: bool = False) -> None:
        """Close the current thread's connection.

        Raises :class:`TransactionError` while a transaction is open, unless
        ``force=True`` (the open transaction is then rolled back by closing).
        """
        holder = self._holder
        if holder is None:
            return
        if holder.pid != os.getpid():
            self._forget(holder)
            return
        if holder.nesting > 0 and not force:
            raise TransactionError(
                "Cannot disconnect while a transaction is open; "
                "finish the transaction or pass force=True."
            )
        self._forget(holder)
        try:
            self._close(holder.conn)
        except Exception:  # pragma: no cover - best effort
            log.debug("Error while closing %s connection", self.name, exc_info=True)

    def close_all(self) -> None:
        """Close every connection opened by this driver (in this process), in all threads.

        Only call this when no other thread is using the driver
        (e.g. at application shutdown).
        """
        pid = os.getpid()
        with self._holders_lock:
            holders = list(self._holders)
            self._holders.clear()
        for holder in holders:
            if holder.pid != pid:
                continue                  # belongs to the parent process
            try:
                self._close(holder.conn)
            except Exception:  # pragma: no cover - best effort
                log.debug("Error while closing %s connection", self.name, exc_info=True)
        self._local = threading.local()

    def is_connected(self) -> bool:
        holder = self._holder
        return holder is not None and holder.pid == os.getpid()

    def in_transaction(self) -> bool:
        return self._nesting > 0

    # ── error classification (overridden by drivers) ──

    def is_transient_error(self, exc: BaseException) -> bool:
        """True if retrying the whole transaction may succeed (deadlock, lock timeout …)."""
        return False

    def _is_disconnect_error(self, exc: BaseException) -> bool:
        """True if *exc* means the connection itself is unusable."""
        return False

    # ── transactions (nested via SAVEPOINTs) ─────

    @contextmanager
    def transaction(self):
        """Context manager for (possibly nested) transactions."""
        self.connect()
        conn = self._conn
        depth = self._nesting

        if depth == 0:
            self._begin(conn)
            self._nesting = 1
            try:
                yield conn
            except BaseException:
                try:
                    self._rollback(conn)
                finally:
                    self._nesting = 0
                raise
            else:
                try:
                    self._commit(conn)
                except BaseException:
                    try:
                        self._rollback(conn)
                    except Exception:  # pragma: no cover - best effort
                        log.debug("Rollback after failed commit also failed", exc_info=True)
                    raise
                finally:
                    self._nesting = 0
        else:
            sp = f"doveorm_sp_{depth}"
            self._exec_control(conn, f"SAVEPOINT {sp}")
            self._nesting = depth + 1
            try:
                yield conn
            except BaseException:
                try:
                    self._exec_control(conn, f"ROLLBACK TO SAVEPOINT {sp}")
                    self._exec_control(conn, f"RELEASE SAVEPOINT {sp}")
                finally:
                    self._nesting = depth
                raise
            else:
                try:
                    self._exec_control(conn, f"RELEASE SAVEPOINT {sp}")
                finally:
                    self._nesting = depth

    def run_in_transaction(self, fn: Callable[[], T], *, retries: int = 3,
                           backoff: float = 0.05, max_backoff: float = 1.0) -> T:
        """Run ``fn()`` inside a transaction, retrying on transient errors.

        Deadlocks, lock-wait timeouts and "database is locked" errors roll
        the transaction back and re-run ``fn`` from scratch (up to
        *retries* extra attempts, exponential backoff with jitter).  ``fn``
        must therefore be safe to re-execute.  When called inside an
        already-open transaction no retry is possible, so ``fn`` runs once
        in a savepoint.
        """
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            raise ValueError("retries must be a non-negative integer.")
        if self.in_transaction():
            with self.transaction():
                return fn()
        attempt = 0
        while True:
            try:
                with self.transaction():
                    return fn()
            except Exception as exc:
                if attempt >= retries or not self.is_transient_error(exc):
                    raise
                if self._is_disconnect_error(exc):
                    self.disconnect(force=True)
                delay = min(max_backoff, backoff * (2 ** attempt))
                delay *= random.uniform(0.5, 1.0)
                log.warning("Transient %s error (%s); retrying transaction in %.3fs (attempt %d/%d).",
                            self.name, exc, delay, attempt + 1, retries)
                time.sleep(delay)
                attempt += 1
