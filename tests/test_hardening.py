"""
tests/test_hardening.py
Regression tests for the security / correctness / performance fixes
listed in report.md.  Each test names the finding it covers.
"""
import datetime
import logging
import os
import sys
import threading
import unittest
import uuid
import warnings
from decimal import Decimal
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import doveorm
from doveorm import (
    Database, Model, Q, Migrator,
    AutoField, BigAutoField, CharField, IntegerField, BooleanField, TextField,
    DateTimeField, DateField, DecimalField, DurationField, UUIDField, JSONField,
    FileField, FilePathField, ForeignKey, HasMany, BelongsTo, ManyToMany,
    ValidationError, DoesNotExist, UnsafeQueryError, MigrationError, ConfigurationError,
)
from doveorm.utils.sql import split_statements, translate_qmark, quote_literal
from doveorm.drivers.mysql import _DictRow

from tests.fakes import _FakeOperationalError, install_fake_mysqldb
from doveorm.model import ModelMeta


def make_db():
    return Database(":memory:")


class CountingDB:
    """Wrap a Database to record every SQL statement executed."""

    def __init__(self, db):
        self.db = db
        self.sql = []
        for name in ("execute", "execute_write", "execute_many"):
            original = getattr(db, name)

            def wrapper(sql, *args, _orig=original, **kw):
                self.sql.append(sql)
                return _orig(sql, *args, **kw)

            setattr(db, name, wrapper)


def user_model(db):
    class User(Model):
        id     = AutoField()
        name   = CharField(max_length=20, nullable=False)
        age    = IntegerField(default=0)
        active = BooleanField(default=True)

    User.use(db)
    User.create_table()
    return User


# ══════════════════════════════════════════════════════════
# S1 – LIMIT / OFFSET injection
# ══════════════════════════════════════════════════════════

class TestLimitOffsetInjection(unittest.TestCase):
    def setUp(self):
        self.User = user_model(make_db())
        self.User.create(name="a")

    def test_string_payload_rejected(self):
        payload = "(SELECT CASE WHEN 1=1 THEN 1 ELSE 0 END)"
        with self.assertRaises(TypeError):
            self.User.all().limit(payload)
        with self.assertRaises(TypeError):
            self.User.all().offset("0; DROP TABLE users")

    def test_negative_and_bool_rejected(self):
        with self.assertRaises(ValueError):
            self.User.all().limit(-1)
        with self.assertRaises(TypeError):
            self.User.all().limit(True)

    def test_offset_without_limit_is_valid_sql(self):
        self.User.create(name="b")
        self.assertEqual([u.name for u in self.User.all().order_by("id").offset(1)], ["b"])


# ══════════════════════════════════════════════════════════
# S2 – DEFAULT literal escaping
# ══════════════════════════════════════════════════════════

class TestDefaultLiteralEscaping(unittest.TestCase):
    def test_quote_in_default(self):
        db = make_db()

        class Note(Model):
            id   = AutoField()
            text = CharField(default="it's")

        Note.use(db)
        Note.create_table()
        db.execute_write("INSERT INTO notes (id) VALUES (?)", [1])
        self.assertEqual(Note.find(1).text, "it's")

    def test_injection_payload_is_inert(self):
        db = make_db()
        user_model(db)

        class Bad(Model):
            id   = AutoField()
            name = CharField(default="x'); DROP TABLE users; --")

        Bad.use(db)
        Bad.create_table()
        self.assertTrue(db.table_exists("users"))
        self.assertEqual(Bad.create().name, "x'); DROP TABLE users; --")

    def test_quote_literal_mysql_backslashes(self):
        self.assertEqual(quote_literal("a\\'b", backslash_escapes=True), "'a\\\\''b'")
        self.assertIsNone(quote_literal(float("nan")))
        self.assertEqual(quote_literal(b"\x01\xff"), "X'01ff'")


# ══════════════════════════════════════════════════════════
# S3 – identifier validation
# ══════════════════════════════════════════════════════════

class TestIdentifierValidation(unittest.TestCase):
    def test_bad_table_name(self):
        with self.assertRaises(ValueError):
            class A(Model):
                id = AutoField()

                class Meta:
                    table_name = "x; DROP TABLE y"

    def test_bad_table_decorator_name(self):
        with self.assertRaises(ValueError):
            @doveorm.table(name="bad name")
            class B(Model):
                id = AutoField()

    def test_unknown_index_column(self):
        with self.assertRaises(ValueError):
            class C(Model):
                id = AutoField()

                class Meta:
                    indexes = [("nope",)]

    def test_on_delete_whitelist(self):
        with self.assertRaises(ValueError):
            ForeignKey("X", on_delete="CASCADE; DROP TABLE x")
        with self.assertRaises(ValueError):
            ForeignKey("X", on_delete="SET NULL", nullable=False)
        self.assertEqual(ForeignKey("X", on_delete="set  null").on_delete, "SET NULL")

    def test_on_delete_constants(self):
        import doveorm
        from doveorm import fields
        self.assertEqual(ForeignKey("X", on_delete=fields.CASCADE).on_delete, "CASCADE")
        self.assertEqual(ForeignKey("X", on_delete=fields.SET_NULL, nullable=True).on_delete, "SET NULL")
        self.assertEqual(ForeignKey("X", on_delete="set_null", nullable=True).on_delete, "SET NULL")
        self.assertEqual(ForeignKey("X", on_delete=doveorm.RESTRICT).on_delete, "RESTRICT")
        self.assertEqual(ForeignKey("X", on_delete=fields.NO_ACTION).on_delete, "NO ACTION")
        self.assertEqual(ForeignKey("X", on_delete=fields.SET_DEFAULT).on_delete, "SET DEFAULT")
        with self.assertRaises(ValueError):
            ForeignKey("X", on_delete=fields.SET_NULL, nullable=False)

    def test_m2m_through_validated(self):
        with self.assertRaises(ValueError):
            ManyToMany("X", through="j; DROP TABLE x")

    def test_engine_validated(self):
        with self.assertRaises(ValueError):
            @doveorm.table(engine="InnoDB; DROP TABLE x")
            class D(Model):
                id = AutoField()


# ══════════════════════════════════════════════════════════
# S4 – query logging redaction
# ══════════════════════════════════════════════════════════

class TestLogRedaction(unittest.TestCase):
    def test_params_redacted_by_default(self):
        db = Database(":memory:", log_queries=True)
        with self.assertLogs("doveorm", level="DEBUG") as cm:
            db.execute("SELECT ?", ["hunter2"])
        out = "\n".join(cm.output)
        self.assertNotIn("hunter2", out)
        self.assertIn("redacted", out)

    def test_params_logged_when_opted_in(self):
        db = Database(":memory:", log_queries=True, log_params=True)
        with self.assertLogs("doveorm", level="DEBUG") as cm:
            db.execute("SELECT ?", ["visible"])
        self.assertIn("visible", "\n".join(cm.output))


# ══════════════════════════════════════════════════════════
# S5 – unfiltered bulk writes
# ══════════════════════════════════════════════════════════

class TestUnsafeBulkWrites(unittest.TestCase):
    def setUp(self):
        self.User = user_model(make_db())
        for n in ("a", "b", "c"):
            self.User.create(name=n)

    def test_where_without_filters_refuses_delete(self):
        with self.assertRaises(UnsafeQueryError):
            self.User.where().delete()
        with self.assertRaises(UnsafeQueryError):
            self.User.where(**{}).update(age=1)
        self.assertEqual(self.User.all().count(), 3)

    def test_all_allows_bulk(self):
        self.assertEqual(self.User.all().update(age=5), 3)
        self.assertEqual(self.User.all().delete(), 3)

    def test_limit_with_bulk_write_refused(self):
        with self.assertRaises(UnsafeQueryError):
            self.User.where(active=True).limit(1).delete()
        self.assertEqual(self.User.all().count(), 3)


# ══════════════════════════════════════════════════════════
# S6 / config
# ══════════════════════════════════════════════════════════

class TestConfig(unittest.TestCase):
    def tearDown(self):
        doveorm.reset_config()

    def test_unknown_setting_rejected(self):
        with self.assertRaises(ConfigurationError):
            doveorm.configure(DATABSE_PATH="typo.db")

    def test_env_bad_int(self):
        with mock.patch.dict(os.environ, {"DOVEORM_DATABASE_PORT": "abc"}):
            with self.assertRaises(ConfigurationError):
                doveorm.configure_from_env()

    def test_env_json_options(self):
        with mock.patch.dict(os.environ, {"DOVEORM_DATABASE_PATH": ":memory:",
                                     "DOVEORM_DATABASE_OPTIONS": '{"timeout": 2}'}):
            db = doveorm.configure_from_env()
        self.assertEqual(db.driver.timeout, 2.0)

    def test_reconfigure_rewires_and_closes(self):
        db1 = doveorm.configure(DATABASE_PATH=":memory:")

        class Wired(Model):
            id = AutoField()

        self.assertIs(Wired._db, db1)
        explicit = make_db()

        class Pinned(Model):
            id = AutoField()

        Pinned.use(explicit)
        db2 = doveorm.configure(DATABASE_PATH=":memory:")
        self.assertIs(Wired._db, db2)
        self.assertIs(Pinned._db, explicit)

    def test_unconfigured_raises(self):
        doveorm.reset_config()
        with self.assertRaises(ConfigurationError):
            doveorm.get_database()

    def test_mysql_requires_name(self):
        with self.assertRaises(ConfigurationError):
            doveorm.configure(DATABASE_ENGINE="mysql", DATABASE_PASSWORD="x")


# ══════════════════════════════════════════════════════════
# S9 – callables passed as values are not executed
# ══════════════════════════════════════════════════════════

class TestCallableValues(unittest.TestCase):
    def test_callable_value_not_called(self):
        User = user_model(make_db())
        called = []
        with self.assertRaises(TypeError):
            User.create(name=lambda: called.append(1) or "x")
        self.assertEqual(called, [])

    def test_unknown_kwarg_rejected(self):
        User = user_model(make_db())
        with self.assertRaises(TypeError):
            User(nmae="typo")


# ══════════════════════════════════════════════════════════
# S10 – placeholder translation
# ══════════════════════════════════════════════════════════

class TestPlaceholderTranslation(unittest.TestCase):
    def test_qmark_in_literal_untouched(self):
        sql = "SELECT * FROM t WHERE a = ? AND b = 'why?' AND c LIKE '50%'"
        self.assertEqual(
            translate_qmark(sql, "%s"),
            "SELECT * FROM t WHERE a = %s AND b = 'why?' AND c LIKE '50%%'",
        )

    def test_native_paramstyle_passthrough(self):
        sql = "SELECT * FROM t WHERE a = %s"
        self.assertEqual(translate_qmark(sql, "%s"), sql)

    def test_split_respects_literals_and_comments(self):
        script = "CREATE TABLE a (x TEXT DEFAULT 'a;b'); -- c;\n/* d; */ INSERT INTO a VALUES ('e;f');"
        self.assertEqual(len(split_statements(script)), 2)

    def test_sqlite_trigger_script(self):
        db = make_db()
        db.execute_script("""
            CREATE TABLE a (x INTEGER);
            CREATE TABLE b (n INTEGER);
            CREATE TRIGGER t AFTER INSERT ON a BEGIN INSERT INTO b VALUES (NEW.x); INSERT INTO b VALUES (NEW.x); END;
            INSERT INTO a VALUES (7);
        """)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM b")[0][0], 2)


# ══════════════════════════════════════════════════════════
# B3 / B4 – save() correctness
# ══════════════════════════════════════════════════════════

class TestSaveCorrectness(unittest.TestCase):
    def setUp(self):
        self.User = user_model(make_db())

    def test_explicit_autofield_pk_is_inserted(self):
        u = self.User(id=99, name="c")
        u.save()
        self.assertEqual(u.id, 99)
        self.assertEqual(self.User.find(99).name, "c")

    def test_save_of_deleted_row_raises(self):
        u = self.User.create(name="a")
        self.User.where(id=u.id).delete()
        u.name = "b"
        with self.assertRaises(DoesNotExist):
            u.save()

    def test_partial_select_does_not_clobber(self):
        self.User.create(name="a", age=42, active=False)
        u = self.User.all().select("name").first()
        self.assertEqual(u.age, 0)                # default shown, as before
        u.name = "z"
        u.save()
        fresh = self.User.all().first()
        self.assertEqual((fresh.name, fresh.age, fresh.active), ("z", 42, False))

    def test_assigning_deferred_field_saves_it(self):
        self.User.create(name="a", age=42)
        u = self.User.all().select("name").first()
        u.age = 7
        u.save()
        self.assertEqual(self.User.all().first().age, 7)

    def test_pk_change_updates_row(self):
        u = self.User.create(name="a")
        old = u.id
        u.id = 500
        u.save()
        self.assertIsNone(self.User.find(old))
        self.assertEqual(self.User.find(500).name, "a")

    def test_non_auto_pk_required(self):
        db = make_db()

        class Token(Model):
            key = CharField(primary_key=True, max_length=10)

        Token.use(db)
        Token.create_table()
        with self.assertRaises(ValidationError):
            Token.create()

    def test_values_normalised_on_save(self):
        db = make_db()

        class Price(Model):
            id = AutoField()
            amount = DecimalField(max_digits=6, decimal_places=2)

        Price.use(db)
        Price.create_table()
        p = Price.create(amount="1.5")
        self.assertEqual(p.amount, Decimal("1.5"))
        self.assertEqual(Price.find(p.id).amount, Decimal("1.50"))


# ══════════════════════════════════════════════════════════
# B5 / B13 / B20 – lookups
# ══════════════════════════════════════════════════════════

class TestLookups(unittest.TestCase):
    def setUp(self):
        self.User = user_model(make_db())
        self.User.create(name="Alice", age=30)
        self.User.create(name="bob_100%", age=20)
        self.User.create(name="Carol", age=None if False else 40)

    def test_unknown_lookup_raises(self):
        with self.assertRaises(ValueError):
            self.User.where(age__gtee=18)
        with self.assertRaises(ValueError):
            self.User.where(name__nope="x")

    def test_contains_escapes_wildcards(self):
        self.assertEqual(len(self.User.where(name__contains="100%").get()), 1)
        self.assertEqual(len(self.User.where(name__contains="_").get()), 1)
        self.assertEqual(len(self.User.where(name__contains="%").get()), 1)

    def test_case_insensitive_variants(self):
        self.assertEqual(self.User.where(name__icontains="ALI").first().name, "Alice")
        self.assertEqual(self.User.where(name__istartswith="car").first().name, "Carol")
        self.assertEqual(self.User.where(name__iexact="alice").count(), 1)

    def test_range(self):
        self.assertEqual(self.User.where(age__range=(25, 35)).count(), 1)

    def test_empty_in(self):
        self.assertEqual(self.User.where(id__in=[]).get(), [])
        self.assertEqual(self.User.where(id__not_in=[]).count(), 3)
        self.assertIn("1 = 0", self.User.where(id__in=[])._build_where(self.User._get_driver())[0])

    def test_in_with_none(self):
        db = make_db()

        class T(Model):
            id = AutoField()
            v = IntegerField(nullable=True)

        T.use(db)
        T.create_table()
        T.create(v=1); T.create(v=None); T.create(v=2)
        self.assertEqual(T.where(v__in=[1, None]).count(), 2)
        self.assertEqual(T.where(v__not_in=[1, None]).count(), 1)
        self.assertEqual(T.where(v=None).count(), 1)
        self.assertEqual(T.where(v__ne=None).count(), 2)

    def test_in_rejects_string(self):
        with self.assertRaises(TypeError):
            self.User.where(name__in="abc").get()

    def test_is_null_requires_bool(self):
        with self.assertRaises(TypeError):
            self.User.where(name__is_null="false").get()

    def test_q_unknown_field_raises_early(self):
        with self.assertRaises(ValueError):
            self.User.all().filter(Q(nope=1))

    def test_model_filter_and_exclude(self):
        self.assertEqual(self.User.filter(Q(age__gte=30) | Q(name="x")).count(), 2)
        self.assertEqual(self.User.exclude(name="Alice").count(), 2)


# ══════════════════════════════════════════════════════════
# B6 – :memory: shared across threads
# ══════════════════════════════════════════════════════════

class TestMemoryThreads(unittest.TestCase):
    def test_threads_see_same_memory_db(self):
        db = make_db()
        User = user_model(db)
        User.create(name="main")
        seen = []

        def worker():
            seen.append(User.all().count())
            User.create(name="thread")

        t = threading.Thread(target=worker)
        t.start(); t.join()
        self.assertEqual(seen, [1])
        self.assertEqual(User.all().count(), 2)

    def test_data_survives_thread_disconnect(self):
        db = make_db()
        User = user_model(db)
        User.create(name="x")
        db.disconnect()
        self.assertEqual(User.all().count(), 1)

    def test_separate_memory_databases_are_isolated(self):
        a, b = make_db(), make_db()
        a.execute_script("CREATE TABLE t (x INTEGER);")
        self.assertFalse(b.table_exists("t"))


# ══════════════════════════════════════════════════════════
# B8 / B14 – datetimes and auto_now
# ══════════════════════════════════════════════════════════

class TestDateTimes(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Event(Model):
            id         = AutoField()
            at         = DateTimeField(nullable=True)
            at_tz      = DateTimeField(nullable=True, use_tz=True)
            created_at = DateTimeField(auto_now_add=True, nullable=True)
            updated_at = DateTimeField(auto_now=True, nullable=True)
            day        = DateField(auto_now_add=True, nullable=True)

        Event.use(self.db)
        Event.create_table()
        self.Event = Event

    def test_microseconds_preserved(self):
        dt = datetime.datetime(2024, 5, 1, 12, 30, 15, 123456)
        e = self.Event.create(at=dt)
        self.assertEqual(self.Event.find(e.id).at, dt)

    def test_aware_converted_to_utc(self):
        eat = datetime.timezone(datetime.timedelta(hours=3))
        e = self.Event.create(at_tz=datetime.datetime(2024, 1, 1, 15, 0, tzinfo=eat))
        got = self.Event.find(e.id).at_tz
        self.assertEqual(got, datetime.datetime(2024, 1, 1, 12, 0, tzinfo=datetime.timezone.utc))
        self.assertEqual(got.tzinfo, datetime.timezone.utc)

    def test_filter_with_aware_datetime(self):
        utc = datetime.timezone.utc
        self.Event.create(at_tz=datetime.datetime(2024, 1, 1, tzinfo=utc))
        self.Event.create(at_tz=datetime.datetime(2025, 1, 1, tzinfo=utc))
        self.assertEqual(self.Event.where(at_tz__gte=datetime.datetime(2024, 6, 1, tzinfo=utc)).count(), 1)

    def test_auto_now_and_auto_now_add(self):
        e = self.Event.create()
        self.assertIsNotNone(e.created_at)
        self.assertIsNotNone(e.updated_at)
        self.assertEqual(e.day, datetime.date.today())
        created, first_update = e.created_at, e.updated_at
        e.save()
        self.assertEqual(e.created_at, created)
        self.assertGreaterEqual(e.updated_at, first_update)

    def test_duration_precision(self):
        db = make_db()

        class D(Model):
            id = AutoField()
            d = DurationField()

        D.use(db)
        D.create_table()
        big = datetime.timedelta(days=99999, microseconds=1)
        self.assertEqual(D.find(D.create(d=big).id).d, big)


# ══════════════════════════════════════════════════════════
# B9 – migrations
# ══════════════════════════════════════════════════════════

class TestMigrations(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Mig(Model):
            id = AutoField()

        Mig.use(self.db)
        self.Mig = Mig

    def test_failed_migration_is_atomic(self):
        m = Migrator(self.db)

        def boom():
            self.Mig.create_table()
            raise RuntimeError("boom")

        m.register("0001", boom)
        with self.assertRaises(MigrationError):
            m.migrate()
        self.assertFalse(self.db.table_exists("migs"))
        self.assertEqual(m.applied(), [])

    def test_rollback_order_is_by_id(self):
        m = Migrator(self.db)
        order = []
        for name in ("0001", "0002", "0003"):
            m.register(name, lambda: None, rollback_fn=lambda n=name: order.append(n))
        m.migrate()
        m.rollback(steps=2)
        self.assertEqual(order, ["0003", "0002"])
        self.assertEqual(m.applied(), ["0001"])

    def test_rollback_without_fn_refused(self):
        m = Migrator(self.db)
        m.register("0001", lambda: None)
        m.migrate()
        with self.assertRaises(MigrationError):
            m.rollback()
        self.assertEqual(m.applied(), ["0001"])
        m.rollback(force=True)
        self.assertEqual(m.applied(), [])

    def test_duplicate_registration(self):
        m = Migrator(self.db)
        m.register("0001", lambda: None)
        with self.assertRaises(ValueError):
            m.register("0001", lambda: None)

    def test_pending(self):
        m = Migrator(self.db)
        m.register("0001", lambda: None)
        m.register("0002", lambda: None)
        self.assertEqual(m.pending(), ["0001", "0002"])
        m.migrate()
        self.assertEqual(m.pending(), [])


# ══════════════════════════════════════════════════════════
# B10 – DDL inside a transaction is not auto-committed
# ══════════════════════════════════════════════════════════

class TestTransactionalDDL(unittest.TestCase):
    def test_create_table_rolls_back(self):
        db = make_db()

        class Tx(Model):
            id = AutoField()

        Tx.use(db)
        with self.assertRaises(RuntimeError):
            with db.transaction():
                Tx.create_table()
                raise RuntimeError
        self.assertFalse(db.table_exists("txes"))

    def test_execute_script_joins_outer_transaction(self):
        db = make_db()
        db.execute_script("CREATE TABLE t (x INTEGER);")
        with self.assertRaises(RuntimeError):
            with db.transaction():
                db.execute_script("INSERT INTO t VALUES (1); INSERT INTO t VALUES (2);")
                raise RuntimeError
        self.assertEqual(db.execute("SELECT COUNT(*) FROM t")[0][0], 0)


# ══════════════════════════════════════════════════════════
# B11 / B12 / B15 / B16 / B17 / B18 / B19 / B20 – misc correctness
# ══════════════════════════════════════════════════════════

class TestMiscCorrectness(unittest.TestCase):
    def setUp(self):
        self.User = user_model(make_db())
        for i, n in enumerate("abcde"):
            self.User.create(name=n, age=i)

    def test_count_respects_limit(self):
        qs = self.User.all().limit(2)
        self.assertEqual(qs.count(), 2)
        self.assertEqual(self.User.all().offset(4).count(), 1)
        self.assertEqual(len(qs), 2)

    def test_update_validates(self):
        with self.assertRaises(ValueError):
            self.User.where(name="a").update(nope=1)
        with self.assertRaises(ValidationError):
            self.User.where(name="a").update(name="x" * 50)
        with self.assertRaises(TypeError):
            self.User.where(name="a").update(age="old")

    def test_last_reverses_ordering(self):
        self.assertEqual(self.User.all().order_by("name").last().name, "e")
        self.assertEqual(self.User.all().order_by("-name").last().name, "a")
        self.assertEqual(self.User.all().last().name, "e")

    def test_meta_ordering_applies_to_where(self):
        db = make_db()

        class Ordered(Model):
            id = AutoField()
            n = IntegerField()

            class Meta:
                ordering = ["-n"]

        Ordered.use(db)
        Ordered.create_table()
        for v in (1, 3, 2):
            Ordered.create(n=v)
        self.assertEqual([o.n for o in Ordered.where(n__gte=1)], [3, 2, 1])

    def test_identity_semantics(self):
        a, b = self.User(name="x"), self.User(name="x")
        self.assertNotEqual(a, b)
        self.assertEqual(a, a)
        with self.assertRaises(TypeError):
            hash(a)

    def test_pk_auto_injected(self):
        db = make_db()
        with warnings.catch_warnings():
            warnings.simplefilter("error")

            class NoPk(Model):
                label = CharField()

        NoPk.use(db)
        NoPk.create_table()
        self.assertIsInstance(NoPk._fields["id"], AutoField)
        self.assertEqual(NoPk.create(label="x").id, 1)

    def test_two_pks_rejected(self):
        with self.assertRaises(TypeError):
            class TwoPk(Model):
                a = AutoField()
                b = CharField(primary_key=True)

    def test_registry_collision_warns(self):
        original = ModelMeta._registry.get("User")
        try:
            with self.assertWarns(RuntimeWarning):
                ModelMeta("User", (Model,), {"__module__": "some.other.module", "id": AutoField()})
        finally:
            ModelMeta._registry["User"] = original

    def test_get_or_create_race(self):
        db = make_db()

        class Slot(Model):
            id = AutoField()
            code = CharField(unique=True)

        Slot.use(db)
        Slot.create_table()
        Slot.create(code="A")
        real_first = doveorm.QuerySet.first
        calls = {"n": 0}

        def stale_first(qs):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_first(qs)

        with mock.patch.object(doveorm.QuerySet, "first", stale_first):
            obj, created = Slot.get_or_create(code="A")
        self.assertFalse(created)
        self.assertEqual(obj.code, "A")
        self.assertEqual(Slot.all().count(), 1)

    def test_integer_ranges_and_bool(self):
        with self.assertRaises(TypeError):
            self.User.create(name="t", age=True)
        with self.assertRaises(ValidationError):
            self.User.create(name="t", age=2 ** 40)

    def test_explicit_none_for_not_null_with_default(self):
        db = make_db()

        class N(Model):
            id = AutoField()
            v = IntegerField(default=5, nullable=False)

        N.use(db)
        N.create_table()
        with self.assertRaises(ValidationError):
            N.create(v=None)
        self.assertEqual(N.create().v, 5)

    def test_relationships_dict_not_shared_with_parent(self):
        class Base(Model):
            id = AutoField()

            class Meta:
                abstract = True

        class Parent(Base):
            kids = HasMany("Kid", foreign_key="parent_id")

        class Child(Parent):
            extra = HasMany("Kid", foreign_key="parent_id")

        self.assertNotIn("extra", Parent._relationships)
        self.assertIn("kids", Child._relationships)


class TestDecimalSQLite(unittest.TestCase):
    def test_ordering_and_comparison_are_numeric(self):
        db = make_db()

        class Item(Model):
            id = AutoField()
            price = DecimalField(max_digits=8, decimal_places=2)

        Item.use(db)
        Item.create_table()
        for p in ("9.00", "10.00", "100.50"):
            Item.create(price=Decimal(p))
        self.assertEqual([str(i.price) for i in Item.all().order_by("price")], ["9.00", "10.00", "100.50"])
        self.assertEqual(Item.where(price__gte=Decimal("10")).count(), 2)
        self.assertEqual(Item.where(price=Decimal("10")).count(), 1)

    def test_precision_validated(self):
        f = DecimalField(max_digits=4, decimal_places=2)
        f.name = "p"
        with self.assertRaises(ValidationError):
            f.validate(Decimal("123.4"))
        with self.assertRaises(ValidationError):
            f.validate(Decimal("1.234"))
        self.assertEqual(f.validate("12.30"), Decimal("12.30"))


# ══════════════════════════════════════════════════════════
# Field hardening
# ══════════════════════════════════════════════════════════

class TestFieldHardening(unittest.TestCase):
    def test_file_field_rejects_traversal(self):
        f = FileField()
        f.name = "doc"
        with self.assertRaises(ValidationError):
            f.validate("uploads/../../etc/passwd")
        with self.assertRaises(ValidationError):
            f.validate("a\x00b")
        self.assertEqual(f.validate("uploads/a.pdf"), "uploads/a.pdf")

    def test_file_path_field_enforced(self):
        f = FilePathField(path="/etc/app/", match=r"\.conf$")
        f.name = "cfg"
        self.assertEqual(f.validate("/etc/app/main.conf"), "/etc/app/main.conf")
        self.assertEqual(f.validate("main.conf"), "main.conf")
        for bad in ("/etc/passwd", "/etc/app/sub/x.conf", "/etc/app/main.txt", "/etc/application/x.conf"):
            with self.assertRaises(ValidationError, msg=bad):
                f.validate(bad)
        rec = FilePathField(path="/etc/app", recursive=True)
        rec.name = "cfg"
        self.assertEqual(rec.validate("/etc/app/sub/x.conf"), "/etc/app/sub/x.conf")

    def test_json_mutable_default_not_shared(self):
        db = make_db()

        class Doc(Model):
            id = AutoField()
            data = JSONField(default={})

        Doc.use(db)
        a, b = Doc(), Doc()
        a.data["k"] = 1
        self.assertEqual(b.data, {})

    def test_json_string_value_roundtrip(self):
        db = make_db()

        class Doc(Model):
            id = AutoField()
            data = JSONField(nullable=True)

        Doc.use(db)
        Doc.create_table()
        self.assertEqual(Doc.find(Doc.create(data="hello").id).data, "hello")

    def test_boolean_rejects_strings(self):
        f = BooleanField()
        f.name = "b"
        with self.assertRaises(TypeError):
            f.validate("false")
        self.assertIs(f.validate(1), True)

    def test_uuid_invalid_string(self):
        f = UUIDField()
        f.name = "u"
        with self.assertRaises(ValidationError):
            f.validate("not-a-uuid")


# ══════════════════════════════════════════════════════════
# ForeignKey typing / UUID primary keys
# ══════════════════════════════════════════════════════════

class TestUUIDRelations(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Org(Model):
            id = UUIDField(primary_key=True, default=uuid.uuid4)
            name = CharField()
            members = ManyToMany("Person", through="org_people", source_fk="org_id", target_fk="person_id")

        class Person(Model):
            id = UUIDField(primary_key=True, default=uuid.uuid4)
            org_id = ForeignKey("Org", nullable=True, on_delete="SET NULL")
            org = BelongsTo("Org", foreign_key="org_id")

        for cls in (Org, Person):
            cls.use(self.db)
            cls.create_table()
        self.db.execute_script(
            "CREATE TABLE org_people (org_id TEXT, person_id TEXT, PRIMARY KEY (org_id, person_id));"
        )
        self.Org, self.Person = Org, Person

    def test_fk_column_type_follows_target(self):
        self.assertTrue(self.Person._fields["org_id"].sql_definition(self.db.driver).startswith("TEXT"))

    def test_fk_accepts_instance_and_roundtrips(self):
        org = self.Org.create(name="acme")
        p = self.Person.create(org_id=org)
        fresh = self.Person.find(p.id)
        self.assertEqual(fresh.org_id, org.id)
        self.assertEqual(fresh.org.name, "acme")

    def test_m2m_with_uuid_pks_and_prefetch(self):
        org = self.Org.create(name="acme")
        p1, p2 = self.Person.create(), self.Person.create()
        org.members.add(p1, p2, p1)
        self.assertEqual(len(self.Org.find(org.id).members), 2)
        loaded = self.Org.all().prefetch_related("members").get()
        self.assertEqual(len(loaded[0].members), 2)
        org.members.remove(p1)
        self.assertEqual(len(self.Org.find(org.id).members), 1)
        org.members.clear()
        self.assertEqual(len(self.Org.find(org.id).members), 0)

    def test_belongs_to_cache_invalidated_on_fk_change(self):
        a, b = self.Org.create(name="a"), self.Org.create(name="b")
        p = self.Person.create(org_id=a.id)
        self.assertEqual(p.org.name, "a")
        p.org_id = b.id
        self.assertEqual(p.org.name, "b")

    def test_m2m_add_type_checked(self):
        org = self.Org.create(name="acme")
        with self.assertRaises(TypeError):
            org.members.add(org)


# ══════════════════════════════════════════════════════════
# Performance: result cache, exists(), bulk ops, prefetch chunking
# ══════════════════════════════════════════════════════════

class TestPerformanceBehaviour(unittest.TestCase):
    def setUp(self):
        self.db = make_db()
        self.User = user_model(self.db)
        for n in "abc":
            self.User.create(name=n)
        self.counter = CountingDB(self.db)

    def test_result_cache(self):
        qs = self.User.where(active=True)
        if qs:
            names = [u.name for u in qs]
        self.assertEqual(len(qs), 3)
        self.assertEqual(len(names), 3)
        self.assertEqual(len(self.counter.sql), 1)

    def test_exists_uses_limit(self):
        self.User.where(name="a").exists()
        self.assertIn("LIMIT 1", self.counter.sql[-1])
        self.assertNotIn("COUNT", self.counter.sql[-1])

    def test_bulk_create_single_transaction(self):
        users = [self.User(name=f"u{i}") for i in range(20)]
        with mock.patch.object(self.db.driver, "_commit", wraps=self.db.driver._commit) as commit:
            self.User.bulk_create(users)
        self.assertEqual(commit.call_count, 1)
        self.assertTrue(all(u.id for u in users))

    def test_prefetch_chunks_large_in_lists(self):
        class Post(Model):
            id = AutoField()
            user_id = ForeignKey("User")

        HasMany("Post", foreign_key="user_id").__set_name__(self.User, "posts")
        self.User.posts = self.User._relationships["posts"]
        Post.use(self.db)
        Post.create_table()
        self.User.bulk_create([self.User(name=f"n{i}") for i in range(1200)])
        users = self.User.all().prefetch_related("posts").get()
        self.assertEqual(len(users), 1203)
        self.assertTrue(all(u.posts == [] for u in users))

    def test_slicing(self):
        self.assertEqual([u.name for u in self.User.all().order_by("name")[1:3]], ["b", "c"])
        self.assertEqual(self.User.all().order_by("name")[2].name, "c")
        with self.assertRaises(IndexError):
            self.User.all()[10]

    def test_unknown_prefetch_name(self):
        with self.assertRaises(ValueError):
            self.User.all().prefetch_related("nope")


# ══════════════════════════════════════════════════════════
# MySQL driver (against a recording fake MySQLdb)
# ══════════════════════════════════════════════════════════

class TestMySQLDriver(unittest.TestCase):
    def setUp(self):
        self.conns = []
        self._patch = install_fake_mysqldb(self.conns)
        logging.getLogger("doveorm").disabled = True
        self.db = doveorm.MySQLDatabase(host="127.0.0.1", user="app", password="pw", database="appdb")

    def tearDown(self):
        logging.getLogger("doveorm").disabled = False
        self._patch.stop()

    def queries(self):
        return [q for q, _ in self.conns[-1].log]

    def test_autocommit_and_found_rows(self):
        self.db.execute("SELECT 1")
        conn = self.conns[0]
        self.assertTrue(conn.autocommit_mode)
        self.assertEqual(conn.cfg["client_flag"] & 2, 2)
        self.assertNotIn("password", conn.cfg)

    def test_no_params_means_no_formatting(self):
        self.db.execute("SELECT '50%'")
        self.assertEqual(self.conns[0].log[-1], ("SELECT '50%'", None))

    def test_params_translated(self):
        self.db.execute("SELECT * FROM t WHERE a = ? AND b LIKE '5%'", [1])
        self.assertEqual(self.conns[0].log[-1],
                         ("SELECT * FROM t WHERE a = %s AND b LIKE '5%%'", (1,)))

    def test_transaction_brackets(self):
        with self.db.transaction():
            self.db.execute_write("UPDATE t SET a = ?", [1])
            with self.db.transaction():
                self.db.execute_write("UPDATE t SET a = ?", [2])
        q = self.queries()
        self.assertEqual(q[0], "SET SESSION sql_mode = %s")     # session hardening on connect
        self.assertEqual(q[1], "START TRANSACTION")
        self.assertIn("SAVEPOINT doveorm_sp_1", q)
        self.assertIn("RELEASE SAVEPOINT doveorm_sp_1", q)
        self.assertEqual(q[-1], "COMMIT")

    def test_reconnect_on_server_gone(self):
        self.db.execute("SELECT 1")
        self.conns[0].fail_next = _FakeOperationalError(2006, "gone away")
        self.db.execute("SELECT 2")
        self.assertEqual(len(self.conns), 2)
        self.assertTrue(self.conns[0].closed)

    def test_no_write_retry_on_lost_connection(self):
        self.db.execute("SELECT 1")
        self.conns[0].fail_next = _FakeOperationalError(2013, "lost")
        with self.assertRaises(_FakeOperationalError):
            self.db.execute_write("UPDATE t SET a = ?", [1])
        self.assertEqual(len(self.conns), 1)

    def test_no_retry_inside_transaction(self):
        with self.assertRaises(_FakeOperationalError):
            with self.db.transaction():
                self.conns[0].fail_next = _FakeOperationalError(2006, "gone")
                self.db.execute("SELECT 1")
        self.assertEqual(len(self.conns), 1)

    def test_ddl(self):
        class MyParent(Model):
            id = BigAutoField()
            body = TextField(nullable=True)
            when = DateTimeField(nullable=True)
            slug = doveorm.SlugField()

        class MyChild(Model):
            id = AutoField()
            parent_id = ForeignKey("MyParent")

        for cls in (MyParent, MyChild):
            cls.use(self.db)
            cls.create_table()
        all_sql = "\n".join(q for c in self.conns for q, _ in c.log)
        self.assertIn("parent_id BIGINT", all_sql)          # FK type follows BigAutoField target
        self.assertIn("DATETIME(6)", all_sql)
        self.assertIn("ENGINE=InnoDB", all_sql)
        create_index = [q for c in self.conns for q, _ in c.log if q.startswith("CREATE INDEX")]
        self.assertTrue(create_index)
        self.assertTrue(all("IF NOT EXISTS" not in q for q in create_index))

    def test_text_unique_rejected(self):
        class MyText(Model):
            id = AutoField()
            body = TextField(unique=True)

        MyText.use(self.db)
        with self.assertRaises(ValueError):
            MyText.create_table()

    def test_no_default_on_text_columns(self):
        f = TextField(default="x")
        f.name = "t"
        self.assertNotIn("DEFAULT", f.sql_definition(self.db.driver))

    def test_offset_only_limit(self):
        self.assertEqual(self.db.driver.limit_offset_sql(None, 5), "LIMIT 18446744073709551615 OFFSET 5")

    def test_dictrow_index(self):
        row = _DictRow(["a", "b"], (1, 2))
        self.assertEqual((row[0], row[1], row["b"], list(row.keys())), (1, 2, 2, ["a", "b"]))


if __name__ == "__main__":
    unittest.main()
