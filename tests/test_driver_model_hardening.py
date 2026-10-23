"""
tests/test_driver_model_hardening.py
Second hardening round: database drivers and models.
"""
import copy
import logging
import os
import pickle
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import doveorm
from doveorm import (
    Database, Model, AutoField, CharField, IntegerField, BooleanField, EmailField,
    DateTimeField, ForeignKey, BelongsTo,
    ValidationError, DoesNotExist, ConcurrencyError, TransactionError, QueryTimeout,
)
from doveorm.drivers.sqlite import SQLiteDriver

from tests.fakes import _FakeOperationalError, install_fake_mysqldb


def make_db():
    return Database(":memory:")


# ══════════════════════════════════════════════════════════
# Drivers – connection lifecycle
# ══════════════════════════════════════════════════════════

class TestForkSafety(unittest.TestCase):
    def test_child_never_reuses_parent_connection(self):
        db = make_db()
        db.execute("SELECT 1")
        parent_conn = db.driver._conn
        with mock.patch("doveorm.drivers.connection.os.getpid", return_value=os.getpid() + 1), \
                mock.patch.object(db.driver, "_close") as close:
            self.assertFalse(db.driver.is_connected())
            db.execute("SELECT 1")
            self.assertIsNot(db.driver._conn, parent_conn)
            close.assert_not_called()

    def test_close_all_skips_parent_connections(self):
        db = make_db()
        db.execute("SELECT 1")
        with mock.patch("doveorm.drivers.connection.os.getpid", return_value=os.getpid() + 1), \
                mock.patch.object(db.driver, "_close") as close:
            db.driver.close_all()
            close.assert_not_called()


class TestConnectionLifetime(unittest.TestCase):
    def test_old_connection_recycled_outside_transaction(self):
        driver = SQLiteDriver(":memory:", max_lifetime=60)
        db = Database(driver=driver)
        db.execute("SELECT 1")
        first = driver._conn
        created = driver._holder.created_at
        with mock.patch("doveorm.drivers.connection.time.monotonic", return_value=created + 61):
            db.execute("SELECT 1")
        self.assertIsNot(driver._conn, first)

    def test_not_recycled_inside_transaction(self):
        driver = SQLiteDriver(":memory:", max_lifetime=60)
        db = Database(driver=driver)
        with db.transaction():
            first = driver._conn
            created = driver._holder.created_at
            with mock.patch("doveorm.drivers.connection.time.monotonic", return_value=created + 61):
                db.execute("SELECT 1")
            self.assertIs(driver._conn, first)

    def test_invalid_lifetime(self):
        with self.assertRaises(ValueError):
            SQLiteDriver(":memory:", max_lifetime=0)


class TestDisconnectGuard(unittest.TestCase):
    def test_disconnect_inside_transaction_refused(self):
        db = make_db()
        with self.assertRaises(TransactionError):
            with db.transaction():
                db.disconnect()

    def test_force_disconnect(self):
        db = make_db()
        db.execute_script("CREATE TABLE t (x INTEGER);")
        try:
            with db.transaction():
                db.execute_write("INSERT INTO t VALUES (?)", [1])
                db.disconnect(force=True)
        except Exception:
            pass
        self.assertEqual(db.execute("SELECT COUNT(*) FROM t")[0][0], 0)


class TestRunInTransaction(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.db.execute_script("CREATE TABLE t (x INTEGER);")

    def test_retries_transient_errors(self):
        calls = []

        def work():
            calls.append(1)
            self.db.execute_write("INSERT INTO t VALUES (?)", [len(calls)])
            if len(calls) < 3:
                raise sqlite3.OperationalError("database is locked")
            return "done"

        with mock.patch("doveorm.drivers.connection.time.sleep") as sleep:
            self.assertEqual(self.db.run_in_transaction(work, retries=3), "done")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)
        # failed attempts were rolled back; only the successful one remains
        self.assertEqual([r[0] for r in self.db.execute("SELECT x FROM t")], [3])

    def test_gives_up_after_retries(self):
        def work():
            raise sqlite3.OperationalError("database is locked")

        with mock.patch("doveorm.drivers.connection.time.sleep"):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.run_in_transaction(work, retries=2)

    def test_non_transient_not_retried(self):
        calls = []

        def work():
            calls.append(1)
            raise ValueError("bug")

        with self.assertRaises(ValueError):
            self.db.run_in_transaction(work)
        self.assertEqual(len(calls), 1)

    def test_nested_runs_once_in_savepoint(self):
        with self.db.transaction():
            self.assertEqual(self.db.run_in_transaction(lambda: 42), 42)


class TestNoSerialisation(unittest.TestCase):
    def test_database_and_driver_cannot_be_pickled_or_copied(self):
        db = make_db()
        for obj in (db, db.driver):
            with self.assertRaises(TypeError):
                pickle.dumps(obj)
            with self.assertRaises(TypeError):
                copy.copy(obj)


# ══════════════════════════════════════════════════════════
# SQLite specifics
# ══════════════════════════════════════════════════════════

class TestSQLiteHardening(unittest.TestCase):
    def test_new_file_is_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "app.db")
            db = Database(path)
            db.execute("SELECT 1")
            mode = stat.S_IMODE(os.stat(path).st_mode)
            db.close_all()
        self.assertEqual(mode, 0o600)

    def test_existing_file_permissions_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "app.db")
            open(path, "wb").close()
            os.chmod(path, 0o640)
            db = Database(path)
            db.execute("SELECT 1")
            mode = stat.S_IMODE(os.stat(path).st_mode)
            db.close_all()
        self.assertEqual(mode, 0o640)

    @unittest.skipIf(sqlite3.sqlite_version_info < (3, 31, 0), "trusted_schema needs SQLite 3.31+")
    def test_trusted_schema_off(self):
        db = make_db()
        self.assertEqual(db.execute("PRAGMA trusted_schema")[0][0], 0)

    def test_statement_timeout(self):
        db = Database(driver=SQLiteDriver(":memory:", statement_timeout=0.05))
        runaway = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
                   "SELECT COUNT(*) FROM c")
        with self.assertRaises(QueryTimeout):
            db.execute(runaway)
        self.assertEqual(db.execute("SELECT 1")[0][0], 1)   # connection still usable

    def test_option_validation(self):
        with self.assertRaises(ValueError):
            SQLiteDriver(":memory:", statement_timeout=-1)
        with self.assertRaises(ValueError):
            SQLiteDriver(":memory:", file_mode=0o1777)
        with self.assertRaises(ValueError):
            SQLiteDriver("a\x00b")

    def test_transient_classification(self):
        driver = make_db().driver
        self.assertTrue(driver.is_transient_error(sqlite3.OperationalError("database is locked")))
        self.assertFalse(driver.is_transient_error(sqlite3.OperationalError("no such table: x")))


# ══════════════════════════════════════════════════════════
# MySQL specifics (recording fake MySQLdb)
# ══════════════════════════════════════════════════════════

class TestMySQLHardening(unittest.TestCase):
    def setUp(self):
        self.conns = []
        self._patch = install_fake_mysqldb(self.conns)
        logging.getLogger("doveorm").disabled = True

    def tearDown(self):
        logging.getLogger("doveorm").disabled = False
        self._patch.stop()

    def test_strict_sql_mode_on_connect(self):
        db = doveorm.MySQLDatabase(host="127.0.0.1", user="app", password="pw", database="d")
        db.execute("SELECT 1")
        query, args = self.conns[0].log[0]
        self.assertEqual(query, "SET SESSION sql_mode = %s")
        self.assertIn("STRICT_ALL_TABLES", args[0])

    def test_sql_mode_opt_out_and_backslash_tracking(self):
        db = doveorm.MySQLDatabase(host="127.0.0.1", user="app", password="pw", database="d", sql_mode=None)
        db.execute("SELECT 1")
        self.assertEqual(self.conns[0].log[0][0], "SELECT 1")
        drv = doveorm.MySQLDatabase(host="127.0.0.1", user="a", password="p", database="d",
                                sql_mode="STRICT_ALL_TABLES,NO_BACKSLASH_ESCAPES").driver
        self.assertFalse(drv.backslash_escapes)

    def test_isolation_level(self):
        db = doveorm.MySQLDatabase(host="127.0.0.1", user="a", password="p", database="d", isolation_level="read_committed")
        db.execute("SELECT 1")
        self.assertIn("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED",
                      [q for q, _ in self.conns[0].log])
        with self.assertRaises(ValueError):
            doveorm.MySQLDatabase(host="127.0.0.1", user="a", password="p", database="d", isolation_level="CHAOS; DROP")

    def test_invalid_sql_mode(self):
        with self.assertRaises(ValueError):
            doveorm.MySQLDatabase(host="127.0.0.1", user="a", password="p", database="d", sql_mode="x'; DROP TABLE t")

    def test_tls_warning_for_remote_hosts(self):
        logging.getLogger("doveorm").disabled = False
        with self.assertLogs("doveorm", level="WARNING") as cm:
            doveorm.MySQLDatabase(host="db.example.com", user="a", password="p", database="d")
        self.assertTrue(any("TLS" in line for line in cm.output))
        with self.assertNoLogs("doveorm", level="WARNING") if hasattr(self, "assertNoLogs") else _null():
            doveorm.MySQLDatabase(host="db.example.com", user="a", password="p", database="d",
                              ssl_mode="REQUIRED")

    def test_repr_has_no_password(self):
        db = doveorm.MySQLDatabase(host="127.0.0.1", user="app", password="s3cr3t-pw", database="d")
        self.assertNotIn("s3cr3t-pw", repr(db.driver))
        self.assertIn("app@127.0.0.1:3306/d", repr(db.driver))

    def test_deadlock_is_retried(self):
        db = doveorm.MySQLDatabase(host="127.0.0.1", user="a", password="p", database="d")
        attempts = []

        def work():
            attempts.append(1)
            if len(attempts) == 1:
                self.conns[-1].fail_next = _FakeOperationalError(1213, "Deadlock found")
            db.execute_write("UPDATE t SET a = ?", [1])
            return "ok"

        with mock.patch("doveorm.drivers.connection.time.sleep"):
            self.assertEqual(db.run_in_transaction(work), "ok")
        self.assertEqual(len(attempts), 2)
        queries = [q for q, _ in self.conns[-1].log]
        self.assertIn("ROLLBACK", queries)
        self.assertEqual(queries[-1], "COMMIT")

    def test_error_classification(self):
        drv = doveorm.MySQLDatabase(host="127.0.0.1", user="a", password="p", database="d").driver
        self.assertTrue(drv.is_transient_error(_FakeOperationalError(1205, "Lock wait timeout")))
        self.assertTrue(drv.is_transient_error(_FakeOperationalError(1213, "Deadlock")))
        self.assertFalse(drv.is_transient_error(_FakeOperationalError(2013, "Lost connection")))
        self.assertFalse(drv.is_transient_error(_FakeOperationalError(1062, "Duplicate")))


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ══════════════════════════════════════════════════════════
# Models
# ══════════════════════════════════════════════════════════

def account_model(db, *, version=False):
    class Account(Model):
        id         = AutoField()
        email      = EmailField(nullable=False)
        password   = CharField(nullable=False, sensitive=True)
        balance    = IntegerField(default=0)
        is_admin   = BooleanField(default=False, editable=False)
        updated_at = DateTimeField(auto_now=True, nullable=True)
        lock       = IntegerField(nullable=True)

        class Meta:
            version_field = "lock" if version else None

    Account.use(db)
    Account.create_table()
    return Account


class TestFieldNameRules(unittest.TestCase):
    def test_field_cannot_shadow_model_api(self):
        for bad in ("save", "delete", "where", "to_dict", "assign"):
            with self.assertRaises(TypeError, msg=bad):
                type(Model)("Bad", (Model,), {"__module__": __name__, bad: IntegerField()})

    def test_private_field_names_rejected(self):
        with self.assertRaises(TypeError):
            type(Model)("Bad", (Model,), {"__module__": __name__, "_secret": IntegerField()})


class TestStrictAttributes(unittest.TestCase):
    def setUp(self):
        self.Account = account_model(make_db())

    def test_typo_raises(self):
        a = self.Account(email="a@b.co", password="x")
        with self.assertRaises(AttributeError):
            a.emial = "typo@b.co"

    def test_private_and_property_setters_allowed(self):
        class WithProp(Model):
            id = AutoField()
            name = CharField(nullable=True)

            @property
            def label(self):
                return self.name

            @label.setter
            def label(self, value):
                self.name = value.upper()

        w = WithProp()
        w._scratch = 1
        w.label = "x"
        self.assertEqual(w.name, "X")

    def test_relationship_is_read_only(self):
        db = make_db()

        class Owner(Model):
            id = AutoField()

        class Pet(Model):
            id = AutoField()
            owner_id = ForeignKey("Owner", nullable=True, on_delete="SET NULL")
            owner = BelongsTo("Owner", foreign_key="owner_id")

        for cls in (Owner, Pet):
            cls.use(db)
        with self.assertRaises(AttributeError) as cm:
            Pet().owner = Owner()
        self.assertIn("foreign-key", str(cm.exception))


class TestSensitiveFields(unittest.TestCase):
    def setUp(self):
        self.Account = account_model(make_db())

    def test_to_dict_omits_sensitive(self):
        a = self.Account.create(email="a@b.co", password="hash")
        self.assertNotIn("password", a.to_dict())
        self.assertEqual(a.to_dict(include_sensitive=True)["password"], "hash")
        self.assertEqual(a.to_dict(fields=["email"]), {"email": "a@b.co"})
        with self.assertRaises(ValueError):
            a.to_dict(fields=["nope"])

    def test_errors_do_not_echo_sensitive_values(self):
        db = make_db()

        class Secretive(Model):
            id = AutoField()
            token = EmailField(sensitive=True)

        Secretive.use(db)
        Secretive.create_table()
        with self.assertRaises(ValidationError) as cm:
            Secretive.create(token="TOPSECRET-VALUE")
        self.assertNotIn("TOPSECRET", str(cm.exception))
        self.assertIn("<redacted>", str(cm.exception))


class TestMassAssignment(unittest.TestCase):
    def setUp(self):
        self.Account = account_model(make_db())

    def test_protected_fields_rejected(self):
        for payload in ({"id": 99}, {"is_admin": True}, {"updated_at": "2020-01-01"}):
            with self.assertRaises(ValidationError, msg=payload):
                self.Account.from_input({"email": "a@b.co", "password": "x", **payload})

    def test_ignore_unknown(self):
        a = self.Account.from_input(
            {"email": "a@b.co", "password": "x", "is_admin": True, "junk": 1}, ignore_unknown=True)
        a.save()
        self.assertFalse(self.Account.find(a.id).is_admin)

    def test_allowed_whitelist(self):
        a = self.Account.create(email="a@b.co", password="x", balance=5)
        with self.assertRaises(ValidationError):
            a.assign({"balance": 1_000_000}, allowed=["email"])
        a.assign({"email": "new@b.co"}, allowed=["email"]).save()
        fresh = self.Account.find(a.id)
        self.assertEqual((fresh.email, fresh.balance), ("new@b.co", 5))

    def test_allowed_cannot_include_pk(self):
        with self.assertRaises(ValueError):
            self.Account().assign({}, allowed=["id"])
        with self.assertRaises(ValueError):
            self.Account().assign({}, allowed=["nope"])

    def test_values_validated_on_save(self):
        a = self.Account.from_input({"email": "not-an-email", "password": "x"})
        with self.assertRaises(ValidationError):
            a.save()

    def test_rejects_non_mapping(self):
        with self.assertRaises(TypeError):
            self.Account().assign([("email", "x")])


class TestCleanHook(unittest.TestCase):
    def test_clean_blocks_save(self):
        db = make_db()

        class Range(Model):
            id = AutoField()
            low = IntegerField()
            high = IntegerField()

            def clean(self):
                if self.low > self.high:
                    raise ValidationError("low must be <= high")

        Range.use(db)
        Range.create_table()
        with self.assertRaises(ValidationError):
            Range.create(low=5, high=1)
        self.assertEqual(Range.all().count(), 0)
        r = Range.create(low=1, high=5)
        r.low = 9
        with self.assertRaises(ValidationError):
            r.full_clean()


class TestUpdateFields(unittest.TestCase):
    def setUp(self):
        self.Account = account_model(make_db())

    def test_only_listed_fields_written(self):
        a = self.Account.create(email="a@b.co", password="x", balance=1)
        stamp = self.Account.find(a.id).updated_at
        a.email = "z@b.co"
        a.balance = 999
        a.save(update_fields=["email"])
        fresh = self.Account.find(a.id)
        self.assertEqual((fresh.email, fresh.balance, fresh.updated_at), ("z@b.co", 1, stamp))

    def test_invalid_update_fields(self):
        a = self.Account(email="a@b.co", password="x")
        with self.assertRaises(ValueError):
            a.save(update_fields=["email"])          # insert
        a.save()
        for bad in (["nope"], ["id"]):
            with self.assertRaises(ValueError):
                a.save(update_fields=bad)
        with self.assertRaises(TypeError):
            a.save(update_fields="email")


class TestOptimisticLocking(unittest.TestCase):
    def setUp(self):
        self.Account = account_model(make_db(), version=True)

    def test_version_starts_at_one_and_increments(self):
        a = self.Account.create(email="a@b.co", password="x")
        self.assertEqual(a.lock, 1)
        a.balance = 5
        a.save()
        self.assertEqual(a.lock, 2)
        self.assertEqual(self.Account.find(a.id).lock, 2)

    def test_stale_save_raises(self):
        a = self.Account.create(email="a@b.co", password="x")
        first, second = self.Account.find(a.id), self.Account.find(a.id)
        first.balance = 10
        first.save()
        second.balance = 20
        with self.assertRaises(ConcurrencyError):
            second.save()
        self.assertEqual(self.Account.find(a.id).balance, 10)
        second.refresh()
        second.balance = 20
        second.save()
        self.assertEqual(self.Account.find(a.id).balance, 20)

    def test_stale_delete_raises(self):
        a = self.Account.create(email="a@b.co", password="x")
        stale = self.Account.find(a.id)
        a.balance = 1
        a.save()
        with self.assertRaises(ConcurrencyError):
            stale.delete()
        self.assertIsNotNone(self.Account.find(a.id))

    def test_deleted_row_is_not_a_conflict(self):
        a = self.Account.create(email="a@b.co", password="x")
        self.Account.where(id=a.id).delete()
        a.balance = 3
        with self.assertRaises(DoesNotExist):
            a.save()

    def test_bulk_update_bumps_version(self):
        a = self.Account.create(email="a@b.co", password="x")
        self.Account.where(id=a.id).update(balance=7)
        self.assertEqual(self.Account.find(a.id).lock, 2)
        a.balance = 8
        with self.assertRaises(ConcurrencyError):
            a.save()

    def test_select_always_loads_version(self):
        self.Account.create(email="a@b.co", password="x")
        row = self.Account.all().select("email").first()
        self.assertEqual(row.lock, 1)
        row.email = "n@b.co"
        row.save()
        self.assertEqual(row.lock, 2)

    def test_version_not_mass_assignable(self):
        with self.assertRaises(ValidationError):
            self.Account.from_input({"email": "a@b.co", "password": "x", "lock": 50})

    def test_version_field_must_be_integer(self):
        with self.assertRaises(TypeError):
            class BadVersion(Model):
                id = AutoField()
                v = CharField()

                class Meta:
                    version_field = "v"


class TestModelGuards(unittest.TestCase):
    def test_find_none(self):
        Account = account_model(make_db())
        self.assertIsNone(Account.find(None))

    def test_abstract_model_cannot_be_queried(self):
        class Base(Model):
            name = CharField()

            class Meta:
                abstract = True

        with self.assertRaises(TypeError):
            Base.where(name="x")
        with self.assertRaises(TypeError):
            Base.all()

    def test_global_config_fallback(self):
        """Regression: Model._ensure_db must resolve the global config."""
        try:
            doveorm.configure(DATABASE_PATH=":memory:")

            class Unbound(Model):
                id = AutoField()

            Unbound._db = None                 # force the lazy lookup path
            self.assertIs(Unbound._ensure_db(), doveorm.get_database())
        finally:
            doveorm.reset_config()



# ══════════════════════════════════════════════════════════
# Configuration is owned by the application
# ══════════════════════════════════════════════════════════

class TestApplicationOwnedConfig(unittest.TestCase):
    def tearDown(self):
        doveorm.reset_config()

    def test_no_deployment_values_in_defaults(self):
        from doveorm.config.defaults import DEFAULTS
        for key in ("DATABASE_PATH", "DATABASE_NAME", "DATABASE_HOST",
                    "DATABASE_USER", "DATABASE_PASSWORD"):
            self.assertIsNone(DEFAULTS[key], key)
        self.assertNotIn("MEDIA_ROOT", DEFAULTS)
        self.assertNotIn("MEDIA_URL", DEFAULTS)

    def test_sqlite_requires_explicit_path(self):
        with self.assertRaises(doveorm.ConfigurationError):
            doveorm.configure()
        self.assertIsNotNone(doveorm.configure(DATABASE_PATH=":memory:"))

    def test_mysql_requires_connection_details(self):
        with self.assertRaises(doveorm.ConfigurationError) as cm:
            doveorm.configure(DATABASE_ENGINE="mysql")
        for name in ("DATABASE_NAME", "DATABASE_USER", "DATABASE_PASSWORD", "DATABASE_HOST"):
            self.assertIn(name, str(cm.exception))

    def test_mysql_explicit_empty_password_and_socket(self):
        conns = []
        patch = install_fake_mysqldb(conns)
        logging.getLogger("doveorm").disabled = True
        try:
            db = doveorm.configure(
                DATABASE_ENGINE="mysql", DATABASE_NAME="d", DATABASE_USER="u",
                DATABASE_PASSWORD="", DATABASE_OPTIONS={"unix_socket": "/tmp/mysql.sock"},
            )
            db.execute("SELECT 1")
            self.assertNotIn("host", conns[0].cfg)
            self.assertEqual(conns[0].cfg["unix_socket"], "/tmp/mysql.sock")
            self.assertIn("socket:/tmp/mysql.sock", repr(db.driver))
        finally:
            logging.getLogger("doveorm").disabled = False
            patch.stop()

    def test_constructors_do_not_guess(self):
        with self.assertRaises(doveorm.ConfigurationError):
            Database()
        with self.assertRaises(doveorm.ConfigurationError):
            Database(":memory:", driver=SQLiteDriver(":memory:"))
        with self.assertRaises(TypeError):
            SQLiteDriver()
        for kwargs in ({}, {"host": "h", "user": "u", "database": "d"},
                       {"host": "h", "password": "p", "database": "d"},
                       {"user": "u", "password": "p", "database": "d"}):
            with self.assertRaises(doveorm.ConfigurationError, msg=kwargs):
                doveorm.MySQLDatabase(**kwargs)

    def test_unconfigured_get_database_raises(self):
        with self.assertRaises(doveorm.ConfigurationError):
            doveorm.get_database()

    def test_unconfigured_model_raises(self):
        class Lonely(Model):
            id = AutoField()

        with self.assertRaises(RuntimeError):
            Lonely.all().count()

    def test_configure_from_file_deprecated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.py")
            with open(path, "w") as fh:
                fh.write('DATABASE_PATH = ":memory:"\n')
            with self.assertWarns(DeprecationWarning):
                doveorm.configure_from_file(path)


if __name__ == "__main__":
    unittest.main()
