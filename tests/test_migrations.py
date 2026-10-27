"""
tests/test_migrations.py
Tests for the Django-style migration system (doveorm.migrations).
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
from doveorm import Database, MigrationError, Model, fields as F, migrations as M, table
from doveorm.migrations.commands import project_models
from doveorm.migrations.loader import MigrationLoader
from doveorm.migrations.writer import Serializer, write_migration
from doveorm.model import ModelMeta

from tests.fakes import install_fake_mysqldb


def edit(path, old, new):
    with open(path) as fh:
        src = fh.read()
    assert old in src
    with open(path, "w") as fh:
        fh.write(src.replace(old, new))


class RegistryMixin:
    def setUp(self):
        super().setUp()
        self._registry = dict(ModelMeta._registry)
        self.dir = tempfile.mkdtemp(prefix="doveorm_mig_")
        self.db = Database(":memory:")

    def tearDown(self):
        self.db.close_all()
        ModelMeta._registry.clear()
        ModelMeta._registry.update(self._registry)
        shutil.rmtree(self.dir, ignore_errors=True)
        super().tearDown()

    def make(self, models, **kwargs):
        return M.makemigrations(self.dir, models=models, **kwargs)

    def migrate(self, target=None, **kwargs):
        return M.migrate(self.dir, target, db=self.db, **kwargs)

    def columns(self, table):
        return [r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")]

    def define(self, name, attrs, meta=None):
        ModelMeta._registry.pop(name, None)
        ns = dict(attrs)
        ns["__module__"] = __name__
        if meta:
            ns["Meta"] = type("Meta", (), meta)
        cls = ModelMeta(name, (Model,), ns)
        cls.use(self.db)
        return cls


# ──────────────────────────────────────────────
# Field deconstruction
# ──────────────────────────────────────────────

class TestDeconstruct(unittest.TestCase):
    def test_defaults_and_non_schema_args_are_dropped(self):
        f = F.CharField(max_length=255, nullable=True, help_text="x", verbose_name="Y",
                        sensitive=True, choices=[("a", "A")])
        self.assertEqual(f.deconstruct(), (F.CharField, {}))

    def test_schema_args_are_kept(self):
        f = F.CharField(max_length=20, nullable=False, unique=True, default="a", db_index=True)
        self.assertEqual(f.deconstruct()[1],
                         {"max_length": 20, "nullable": False, "unique": True,
                          "default": "a", "db_index": True})

    def test_subclass_default_is_respected(self):
        self.assertEqual(F.SlugField(max_length=50).deconstruct()[1], {})
        self.assertEqual(F.SlugField(max_length=255).deconstruct()[1], {"max_length": 255})

    def test_callable_default_dropped(self):
        f = F.UUIDField(primary_key=True, default=uuid.uuid4)
        self.assertEqual(f.deconstruct()[1], {"primary_key": True})

    def test_bool_vs_int_default_is_a_change(self):
        from doveorm.migrations.state import field_signature
        self.assertNotEqual(field_signature(F.BooleanField(default=1)),
                            field_signature(F.BooleanField(default=True)))

    def test_fk_positional_and_class_target(self):
        class Target(Model):
            pass
        try:
            f = F.ForeignKey(Target, "SET NULL")
            self.assertEqual(f.deconstruct()[1], {"to": "Target", "on_delete": "SET NULL"})
            clone = f.clone()
            self.assertEqual(clone.to, "Target")
        finally:
            ModelMeta._registry.pop("Target", None)

    def test_auto_fields(self):
        self.assertEqual(F.AutoField().deconstruct(), (F.AutoField, {}))
        self.assertTrue(F.BigAutoField().clone().primary_key)

    def test_clone_matches_ddl(self):
        for f in (F.DecimalField(max_digits=8, decimal_places=3, nullable=False),
                  F.DateTimeField(auto_now=True), F.BooleanField(default=False),
                  F.IPAddressField(), F.JSONField(nullable=True)):
            self.assertEqual(f.clone().sql_definition(), f.sql_definition())


# ──────────────────────────────────────────────
# Optimizer
# ──────────────────────────────────────────────

class TestOptimizer(unittest.TestCase):
    def setUp(self):
        self.user = M.CreateModel("User", [("id", F.AutoField())], table="users")

    def test_create_absorbs_add_alter_rename(self):
        ops = [self.user,
               M.AddField("User", "a", F.IntegerField(nullable=True)),
               M.AlterField("User", "a", F.IntegerField(default=3)),
               M.RenameField("User", "a", "b")]
        out = M.optimize(ops)
        self.assertEqual(len(out), 1)
        self.assertEqual([n for n, _ in out[0].fields], ["id", "b"])
        self.assertEqual(out[0].fields[1][1].default, 3)

    def test_create_then_delete_vanishes(self):
        self.assertEqual(M.optimize([self.user, M.DeleteModel("User")]), [])

    def test_add_then_remove_vanishes(self):
        ops = [M.AddField("User", "a", F.IntegerField()), M.RemoveField("User", "a")]
        self.assertEqual(M.optimize(ops), [])

    def test_barrier_blocks(self):
        ops = [M.AddField("User", "a", F.IntegerField()), M.RunSQL("SELECT 1"),
               M.RemoveField("User", "a")]
        self.assertEqual(len(M.optimize(ops)), 3)

    def test_fk_dependency_respected(self):
        post = M.CreateModel("Post", [("id", F.AutoField()), ("u", F.ForeignKey("User"))],
                             table="posts")
        out = M.optimize([self.user, post, M.AddField("User", "age", F.IntegerField())])
        self.assertEqual([type(o).__name__ for o in out], ["CreateModel", "CreateModel"])
        self.assertEqual(out[0].name, "User")
        self.assertIn("age", dict(out[0].fields))

    def test_delete_of_referenced_model_not_moved(self):
        post = M.CreateModel("Post", [("id", F.AutoField()), ("u", F.ForeignKey("User"))],
                             table="posts")
        out = M.optimize([self.user, post, M.DeleteModel("User")])
        self.assertEqual(len(out), 3)

    def test_rename_chains(self):
        out = M.optimize([M.RenameField("U", "a", "b"), M.RenameField("U", "b", "c")])
        self.assertEqual(out, [M.RenameField("U", "a", "c")])
        self.assertEqual(M.optimize([M.RenameModel("A", "B"), M.RenameModel("B", "A")]), [])


# ──────────────────────────────────────────────
# Autodetector
# ──────────────────────────────────────────────

def state(*model_states):
    return M.ProjectState(model_states)


def ms(name, table, fields, **options):
    return M.ModelState(name, table, [("id", F.AutoField())] + list(fields), options)


class TestAutodetector(unittest.TestCase):
    def detect(self, old, new, **q):
        return M.Autodetector(old, new, M.Questioner(**q)).changes()

    def test_create_in_fk_order(self):
        new = state(ms("Post", "posts", [("u", F.ForeignKey("User"))]), ms("User", "users", []))
        ops = self.detect(state(), new)
        self.assertEqual([o.name for o in ops], ["User", "Post"])

    def test_fk_cycle_is_broken(self):
        new = state(ms("A", "as_", [("b", F.ForeignKey("B", nullable=True))]),
                    ms("B", "bs", [("a", F.ForeignKey("A", nullable=True))]))
        ops = self.detect(state(), new)
        self.assertEqual([type(o).__name__ for o in ops], ["CreateModel", "CreateModel", "AddField"])

    def test_delete_cycle(self):
        old = state(ms("A", "as_", [("b", F.ForeignKey("B", nullable=True))]),
                    ms("B", "bs", [("a", F.ForeignKey("A", nullable=True))]))
        ops = self.detect(old, state())
        self.assertEqual(sum(isinstance(o, M.DeleteModel) for o in ops), 2)

    def test_not_null_addition_requires_value(self):
        old = state(ms("User", "users", []))
        new = state(ms("User", "users", [("age", F.IntegerField(nullable=False))]))
        with self.assertRaises(MigrationError):
            self.detect(old, new)
        ops = self.detect(old, new, defaults={"User.age": 0})
        self.assertEqual(ops[0].backfill, 0)

    def test_rename_only_when_confirmed(self):
        old = state(ms("User", "users", [("nick", F.CharField(nullable=True))]))
        new = state(ms("User", "users", [("handle", F.CharField(nullable=True))]))
        self.assertEqual([type(o).__name__ for o in self.detect(old, new)], ["RemoveField", "AddField"])
        ops = self.detect(old, new, renames={"User.handle": "nick"})
        self.assertEqual(ops, [M.RenameField("User", "nick", "handle")])

    def test_index_removed_before_its_field(self):
        old = state(ms("User", "users", [("a", F.IntegerField()), ("b", F.IntegerField())],
                       indexes=[("a", "b")]))
        new = state(ms("User", "users", [("a", F.IntegerField())]))
        ops = self.detect(old, new)
        self.assertIsInstance(ops[0], M.AlterIndexes)
        self.assertIsInstance(ops[1], M.RemoveField)

    def test_table_rename(self):
        ops = self.detect(state(ms("User", "users", [])), state(ms("User", "people", [])))
        self.assertEqual(ops, [M.AlterModelTable("User", "people")])

    def test_non_schema_change_is_ignored(self):
        old = state(ms("User", "users", [("a", F.CharField(nullable=True))]))
        new = state(ms("User", "users", [("a", F.CharField(help_text="hi", sensitive=True))]))
        self.assertEqual(self.detect(old, new), [])


# ──────────────────────────────────────────────
# Writer / loader round-trip
# ──────────────────────────────────────────────

class TestWriter(unittest.TestCase):
    def test_round_trip(self):
        ops = [
            M.CreateModel("User", [("id", F.AutoField()),
                                   ("bal", F.DecimalField(max_digits=8, default=decimal.Decimal("1.50"))),
                                   ("born", F.DateField(default=datetime.date(2000, 1, 2)))],
                          table="users", options={"unique_together": [("bal", "born")]}),
            M.AddField("User", "tok", F.UUIDField(nullable=False), backfill=uuid.UUID(int=5)),
            M.RunSQL("UPDATE users SET bal = 0", reverse_sql=["SELECT 1"]),
        ]
        src = write_migration(operations=ops, dependencies=["0001_x"])
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "0002_y.py"), "w") as fh:
                fh.write(src)
            loaded = MigrationLoader(d).disk["0002_y"]
            self.assertEqual(loaded.dependencies, ["0001_x"])
            self.assertEqual(loaded.operations, ops)
        finally:
            shutil.rmtree(d)

    def test_lambda_rejected(self):
        with self.assertRaises(MigrationError):
            Serializer().serialize(lambda db: None)

    def test_non_utc_datetime_rejected(self):
        tz = datetime.timezone(datetime.timedelta(hours=3))
        with self.assertRaises(MigrationError):
            Serializer().serialize(datetime.datetime(2020, 1, 1, tzinfo=tz))


# ──────────────────────────────────────────────
# End to end on SQLite
# ──────────────────────────────────────────────

class TestSQLiteEndToEnd(RegistryMixin, unittest.TestCase):
    def v1(self):
        User = self.define("User", {"name": F.CharField(max_length=50, nullable=False),
                                    "email": F.EmailField(unique=True)})
        Post = self.define("Post", {"title": F.CharField(nullable=False),
                                    "author_id": F.ForeignKey("User", on_delete="CASCADE")})
        return User, Post

    def seed(self):
        User, Post = self.v1()
        self.make([User, Post])
        self.migrate()
        u = User.create(name="ann", email="ann@x.io")
        Post.create(title="hello", author_id=u.id)
        return User, Post

    def test_initial_and_noop(self):
        User, Post = self.v1()
        result = self.make([User, Post])
        self.assertEqual(result.name, "0001_initial")
        self.assertTrue(os.path.exists(result.path))
        self.assertIsNone(self.make([User, Post]))
        self.assertEqual(self.migrate(), [("0001_initial", False)])
        self.assertEqual(self.migrate(), [])
        self.assertTrue(self.db.table_exists("posts"))

    def test_migrate_false_models_are_left_alone(self):
        Mine = self.define("Mine", {"name": F.CharField(max_length=20)})
        Ext = self.define("Ext", {"name": F.CharField(max_length=20)}, meta={"migrate": False})
        Deco = self.define("Deco", {"name": F.CharField(max_length=20)})
        table(migrate=False)(Deco)
        self.assertFalse(Ext._migrate)
        self.assertFalse(Deco._migrate)
        self.assertEqual([m.__name__ for m in project_models([Mine, Ext, Deco])], ["Mine"])
        self.make([Mine, Ext, Deco])
        self.migrate()
        self.assertTrue(self.db.table_exists("mines"))
        self.assertFalse(self.db.table_exists("exts"))
        self.assertFalse(self.db.table_exists("decos"))
        self.assertIsNone(self.make([Mine, Ext, Deco]))
        # the model still works against a table someone else created
        self.db.execute("CREATE TABLE exts (id INTEGER PRIMARY KEY, name VARCHAR(20))")
        Ext.create(name="x")
        self.assertEqual(Ext.filter(name="x").count(), 1)

    def test_foreign_key_to_migrate_false_model_is_explained(self):
        Ext = self.define("Ext2", {"name": F.CharField(max_length=20)}, meta={"migrate": False})
        Mine = self.define("Mine2", {"ext_id": F.ForeignKey("Ext2")})
        with self.assertRaisesRegex(Exception, "migrate = False"):
            self.make([Mine, Ext])

    def test_remake_keeps_rows_and_does_not_cascade(self):
        self.seed()
        User = self.define("User", {"name": F.CharField(max_length=50, nullable=False),
                                    "email": F.EmailField(unique=True),
                                    "age": F.IntegerField(nullable=False)})
        Post = self.define("Post", {"title": F.CharField(nullable=False),
                                    "author_id": F.ForeignKey("User", on_delete="CASCADE")})
        self.make([User, Post], defaults={"User.age": 7})
        self.migrate()
        self.assertEqual([tuple(r) for r in self.db.execute("SELECT name, age FROM users")], [("ann", 7)])
        self.assertEqual(len(self.db.execute("SELECT * FROM posts")), 1)
        self.assertEqual(self.db.execute("PRAGMA foreign_keys")[0][0], 1)
        # NOT NULL without default is enforced afterwards
        with self.assertRaises(Exception):
            self.db.execute_write("INSERT INTO users (name, email) VALUES ('b', 'b@x.io')")

    def test_rollback_and_reapply(self):
        self.seed()
        User = self.define("User", {"name": F.CharField(max_length=50, nullable=False),
                                    "email": F.EmailField(unique=True),
                                    "phone": F.CharField(max_length=20, db_index=True)})
        Post = self.define("Post", {"title": F.CharField(nullable=False),
                                    "author_id": F.ForeignKey("User", on_delete="CASCADE")})
        self.make([User, Post])
        self.migrate()
        self.assertIn("phone", self.columns("users"))
        self.assertTrue(self.db.index_exists("users", "idx_users_phone"))
        self.assertEqual(M.rollback(self.dir, db=self.db), [("0002_user_phone", True)])
        self.assertNotIn("phone", self.columns("users"))
        self.assertEqual(len(self.db.execute("SELECT * FROM posts")), 1)
        self.assertEqual(self.migrate(), [("0002_user_phone", False)])
        self.assertEqual(self.migrate("zero")[-1], ("0001_initial", True))
        self.assertFalse(self.db.table_exists("users"))

    def test_target_prefix_and_plan(self):
        self.seed()
        M.makemigrations(self.dir, empty=True, name="noop")
        self.assertEqual(M.plan(self.dir, db=self.db), [("0002_noop", False)])
        self.assertEqual(M.plan(self.dir, "0001", db=self.db), [])
        self.migrate()
        self.assertEqual(M.plan(self.dir, "0001", db=self.db), [("0002_noop", True)])

    def test_alter_to_not_null_with_backfill(self):
        Item = self.define("Item", {"label": F.CharField(nullable=True)})
        self.make([Item])
        self.migrate()
        self.db.execute_write("INSERT INTO items (label) VALUES (NULL)")
        Item = self.define("Item", {"label": F.CharField(nullable=False)})
        self.make([Item], defaults={"Item.label": "n/a"})
        self.migrate()
        self.assertEqual(self.db.execute("SELECT label FROM items")[0][0], "n/a")

    def test_rename_field_keeps_data(self):
        Item = self.define("Item", {"nick": F.CharField(nullable=True)})
        self.make([Item])
        self.migrate()
        self.db.execute_write("INSERT INTO items (nick) VALUES ('x')")
        Item = self.define("Item", {"handle": F.CharField(nullable=True)})
        self.make([Item], questioner=M.Questioner(renames={"Item.handle": "nick"}))
        self.migrate()
        self.assertEqual(self.db.execute("SELECT handle FROM items")[0][0], "x")

    def test_failed_migration_is_rolled_back(self):
        self.seed()
        M.makemigrations(self.dir, empty=True, name="boom")
        edit(os.path.join(self.dir, "0002_boom.py"), "operations = [\n    ]",
             "operations = [\n        migrations.RunSQL('CREATE TABLE t1 (id INTEGER)'),\n"
             "        migrations.RunSQL('THIS IS NOT SQL'),\n    ]")
        with self.assertRaises(MigrationError):
            self.migrate()
        self.assertFalse(self.db.table_exists("t1"))
        self.assertEqual(M.showmigrations(self.dir, db=self.db)[-1], ("0002_boom", False))
        self.assertEqual(self.db.execute("PRAGMA foreign_keys")[0][0], 1)

    def test_dangling_fk_aborts_remake(self):
        self.seed()
        self.db.execute_ddl("PRAGMA foreign_keys = OFF")
        self.db.execute_write("INSERT INTO posts (title, author_id) VALUES ('orphan', 999)")
        self.db.execute_ddl("PRAGMA foreign_keys = ON")
        User = self.define("User", {"name": F.CharField(max_length=50, nullable=False),
                                    "email": F.EmailField(unique=True)})
        Post = self.define("Post", {"title": F.CharField(nullable=True),
                                    "author_id": F.ForeignKey("User", on_delete="CASCADE")})
        self.make([User, Post])
        with self.assertRaises(MigrationError) as cm:
            self.migrate()
        self.assertIn("foreign key", str(cm.exception))
        self.assertEqual(M.showmigrations(self.dir, db=self.db)[-1][1], False)

    def test_irreversible_refused(self):
        self.seed()
        M.makemigrations(self.dir, empty=True, name="data")
        edit(os.path.join(self.dir, "0002_data.py"), "operations = [\n    ]",
             "operations = [\n        migrations.RunSQL('SELECT 1'),\n    ]")
        self.migrate()
        with self.assertRaises(MigrationError):
            M.rollback(self.dir, db=self.db)
        self.assertEqual(M.showmigrations(self.dir, db=self.db)[-1], ("0002_data", True))

    def test_fake_initial(self):
        User, Post = self.v1()
        User.create_table()
        Post.create_table()
        self.make([User, Post])
        with self.assertRaises(MigrationError):
            self.migrate()
        self.assertEqual(self.migrate(fake_initial=True), [("0001_initial", False)])
        self.assertEqual(M.showmigrations(self.dir, db=self.db), [("0001_initial", True)])

    def test_sqlmigrate_runs_nothing(self):
        User, Post = self.v1()
        self.make([User, Post])
        sql = M.sqlmigrate(self.dir, "0001", db=self.db)
        self.assertTrue(sql[0].startswith("CREATE TABLE users"))
        self.assertFalse(self.db.table_exists("users"))

    def test_check_mode(self):
        User, Post = self.v1()
        with self.assertRaises(MigrationError):
            self.make([User, Post], check=True)
        self.make([User, Post])
        self.assertIsNone(self.make([User, Post], check=True))

    def test_conflict_and_merge(self):
        User, Post = self.v1()
        self.make([User, Post])
        for name in ("a", "b"):
            with open(os.path.join(self.dir, f"0002_{name}.py"), "w") as fh:
                fh.write(write_migration(operations=[], dependencies=["0001_initial"]))
        with self.assertRaises(MigrationError):
            self.migrate()
        merged = M.makemigrations(self.dir, merge=True)
        self.assertEqual(merged.name, "0003_merge_0002_0002")
        self.assertEqual(len(self.migrate()), 4)

    def test_inconsistent_history(self):
        User, Post = self.v1()
        self.make([User, Post])
        M.makemigrations(self.dir, empty=True, name="x")
        M.MigrationRecorder(self.db).ensure_table()
        M.MigrationRecorder(self.db).record_applied("0002_x")
        with self.assertRaises(MigrationError):
            self.migrate()

    def test_squash(self):
        User, Post = self.seed()
        User = self.define("User", {"name": F.CharField(max_length=50, nullable=False),
                                    "email": F.EmailField(unique=True),
                                    "age": F.IntegerField(default=0, nullable=False)})
        self.make([User, Post])
        self.migrate()
        squashed = M.squashmigrations(self.dir, "0002")
        self.assertEqual(len(squashed.operations), 2)
        # Fully applied history: squash counts as applied.
        self.assertEqual(self.migrate(), [])
        # Fresh database: only the squash runs.
        fresh = Database(":memory:")
        try:
            self.assertEqual(M.migrate(self.dir, db=fresh), [(squashed.name, False)])
            applied = {n for n, a in M.showmigrations(self.dir, db=fresh) if a}
            self.assertIn(squashed.name, applied)
            self.assertIn("age", [r["name"] for r in fresh.execute("PRAGMA table_info(users)")])
        finally:
            fresh.close_all()

    def test_legacy_function_migrations(self):
        with open(os.path.join(self.dir, "0001_legacy.py"), "w") as fh:
            fh.write("from doveorm import get_database\n"
                     "name = '0001_legacy'\n"
                     "STATE = []\n"
                     "def up():\n    STATE.append('up')\n"
                     "def down():\n    STATE.append('down')\n")
        self.assertEqual(self.migrate(), [("0001_legacy", False)])
        User, Post = self.v1()
        result = self.make([User, Post])
        self.assertEqual(result.name, "0002_initial")
        self.assertEqual(self.migrate(), [("0002_initial", False)])
        self.assertEqual(M.rollback(self.dir, 2, db=self.db),
                         [("0002_initial", True), ("0001_legacy", True)])

    def test_legacy_migrator_shares_history(self):
        doveorm.Migrator(self.db).register("0001_old", lambda: None)
        m = doveorm.Migrator(self.db)
        m.register("0001_old", lambda: None)
        m.migrate()
        self.assertEqual(M.MigrationRecorder(self.db).applied(), ["0001_old"])

    def test_pre_0_6_history_table_is_renamed(self):
        self.db.execute_ddl("CREATE TABLE migrations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                            "name TEXT NOT NULL UNIQUE, applied_at TEXT)")
        self.db.execute_write("INSERT INTO migrations (name) VALUES ('0001_old')")
        rec = M.MigrationRecorder(self.db)
        self.assertEqual(rec.applied(), ["0001_old"])          # readable before the rename
        self.assertFalse(self.db.table_exists("doveorm_migrations"))
        rec.ensure_table()
        self.assertFalse(self.db.table_exists("migrations"))
        self.assertEqual(rec.applied(), ["0001_old"])
        rec.record_applied("0002_new")
        self.assertEqual(rec.applied(), ["0001_old", "0002_new"])

    def test_unrelated_migrations_table_is_left_alone(self):
        self.db.execute_ddl("CREATE TABLE migrations (id INTEGER PRIMARY KEY, batch INTEGER)")
        rec = M.MigrationRecorder(self.db)
        self.assertEqual(rec.applied(), [])
        rec.ensure_table()
        self.assertTrue(self.db.table_exists("migrations"))
        self.assertTrue(self.db.table_exists("doveorm_migrations"))

    def test_internal_model_not_migrated(self):
        names = [m.__name__ for m in M.commands.project_models()]
        self.assertNotIn("MigrationRecord", names)

    def test_cli(self):
        from doveorm.migrations.cli import main
        User, Post = self.v1()
        self.assertEqual(main(["makemigrations", "--noinput"], directory=self.dir,
                              models=[User, Post], db=self.db), 0)
        self.assertEqual(main(["migrate"], directory=self.dir, models=[User, Post], db=self.db), 0)
        self.assertEqual(main(["makemigrations", "--check"], directory=self.dir,
                              models=[User, Post], db=self.db), 0)
        self.assertEqual(main(["migrate", "nope"], directory=self.dir, db=self.db), 1)


# ──────────────────────────────────────────────
# MySQL DDL (fake driver, SQL collected only)
# ──────────────────────────────────────────────

class TestMySQLDDL(unittest.TestCase):
    def setUp(self):
        self.conns = []
        self._patch = install_fake_mysqldb(self.conns)
        self.db = doveorm.MySQLDatabase(host="127.0.0.1", user="app", password="pw", database="d")

    def tearDown(self):
        self._patch.stop()

    def sql(self, ops, before=None, backwards=False):
        from doveorm.migrations.backends import editor_for
        editor = editor_for(self.db, collect_sql=True)
        s = before or M.ProjectState()
        steps = []
        for op in ops:
            after = s.clone()
            op.state_forwards(after)
            steps.append((op, s, after))
            s = after
        for op, b, a in (reversed(steps) if backwards else steps):
            (op.database_backwards if backwards else op.database_forwards)(editor, b, a)
        return editor.collected

    def base(self):
        return state(ms("User", "users", [("name", F.CharField(max_length=40, nullable=True))]),
                     ms("Post", "posts", [("u", F.ForeignKey("User"))]))

    def test_create_model(self):
        out = self.sql([M.CreateModel("User", [("id", F.BigAutoField()),
                                               ("e", F.CharField(max_length=99, unique=True))],
                                      table="users", options={"engine": "InnoDB"})])
        self.assertIn("id BIGINT PRIMARY KEY AUTO_INCREMENT", out[0])
        self.assertIn("e VARCHAR(99) UNIQUE", out[0])
        self.assertTrue(out[0].rstrip(";").endswith("ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"))

    def test_fk_follows_target_pk_type(self):
        out = self.sql([M.CreateModel("A", [("id", F.BigAutoField())], table="a_s"),
                        M.CreateModel("B", [("id", F.AutoField()), ("a", F.ForeignKey("A"))],
                                      table="b_s")])
        self.assertIn("a BIGINT", out[1])
        self.assertIn("CONSTRAINT fk_b_s_a FOREIGN KEY (a) REFERENCES a_s(id) ON DELETE CASCADE", out[1])

    def test_alter_field_modify_and_unique(self):
        out = self.sql([M.AlterField("User", "name", F.CharField(max_length=80, nullable=False,
                                                                  unique=True), backfill="?")],
                       before=self.base())
        self.assertEqual(out[0], "UPDATE users SET name = ? WHERE name IS NULL;  -- params: 1")
        self.assertEqual(out[1], "ALTER TABLE users MODIFY COLUMN name VARCHAR(80) NOT NULL;")
        self.assertEqual(out[2], "CREATE UNIQUE INDEX uq_users_name ON users (name);")

    def test_add_not_null_uses_temporary_default(self):
        out = self.sql([M.AddField("User", "age", F.IntegerField(nullable=False), backfill=0)],
                       before=self.base())
        self.assertEqual(out, ["ALTER TABLE users ADD COLUMN age INT NOT NULL DEFAULT 0;",
                               "ALTER TABLE users ALTER COLUMN age DROP DEFAULT;"])

    def test_pk_type_change_updates_children(self):
        out = self.sql([M.AlterField("User", "id", F.BigAutoField())], before=self.base())
        self.assertIn("ALTER TABLE posts DROP FOREIGN KEY fk_posts_u;", out)
        self.assertIn("ALTER TABLE users MODIFY COLUMN id BIGINT NOT NULL AUTO_INCREMENT;", out)
        self.assertIn("ALTER TABLE posts MODIFY COLUMN u BIGINT;", out)
        self.assertEqual(out[-1], "ALTER TABLE posts ADD CONSTRAINT fk_posts_u FOREIGN KEY (u) "
                                  "REFERENCES users(id) ON DELETE CASCADE;")

    def test_rename_and_remove(self):
        out = self.sql([M.RenameField("User", "name", "title"), M.RemoveField("Post", "u")],
                       before=self.base())
        self.assertEqual(out[0], "ALTER TABLE users CHANGE name title VARCHAR(40);")
        self.assertEqual(out[1:], ["ALTER TABLE posts DROP FOREIGN KEY fk_posts_u;",
                                   "ALTER TABLE posts DROP COLUMN u;"])

    def test_text_index_rejected(self):
        with self.assertRaises(MigrationError):
            self.sql([M.AddField("User", "bio", F.TextField(unique=True))], before=self.base())

    def test_backwards(self):
        out = self.sql([M.AlterModelTable("User", "people")], before=self.base(), backwards=True)
        self.assertEqual(out, ["RENAME TABLE people TO users;"])


if __name__ == "__main__":
    unittest.main()
