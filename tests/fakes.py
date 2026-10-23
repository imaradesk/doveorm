"""Test doubles shared by the test suites (a recording fake ``MySQLdb``)."""
import sys
import types
from unittest import mock


class _FakeOperationalError(Exception):
    pass


class _FakeIntegrityError(Exception):
    pass


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self.rowcount = 0
        self.lastrowid = 0
        self._rows = []

    def execute(self, query, args=None):
        self.conn.log.append((query, args))
        if self.conn.fail_next is not None:
            exc, self.conn.fail_next = self.conn.fail_next, None
            raise exc
        if query.lstrip().upper().startswith("SELECT"):
            self.description = [("x",)]
            self._rows = []
        else:
            self.description = None
            self.rowcount = 1
            self.lastrowid = 1

    def executemany(self, query, rows):
        self.conn.log.append((query, rows))
        self.rowcount = len(rows)

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = []
        self.autocommit_mode = None
        self.fail_next = None
        self.closed = False

    def autocommit(self, flag):
        self.autocommit_mode = flag

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.log.append(("COMMIT", None))

    def rollback(self):
        self.log.append(("ROLLBACK", None))

    def ping(self):
        pass

    def close(self):
        self.closed = True


def install_fake_mysqldb(conns: list):
    """Patch ``sys.modules`` with a fake MySQLdb; returns the started patcher."""
    fake = types.ModuleType("MySQLdb")
    fake.OperationalError = _FakeOperationalError
    fake.IntegrityError = _FakeIntegrityError

    def connect(**cfg):
        conn = _FakeConn(cfg)
        conns.append(conn)
        return conn

    fake.connect = connect
    constants = types.ModuleType("MySQLdb.constants")
    constants.CLIENT = types.SimpleNamespace(FOUND_ROWS=2)
    fake.constants = constants
    patcher = mock.patch.dict(sys.modules, {"MySQLdb": fake, "MySQLdb.constants": constants})
    patcher.start()
    return patcher
