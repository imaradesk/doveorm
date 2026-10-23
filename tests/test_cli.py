"""
tests/test_cli.py
Tests for the ``doveorm`` management command (doveorm.cli).
"""
import contextlib
import io
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import doveorm
from doveorm.cli import health, main
from doveorm.cli.project import Project
from doveorm.exceptions import ConfigurationError
from doveorm.model import ModelMeta

from tests.fakes import install_fake_mysqldb

MODELS = """
from doveorm import Model, CharField, IntegerField, ForeignKey

class Customer(Model):
    name = CharField(max_length=80)

class Order(Model):
    customer = ForeignKey("Customer")
    total = IntegerField(default=0)
"""


class ProjectMixin:
    """A throw-away project folder (cwd) with a uniquely named app package."""

    def setUp(self):
        super().setUp()
        self._registry = dict(ModelMeta._registry)
        self._cwd = os.getcwd()
        self._path = list(sys.path)
        self._modules = set(sys.modules)
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="doveorm_cli_"))
        os.chdir(self.root)
        self.app = "app_" + uuid.uuid4().hex[:8]
        os.mkdir(self.app)
        self.write(f"{self.app}/__init__.py", "")
        self.write(f"{self.app}/models.py", MODELS)
        self.write(f"{self.app}/settings.py", 'DOVEORM = {"DATABASE_PATH": "app.db"}\n')
        doveorm.reset_config()

    def tearDown(self):
        doveorm.reset_config()
        os.chdir(self._cwd)
        sys.path[:] = self._path
        for name in set(sys.modules) - self._modules:
            del sys.modules[name]
        ModelMeta._registry.clear()
        ModelMeta._registry.update(self._registry)
        shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()

    def write(self, path, text):
        with open(os.path.join(self.root, path), "w") as fh:
            fh.write(textwrap.dedent(text))

    def config(self, extra=""):
        self.write("doveorm.toml", f'models = ["{self.app}.models"]\n'
                                   f'settings = "{self.app}.settings:DOVEORM"\n' + extra)

    def run_cli(self, *argv, code=0):
        # Each run is a fresh process in real life: re-import the app's modules.
        for name in [m for m in sys.modules if m == self.app or m.startswith(self.app + ".")]:
            del sys.modules[name]
        for name in ("Customer", "Order"):
            ModelMeta._registry.pop(name, None)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(list(argv))
        doveorm.reset_config()
        if code is not None:
            self.assertEqual(rc, code, f"stdout:\n{out.getvalue()}\nstderr:\n{err.getvalue()}")
        return out.getvalue() + err.getvalue()

    def add_field(self):
        self.write(f"{self.app}/models.py", MODELS.replace(
            "name = CharField(max_length=80)",
            "name = CharField(max_length=80)\n    phone = CharField(max_length=20, nullable=True)"))


# ──────────────────────────────────────────────
# init / project settings
# ──────────────────────────────────────────────

class TestInitAndProject(ProjectMixin, unittest.TestCase):
    def test_init_writes_config_and_folder(self):
        out = self.run_cli("init", "-m", f"{self.app}.models", "-s", f"{self.app}.settings:DOVEORM")
        self.assertIn("Created doveorm.toml", out)
        self.assertTrue(os.path.isdir("migrations"))
        project = Project.load()
        self.assertEqual(project.models, [f"{self.app}.models"])
        self.assertEqual(project.settings, f"{self.app}.settings:DOVEORM")
        self.assertEqual(project.migrations, os.path.join(self.root, "migrations"))
        self.assertIn("already exists", self.run_cli("init", code=1))

    def test_init_pyproject(self):
        self.write("pyproject.toml", '[project]\nname = "x"\n')
        self.run_cli("init", "--pyproject", "-m", f"{self.app}.models")
        self.assertEqual(Project.load().models, [f"{self.app}.models"])
        self.assertIn("already has", self.run_cli("init", "--pyproject", code=1))

    def test_config_found_in_parent_directory(self):
        self.config('migrations = "db/migrations"\n')
        os.makedirs("sub/deeper")
        os.chdir("sub/deeper")
        project = Project.load()
        self.assertEqual(project.root, self.root)
        self.assertEqual(project.migrations, os.path.join(self.root, "db", "migrations"))

    def test_pyproject_without_table_is_skipped(self):
        self.write("pyproject.toml", '[project]\nname = "x"\n')
        project = Project.load()
        self.assertIsNone(project.config_path)
        self.assertEqual(project.migrations, os.path.join(self.root, "migrations"))

    def test_flags_override_file(self):
        self.config()
        os.mkdir("elsewhere")
        project = Project.load(models=["other.models"], migrations="elsewhere")
        self.assertEqual(project.models, ["other.models"])
        self.assertEqual(project.migrations, os.path.join(self.root, "elsewhere"))

    def test_bad_config_values(self):
        self.write("doveorm.toml", 'model = ["x"]\n')
        with self.assertRaisesRegex(ConfigurationError, "unknown key"):
            Project.load()
        self.write("doveorm.toml", "models = 3\n")
        with self.assertRaisesRegex(ConfigurationError, "list of module names"):
            Project.load()
        self.write("doveorm.toml", "models = [\n")
        with self.assertRaisesRegex(ConfigurationError, "invalid TOML"):
            Project.load()

    def test_settings_forms(self):
        self.write(f"{self.app}/s_module.py", 'DATABASE_PATH = ":memory:"\nOTHER = 1\n')
        self.write(f"{self.app}/s_func.py", textwrap.dedent("""
            import doveorm
            def returns_dict():
                return {"DATABASE_PATH": ":memory:"}
            def configures():
                doveorm.configure(DATABASE_PATH=":memory:")
            def does_nothing():
                return None
            WRONG = 42
        """))
        for spec in ("s_module", "s_func:returns_dict", "s_func:configures"):
            doveorm.reset_config()
            Project.load(settings=f"{self.app}.{spec}").setup()
            self.assertTrue(doveorm.is_configured(), spec)
        for spec, message in (("s_func:does_nothing", "without calling"),
                              ("s_func:WRONG", "must be a dict"),
                              ("s_func:missing", "no attribute"),
                              ("nope_module", "module not found")):
            doveorm.reset_config()
            with self.assertRaisesRegex(ConfigurationError, message):
                Project.load(settings=f"{self.app}.{spec}" if spec != "nope_module" else spec).setup()

    def test_environment_settings(self):
        self.write("doveorm.toml", f'models = ["{self.app}.models"]\n')
        with mock.patch.dict(os.environ, {"DOVEORM_DATABASE_PATH": "env.db"}):
            self.run_cli("generate")
            self.run_cli("apply")
        self.assertTrue(os.path.exists("env.db"))

    def test_no_database_configured(self):
        self.write("doveorm.toml", f'models = ["{self.app}.models"]\n')
        self.assertIn("No database configured", self.run_cli("apply", code=1))

    def test_missing_models_module(self):
        self.write("doveorm.toml", 'models = ["no_such_app.models"]\n')
        self.assertIn("module not found", self.run_cli("generate", code=1))


# ──────────────────────────────────────────────
# Migration commands
# ──────────────────────────────────────────────

class TestMigrationCommands(ProjectMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config()

    def test_generate_refuses_without_models(self):
        self.write("doveorm.toml", f'settings = "{self.app}.settings:DOVEORM"\n')
        self.assertIn("No models are registered", self.run_cli("generate", code=1))
        self.assertFalse(os.path.isdir("migrations"))
        self.assertIn("No models are registered", self.run_cli("verify", code=1))

    def test_full_workflow(self):
        out = self.run_cli("generate")
        self.assertIn("0001_initial.py", out)
        self.assertIn("Create model Customer", out)
        self.assertIn("[ ] 0001_initial", self.run_cli("history"))
        self.assertIn("apply   0001_initial", self.run_cli("apply", "--dry-run"))
        db = doveorm.Database("app.db")
        self.assertEqual(health.list_tables(db), [])      # --dry-run created nothing
        db.close_all()
        self.assertIn("Applied 1 migration(s).", self.run_cli("apply"))
        self.assertIn("up to date", self.run_cli("apply"))
        self.assertIn("[x] 0001_initial", self.run_cli("history"))
        self.assertIn("No pending", self.run_cli("history", "--pending"))
        self.assertIn("No changes detected.", self.run_cli("generate"))
        self.assertIn("in sync", self.run_cli("verify", "--database"))
        self.assertIn("CREATE TABLE customers", self.run_cli("sql", "0001"))
        self.assertIn("DROP TABLE", self.run_cli("sql", "0001", "--backwards"))

    def test_change_detection_and_revert(self):
        self.run_cli("generate")
        self.run_cli("apply")
        self.add_field()
        out = self.run_cli("verify", code=1)
        self.assertIn("Add field phone to Customer", out)
        self.assertIn("phone", self.run_cli("generate", "--dry-run"))
        self.assertFalse(os.path.exists("migrations/0002_add_phone_to_customer.py"))
        self.run_cli("generate", "--name", "phone")
        self.assertIn("pending migrations: 0002_phone", self.run_cli("verify", "--database", code=1))
        self.run_cli("apply")
        self.assertIn("revert  0002_phone", self.run_cli("revert", "--dry-run"))
        self.assertIn("Reverted 1", self.run_cli("revert"))
        self.assertIn("[ ] 0002_phone", self.run_cli("history"))
        self.run_cli("apply", "0002")
        self.assertIn("Reverted 2", self.run_cli("revert", "--all"))
        self.assertIn("Nothing to revert", self.run_cli("revert"))
        self.run_cli("apply", "0001")
        self.assertIn("[ ] 0002_phone", self.run_cli("history"))

    def test_not_null_default_and_rename_flags(self):
        self.run_cli("generate")
        src = MODELS.replace("total = IntegerField(default=0)",
                             "amount = IntegerField(default=0)\n    code = CharField(max_length=5, nullable=False)")
        self.write(f"{self.app}/models.py", src)
        self.assertIn("NOT NULL", self.run_cli("generate", "--no-input", code=1))
        self.assertIn("not a Python literal",
                      self.run_cli("generate", "--default", "Order.code=abc", code=1))
        out = self.run_cli("generate", "--no-input", "--default", "Order.code='x'",
                           "--rename", "Order.amount=total")
        self.assertIn("Rename field total on Order to amount", out)

    def test_squash_and_merge(self):
        self.run_cli("generate")
        self.add_field()
        self.run_cli("generate")
        self.assertIn("Nothing to merge", self.run_cli("merge"))
        out = self.run_cli("squash", "0002", "--dry-run")
        self.assertIn("replaces", out)
        self.assertIn("Created", self.run_cli("squash", "0001", "0002", "--name", "base"))
        self.assertTrue(os.path.exists("migrations/0001_base.py"))

    def test_run_from_subfolder_uses_project_paths(self):
        os.makedirs("src/deep")
        os.chdir("src/deep")
        self.run_cli("generate")
        self.run_cli("apply")
        self.assertEqual(os.getcwd(), os.path.join(self.root, "src", "deep"))   # restored
        self.assertEqual(os.listdir("."), [])                  # nothing created here
        self.assertTrue(os.path.exists(os.path.join(self.root, "migrations", "0001_initial.py")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "app.db")))
        self.assertIn("[x] 0001_initial", self.run_cli("history"))

    def test_options_after_command(self):
        self.run_cli("generate", "-c", "doveorm.toml")
        out = self.run_cli("apply", "-v")
        self.assertIn("Applied 1", out)

    def test_adopt_existing_tables(self):
        self.run_cli("generate")
        db = doveorm.Database("app.db")
        db.execute_script("CREATE TABLE customers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);"
                          "CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                          "customer INTEGER, total INTEGER);")
        db.close_all()
        self.assertIn("Error", self.run_cli("apply", code=1))
        self.assertIn("Applied 1", self.run_cli("apply", "--adopt"))


# ──────────────────────────────────────────────
# status / doctor / models
# ──────────────────────────────────────────────

class TestDatabaseCommands(ProjectMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config()
        self.run_cli("generate")

    def test_status(self):
        self.run_cli("apply")
        out = self.run_cli("status")
        for text in ("Engine         sqlite", "Location       app.db", "Connection     ok",
                     "Applied  1", "Latest   0001_initial", "Pending  none"):
            self.assertIn(text, out)

    def test_status_unreachable(self):
        self.write(f"{self.app}/settings.py",
                   'DOVEORM = {"DATABASE_PATH": "/nonexistent-dir/x/app.db"}\n')
        self.assertIn("FAILED", self.run_cli("status", code=1))
        self.assertIn("[FAIL] connection", self.run_cli("doctor", code=1))

    def test_doctor_healthy(self):
        self.run_cli("apply")
        out = self.run_cli("doctor")
        self.assertIn("0 failed, 0 warning(s)", out)
        self.assertIn("integrity", out)

    def test_doctor_pending_and_permissions(self):
        self.run_cli("apply", "--fake")          # recorded, but no tables
        out = self.run_cli("doctor", code=1)
        self.assertIn("[FAIL] database schema", out)
        self.assertIn("table 'customers' (model Customer) is missing", out)
        self.run_cli("revert", "--fake")
        out = self.run_cli("doctor")
        self.assertIn("[warn] pending migrations", out)
        self.assertIn("pending migrations may fix this", out)
        self.run_cli("apply")
        os.chmod("app.db", 0o644)
        out = self.run_cli("doctor", "--strict", code=1)
        self.assertIn("readable by other users", out)

    def test_doctor_dangling_foreign_key(self):
        self.run_cli("apply")
        import sqlite3
        conn = sqlite3.connect("app.db")         # foreign keys are off by default here
        conn.execute("INSERT INTO orders (customer, total) VALUES (99, 1)")
        conn.commit()
        conn.close()
        out = self.run_cli("doctor", code=1)
        self.assertIn("[FAIL] foreign keys", out)

    def test_doctor_unmigrated_change_and_unknown_history(self):
        self.run_cli("apply")
        db = doveorm.Database("app.db")
        db.execute_write("INSERT INTO migrations (name) VALUES ('0099_gone')")
        db.close_all()
        self.add_field()
        out = self.run_cli("doctor", code=1)
        self.assertIn("[warn] models vs migrations  Add field phone to Customer", out)
        self.assertIn("0099_gone", out)
        self.assertIn("missing column(s) phone", out)

    def test_models(self):
        out = self.run_cli("models")
        self.assertRegex(out, r"Customer\s+customers\s+2\s+missing")
        self.run_cli("apply")
        self.assertRegex(self.run_cli("models"), r"Order\s+orders\s+3\s+ok")

    def test_console(self):
        self.run_cli("apply")
        with mock.patch("code.interact") as interact:
            out = self.run_cli("console", "--plain")
        ns = interact.call_args.kwargs["local"]
        self.assertIn("Customer", ns)
        self.assertIs(ns["doveorm"], doveorm)
        self.assertIn("db", ns)
        self.assertEqual(out, "")

    def test_connect_sqlite(self):
        with mock.patch("shutil.which", return_value="/usr/bin/sqlite3"), \
                mock.patch("subprocess.call", return_value=0) as call:
            self.run_cli("connect")
        self.assertEqual(call.call_args.args[0], ["/usr/bin/sqlite3", "app.db"])
        with mock.patch("shutil.which", return_value=None):
            self.assertIn("not found on PATH", self.run_cli("connect", code=1))
        self.write(f"{self.app}/settings.py", 'DOVEORM = {"DATABASE_PATH": ":memory:"}\n')
        self.assertIn("in-memory", self.run_cli("connect", code=1))


class TestMySQL(ProjectMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.conns = []
        self._patch = install_fake_mysqldb(self.conns)
        self.write(f"{self.app}/settings.py", textwrap.dedent("""
            DOVEORM = {"DATABASE_ENGINE": "mysql", "DATABASE_NAME": "shop",
                       "DATABASE_HOST": "127.0.0.1", "DATABASE_USER": "app",
                       "DATABASE_PASSWORD": 'p"w\\\\x'}
        """))
        self.config()

    def tearDown(self):
        self._patch.stop()
        super().tearDown()

    def test_connect_keeps_password_off_the_command_line(self):
        seen = {}

        def fake_call(argv):
            option_file = argv[1].split("=", 1)[1]
            seen["argv"] = argv
            seen["mode"] = stat.S_IMODE(os.stat(option_file).st_mode)
            with open(option_file) as fh:
                seen["content"] = fh.read()
            seen["file"] = option_file
            return 0

        with mock.patch("shutil.which", return_value="/usr/bin/mysql"), \
                mock.patch("subprocess.call", side_effect=fake_call):
            self.run_cli("connect")
        self.assertTrue(seen["argv"][1].startswith("--defaults-extra-file="))
        self.assertNotIn('p"w', " ".join(seen["argv"]))
        self.assertIn("--host=127.0.0.1", seen["argv"])
        self.assertEqual(seen["argv"][-1], "shop")
        self.assertEqual(seen["mode"], 0o600)
        self.assertEqual(seen["content"], '[client]\npassword="p\\"w\\\\x"\n')
        self.assertFalse(os.path.exists(seen["file"]))

    def test_status_and_doctor_never_show_password(self):
        out = self.run_cli("status") + self.run_cli("doctor", code=None)
        self.assertIn("app@127.0.0.1:3306/shop", out)
        self.assertNotIn('p"w', out)
        self.assertIn("[ok  ] tls", out)


if __name__ == "__main__":
    unittest.main()
