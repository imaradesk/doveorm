"""
tests/test_orm.py
Unit tests for DoveORM.
"""
import sys, os, datetime, unittest, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from doveorm import (
    Database, Model,
    AutoField, CharField, IntegerField, BooleanField, FloatField,
    DateTimeField, ForeignKey, TextField,
    HasMany, HasOne, BelongsTo, ManyToMany,
    Migrator, Q,
    # New fields
    BigIntegerField, SmallIntegerField,
    PositiveIntegerField, PositiveSmallIntegerField, PositiveBigIntegerField,
    BigAutoField, SmallAutoField,
    DecimalField,
    SlugField, EmailField, URLField,
    IPAddressField, GenericIPAddressField,
    DateField, TimeField, DurationField,
    UUIDField, JSONField, BinaryField,
    FileField, ImageField, FilePathField,
    OneToOneField,
)


def make_db():
    db = Database(":memory:")
    return db


class TestFields(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Product(Model):
            id     = AutoField()
            name   = CharField(max_length=50, nullable=False)
            price  = FloatField(default=0.0)
            active = BooleanField(default=True)

        Product.use(self.db)
        Product.create_table()
        self.Product = Product

    def test_create_and_find(self):
        p = self.Product.create(name="Widget", price=9.99)
        found = self.Product.find(p.id)
        self.assertEqual(found.name, "Widget")
        self.assertAlmostEqual(found.price, 9.99)

    def test_boolean_roundtrip(self):
        p = self.Product.create(name="Gadget", active=False)
        found = self.Product.find(p.id)
        self.assertFalse(found.active)

    def test_nullable_violation(self):
        with self.assertRaises(ValueError):
            self.Product.create(name=None)


class TestQuerySet(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class User(Model):
            id     = AutoField()
            name   = CharField(max_length=80, nullable=False)
            age    = IntegerField(default=0)
            active = BooleanField(default=True)

        User.use(self.db)
        User.create_table()
        self.User = User
        User.create(name="Titus", age=28, active=True)
        User.create(name="Alice", age=22, active=True)
        User.create(name="Bob",   age=35, active=False)

    def test_where_equality(self):
        result = self.User.where(name="Titus").get()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "Titus")

    def test_where_multi(self):
        result = self.User.where(name="Titus", age=28).get()
        self.assertEqual(len(result), 1)

    def test_where_gte(self):
        result = self.User.where(age__gte=28).get()
        self.assertEqual(len(result), 2)  # Titus, Bob

    def test_where_in(self):
        result = self.User.where(age__in=[22, 35]).get()
        self.assertEqual(len(result), 2)

    def test_order_by_desc(self):
        names = [u.name for u in self.User.all().order_by("-age").get()]
        self.assertEqual(names[0], "Bob")  # oldest first

    def test_count(self):
        self.assertEqual(self.User.all().count(), 3)
        self.assertEqual(self.User.where(active=True).count(), 2)

    def test_limit_offset(self):
        result = self.User.all().order_by("age").limit(2).offset(1).get()
        self.assertEqual(len(result), 2)

    def test_first_last(self):
        first = self.User.all().order_by("age").first()
        self.assertEqual(first.name, "Alice")

    def test_exists(self):
        self.assertTrue(self.User.where(name="Titus").exists())
        self.assertFalse(self.User.where(name="Ghost").exists())

    def test_bulk_update(self):
        n = self.User.where(active=False).update(active=True)
        self.assertEqual(n, 1)
        self.assertEqual(self.User.where(active=True).count(), 3)

    def test_bulk_delete(self):
        self.User.where(active=False).delete()
        self.assertEqual(self.User.all().count(), 2)

    def test_get_or_create(self):
        user, created = self.User.get_or_create(
            defaults={"age": 19}, name="Zara", active=True
        )
        self.assertTrue(created)
        _, created2 = self.User.get_or_create(name="Zara", active=True)
        self.assertFalse(created2)


class TestRelationships(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Author(Model):
            id    = AutoField()
            name  = CharField(max_length=80, nullable=False)
            posts = HasMany("Article", foreign_key="author_id")

        class Article(Model):
            id        = AutoField()
            author_id = ForeignKey("Author", on_delete="CASCADE", nullable=False)
            title     = CharField(max_length=200, nullable=False)
            author    = BelongsTo("Author", foreign_key="author_id")

        for cls in [Author, Article]:
            cls.use(self.db)
        Author.create_table()
        Article.create_table()
        self.Author = Author
        self.Article = Article

        self.a = Author.create(name="Titus")
        self.p1 = Article.create(author_id=self.a.id, title="ORM Deep Dive")
        self.p2 = Article.create(author_id=self.a.id, title="SQLite Tips")

    def test_has_many(self):
        posts = self.a.posts
        self.assertEqual(len(posts), 2)

    def test_belongs_to(self):
        author = self.p1.author
        self.assertEqual(author.name, "Titus")

    def test_cascade_delete(self):
        self.a.delete()
        self.assertEqual(self.Article.all().count(), 0)


# ══════════════════════════════════════════════════════════
# Issue 1: SQL Injection — column names must be validated
# ══════════════════════════════════════════════════════════

class TestSQLInjection(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Item(Model):
            id   = AutoField()
            name = CharField(max_length=80)

        Item.use(self.db)
        Item.create_table()
        self.Item = Item

    def test_reject_malicious_column_in_where(self):
        with self.assertRaises(ValueError):
            self.Item.where(**{"name; DROP TABLE items--": "x"}).get()

    def test_reject_unknown_column_in_where(self):
        with self.assertRaises(ValueError):
            self.Item.where(nonexistent=42).get()

    def test_reject_malicious_column_in_order_by(self):
        with self.assertRaises(ValueError):
            self.Item.all().order_by("name; DROP TABLE items").get()

    def test_reject_unknown_column_in_select(self):
        with self.assertRaises(ValueError):
            self.Item.all().select("nope").get()


# ══════════════════════════════════════════════════════════
# Issue 2: Thread safety — per-thread connections
# ══════════════════════════════════════════════════════════

class TestThreadSafety(unittest.TestCase):
    def test_per_thread_connections(self):
        db = Database(":memory:")
        connections = {}

        def worker(name):
            db.connect()
            connections[name] = db.driver._conn

        t1 = threading.Thread(target=worker, args=("t1",))
        t2 = threading.Thread(target=worker, args=("t2",))
        t1.start(); t2.start()
        t1.join();  t2.join()

        # Different threads must get different connection objects
        self.assertIsNotNone(connections["t1"])
        self.assertIsNotNone(connections["t2"])
        self.assertIsNot(connections["t1"], connections["t2"])


# ══════════════════════════════════════════════════════════
# Issue 3: Nested transactions via SAVEPOINTs
# ══════════════════════════════════════════════════════════

class TestNestedTransactions(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Counter(Model):
            id    = AutoField()
            value = IntegerField(default=0)

        Counter.use(self.db)
        Counter.create_table()
        self.Counter = Counter

    def test_nested_commit(self):
        """Both outer and inner commit succeed."""
        with self.db.transaction():
            self.Counter.create(value=1)
            with self.db.transaction():
                self.Counter.create(value=2)
        self.assertEqual(self.Counter.all().count(), 2)

    def test_nested_rollback(self):
        """Inner failure rolls back only the inner savepoint."""
        with self.db.transaction():
            self.Counter.create(value=1)
            try:
                with self.db.transaction():
                    self.Counter.create(value=2)
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
        # Outer committed, inner rolled back
        self.assertEqual(self.Counter.all().count(), 1)


# ══════════════════════════════════════════════════════════
# Issue 8: __hash__ alongside __eq__
# ══════════════════════════════════════════════════════════

class TestModelHash(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Tag(Model):
            id    = AutoField()
            label = CharField(max_length=30)

        Tag.use(self.db)
        Tag.create_table()
        self.Tag = Tag

    def test_hash_consistency(self):
        t1 = self.Tag.create(label="python")
        t2 = self.Tag.find(t1.id)
        self.assertEqual(hash(t1), hash(t2))

    def test_usable_in_set(self):
        t1 = self.Tag.create(label="python")
        t2 = self.Tag.find(t1.id)
        s = {t1, t2}
        self.assertEqual(len(s), 1)

    def test_usable_as_dict_key(self):
        t = self.Tag.create(label="python")
        d = {t: "value"}
        self.assertEqual(d[t], "value")


# ══════════════════════════════════════════════════════════
# Issue 9: refresh() on deleted record
# ══════════════════════════════════════════════════════════

class TestRefreshDeleted(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Note(Model):
            id   = AutoField()
            text = CharField(max_length=200)

        Note.use(self.db)
        Note.create_table()
        self.Note = Note

    def test_refresh_after_delete_raises(self):
        n = self.Note.create(text="hello")
        pk = n.id
        # Delete via SQL so the instance still has its pk
        self.Note.where(id=pk).delete()
        with self.assertRaises(RuntimeError):
            n.refresh()


# ══════════════════════════════════════════════════════════
# Issue 11: Smarter pluralization + Meta.table_name
# ══════════════════════════════════════════════════════════

class TestTableNaming(unittest.TestCase):
    def test_smart_pluralization(self):
        db = make_db()

        class Category(Model):
            id   = AutoField()
            name = CharField(max_length=50)

        self.assertEqual(Category._table_name(), "categories")

    def test_sibilant_pluralization(self):
        """Words ending in s/x/z/ch/sh get 'es'."""
        db = make_db()

        class UserBatch(Model):
            id   = AutoField()
            name = CharField(max_length=50)

        self.assertEqual(UserBatch._table_name(), "user_batches")

    def test_meta_table_name_override(self):
        db = make_db()

        class Person(Model):
            id   = AutoField()
            name = CharField(max_length=50)
            class Meta:
                table_name = "people"

        self.assertEqual(Person._table_name(), "people")


# ══════════════════════════════════════════════════════════
# Issue 12: BooleanField default in DDL
# ══════════════════════════════════════════════════════════

class TestBooleanDefault(unittest.TestCase):
    def test_boolean_default_roundtrip(self):
        """A row inserted relying on the DEFAULT should get the correct bool."""
        db = make_db()

        class Flag(Model):
            id     = AutoField()
            label  = CharField(max_length=30, nullable=False)
            active = BooleanField(default=True)

        Flag.use(db)
        Flag.create_table()

        # Insert using raw SQL to rely on the DEFAULT clause
        db.execute_write(
            "INSERT INTO flags (label) VALUES (?)", ["test"]
        )
        row = Flag.all().first()
        self.assertTrue(row.active)

    def test_boolean_default_ddl_value(self):
        """The generated DDL should say DEFAULT 1, not DEFAULT True."""
        f = BooleanField(default=True)
        f.name = "active"
        defn = f.sql_definition()
        self.assertIn("DEFAULT 1", defn)
        self.assertNotIn("DEFAULT True", defn)


# ══════════════════════════════════════════════════════════
# Issue 13: db_index support
# ══════════════════════════════════════════════════════════

class TestIndexes(unittest.TestCase):
    def test_field_db_index(self):
        db = make_db()

        class LogEntry(Model):
            id      = AutoField()
            message = CharField(max_length=200)
            level   = IntegerField(default=0, db_index=True)

        LogEntry.use(db)
        LogEntry.create_table()

        # Check sqlite_master for the index
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_log_entr%'",
        )
        idx_names = [r[0] for r in rows]
        self.assertTrue(any("level" in n for n in idx_names))

    def test_meta_composite_index(self):
        db = make_db()

        class Event(Model):
            id   = AutoField()
            kind = CharField(max_length=50)
            year = IntegerField(default=2024)
            class Meta:
                indexes = [("kind", "year")]

        Event.use(db)
        Event.create_table()

        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_event%'",
        )
        idx_names = [r[0] for r in rows]
        self.assertTrue(any("kind" in n and "year" in n for n in idx_names))


# ══════════════════════════════════════════════════════════
# Issue 14: select() partial columns don't crash _from_row
# ══════════════════════════════════════════════════════════

class TestSelectPartial(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Employee(Model):
            id   = AutoField()
            name = CharField(max_length=80, nullable=False)
            age  = IntegerField(default=0)

        Employee.use(self.db)
        Employee.create_table()
        self.Employee = Employee
        Employee.create(name="Alice", age=30)

    def test_select_subset_does_not_crash(self):
        results = self.Employee.all().select("name").get()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "Alice")

    def test_unselected_field_gets_default(self):
        results = self.Employee.all().select("name").get()
        # age was not selected → should be the field default (0)
        self.assertEqual(results[0].age, 0)


# ══════════════════════════════════════════════════════════
# Issue 5 + 10: Migrator rollback with fn + no utcnow
# ══════════════════════════════════════════════════════════

class TestMigratorRollback(unittest.TestCase):
    def test_rollback_calls_rollback_fn(self):
        db = make_db()
        called = []

        class Dummy(Model):
            id  = AutoField()
            val = IntegerField(default=0)

        Dummy.use(db)

        m = Migrator(db)
        m.register(
            "0001_create_dummy",
            lambda: Dummy.create_table(),
            rollback_fn=lambda: (Dummy.drop_table(), called.append(True)),
        )
        m.migrate()
        self.assertTrue(db.table_exists("dummies"))

        m.rollback(steps=1)
        self.assertTrue(len(called) > 0, "rollback_fn was not called")
        self.assertFalse(db.table_exists("dummies"))

    def test_migration_timestamp_is_tz_aware(self):
        from doveorm.migrations import MigrationRecord as Migration
        db = make_db()

        class X(Model):
            id = AutoField()
            v = IntegerField(default=0)

        X.use(db)
        m = Migrator(db)
        m.register("0001_x", lambda: X.create_table())
        m.migrate()

        rec = Migration.where(name="0001_x").first()
        self.assertIsNotNone(rec.applied_at)
        # The stored datetime should be timezone-aware (UTC)
        self.assertIsNotNone(rec.applied_at.tzinfo)


# ══════════════════════════════════════════════════════════
# Issue 6: Prefetch related (eager loading)
# ══════════════════════════════════════════════════════════

class TestPrefetchRelated(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Writer(Model):
            id    = AutoField()
            name  = CharField(max_length=80, nullable=False)
            posts = HasMany("Story", foreign_key="writer_id")

        class Story(Model):
            id        = AutoField()
            writer_id = ForeignKey("Writer", on_delete="CASCADE", nullable=False)
            title     = CharField(max_length=200, nullable=False)
            writer    = BelongsTo("Writer", foreign_key="writer_id")

        for cls in [Writer, Story]:
            cls.use(self.db)
        Writer.create_table()
        Story.create_table()
        self.Writer = Writer
        self.Story = Story

        self.w1 = Writer.create(name="Ada")
        self.w2 = Writer.create(name="Grace")
        Story.create(writer_id=self.w1.id, title="Story A1")
        Story.create(writer_id=self.w1.id, title="Story A2")
        Story.create(writer_id=self.w2.id, title="Story G1")

    def test_prefetch_has_many(self):
        writers = self.Writer.all().prefetch_related("posts").get()
        # Results should already have cached posts
        for w in writers:
            self.assertIn("posts", w._prefetch_cache)
        ada = [w for w in writers if w.name == "Ada"][0]
        self.assertEqual(len(ada.posts), 2)

    def test_prefetch_belongs_to(self):
        stories = self.Story.all().order_by("id").prefetch_related("writer").get()
        for s in stories:
            self.assertIn("writer", s._prefetch_cache)
        # First two stories belong to Ada, third to Grace
        self.assertEqual(stories[0].writer.name, "Ada")
        self.assertEqual(stories[2].writer.name, "Grace")


# ══════════════════════════════════════════════════════════
# Issue 7: ManyToMany .add() / .remove()
# ══════════════════════════════════════════════════════════

class TestManyToManyManager(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Student(Model):
            id      = AutoField()
            name    = CharField(max_length=80, nullable=False)
            courses = ManyToMany("Course", through="student_courses",
                                 source_fk="student_id", target_fk="course_id")

        class Course(Model):
            id    = AutoField()
            title = CharField(max_length=100, nullable=False)

        for cls in [Student, Course]:
            cls.use(self.db)
        Student.create_table()
        Course.create_table()

        # Create join table manually
        self.db.execute_script("""
            CREATE TABLE IF NOT EXISTS student_courses (
                student_id INTEGER NOT NULL,
                course_id  INTEGER NOT NULL,
                PRIMARY KEY (student_id, course_id)
            );
        """)

        self.Student = Student
        self.Course = Course

    def test_add_and_iterate(self):
        s = self.Student.create(name="Alice")
        c1 = self.Course.create(title="Math")
        c2 = self.Course.create(title="Science")

        s.courses.add(c1, c2)

        mgr = s.courses
        self.assertEqual(len(mgr), 2)
        titles = sorted(c.title for c in mgr)
        self.assertEqual(titles, ["Math", "Science"])

    def test_remove(self):
        s = self.Student.create(name="Bob")
        c1 = self.Course.create(title="Art")
        c2 = self.Course.create(title="Music")

        s.courses.add(c1, c2)
        self.assertEqual(len(s.courses), 2)

        s.courses.remove(c1)
        self.assertEqual(len(s.courses), 1)
        self.assertEqual(s.courses[0].title, "Music")


# ══════════════════════════════════════════════════════════
# Q objects – OR filters
# ══════════════════════════════════════════════════════════

class TestQObjects(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Person(Model):
            id     = AutoField()
            name   = CharField(max_length=80, nullable=False)
            age    = IntegerField(default=0)
            active = BooleanField(default=True)

            class Meta:
                table_name = "q_people"

        Person.use(self.db)
        Person.create_table()
        self.Person = Person
        Person.create(name="Alice", age=20, active=True)
        Person.create(name="Bob",   age=35, active=False)
        Person.create(name="Carol", age=28, active=True)

    def test_or_filter(self):
        result = self.Person.all().filter(Q(name="Alice") | Q(name="Bob")).get()
        names = sorted(r.name for r in result)
        self.assertEqual(names, ["Alice", "Bob"])

    def test_and_filter(self):
        result = self.Person.all().filter(Q(active=True) & Q(age__gte=25)).get()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, "Carol")

    def test_negated_filter(self):
        result = self.Person.all().filter(~Q(name="Alice")).get()
        names = sorted(r.name for r in result)
        self.assertEqual(names, ["Bob", "Carol"])

    def test_combined_where_and_q(self):
        """Mixing .where() with .filter(Q) should AND them together."""
        result = self.Person.where(active=True).filter(
            Q(name="Alice") | Q(name="Carol")
        ).get()
        names = sorted(r.name for r in result)
        self.assertEqual(names, ["Alice", "Carol"])


# ══════════════════════════════════════════════════════════
# BooleanField to_db in filter values
# ══════════════════════════════════════════════════════════

class TestFilterToDb(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Toggle(Model):
            id      = AutoField()
            label   = CharField(max_length=50, nullable=False)
            enabled = BooleanField(default=False)

            class Meta:
                table_name = "toggles"

        Toggle.use(self.db)
        Toggle.create_table()
        self.Toggle = Toggle
        Toggle.create(label="lights", enabled=True)
        Toggle.create(label="sound",  enabled=False)

    def test_where_with_python_bool(self):
        """where(enabled=True) should convert True→1 via to_db before comparing."""
        result = self.Toggle.where(enabled=True).get()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].label, "lights")

    def test_where_with_python_false(self):
        result = self.Toggle.where(enabled=False).get()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].label, "sound")


# ══════════════════════════════════════════════════════════
# Database context manager
# ══════════════════════════════════════════════════════════

class TestDatabaseContextManager(unittest.TestCase):
    def test_context_manager(self):
        with Database(":memory:") as db:
            self.assertTrue(db.driver.is_connected())
        self.assertFalse(db.driver.is_connected())


# ══════════════════════════════════════════════════════════
# Database.raw() escape hatch
# ══════════════════════════════════════════════════════════

class TestRawQuery(unittest.TestCase):
    def test_raw_select(self):
        db = make_db()

        class Fruit(Model):
            id   = AutoField()
            name = CharField(max_length=50)

            class Meta:
                table_name = "fruits"

        Fruit.use(db)
        Fruit.create_table()
        Fruit.create(name="Apple")
        Fruit.create(name="Banana")

        rows = db.raw("SELECT name FROM fruits ORDER BY name")
        names = [r[0] for r in rows]
        self.assertEqual(names, ["Apple", "Banana"])


# ══════════════════════════════════════════════════════════
# ManyToMany duplicate add prevention
# ══════════════════════════════════════════════════════════

class TestManyToManyDuplicateAdd(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Player(Model):
            id    = AutoField()
            name  = CharField(max_length=80, nullable=False)
            teams = ManyToMany("Team", through="player_teams",
                               source_fk="player_id", target_fk="team_id")

            class Meta:
                table_name = "players"

        class Team(Model):
            id   = AutoField()
            name = CharField(max_length=80, nullable=False)

            class Meta:
                table_name = "teams"

        for cls in [Player, Team]:
            cls.use(self.db)
        Player.create_table()
        Team.create_table()

        self.db.execute_script("""
            CREATE TABLE IF NOT EXISTS player_teams (
                player_id INTEGER NOT NULL,
                team_id   INTEGER NOT NULL,
                PRIMARY KEY (player_id, team_id)
            );
        """)
        self.Player = Player
        self.Team = Team

    def test_add_same_twice_no_duplicate(self):
        p = self.Player.create(name="Titus")
        t = self.Team.create(name="Red")

        mgr = p.teams
        mgr.add(t)
        mgr.add(t)  # add again
        self.assertEqual(len(mgr), 1)  # should still be 1


# ══════════════════════════════════════════════════════════
# Driver architecture tests
# ══════════════════════════════════════════════════════════

class TestDriverArchitecture(unittest.TestCase):
    """Test the driver abstraction without needing a real MySQL server."""

    def test_sqlite_driver_name(self):
        db = Database(":memory:")
        self.assertEqual(db.driver_name, "sqlite")
        self.assertEqual(db.placeholder, "?")

    def test_sqlite_driver_type_map(self):
        from doveorm.drivers.sqlite import SQLiteDriver
        drv = SQLiteDriver(":memory:")
        m = drv.sql_type_map()
        self.assertEqual(m["BOOLEAN"], "INTEGER")
        self.assertEqual(m["DATETIME"], "TEXT")
        self.assertEqual(m["JSON"], "TEXT")

    def test_mysql_driver_type_map(self):
        from doveorm.drivers.mysql import MySQLDriver
        drv = MySQLDriver(host="localhost", user="test", password="", database="test")
        m = drv.sql_type_map()
        self.assertEqual(m["INTEGER"], "INT")
        self.assertEqual(m["BOOLEAN"], "TINYINT(1)")
        self.assertEqual(m["DATETIME"], "DATETIME")
        self.assertEqual(m["JSON"], "JSON")
        self.assertEqual(m["TEXT"], "LONGTEXT")

    def test_mysql_driver_placeholder(self):
        from doveorm.drivers.mysql import MySQLDriver
        drv = MySQLDriver(host="localhost", user="test", password="", database="test")
        self.assertEqual(drv.placeholder, "%s")
        self.assertEqual(drv.adapt_sql("SELECT * WHERE id = ?"), "SELECT * WHERE id = %s")

    def test_mysql_driver_autoincrement(self):
        from doveorm.drivers.mysql import MySQLDriver
        drv = MySQLDriver(host="localhost", user="test", password="", database="test")
        self.assertIn("AUTO_INCREMENT", drv.autoincrement_definition())

    def test_mysql_driver_insert_ignore(self):
        from doveorm.drivers.mysql import MySQLDriver
        drv = MySQLDriver(host="localhost", user="test", password="", database="test")
        self.assertEqual(drv.insert_ignore_prefix(), "INSERT IGNORE")

    def test_field_sql_definition_with_sqlite_driver(self):
        """Fields produce correct DDL when given a SQLite driver."""
        from doveorm.drivers.sqlite import SQLiteDriver
        drv = SQLiteDriver(":memory:")
        b = BooleanField(default=True)
        b.name = "active"
        defn = b.sql_definition(drv)
        self.assertIn("INTEGER", defn)
        self.assertIn("DEFAULT 1", defn)

    def test_field_sql_definition_with_mysql_driver(self):
        """Fields produce correct DDL when given a MySQL driver."""
        from doveorm.drivers.mysql import MySQLDriver
        drv = MySQLDriver(host="localhost", user="test", password="", database="test")
        b = BooleanField(default=True)
        b.name = "active"
        defn = b.sql_definition(drv)
        self.assertIn("TINYINT(1)", defn)
        self.assertIn("DEFAULT 1", defn)

    def test_charfield_mysql_uses_varchar(self):
        from doveorm.drivers.mysql import MySQLDriver
        drv = MySQLDriver(host="localhost", user="test", password="", database="test")
        f = CharField(max_length=100)
        f.name = "name"
        defn = f.sql_definition(drv)
        self.assertIn("VARCHAR(100)", defn)

    def test_charfield_sqlite_uses_text(self):
        from doveorm.drivers.sqlite import SQLiteDriver
        drv = SQLiteDriver(":memory:")
        f = CharField(max_length=100)
        f.name = "name"
        defn = f.sql_definition(drv)
        self.assertIn("TEXT", defn)
        self.assertNotIn("VARCHAR", defn)

    def test_database_repr_shows_driver(self):
        db = Database(":memory:")
        r = repr(db)
        self.assertIn("sqlite", r)


# ══════════════════════════════════════════════════════════
# Additional field types
# ══════════════════════════════════════════════════════════

class TestNewNumericFields(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Stats(Model):
            id        = AutoField()
            big_val   = BigIntegerField(default=0)
            small_val = SmallIntegerField(default=0)
            pos_val   = PositiveIntegerField(default=0)
            pos_small = PositiveSmallIntegerField(default=0)
            pos_big   = PositiveBigIntegerField(default=0)
            price     = DecimalField(max_digits=8, decimal_places=2, nullable=True)

            class Meta:
                table_name = "stats"

        Stats.use(self.db)
        Stats.create_table()
        self.Stats = Stats

    def test_big_integer_roundtrip(self):
        s = self.Stats.create(big_val=9_999_999_999)
        found = self.Stats.find(s.id)
        self.assertEqual(found.big_val, 9_999_999_999)

    def test_small_integer_roundtrip(self):
        s = self.Stats.create(small_val=123)
        found = self.Stats.find(s.id)
        self.assertEqual(found.small_val, 123)

    def test_positive_integer_rejects_negative(self):
        with self.assertRaises(ValueError):
            self.Stats.create(pos_val=-1)

    def test_positive_small_rejects_negative(self):
        with self.assertRaises(ValueError):
            self.Stats.create(pos_small=-5)

    def test_positive_big_rejects_negative(self):
        with self.assertRaises(ValueError):
            self.Stats.create(pos_big=-100)

    def test_decimal_roundtrip(self):
        from decimal import Decimal
        s = self.Stats.create(price=Decimal("19.99"))
        found = self.Stats.find(s.id)
        self.assertEqual(found.price, Decimal("19.99"))

    def test_decimal_none(self):
        s = self.Stats.create(price=None)
        found = self.Stats.find(s.id)
        self.assertIsNone(found.price)


class TestStringFields(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Page(Model):
            id      = AutoField()
            slug    = SlugField(max_length=100, unique=True)
            email   = EmailField(nullable=True)
            website = URLField(nullable=True)
            ip      = IPAddressField(nullable=True)
            any_ip  = GenericIPAddressField(nullable=True)

            class Meta:
                table_name = "pages"

        Page.use(self.db)
        Page.create_table()
        self.Page = Page

    def test_slug_valid(self):
        p = self.Page.create(slug="hello-world")
        self.assertEqual(self.Page.find(p.id).slug, "hello-world")

    def test_slug_invalid(self):
        with self.assertRaises(ValueError):
            self.Page.create(slug="hello world!!")

    def test_email_valid(self):
        p = self.Page.create(slug="e1", email="user@example.com")
        self.assertEqual(self.Page.find(p.id).email, "user@example.com")

    def test_email_invalid(self):
        with self.assertRaises(ValueError):
            self.Page.create(slug="e2", email="not-an-email")

    def test_url_valid(self):
        p = self.Page.create(slug="u1", website="https://example.com")
        self.assertEqual(self.Page.find(p.id).website, "https://example.com")

    def test_url_invalid(self):
        with self.assertRaises(ValueError):
            self.Page.create(slug="u2", website="not a url")

    def test_ipv4_valid(self):
        p = self.Page.create(slug="ip1", ip="192.168.1.1")
        self.assertEqual(self.Page.find(p.id).ip, "192.168.1.1")

    def test_ipv4_invalid(self):
        with self.assertRaises(ValueError):
            self.Page.create(slug="ip2", ip="999.999.999.999")

    def test_generic_ip_ipv6(self):
        p = self.Page.create(slug="ip3", any_ip="::1")
        self.assertEqual(self.Page.find(p.id).any_ip, "::1")

    def test_generic_ip_invalid(self):
        with self.assertRaises(ValueError):
            self.Page.create(slug="ip4", any_ip="not-an-ip")


class TestTimeAndDurationFields(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Schedule(Model):
            id         = AutoField()
            start_time = TimeField(nullable=True)
            duration   = DurationField(nullable=True)

            class Meta:
                table_name = "schedules"

        Schedule.use(self.db)
        Schedule.create_table()
        self.Schedule = Schedule

    def test_time_roundtrip(self):
        t = datetime.time(14, 30, 0)
        s = self.Schedule.create(start_time=t)
        found = self.Schedule.find(s.id)
        self.assertEqual(found.start_time, t)

    def test_duration_roundtrip(self):
        d = datetime.timedelta(hours=2, minutes=30)
        s = self.Schedule.create(duration=d)
        found = self.Schedule.find(s.id)
        self.assertEqual(found.duration, d)

    def test_duration_none(self):
        s = self.Schedule.create(duration=None)
        found = self.Schedule.find(s.id)
        self.assertIsNone(found.duration)


class TestUUIDField(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Token(Model):
            id    = AutoField()
            value = UUIDField(nullable=False)

            class Meta:
                table_name = "tokens"

        Token.use(self.db)
        Token.create_table()
        self.Token = Token

    def test_uuid_roundtrip(self):
        import uuid
        u = uuid.uuid4()
        t = self.Token.create(value=u)
        found = self.Token.find(t.id)
        self.assertEqual(found.value, u)

    def test_uuid_from_string(self):
        import uuid
        u = uuid.uuid4()
        t = self.Token.create(value=str(u))
        found = self.Token.find(t.id)
        self.assertEqual(found.value, u)


class TestBinaryField(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Blob(Model):
            id   = AutoField()
            data = BinaryField(nullable=True)

            class Meta:
                table_name = "blobs"

        Blob.use(self.db)
        Blob.create_table()
        self.Blob = Blob

    def test_binary_roundtrip(self):
        raw = b"\x00\x01\x02\xff"
        b = self.Blob.create(data=raw)
        found = self.Blob.find(b.id)
        self.assertEqual(found.data, raw)

    def test_binary_rejects_string(self):
        with self.assertRaises(TypeError):
            self.Blob.create(data="not bytes")


class TestFileAndImageFields(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Upload(Model):
            id       = AutoField()
            document = FileField(upload_to="uploads/docs/", nullable=True)
            avatar   = ImageField(upload_to="uploads/avatars/", nullable=True)
            config   = FilePathField(path="/etc/app/", nullable=True)

            class Meta:
                table_name = "uploads"

        Upload.use(self.db)
        Upload.create_table()
        self.Upload = Upload

    def test_file_field_stores_path(self):
        u = self.Upload.create(document="uploads/docs/report.pdf")
        found = self.Upload.find(u.id)
        self.assertEqual(found.document, "uploads/docs/report.pdf")

    def test_image_field_stores_path(self):
        u = self.Upload.create(avatar="uploads/avatars/me.png")
        found = self.Upload.find(u.id)
        self.assertEqual(found.avatar, "uploads/avatars/me.png")

    def test_filepath_field_stores_path(self):
        u = self.Upload.create(config="/etc/app/main.conf")
        found = self.Upload.find(u.id)
        self.assertEqual(found.config, "/etc/app/main.conf")

    def test_upload_to_attribute(self):
        f = FileField(upload_to="media/")
        self.assertEqual(f.upload_to, "media/")

    def test_image_dimension_attrs(self):
        f = ImageField(upload_to="img/", width_field="w", height_field="h")
        self.assertEqual(f.width_field, "w")
        self.assertEqual(f.height_field, "h")


class TestOneToOneField(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Account(Model):
            id   = AutoField()
            name = CharField(max_length=80, nullable=False)

            class Meta:
                table_name = "accounts"

        class Profile(Model):
            id         = AutoField()
            account_id = OneToOneField("Account", on_delete="CASCADE", nullable=False)
            bio        = TextField(nullable=True, default="")
            account    = BelongsTo("Account", foreign_key="account_id")

            class Meta:
                table_name = "profiles"

        for cls in [Account, Profile]:
            cls.use(self.db)
        Account.create_table()
        Profile.create_table()
        self.Account = Account
        self.Profile = Profile

    def test_one_to_one_roundtrip(self):
        a = self.Account.create(name="Titus")
        p = self.Profile.create(account_id=a.id, bio="Hello!")
        found = self.Profile.find(p.id)
        self.assertEqual(found.account.name, "Titus")

    def test_one_to_one_unique_enforced(self):
        """Cannot create two profiles for the same account."""
        a = self.Account.create(name="Alice")
        self.Profile.create(account_id=a.id, bio="First")
        with self.assertRaises(Exception):
            self.Profile.create(account_id=a.id, bio="Second")


class TestChoicesValidation(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Ticket(Model):
            id     = AutoField()
            status = CharField(
                max_length=20, nullable=False,
                choices=[("open", "Open"), ("closed", "Closed"), ("pending", "Pending")],
            )

            class Meta:
                table_name = "tickets"

        Ticket.use(self.db)
        Ticket.create_table()
        self.Ticket = Ticket

    def test_valid_choice(self):
        t = self.Ticket.create(status="open")
        self.assertEqual(t.status, "open")

    def test_invalid_choice(self):
        with self.assertRaises(ValueError):
            self.Ticket.create(status="invalid")


# ══════════════════════════════════════════════════════════
# Dynamic findBy finders
# ══════════════════════════════════════════════════════════

class TestDynamicFindBy(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

        class Customer(Model):
            id       = AutoField()
            username = CharField(max_length=80, nullable=False, unique=True)
            email    = CharField(max_length=120, nullable=False)
            age      = IntegerField(default=0)
            active   = BooleanField(default=True)

            class Meta:
                table_name = "customers"

        Customer.use(self.db)
        Customer.create_table()
        self.Customer = Customer

        Customer.create(username="titus", email="titus@example.com", age=28, active=True)
        Customer.create(username="alice", email="alice@example.com", age=22, active=True)
        Customer.create(username="bob",   email="bob@example.com",   age=35, active=False)

    def test_findBy_single_field(self):
        """User.findByUsername("titus") returns first match or None."""
        user = self.Customer.findByUsername("titus")
        self.assertIsNotNone(user)
        self.assertEqual(user.username, "titus")

    def test_findBy_returns_none_when_not_found(self):
        user = self.Customer.findByUsername("ghost")
        self.assertIsNone(user)

    def test_findBy_email(self):
        user = self.Customer.findByEmail("alice@example.com")
        self.assertIsNotNone(user)
        self.assertEqual(user.username, "alice")

    def test_findAllBy_returns_list(self):
        """findAllByActive(True) returns all active customers."""
        users = self.Customer.findAllByActive(True)
        self.assertIsInstance(users, list)
        self.assertEqual(len(users), 2)
        names = sorted(u.username for u in users)
        self.assertEqual(names, ["alice", "titus"])

    def test_findAllBy_returns_empty_list(self):
        users = self.Customer.findAllByAge(99)
        self.assertIsInstance(users, list)
        self.assertEqual(len(users), 0)

    def test_findBy_compound_and(self):
        """findByActiveAndAge(True, 28) – two conditions joined by AND."""
        user = self.Customer.findByActiveAndAge(True, 28)
        self.assertIsNotNone(user)
        self.assertEqual(user.username, "titus")

    def test_findBy_compound_no_match(self):
        user = self.Customer.findByActiveAndAge(False, 22)
        self.assertIsNone(user)

    def test_findAllBy_compound_and(self):
        users = self.Customer.findAllByActiveAndAge(True, 22)
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0].username, "alice")

    def test_countBy(self):
        """countByActive(True) returns an int count."""
        count = self.Customer.countByActive(True)
        self.assertEqual(count, 2)

    def test_countBy_zero(self):
        count = self.Customer.countByAge(99)
        self.assertEqual(count, 0)

    def test_existsBy(self):
        """existsByEmail("titus@example.com") returns True."""
        self.assertTrue(self.Customer.existsByEmail("titus@example.com"))

    def test_existsBy_false(self):
        self.assertFalse(self.Customer.existsByEmail("nobody@example.com"))

    def test_existsBy_compound(self):
        self.assertTrue(self.Customer.existsByUsernameAndActive("bob", False))
        self.assertFalse(self.Customer.existsByUsernameAndActive("bob", True))

    def test_findBy_unknown_field_raises(self):
        """Accessing an unknown field via findBy should raise AttributeError."""
        with self.assertRaises(AttributeError):
            self.Customer.findByNonexistent("x")

    def test_findBy_wrong_arg_count_raises(self):
        """Passing wrong number of args raises TypeError."""
        with self.assertRaises(TypeError):
            self.Customer.findByUsernameAndAge("titus")  # missing age arg

    def test_unrelated_attr_still_raises(self):
        """Accessing a totally unrelated attr still raises AttributeError."""
        with self.assertRaises(AttributeError):
            _ = self.Customer.some_random_attribute

    def test_findBy_camel_to_snake_multi_word(self):
        """CamelCase is correctly converted: AuthorId → author_id."""
        db2 = make_db()

        class BlogPost(Model):
            id        = AutoField()
            author_id = IntegerField(default=0)
            published = BooleanField(default=False)
            class Meta:
                table_name = "blog_posts_findby"

        BlogPost.use(db2)
        BlogPost.create_table()
        BlogPost.create(author_id=42, published=True)
        BlogPost.create(author_id=42, published=False)
        BlogPost.create(author_id=99, published=True)

        posts = BlogPost.findAllByAuthorId(42)
        self.assertEqual(len(posts), 2)

        post = BlogPost.findByAuthorIdAndPublished(42, True)
        self.assertIsNotNone(post)

        count = BlogPost.countByAuthorId(99)
        self.assertEqual(count, 1)


# ══════════════════════════════════════════════════════════
# @table() decorator – alternative to class Meta
# ══════════════════════════════════════════════════════════

from doveorm import table


class TestTableDecorator(unittest.TestCase):
    def test_table_name_override(self):
        """@table(name="...") sets the table name."""
        db = make_db()

        @table(name="custom_widgets")
        class Widget(Model):
            id    = AutoField()
            label = CharField(max_length=50)

        Widget.use(db)
        self.assertEqual(Widget._table_name(), "custom_widgets")

    def test_table_indexes(self):
        """@table(indexes=[...]) creates indexes on create_table."""
        db = make_db()

        @table(name="indexed_items", indexes=[("color",)])
        class ColorItem(Model):
            id    = AutoField()
            color = CharField(max_length=30)

        ColorItem.use(db)
        ColorItem.create_table()

        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_indexed%'"
        )
        idx_names = [r[0] for r in rows]
        self.assertTrue(any("color" in n for n in idx_names))

    def test_table_unique_together(self):
        """@table(unique_together=[...]) creates unique constraints."""
        db = make_db()

        @table(name="team_members", unique_together=[("team", "member")])
        class TeamMember(Model):
            id     = AutoField()
            team   = CharField(max_length=50)
            member = CharField(max_length=50)

        TeamMember.use(db)
        TeamMember.create_table()

        TeamMember.create(team="alpha", member="alice")
        with self.assertRaises(Exception):
            TeamMember.create(team="alpha", member="alice")  # duplicate

    def test_table_ordering(self):
        """@table(ordering=[...]) sets default ordering on .all()."""
        db = make_db()

        @table(name="sorted_items", ordering=["-id"])
        class SortedItem(Model):
            id    = AutoField()
            value = IntegerField(default=0)

        SortedItem.use(db)
        SortedItem.create_table()
        SortedItem.create(value=10)
        SortedItem.create(value=20)
        SortedItem.create(value=30)

        items = SortedItem.all().get()
        # Default ordering is -id, so newest first
        self.assertEqual(items[0].value, 30)
        self.assertEqual(items[-1].value, 10)

    def test_table_overrides_meta(self):
        """@table() takes precedence over class Meta."""
        db = make_db()

        @table(name="decorator_wins")
        class Conflict(Model):
            id  = AutoField()
            val = IntegerField(default=0)
            class Meta:
                table_name = "meta_name"

        self.assertEqual(Conflict._table_name(), "decorator_wins")

    def test_table_without_name_uses_auto(self):
        """@table() with no name arg still auto-generates the table name."""
        db = make_db()

        @table(indexes=[("score",)])
        class HighScore(Model):
            id    = AutoField()
            score = IntegerField(default=0)

        # Auto-generated: high_scores
        self.assertEqual(HighScore._table_name(), "high_scores")

    def test_table_decorator_with_findby(self):
        """@table and findBy work together."""
        db = make_db()

        @table(name="employees_v2")
        class Employee(Model):
            id   = AutoField()
            name   = CharField(max_length=80)
            dept   = CharField(max_length=50)

        Employee.use(db)
        Employee.create_table()
        Employee.create(name="Ada", dept="Engineering")
        Employee.create(name="Grace", dept="Engineering")
        Employee.create(name="Titus", dept="Sales")

        result = Employee.findAllByDept("Engineering")
        self.assertEqual(len(result), 2)

        count = Employee.countByDept("Sales")
        self.assertEqual(count, 1)

        self.assertTrue(Employee.existsByName("Titus"))
        self.assertFalse(Employee.existsByName("Nobody"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
