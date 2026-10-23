"""
tests/test_accuracy.py
Query semantics that must be identical on every backend (and match Django):
NULL handling under negation, lookup-value validation, quoted identifiers,
decimal / boolean / JSON lookups, text lookups on non-text columns and
explicit auto-increment keys.

Runs on SQLite always, on PostgreSQL when DOVEORM_TEST_PG_* is set (see
tests/test_postgresql.py), and checks the SQL sent to MySQL via the fake
``MySQLdb``.
"""
import datetime
import decimal
import os
import shutil
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import doveorm
from doveorm import Database, Model, PostgreSQLDatabase, Q, ValidationError, fields as F, migrations as M
from doveorm.cli.health import schema_drift
from doveorm.model import ModelMeta
from doveorm.relationships import ManyToMany

from tests.fakes import install_fake_mysqldb
from tests.test_postgresql import HAVE_SERVER, PG_ENV, _installed


class _Backend:
    """Mixin: ``self.db`` is a fresh, empty database of one backend."""

    def setUp(self):
        super().setUp()
        self._registry = dict(ModelMeta._registry)
        self.db = self.make_db()

    def tearDown(self):
        self.db.close_all()
        ModelMeta._registry.clear()
        ModelMeta._registry.update(self._registry)
        super().tearDown()

    def define(self, name, attrs, meta=None, create=True):
        ModelMeta._registry.pop(name, None)
        ns = dict(attrs, __module__=__name__)
        if meta:
            ns["Meta"] = type("Meta", (), meta)
        cls = ModelMeta(name, (Model,), ns)
        cls.use(self.db)
        if create:
            cls.create_table()
        return cls

    def people(self):
        P = self.define("Person", {
            "id": F.AutoField(), "name": F.CharField(max_length=30, nullable=True),
            "age": F.IntegerField(nullable=True), "active": F.BooleanField(default=True),
            "token": F.UUIDField(nullable=True), "meta": F.JSONField(nullable=True)})
        P.create(name="Ann", age=30, active=True, token=uuid.UUID(int=0xABCDEF), meta={"a": 1, "b": 2})
        P.create(name="bob", age=None, active=False)
        P.create(name=None, age=12, active=True)
        return P

    @staticmethod
    def names(qs):
        return sorted(str(p.name) for p in qs.get())


class _AccuracyTests(_Backend):

    # ── NULLs under negation (Django semantics) ──

    def test_exclude_keeps_null_rows(self):
        P = self.people()
        self.assertEqual(self.names(P.all().exclude(name="Ann")), ["None", "bob"])
        self.assertEqual(self.names(P.all().exclude(age__gt=20)), ["None", "bob"])
        self.assertEqual(self.names(P.all().exclude(name__in=["Ann", None])), ["bob"])

    def test_ne_and_not_in_keep_null_rows(self):
        P = self.people()
        self.assertEqual(self.names(P.where(name__ne="Ann")), ["None", "bob"])
        self.assertEqual(self.names(P.where(name__not_in=["Ann"])), ["None", "bob"])
        self.assertEqual(self.names(P.where(name__not_in=["Ann", None])), ["bob"])

    def test_nested_negation(self):
        P = self.people()
        self.assertEqual(self.names(P.where().filter(~~Q(name="Ann"))), ["Ann"])
        self.assertEqual(self.names(P.where().filter(~Q(name="bob") | Q(age=12))), ["Ann", "None"])

    # ── lookup values are validated, not coerced into wrong matches ──

    def test_invalid_lookup_values_raise(self):
        P = self.people()
        for bad in (dict(active="false"), dict(active=2), dict(age="abc"), dict(age=True),
                    dict(age=1.5), dict(age__in=[1, "x"]), dict(token="nope"), dict(name="a\x00b")):
            with self.subTest(**{k: repr(v) for k, v in bad.items()}):
                with self.assertRaises((ValidationError, TypeError)):
                    P.where(**bad).get()

    def test_accepted_lookup_forms(self):
        P = self.people()
        self.assertEqual(P.where(age="30").count(), 1)
        self.assertEqual(P.where(age=30.0).count(), 1)
        self.assertEqual(P.where(active=1).count(), 2)
        self.assertEqual(P.where(active="True").count(), 2)    # Django's accepted strings
        self.assertEqual(P.where(active="0").count(), 1)
        self.assertEqual(P.where(token=str(uuid.UUID(int=0xABCDEF))).count(), 1)

    def test_nul_characters_are_rejected_on_save(self):
        P = self.people()
        with self.assertRaises(ValidationError):
            P.create(name="a\x00b")

    def test_decimal_lookups_are_not_rounded(self):
        D = self.define("Price", {"id": F.AutoField(),
                                  "p": F.DecimalField(max_digits=6, decimal_places=2)})
        D.create(p="2.50")
        self.assertEqual(D.where(p__gt="2.499").count(), 1)
        self.assertEqual(D.where(p__lt="2.501").count(), 1)
        self.assertEqual(D.where(p="2.5").count(), 1)
        self.assertEqual(D.where(p=decimal.Decimal("2.500")).count(), 1)
        self.assertEqual(D.where(p="2.501").count(), 0)
        self.assertEqual(D.where(p__in=["2.5", "3"]).count(), 1)

    def test_json_exact_ignores_key_order(self):
        P = self.people()
        self.assertEqual(P.where(meta={"b": 2, "a": 1}).count(), 1)

    def test_text_lookups_on_non_text_columns(self):
        P = self.people()
        self.assertEqual(self.names(P.where(age__contains=3)), ["Ann"])
        self.assertEqual(self.names(P.where(age__iexact=12)), ["None"])
        self.assertEqual(self.names(P.where(token__icontains="ABCDEF")), ["Ann"])

    # ── identifiers ──

    def test_reserved_and_mixed_case_names(self):
        T = self.define("Slot", {"id": F.AutoField(), "order": F.IntegerField(default=0, db_index=True),
                                 "user": F.CharField(max_length=10, nullable=True),
                                 "firstName": F.CharField(max_length=10, nullable=True)},
                        meta={"table_name": "group", "unique_together": [("order", "user")]})
        T.create_table()                                   # IF NOT EXISTS with quoted names
        s = T.create(order=2, user="u", firstName="Zoe")
        got = T.where(order=2, firstName="Zoe").order_by("-order").first()
        self.assertEqual((got.order, got.user, got.firstName), (2, "u", "Zoe"))
        self.assertEqual(T.where(order__gte=2).update(user="v"), 1)
        self.assertEqual(T.where().order_by("order").offset(0).limit(1).count(), 1)
        s.delete()
        self.assertTrue(self.db.table_exists("group"))
        self.assertTrue(self.db.index_exists("group", "idx_group_order"))
        T.drop_table()
        self.assertFalse(self.db.table_exists("group"))

    def test_many_to_many_with_reserved_names(self):
        User = self.define("User", {"id": F.AutoField(), "name": F.CharField(max_length=10),
                                    "roles": ManyToMany("Role", through="user", source_fk="from",
                                                        target_fk="to")},
                           meta={"table_name": "order"})
        Role = self.define("Role", {"id": F.AutoField(), "name": F.CharField(max_length=10)},
                           meta={"table_name": "Roles"})
        qn = self.db.driver.quote_name
        self.db.execute_ddl(f"CREATE TABLE {qn('user')} ({qn('from')} INTEGER NOT NULL, "
                            f"{qn('to')} INTEGER NOT NULL, PRIMARY KEY ({qn('from')}, {qn('to')}))")
        u = User.create(name="u")
        r1, r2 = Role.create(name="a"), Role.create(name="b")
        u.roles.add(r1, r2, r1)
        u.roles.remove(r2)
        loaded = User.where().prefetch_related("roles").get()[0]
        self.assertEqual([r.name for r in loaded.roles], ["a"])
        u.roles.clear()
        self.assertEqual(len(User.where().first().roles), 0)

    def test_migrations_with_reserved_and_mixed_case_names(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        Group = self.define("Group", {
            "id": F.AutoField(), "order": F.IntegerField(default=0, db_index=True),
            "userName": F.CharField(max_length=20, unique=True, nullable=True)},
            meta={"table_name": "group", "unique_together": [("order", "userName")]}, create=False)
        Item = self.define("Item", {"id": F.AutoField(), "group": F.ForeignKey("Group"),
                                    "select": F.CharField(max_length=5, nullable=True)},
                           meta={"table_name": "Items"}, create=False)
        M.makemigrations(d, models=[Group, Item], name="initial")
        M.migrate(d, db=self.db)
        g = Group.create(order=1, userName="A")
        Item.create(group=g.id, select="x")
        self.assertEqual(schema_drift(self.db, [Group, Item]), [])

        Group = self.define("Group", {
            "id": F.BigAutoField(), "order": F.BigIntegerField(default=5),
            "userName": F.CharField(max_length=40, nullable=True),
            "check": F.IntegerField(nullable=False)}, meta={"table_name": "group"}, create=False)
        Item = self.define("Item", {"id": F.AutoField(), "group": F.ForeignKey("Group"),
                                    "from": F.CharField(max_length=5, nullable=True)},
                           meta={"table_name": "Items"}, create=False)
        q = M.Questioner(defaults={"Group.check": 7}, renames={"Item.from": "select"})
        M.makemigrations(d, models=[Group, Item], name="alter", questioner=q)
        M.migrate(d, db=self.db)
        self.assertEqual(schema_drift(self.db, [Group, Item]), [])
        self.assertEqual(Group.where().first().check, 7)
        self.assertEqual(Item.where(**{"from": "x"}).count(), 1)
        dup = Group.create(order=2, userName="A", check=1)        # the unique index is gone
        dup.delete()

        M.migrate(d, "zero", db=self.db)
        self.assertFalse(self.db.table_exists("group"))
        self.assertFalse(self.db.table_exists("Items"))

    # ── keys ──

    def test_explicit_auto_increment_keys(self):
        T = self.define("Ticket", {"id": F.BigAutoField(), "t": F.CharField(max_length=5)})
        T.create(id=50, t="a")
        T.create(id=10, t="b")                       # lower key: must not move the counter back
        self.assertEqual(T.create(t="c").id, 51)

    def test_datetime_aware_lookups(self):
        E = self.define("Ev", {"id": F.AutoField(), "at": F.DateTimeField(use_tz=True)})
        nairobi = datetime.timezone(datetime.timedelta(hours=3))
        E.create(at=datetime.datetime(2024, 1, 1, 12, 0, tzinfo=nairobi))
        utc9 = datetime.datetime(2024, 1, 1, 9, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(E.where(at=utc9).first().at, utc9)
        self.assertEqual(E.where(at__gt=utc9).count(), 0)


class TestAccuracySQLite(_AccuracyTests, unittest.TestCase):
    def make_db(self):
        return Database(":memory:")


def _pg_class(label, library, options):
    class Case(_AccuracyTests, unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            if not HAVE_SERVER:
                raise unittest.SkipTest("set DOVEORM_TEST_PG_HOST / _USER / _NAME to run")
            if not _installed(library):
                raise unittest.SkipTest(f"{library} is not installed")
            cls.schema = f"doveorm_a_{uuid.uuid4().hex[:10]}"
            admin = PostgreSQLDatabase(**PG_ENV, library=library)
            admin.execute_ddl(f"CREATE SCHEMA {cls.schema}")
            admin.close_all()

        @classmethod
        def tearDownClass(cls):
            admin = PostgreSQLDatabase(**PG_ENV, library=library)
            admin.execute_ddl(f"DROP SCHEMA IF EXISTS {cls.schema} CASCADE")
            admin.close_all()

        def make_db(self):
            db = PostgreSQLDatabase(**PG_ENV, library=library, schema=self.schema, **options)
            db.execute_script(f"DROP SCHEMA {self.schema} CASCADE; CREATE SCHEMA {self.schema}")
            return db

    Case.__name__ = Case.__qualname__ = f"TestAccuracyPostgreSQL_{label}"
    return Case


TestAccuracyPostgreSQL_psycopg = _pg_class("psycopg", "psycopg", {})
TestAccuracyPostgreSQL_psycopg2 = _pg_class("psycopg2", "psycopg2", {})
TestAccuracyPostgreSQL_server_binding = _pg_class("server_binding", "psycopg", {"server_side_binding": True})


class TestMySQLSQL(_Backend, unittest.TestCase):
    """The SQL MySQL receives (fake MySQLdb: statements are recorded, not run)."""

    def make_db(self):
        self.conns = []
        self._patch = install_fake_mysqldb(self.conns)
        self.addCleanup(self._patch.stop)
        return doveorm.MySQLDatabase(host="127.0.0.1", user="app", password="pw", database="d")

    def sql(self):
        return [q.replace("\n", " ") for q, _ in self.conns[0].log]

    def test_quoting_json_and_null_semantics(self):
        G = self.define("G", {"id": F.AutoField(), "order": F.IntegerField(default=0, db_index=True),
                              "meta": F.JSONField(nullable=True), "name": F.CharField(max_length=9, nullable=True)},
                        meta={"table_name": "group"})
        G.where(meta={"b": 1, "a": 2}, order__gt=1).get()
        G.all().exclude(name="x").get()
        G.where(order=1).update(name="y")
        sql = self.sql()
        self.assertIn("CREATE TABLE IF NOT EXISTS `group` (     id INT PRIMARY KEY AUTO_INCREMENT,     "
                      "`order` INT DEFAULT 0,     meta JSON,     name VARCHAR(9) ) "
                      "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4", sql)
        self.assertIn("CREATE INDEX idx_group_order ON `group` (`order`)", sql)
        self.assertIn("SELECT * FROM `group` WHERE meta = CAST(%s AS JSON) AND `order` > %s", sql)
        self.assertIn("SELECT * FROM `group` WHERE (NOT ((name = %s AND name IS NOT NULL)))", sql)
        self.assertIn("UPDATE `group` SET name = %s WHERE `order` = %s", sql)

    def test_plain_names_stay_unquoted(self):
        from doveorm.utils.sql import quote_name
        self.assertEqual(quote_name("email"), "email")
        self.assertEqual(quote_name("order"), '"order"')
        self.assertEqual(quote_name("Order", "`"), "`Order`")
        self.assertEqual(quote_name("userName"), "userName")
        self.assertEqual(quote_name("userName", preserve_case=True), '"userName"')


if __name__ == "__main__":
    unittest.main()
