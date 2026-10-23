<p align="center">
  <h1 align="center">🕊️ DoveORM</h1>
  <p align="center">
    <strong>A lightweight, expressive, security-hardened Python ORM — inspired by Django, Spring Boot & ActiveRecord.</strong>
  </p>
  <p align="center">
    <em>Python 3.9+ · Zero dependencies for SQLite · mysqlclient for MySQL · Works with Flask, FastAPI, or standalone</em>
  </p>
</p>

<p align="center">
  <a href="#-quick-start">Quick Start</a> •
  <a href="#%EF%B8%8F-configuration">Configuration</a> •
  <a href="#-defining-models">Models</a> •
  <a href="#-fields-reference">Fields</a> •
  <a href="#-querying">Querying</a> •
  <a href="#-working-with-instances">Instances</a> •
  <a href="#-relationships">Relationships</a> •
  <a href="#-transactions">Transactions</a> •
  <a href="#-migrations">Migrations</a> •
  <a href="#%EF%B8%8F-the-doveorm-command">CLI</a> •
  <a href="#-drivers">Drivers</a> •
  <a href="#-security">Security</a>
</p>

---

## ✨ What is DoveORM?

**DoveORM** is a lightweight yet fully-featured Python ORM designed to feel familiar whether you come from **Django**, **Spring Boot / JPA**, or **Ruby on Rails / ActiveRecord**.

### Highlights

| Feature | |
|---|---|
| **Django-style** `class Meta` and **Spring Boot-style** `@table()` decorator | ✅ |
| **Dynamic finders** — `findByEmail`, `findAllByActive`, `countBy…`, `existsBy…` | ✅ |
| **Chainable, lazy QuerySet** with `Q` objects, result caching and slicing | ✅ |
| **Django-compatible field set** (30+ fields) with validation | ✅ |
| **SQLite + MySQL/MariaDB** behind a pluggable driver interface | ✅ |
| **Thread-safe & fork-safe** connections, **nested transactions** (SAVEPOINTs) | ✅ |
| **Deadlock-retrying transactions** — `db.run_in_transaction(fn)` | ✅ |
| **Django-style migrations** — autodetected, optimized, reversible, squashable | ✅ |
| **`doveorm` command** — `generate`, `apply`, `revert`, `status`, `doctor`, `console`… | ✅ |
| **pip-installable**, standalone — no web framework required | ✅ |
| **Optimistic locking**, **mass-assignment protection**, **sensitive fields** | ✅ |
| **Injection-hardened** — bound parameters, validated identifiers, strict SQL modes | ✅ |
| **Explicit configuration** — the ORM never guesses where your data lives | ✅ |

---

## 🚀 Quick Start

### Installation

DoveORM is a normal pip package (Python 3.9+). Install it straight from the Git repository:

```bash
pip install "doveorm @ git+https://github.com/imaradesk/imara-orm.git"

# with MySQL / MariaDB support (adds mysqlclient)
pip install "doveorm[mysql] @ git+https://github.com/imaradesk/imara-orm.git"

# pin a tag, branch or commit
pip install "doveorm @ git+https://github.com/imaradesk/imara-orm.git@v0.5.0"
```

In a `requirements.txt`:

```text
doveorm[mysql] @ git+https://github.com/imaradesk/imara-orm.git@v0.5.0
```

From a local clone (`-e` for an editable install while you work on DoveORM itself):

```bash
git clone https://github.com/imaradesk/imara-orm.git
pip install -e ./imara-orm
```

Installing gives you the `doveorm` package and the [`doveorm` command](#%EF%B8%8F-the-doveorm-command).
SQLite needs nothing else. On Python 3.9/3.10 pip also installs `tomli`, which the command
uses to read its config file.

### Hello World — SQLite

```python
from doveorm import Model, configure, CharField, IntegerField

configure(DATABASE_PATH="app.db")       # or ":memory:" for a throw-away database

class User(Model):
    name = CharField(max_length=100, nullable=False)
    age  = IntegerField(default=0)

User.create_table()

user = User.create(name="Titus", age=25)
print(user.id)         # 1  (auto-generated primary key)

user.age = 26
user.save()            # UPDATE

adults = User.where(age__gte=18).order_by("name").get()
user.delete()
```

### Hello World — MySQL

```python
import os
from doveorm import Model, configure, CharField, FloatField

configure(
    DATABASE_ENGINE="mysql",
    DATABASE_NAME="myapp",
    DATABASE_HOST="127.0.0.1",
    DATABASE_USER="app",
    DATABASE_PASSWORD=os.environ["DB_PASSWORD"],   # never hard-code secrets
)

class Product(Model):
    name  = CharField(max_length=200)
    price = FloatField(default=0.0)

Product.create_table()
Product.create(name="Widget", price=9.99)
```

---

## ⚙️ Configuration

**Your application owns the configuration; the ORM only consumes it.** The ORM ships
defaults for its own *behaviour* (logging, timeouts, strictness) but never for *where your
data lives*:

- SQLite needs an explicit `DATABASE_PATH` (pass `":memory:"` explicitly for a throw-away DB).
- MySQL needs `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_PASSWORD` (use `""` explicitly for
  passwordless auth) and `DATABASE_HOST` — or `DATABASE_OPTIONS={"unix_socket": ...}`.
- Missing settings raise `ConfigurationError`; nothing silently falls back to `root` or an
  in-memory database, and `get_database()` raises until `configure()` has been called.
- Unknown setting names (typos) raise `ConfigurationError` too.
- Read secrets from the environment or a secret manager in *your* code — see
  `makao-api/settings.py` for an example.

### 1. Keyword arguments (recommended)

```python
import os
from doveorm import configure

# SQLite
db = configure(
    DATABASE_ENGINE="sqlite",      # "sqlite" (default) or "mysql"
    DATABASE_PATH="app.db",        # file path or ":memory:"
    LOG_QUERIES=True,              # log every SQL statement (values redacted)
)

# MySQL
db = configure(
    DATABASE_ENGINE="mysql",
    DATABASE_NAME="myapp",
    DATABASE_HOST="127.0.0.1",
    DATABASE_PORT=3306,
    DATABASE_USER="app",
    DATABASE_PASSWORD=os.environ["DB_PASSWORD"],
    DATABASE_OPTIONS={"ssl_mode": "REQUIRED", "isolation_level": "READ COMMITTED"},
)
```

### 2. Django-style `DATABASES` dict

```python
import os
from doveorm import configure

configure(DATABASES={
    "default": {
        "ENGINE": "mysql",
        "NAME": "myapp",
        "HOST": "127.0.0.1",
        "PORT": 3306,
        "USER": "app",
        "PASSWORD": os.environ["DB_PASSWORD"],
    }
})
```

### 3. Environment variables

```bash
export DOVEORM_DATABASE_ENGINE=mysql
export DOVEORM_DATABASE_NAME=myapp
export DOVEORM_DATABASE_HOST=127.0.0.1
export DOVEORM_DATABASE_USER=app
export DOVEORM_DATABASE_PASSWORD='...'                    # from your secret store
export DOVEORM_DATABASE_OPTIONS='{"ssl_mode": "REQUIRED"}' # dict settings are JSON
```

```python
from doveorm import configure_from_env
db = configure_from_env()          # reads DOVEORM_* variables
```

### 4. From your own settings module

```python
# settings.py  (part of your application)
import os
ORM_SETTINGS = {
    "DATABASE_ENGINE": "sqlite",
    "DATABASE_PATH": os.environ.get("APP_DB_PATH", "production.db"),
    "LOG_QUERIES": False,
}
```

```python
from doveorm import configure
from settings import ORM_SETTINGS
db = configure(**ORM_SETTINGS)
```

`configure_from_file()` still works but is **deprecated**: it executes the file it is given.

### 5. Without global configuration

For tests or several databases in one process, skip `configure()` and bind models directly:

```python
from doveorm import Database

db = Database(":memory:")      # or Database("app.db"), or Database(driver=...)
User.use(db)
User.create_table()
```

### All configuration options

| Setting | Default | Description |
|---|---|---|
| `DATABASE_ENGINE` | `"sqlite"` | `"sqlite"` or `"mysql"` |
| `DATABASE_PATH` | **required (SQLite)** | SQLite file path, or `":memory:"` |
| `DATABASE_NAME` | **required (MySQL)** | Database name (MySQL) / alias for the path (SQLite) |
| `DATABASE_HOST` | **required (MySQL)** | MySQL host — unless `DATABASE_OPTIONS["unix_socket"]` |
| `DATABASE_PORT` | `3306` | MySQL port |
| `DATABASE_USER` | **required (MySQL)** | MySQL user |
| `DATABASE_PASSWORD` | **required (MySQL)** | MySQL password (`""` must be explicit) |
| `DATABASE_CHARSET` | `"utf8mb4"` | MySQL charset |
| `DATABASE_OPTIONS` | `{}` | Extra driver options — see [Drivers](#-drivers) |
| `LOG_QUERIES` | `False` | Log every SQL statement to the `doveorm` logger |
| `LOG_QUERY_PARAMS` | `False` | Also log bound values (may expose secrets / PII) |
| `AUTO_WIRE_MODELS` | `True` | Bind every model to the configured database automatically |
| `USE_TZ` | `False` | `DateTimeField` returns aware UTC datetimes |

---

## 📐 Defining Models

### Style 1: `@table()` decorator (Spring Boot / JPA style)

```python
from doveorm import Model, table, CharField, BooleanField, HasMany

@table(
    name="users",                          # explicit table name
    indexes=[("username",), ("email",)],   # (composite) indexes
    unique_together=[("email",)],          # unique constraints
    ordering=["-id"],                      # default ORDER BY for every query
    engine="InnoDB",                       # MySQL engine
    charset="utf8mb4",                     # MySQL charset
)
class User(Model):
    username = CharField(max_length=80, nullable=False, unique=True)
    email    = CharField(max_length=120, nullable=False, unique=True)
    active   = BooleanField(default=True)

    posts = HasMany("Post", foreign_key="author_id")
```

### Style 2: `class Meta` (Django style)

```python
from doveorm import Model, CharField, TextField, BooleanField, ForeignKey, DateTimeField

class Post(Model):
    title      = CharField(max_length=200, nullable=False)
    body       = TextField(nullable=False)
    published  = BooleanField(default=False)
    author_id  = ForeignKey("User", on_delete="CASCADE", nullable=False)
    created_at = DateTimeField(auto_now_add=True, nullable=True)

    class Meta:
        table_name = "posts"
        indexes = [("author_id",), ("published",)]
        ordering = ["-created_at"]
```

If both are present, `@table()` values win. Supported options: `table_name` / `name`,
`indexes`, `unique_together`, `ordering`, `engine`, `charset`, `version_field`, and
`abstract` (Meta only).

### Style 3: Minimal

```python
class Tag(Model):
    name = CharField(max_length=50, unique=True)

# Table name: "tags" (pluralized snake_case)
# Primary key: id = AutoField() is injected automatically
```

### Abstract base models

```python
class TimeStamped(Model):
    created_at = DateTimeField(auto_now_add=True, nullable=True)
    updated_at = DateTimeField(auto_now=True, nullable=True)

    class Meta:
        abstract = True        # no table, cannot be instantiated or queried

class Article(TimeStamped):
    title = CharField(max_length=200)
```

### UUID primary keys

```python
import uuid
from doveorm import Model, UUIDField, CharField

class Ticket(Model):
    id    = UUIDField(primary_key=True, default=uuid.uuid4)   # pass the callable
    title = CharField(max_length=200)

Ticket.create_table()
ticket = Ticket.create(title="Bug report")
print(ticket.id)  # UUID('a1b2c3d4-...')
```

### Table name rules

| Class name | Auto table name |
|---|---|
| `User` | `users` |
| `Category` | `categories` |
| `Address` | `addresses` |
| `BlogPost` | `blog_posts` |

### Naming rules (enforced)

- Table, column, index and join-table names must be plain identifiers (`[A-Za-z_][A-Za-z0-9_]*`, ≤ 64 chars).
- Field names may not start with `_` and may not shadow the Model API (`save`, `delete`, `where`, `to_dict`, …).
- A model may declare at most one primary key.

---

## 📋 Fields Reference

### Auto-increment primary keys

| Field | SQLite | MySQL |
|---|---|---|
| `AutoField()` | `INTEGER PRIMARY KEY AUTOINCREMENT` | `INT PRIMARY KEY AUTO_INCREMENT` |
| `BigAutoField()` | `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGINT PRIMARY KEY AUTO_INCREMENT` |
| `SmallAutoField()` | `INTEGER PRIMARY KEY AUTOINCREMENT` | `SMALLINT PRIMARY KEY AUTO_INCREMENT` |

An explicit value (`User(id=99)`) is inserted as given.

### Numeric fields

| Field | SQLite | MySQL | Validation |
|---|---|---|---|
| `IntegerField()` | `INTEGER` | `INT` | 32-bit range; `bool` rejected |
| `BigIntegerField()` | `INTEGER` | `BIGINT` | 64-bit range |
| `SmallIntegerField()` | `INTEGER` | `SMALLINT` | 16-bit range |
| `PositiveIntegerField()` / `PositiveSmallIntegerField()` / `PositiveBigIntegerField()` | `INTEGER` | `INT` / `SMALLINT` / `BIGINT` | ≥ 0 |
| `FloatField()` | `REAL` | `DOUBLE` | finite numbers only |
| `DecimalField(max_digits=10, decimal_places=2)` | `TEXT` | `DECIMAL(m,d)` | digits & places enforced |

`DecimalField` values are stored exactly; on SQLite, range lookups and ordering compare numerically.

### String / text fields

| Field | SQLite | MySQL | Validation |
|---|---|---|---|
| `CharField(max_length=255)` | `TEXT` | `VARCHAR(n)` | `str`, length |
| `TextField()` | `TEXT` | `LONGTEXT` | `str` |
| `SlugField(max_length=50)` | `TEXT` | `VARCHAR(n)` | `[-a-zA-Z0-9_]+`, indexed |
| `EmailField(max_length=254)` | `TEXT` | `VARCHAR(n)` | email format |
| `URLField(max_length=200)` | `TEXT` | `VARCHAR(n)` | `http(s)://` URL |
| `IPAddressField()` | `TEXT` | `VARCHAR(15)` | IPv4 |
| `GenericIPAddressField(protocol="both")` | `TEXT` | `VARCHAR(45)` | IPv4 / IPv6 |

### Boolean field

| Field | SQLite | MySQL | Notes |
|---|---|---|---|
| `BooleanField(default=True)` | `INTEGER` (0/1) | `TINYINT(1)` | Accepts `bool` (or `0`/`1`); strings are rejected |

### Date / time fields

| Field | SQLite | MySQL | Notes |
|---|---|---|---|
| `DateField()` | `TEXT` | `DATE` | `datetime.date` |
| `DateTimeField(use_tz=None)` | `TEXT` | `DATETIME(6)` | microseconds kept; aware values stored as UTC |
| `TimeField()` | `TEXT` | `TIME(6)` | `datetime.time` |
| `DurationField()` | `INTEGER` | `BIGINT` | `datetime.timedelta`, exact microseconds |

`DateField`, `DateTimeField` and `TimeField` support `auto_now=True` (set on every save) and
`auto_now_add=True` (set on insert when empty). With `use_tz=True` (or `USE_TZ=True`),
`DateTimeField` returns aware UTC datetimes.

### UUID, JSON and binary fields

| Field | SQLite | MySQL | Notes |
|---|---|---|---|
| `UUIDField(default=uuid.uuid4)` | `TEXT` | `CHAR(32)` | Stored as 32-char hex; returns `uuid.UUID` |
| `JSONField(default=dict)` | `TEXT` | `JSON` | Any JSON-serialisable value; mutable defaults are copied per instance |
| `BinaryField(max_length=None)` | `BLOB` | `LONGBLOB` | `bytes` |

### File / path fields

| Field | SQLite | MySQL | Validation |
|---|---|---|---|
| `FileField(upload_to="uploads/")` | `TEXT` | `VARCHAR(255)` | no NUL bytes, no `..` segments |
| `ImageField(upload_to="avatars/", width_field=…, height_field=…)` | `TEXT` | `VARCHAR(255)` | as `FileField` |
| `FilePathField(path="/etc/app", match=r"\.conf$", recursive=False)` | `TEXT` | `VARCHAR(100)` | must live inside `path`; name must match `match` |

These fields store paths only — file storage is up to your application.

### Relationship fields

| Field | SQL type | Notes |
|---|---|---|
| `ForeignKey("User", on_delete="CASCADE")` | type of the target's PK | Accepts a model instance or a key |
| `OneToOneField("User", on_delete="CASCADE")` | type of the target's PK + `UNIQUE` | |

`on_delete` must be one of `CASCADE`, `SET NULL` (requires `nullable=True`), `SET DEFAULT`,
`RESTRICT`, `NO ACTION`.

### Common field options

```python
CharField(
    primary_key=False,       # make this the PK
    nullable=True,           # allow NULL
    default="hello",         # default value, or a callable such as uuid.uuid4
    unique=False,            # UNIQUE constraint
    db_index=False,          # create an index on this column
    choices=[                # restrict allowed values
        ("draft", "Draft"),
        ("published", "Published"),
    ],
    sensitive=False,         # hide from to_dict() and error messages (passwords, tokens)
    editable=True,           # may be set via Model.assign() / Model.from_input()
    verbose_name="Title",
    help_text="Enter title",
)
```

---

## 🔍 Querying

### Basic CRUD

```python
user = User.create(name="Titus", age=25)     # INSERT
user = User.find(1)                          # by primary key (None if missing)
users = User.all().get()                     # all rows
users = User.where(active=True).get()        # filtered
user.name = "Titus M."; user.save()          # UPDATE
user.delete()                                # DELETE
```

### Chainable QuerySet

```python
# Nothing hits the database until the QuerySet is evaluated
users = (
    User.where(active=True)
        .where(age__gte=18)
        .exclude(name="admin")
        .order_by("-created_at", "name")
        .limit(10)
        .offset(20)
        .select("name", "email")     # partial load (PK always included)
        .get()
)
```

QuerySets are evaluated by `.get()`, iteration, `len()`, `bool()` or indexing; the result is
cached, so `if qs: for u in qs: ...` runs one query. `.get()` always re-runs the query.
Slicing works too: `User.all().order_by("id")[10:20]`.

### Lookups (`field__lookup=value`)

```python
User.where(age=25)                    # age = 25          (also age__eq / age__exact)
User.where(age__ne=25)                # age != 25
User.where(age__lt=30)                # also lte, gt, gte
User.where(age__range=(18, 30))       # BETWEEN 18 AND 30
User.where(id__in=[1, 2, 3])          # IN (...)   — an empty list matches nothing
User.where(id__not_in=[4, 5])         # NOT IN (...)
User.where(email__is_null=True)       # IS NULL    (also isnull; value must be a bool)
User.where(email=None)                # IS NULL
User.where(name__contains="itu")      # LIKE '%itu%'  — wildcards in the value are escaped
User.where(name__icontains="ITU")     # case-insensitive
User.where(name__startswith="Ti")     # also istartswith, endswith, iendswith
User.where(name__iexact="titus")      # case-insensitive equality
User.where(name__like="T%s")          # raw LIKE pattern (you control the wildcards)
```

Unknown fields or lookups (e.g. `age__gtee=18`) raise `ValueError` instead of silently
matching the wrong rows. `contains` / `startswith` / `endswith` follow the backend's
collation for case sensitivity; the `i*` variants are always case-insensitive.

### Q objects — OR / AND / NOT

```python
from doveorm import Q

User.filter(Q(role="admin") | Q(role="editor")).get()
User.filter(Q(active=True) & (Q(role="admin") | Q(age__gte=21))).get()
User.filter(~Q(banned=True)).get()
User.where(active=True).filter(Q(age__gte=18) | Q(name="admin")).get()
```

### Terminal methods

```python
qs = User.where(active=True)
qs.get()        # → list[User]
qs.first()      # → User | None   (ordered by PK if no ordering is set)
qs.last()       # → User | None   (reverse of the current ordering)
qs.count()      # → int           (respects limit / offset)
qs.exists()     # → bool          (SELECT 1 … LIMIT 1)
qs.update(active=False)   # → rows matched; values are validated
qs.delete()               # → rows deleted
```

**Bulk-write safety:** `update()` / `delete()` without a filter raise `UnsafeQueryError`
unless the QuerySet came from `Model.all()` (`User.all().delete()`), and bulk writes combined
with `limit()` / `offset()` are refused rather than silently ignoring the limit.

### `get_or_create` and `bulk_create`

```python
user, created = User.get_or_create(
    username="titus",
    defaults={"email": "titus@example.com"},
)   # race-safe when a UNIQUE constraint covers the lookup

users = User.bulk_create([User(username=f"u{i}") for i in range(100)])   # one transaction
```

### Dynamic finders (Spring Boot / JPA style) 🔥

```python
user  = User.findByUsername("titus")                  # first match or None
posts = Post.findAllByAuthorId(1)                      # list
user  = User.findByUsernameAndActive("titus", True)    # compound with And
count = User.countByActive(True)                       # int
found = Tag.existsByName("python")                     # bool
```

CamelCase is converted to snake_case: `findByAuthorId` → `author_id`.

### Eager loading

```python
users = User.all().prefetch_related("posts").get()     # 2 queries total
for user in users:
    print(user.posts)                                  # no extra query
```

Works for `HasMany`, `HasOne`, `BelongsTo` and `ManyToMany`; large ID lists are batched in
chunks of 500. Unknown relationship names raise `ValueError`.

### Raw SQL

```python
from doveorm import get_database

rows = get_database().raw(
    "SELECT u.id, COUNT(p.id) AS post_count "
    "FROM users u LEFT JOIN posts p ON p.author_id = u.id "
    "WHERE u.active = ? GROUP BY u.id",
    [True],                       # always pass user input as parameters
)
```

Write `?` placeholders on every backend — they are translated for MySQL (`?` inside string
literals is left alone).

---

## 🧱 Working with Instances

### Validation and the `clean()` hook

Every field is validated on `save()` (types, ranges, lengths, formats, choices). Override
`clean()` for rules spanning several fields:

```python
from doveorm import ValidationError

class Booking(Model):
    start = IntegerField()
    end   = IntegerField()

    def clean(self):
        if self.start > self.end:
            raise ValidationError("start must be <= end")
```

`full_clean()` runs the same checks without saving.

### Safe input from requests (mass assignment)

Never pass a request body straight to `create(**data)`. Use `from_input()` / `assign()`:

```python
user = User.from_input(request.json)                    # new, unsaved instance
user.save()

user.assign(request.json, allowed=["name", "email"])    # explicit whitelist
user.save()
```

Only `editable` fields may be assigned — never the primary key, the version field,
`auto_now` / `auto_now_add` fields, or fields declared `editable=False`. Other keys raise
`ValidationError` (or are skipped with `ignore_unknown=True`).

### Sensitive fields

```python
class Account(Model):
    email    = EmailField()
    password = CharField(max_length=255, sensitive=True)

account.to_dict()                         # no "password" key
account.to_dict(include_sensitive=True)   # explicit opt-in
account.to_dict(fields=["email"])         # pick fields
```

Sensitive values are also shown as `<redacted>` in validation error messages.

### Partial updates

```python
user.save(update_fields=["email"])        # UPDATE only the email column
```

`select()`-loaded instances only write back the fields that were loaded or assigned, so a
partial load never overwrites other columns with defaults.

### Optimistic locking

```python
class Account(Model):
    balance = IntegerField(default=0)
    version = IntegerField(nullable=True)

    class Meta:
        version_field = "version"        # or @table(version_field="version")

a = Account.find(1); b = Account.find(1)
a.balance = 10; a.save()                 # version 1 → 2
b.balance = 20; b.save()                 # raises ConcurrencyError — b is stale
```

Stale `delete()` calls raise `ConcurrencyError` too, and bulk `update()` bumps the version.

### Strict attributes

Assigning to something that is not a field raises `AttributeError`, so typos such as
`user.emial = "x"` fail loudly. Relationship attributes are read-only — set the foreign-key
field instead.

### Other helpers

```python
user.refresh()            # reload from the database (DoesNotExist if it was deleted)
user.to_dict()            # plain dict of field values
user == other             # same class and same primary key
```

Unsaved instances are only equal to themselves and are unhashable.

---

## 🔗 Relationships

### HasMany (one-to-many)

```python
class User(Model):
    name  = CharField(max_length=100)
    posts = HasMany("Post", foreign_key="author_id")

class Post(Model):
    title     = CharField(max_length=200)
    author_id = ForeignKey("User", on_delete="CASCADE")

User.find(1).posts        # → [Post, ...] (queried on each access; use prefetch_related)
```

### HasOne (one-to-one)

```python
class User(Model):
    profile = HasOne("Profile", foreign_key="user_id")

class Profile(Model):
    bio     = TextField()
    user_id = OneToOneField("User", on_delete="CASCADE")

User.find(1).profile      # → Profile | None
```

### BelongsTo (inverse)

```python
class Post(Model):
    author_id = ForeignKey("User", on_delete="CASCADE")
    author    = BelongsTo("User", foreign_key="author_id")

post = Post.find(1)
post.author               # → User (cached until author_id changes)
```

### ManyToMany

```python
class Post(Model):
    title = CharField(max_length=200)
    tags  = ManyToMany("Tag", through="post_tags", source_fk="post_id", target_fk="tag_id")

class Tag(Model):
    name = CharField(max_length=50, unique=True)

get_database().execute_script("""
    CREATE TABLE IF NOT EXISTS post_tags (
        post_id INTEGER NOT NULL,
        tag_id  INTEGER NOT NULL,
        PRIMARY KEY (post_id, tag_id)
    );
""")

post.tags.add(tag1, tag2)   # one batched INSERT; duplicates ignored
post.tags.remove(tag1)
post.tags.clear()
len(post.tags)              # loads lazily on first use
```

---

## 🧪 Transactions

```python
from doveorm import get_database

db = get_database()

with db.transaction():                   # also available as db.atomic()
    User.create(name="Alice")
    Post.create(title="Hello", author_id=1)
    # committed on success, rolled back on exception

with db.transaction():                   # nested → SAVEPOINTs
    User.create(name="Bob")
    try:
        with db.transaction():
            User.create(name="Charlie")
            raise ValueError("oops")     # only the inner block is rolled back
    except ValueError:
        pass
```

### Retrying deadlocks and lock timeouts

```python
def transfer():
    src = Account.find(1); dst = Account.find(2)
    src.balance -= 10; dst.balance += 10
    src.save(); dst.save()

db.run_in_transaction(transfer, retries=3)
```

MySQL deadlocks (1213), lock-wait timeouts (1205) and SQLite "database is locked" errors roll
back and re-run the function with exponential backoff. The function may run more than once,
so keep side effects outside the database out of it.

`db.disconnect()` refuses to run while a transaction is open (`force=True` overrides).

---

## 🔄 Migrations

Django-style migrations: you change your models, `doveorm generate` writes a migration file
describing the change, and `doveorm apply` applies it. Files live in your project and go in
version control. Applied migrations are recorded in the `migrations` table.

### Workflow

```bash
doveorm init -m myapp.models -s myapp.settings:DOVEORM   # once: writes doveorm.toml
doveorm generate                 # detect model changes → migrations/0001_initial.py
doveorm apply                    # apply everything pending
doveorm history                  # [x] applied  [ ] pending
doveorm sql 0002                 # print the SQL, run nothing
doveorm apply 0001               # move back to 0001 (unapplies 0002+)
doveorm revert 2                 # unapply the last two
doveorm revert --all             # unapply everything
doveorm apply --dry-run          # show what would run
doveorm verify                   # CI: exit 1 if a model change has no migration
doveorm squash 0007              # fold 0001…0007 into one optimized migration
```

Migration targets accept a unique prefix (`0003`) instead of the full name. See
[the `doveorm` command](#%EF%B8%8F-the-doveorm-command) for setup and every option.

### What a migration file looks like

```python
"""Generated by DoveORM 0.5.0 on 2026-10-23 07:00 UTC."""
from doveorm import fields, migrations


class Migration(migrations.Migration):

    dependencies = [
        '0001_initial',
    ]

    operations = [
        migrations.AddField(
            model_name='User',
            name='age',
            field=fields.IntegerField(nullable=False, default=0),
        ),
        migrations.AlterIndexes(
            name='Post',
            indexes=[('title',)],
        ),
    ]
```

Files are plain Python. Review them, edit them, and add operations by hand.

### Operations

| Operation | Does |
|---|---|
| `CreateModel(name, fields, table=, options=)` / `DeleteModel(name)` | create / drop a table |
| `RenameModel(old_name, new_name)` | rename the model (FKs follow; table unchanged) |
| `AlterModelTable(name, table)` | rename the table |
| `AddField` / `RemoveField` / `AlterField` / `RenameField` | column changes (`AlterField` covers type, null, default, unique, index, FK) |
| `AlterIndexes(name, indexes)` / `AlterUniqueTogether(name, unique_together)` | composite indexes / constraints |
| `AlterTableOptions(name, engine=, charset=)` | MySQL engine / charset |
| `RunSQL(sql, reverse_sql=None, state_operations=())` | raw SQL (reversible when `reverse_sql` is given) |
| `RunPython(code, reverse_code=None)` | data migrations; `code(db)` gets the `Database` |

```python
def fill_slugs(db):
    for row in db.execute("SELECT id, title FROM posts WHERE slug IS NULL"):
        db.execute_write("UPDATE posts SET slug = ? WHERE id = ?", [slugify(row["title"]), row["id"]])

operations = [
    migrations.AddField("Post", "slug", fields.SlugField(nullable=True)),
    migrations.RunPython(fill_slugs, reverse_code=migrations.RunPython.noop),
]
```

> In `RunPython`, query tables with SQL (or the query API on tables as they are *at that
> point*). Importing your current model classes in a migration ties it to today's schema.

### How it detects changes

- The schema the migration files describe is rebuilt **in memory** by replaying their
  operations, and compared with your models. There is no snapshot file to drift, and
  `doveorm generate` does not need a database connection.
- Only arguments that affect the database count (`max_length`, `nullable`, `default`,
  `unique`, `db_index`, `max_digits`, FK target/`on_delete`…). Changing `help_text`,
  `choices`, `sensitive`, `verbose_name`, `upload_to`… never produces a migration.
- **Renames** are never guessed. They show up as remove + add unless you confirm them when
  prompted, or pass `--rename User.handle=nick` (`renames={"User.handle": "nick"}` in Python).
- **NOT NULL columns without a default** need a value for existing rows. You are asked, or
  you pass `--default User.age=0` (`defaults={"User.age": 0}` in Python). The value fills existing rows (`backfill`) and does
  not become the column default. The same applies when a column becomes NOT NULL: existing
  NULLs are replaced.
- Operations are ordered safely: models are created in foreign-key order (cycles are
  broken with a later `AddField`), indexes go before the columns they use, and
  referencing models are dropped first.
- Every detected change list is **verified**: replaying it must reproduce your models
  exactly, or `doveorm generate` fails instead of writing a wrong file.

### Optimized

- **Optimizer**: `CreateModel` + later `AddField`/`AlterField`/`RenameField` collapse into
  one `CreateModel`; add-then-remove disappears; repeated alters collapse. Operations are
  only merged across operations that are independent of them, never across
  `RunSQL`/`RunPython`.
- **Squashing**: `doveorm squash` rewrites a long history as a few operations and records
  which files it `replaces`. Databases that already applied the old files keep working;
  new databases run only the squash. Delete the old files once every database has
  migrated past them.
- **Fast runs**: the history table is read once; the in-memory schema is built in one pass
  with copy-on-write states; migration files are cached by content hash.

### Safety

| | |
|---|---|
| **Atomic (SQLite)** | each migration and its history row commit together; a failure leaves nothing behind |
| **Table rebuilds (SQLite)** | changes SQLite can't `ALTER` use the documented copy-and-swap rebuild, with foreign-key enforcement suspended so `ON DELETE CASCADE` can't fire, then `PRAGMA foreign_key_check` before commit |
| **MySQL** | DDL commits implicitly, so a migration is not atomic there: keep one logical change per migration. The history row is only written on success. FK/unique names are looked up in `information_schema`, so tables created by `create_table()` migrate too. Changing a PK's type also updates the FK columns that point to it |
| **Concurrent deploys** | MySQL takes a named lock (`GET_LOCK`); on SQLite the UNIQUE history row stops double-application |
| **Irreversible steps** | rolling back a migration with `RunSQL`/`RunPython` that has no reverse is refused before anything runs |
| **Consistency** | an applied migration whose dependency is not applied, a dependency cycle, or two "latest" migrations (join them with `doveorm merge`) all stop `doveorm apply` |
| **Existing databases** | `doveorm apply --adopt` records an initial migration without running it when all its tables and columns already exist |

### Python API

Everything the CLI does is available as functions:

```python
from doveorm import migrations

migrations.makemigrations("migrations", models=[User, Post], defaults={"User.age": 0})
migrations.migrate("migrations")                 # uses the configured database
migrations.migrate("migrations", "0002", db=db)  # or an explicit one
migrations.showmigrations("migrations")          # [("0001_initial", True), …]
migrations.sqlmigrate("migrations", "0002")
migrations.rollback("migrations", steps=1)
```

### Legacy `Migrator`

The function-based `Migrator` still works and shares the same history table:

```python
from doveorm import Migrator, get_database

migrator = Migrator(get_database())
migrator.register("0001_create_users", User.create_table, rollback_fn=User.drop_table)
migrator.migrate()
migrator.rollback(steps=1)
```

Old migration files that define `up()` / `down()` instead of a `Migration` class are loaded
as `RunPython` steps, each depending on the file before it. To move such a project over, run
`doveorm generate` (it writes an `initial` migration after the old files), then
`doveorm apply --adopt`.

---

## 🛠️ The `doveorm` command

Project management lives in the ORM, not in a web framework: the same `doveorm` command
works for a Flask app, a FastAPI service, a worker or a plain script. It is installed with
the package (`python -m doveorm` works too).

### Project setup

Run `doveorm init` once in your project root. It writes `doveorm.toml` and creates the
migrations folder:

```toml
# doveorm.toml
models     = ["myapp.models"]          # modules that define your models
migrations = "migrations"              # relative to this file
settings   = "myapp.settings:DOVEORM"  # optional (see below)
```

Prefer one config file? `doveorm init --pyproject` puts the same keys under
`[tool.doveorm]` in your `pyproject.toml`. The command looks for either file in the
current folder and its parents, so it works from anywhere inside the project.

Everything the command creates goes into **your project**, never into the installed
`doveorm` package: migration files go to the `migrations` folder next to `doveorm.toml`. Commands
run from that folder, so a relative `DATABASE_PATH = "app.db"` is the same file whichever
subfolder you start in.

`settings` points at your database settings, which stay in your own code:

| `settings =` | Meaning |
|---|---|
| `"myapp.settings:DOVEORM"` | a dict of settings → `configure(**DOVEORM)` |
| `"myapp.settings"` | a module; its `DATABASE_*` / `LOG_*`… variables are used |
| `"myapp.boot:setup"` | a function that returns such a dict, or calls `doveorm.configure()` itself |
| *(not set)* | `DOVEORM_*` environment variables (`DOVEORM_DATABASE_PATH=app.db` …) |

Flags override the file for one run: `--settings`, `--models` (repeatable or
comma-separated), `--migrations`, `--config path/to/doveorm.toml`. Global flags work before
or after the command name.

### Commands

| Command | What it does |
|---|---|
| `doveorm init` | create `doveorm.toml` (or `[tool.doveorm]` with `--pyproject`) and the migrations folder |
| `doveorm generate` | write the next migration from model changes (`--name`, `--empty`, `--dry-run`, `--no-input`, `--default Model.field=VALUE`, `--rename New=Old`) |
| `doveorm apply [TARGET]` | apply pending migrations, or move forwards/backwards to `TARGET` (`--dry-run`, `--fake`, `--adopt`) |
| `doveorm revert [N]` | unapply the last `N` migrations (default 1; `--all`, `--dry-run`, `--fake`) |
| `doveorm history` | every migration, `[x]` applied / `[ ]` pending (`--pending`) |
| `doveorm sql NAME` | print the SQL a migration runs (`--backwards` for the undo SQL) |
| `doveorm squash [START] END` | combine a range of migrations into one optimized migration |
| `doveorm merge` | join two migration branches (e.g. after merging Git branches) |
| `doveorm verify` | CI gate: exit 1 if a model change has no migration (`--database`: also pending migrations and schema drift) |
| `doveorm status` | connection, server version, size, migration state |
| `doveorm doctor` | health checks; exit 1 on a failure (`--strict`: also on warnings) |
| `doveorm models` | each model, its table, field count and whether the table matches |
| `doveorm console` | Python shell with `doveorm`, `db` and every model loaded (IPython if installed; `--plain`) |
| `doveorm connect` | open the database's own client (`sqlite3` / `mysql`) with the configured credentials |

`-v` / `--verbose` also logs the SQL each migration runs; `doveorm --version` prints the
version; `doveorm COMMAND --help` lists every option.

### Database status and health checks

```console
$ doveorm status
Database
  Engine         sqlite
  Location       app.db
  Server         SQLite 3.45.1
  Connection     ok (1.5 ms)
  Journal mode   wal
  Foreign keys   on
  Size           24.0 KB
  Tables         3
  Query logging  off

Migrations
  Folder   migrations
  Applied  1
  Latest   0001_initial
  Pending  none
```

```console
$ doveorm doctor
  [ok  ] connection            SQLite 3.45.1 at app.db (1.4 ms)
  [ok  ] migration history     1 applied
  [ok  ] pending migrations    none
  [ok  ] models vs migrations  every model change has a migration
  [ok  ] database schema       every model table and column exists
  [ok  ] integrity             PRAGMA quick_check passed
  [ok  ] foreign keys          no dangling references
  [ok  ] journal mode          wal
  [ok  ] file permissions      0o600

9 checks: 0 failed, 0 warning(s).
```

`doctor` checks:

- **Everywhere:** connection, migration history consistency (conflicting branches,
  applied migrations whose file is gone), pending migrations, model changes without a
  migration, and missing tables or columns.
- **SQLite:** `PRAGMA quick_check`, dangling foreign keys, WAL journal mode, and database
  file permissions.
- **MySQL:** strict `sql_mode`, `utf8mb4` charset, and TLS on non-local connections.
- **Settings:** warns when `LOG_QUERY_PARAMS` is on.

`status`, `doctor`, `models` and `verify` only read; they never change data or schema.
`quick_check` and the foreign-key check read whole tables, so `doctor` can take a while on a
very large SQLite file.

### In CI and deployments

```bash
# CI: fail the build if someone changed a model but forgot the migration
doveorm verify

# deploy: migrate, then make sure the database matches the code
doveorm apply && doveorm verify --database
```

### From Python

```python
from doveorm.cli import main
main(["apply"])                  # same as `doveorm apply`; returns the exit code
```

`doveorm.cli.health` has the checks as functions (`database_info`, `migration_info`,
`schema_drift`, `run_checks`) if you want them in your own health endpoint.

> **Security:** the command imports the `settings` and `models` modules you list, just like
> starting your app does, so only run it in projects you trust. `status` and `doctor` never
> print the password. `connect` passes the MySQL password through a private temporary option
> file (mode 0600, deleted afterwards), never on the command line where other users could see
> it in the process list.

---

## 🔌 Drivers

DoveORM talks to the database through a pluggable `DatabaseDriver` interface.

| Driver | Backend | Library | Install |
|---|---|---|---|
| `SQLiteDriver` | SQLite 3 | `sqlite3` (stdlib) | built-in |
| `MySQLDriver` | MySQL 5.7+ / MariaDB | `mysqlclient` | `pip install mysqlclient` |

Both drivers use one connection per thread, nested transactions via SAVEPOINTs, and are
**fork-safe**: a child process never reuses its parent's connection.

### SQLite

```python
from doveorm import Database
from doveorm.drivers import SQLiteDriver

db = Database(driver=SQLiteDriver(
    "app.db",
    timeout=5.0,               # wait for locks (seconds)
    statement_timeout=2.0,     # abort runaway statements → QueryTimeout
    begin_mode="IMMEDIATE",    # DEFERRED | IMMEDIATE | EXCLUSIVE
    pragmas={"synchronous": "NORMAL"},
    file_mode=0o600,           # permissions for a newly created DB file
    max_lifetime=None,         # recycle connections older than N seconds
))
```

- Autocommit mode with explicit `BEGIN`/`COMMIT`, so transaction boundaries are exactly yours.
- WAL journal, `foreign_keys = ON`, `trusted_schema = OFF`.
- `":memory:"` databases are shared by all threads of the process (not across `fork()`).

### MySQL / MariaDB

```python
import os
from doveorm import MySQLDatabase

db = MySQLDatabase(
    host="db.internal", user="app", password=os.environ["DB_PASSWORD"], database="myapp",
    ssl_mode="REQUIRED",               # passed through to mysqlclient
    isolation_level="READ COMMITTED",  # validated against the 4 SQL levels
    sql_mode=None,                     # keep the server's sql_mode (default: strict)
    max_lifetime=3600,                 # recycle connections hourly
    connect_timeout=10,
)
```

- `autocommit=True` outside transactions, so reads always see committed data.
- Every session runs with a strict `sql_mode` by default: invalid or truncated data raises instead of being silently altered.
- `CLIENT.FOUND_ROWS` makes UPDATE return rows *matched*, consistent with SQLite.
- Lost connections (2006/2013) are re-established and the statement retried once — never inside a transaction, and writes only when the server never received them.
- Logs a warning for remote connections without TLS and for empty passwords.
- `InnoDB` / `utf8mb4` tables by default.

### Writing a custom driver

Subclass `DatabaseDriver` and implement the primitive hooks; connection handling,
transactions, retries and fork safety are inherited:

```python
from doveorm import Database
from doveorm.drivers.base import DatabaseDriver

class PostgreSQLDriver(DatabaseDriver):
    name = "postgresql"
    placeholder = "%s"

    def _open(self): ...                          # return a new DB-API connection
    def _exec_control(self, conn, sql): ...       # BEGIN / SAVEPOINT / RELEASE …
    def _begin(self, conn): ...
    def _commit(self, conn): ...
    def _rollback(self, conn): ...
    def execute(self, sql, params=None): ...      # SELECT → rows with row["col"], row[0], row.keys()
    def execute_write(self, sql, params=None): ...
    def execute_many(self, sql, seq_of_params): ...
    def _execute_statement(self, sql): ...        # one parameterless statement (DDL)
    def table_exists(self, table_name): ...
    def index_exists(self, table_name, index): ...

db = Database(driver=PostgreSQLDriver(...))
```

---

## 🔐 Security

DoveORM is hardened by default:

| Area | Protection |
|---|---|
| SQL injection | All values are bound parameters; `limit` / `offset` must be integers; table, column, index, join-table, engine and charset names are validated identifiers; `on_delete` is whitelisted; DDL `DEFAULT` literals are escaped |
| Destructive queries | Unfiltered bulk `update()` / `delete()` require `Model.all()`; bulk writes with `limit()` are refused |
| Mass assignment | `from_input()` / `assign()` only set editable, whitelisted fields |
| Data exposure | `sensitive=True` fields are excluded from `to_dict()` and redacted in errors; query logging redacts parameters by default |
| Credentials | No default users or passwords; drivers and databases cannot be pickled or copied; `repr()` never shows passwords; TLS warning for remote MySQL |
| Data integrity | Strict MySQL `sql_mode`, typed field validation, optimistic locking, atomic migrations |
| Files | SQLite files created `0600`; `FileField` rejects `..` and NUL; `FilePathField` confines paths |
| Runtime | Fork-safe connections, connection recycling, SQLite statement timeouts |

### Exceptions

All ORM errors derive from `ORMError` (and from the matching built-in where it makes sense):

| Exception | Raised when |
|---|---|
| `ConfigurationError` (`ValueError`) | settings are missing, unknown or invalid |
| `ValidationError` (`ValueError`) | a field value or `clean()` check fails |
| `DoesNotExist` (`RuntimeError`) | saving / refreshing a row that no longer exists |
| `ConcurrencyError` | an optimistic-locking conflict is detected |
| `UnsafeQueryError` | a dangerous bulk write is refused |
| `TransactionError` | e.g. disconnecting inside a transaction |
| `QueryTimeout` (`TimeoutError`) | a statement exceeds `statement_timeout` |
| `MigrationError` | a migration or rollback fails |

### Logging

Everything is logged to the `doveorm` logger (migrations to `doveorm.migrations`; executed migration SQL at DEBUG):

```python
import logging
logging.getLogger("doveorm").setLevel(logging.DEBUG)    # with LOG_QUERIES=True
```

---

## 🌐 Flask Integration

```python
import os
from flask import Flask, abort, jsonify, request
from doveorm import configure, Model, CharField, BooleanField, table

configure(DATABASE_PATH=os.environ.get("TODO_DB", "todos.db"))

@table(name="todos")
class Todo(Model):
    title = CharField(max_length=200, nullable=False)
    done  = BooleanField(default=False)

Todo.create_table()                 # or `doveorm apply` at deploy time

app = Flask(__name__)

@app.get("/todos")
def list_todos():
    return jsonify([t.to_dict() for t in Todo.all()])

@app.post("/todos")
def create_todo():
    todo = Todo.from_input(request.get_json(), allowed=["title"]).save()
    return jsonify(todo.to_dict()), 201

@app.put("/todos/<int:todo_id>")
def update_todo(todo_id):
    todo = Todo.find(todo_id) or abort(404)
    todo.assign(request.get_json(), allowed=["title", "done"]).save()
    return jsonify(todo.to_dict())
```

---

## 📁 Project Structure

```
pyproject.toml             # packaging: pip install, the `doveorm` command, extras
doveorm/
├── __init__.py            # Public API — import everything from here
├── __main__.py            # python -m doveorm
├── exceptions.py          # ORMError and subclasses
├── database.py            # Database façade (driver-agnostic)
├── cli/
│   ├── main.py            # the `doveorm` command: parser and commands
│   ├── project.py         # finds doveorm.toml / [tool.doveorm], loads settings and models
│   └── health.py          # status and doctor checks (read-only)
├── migrations/
│   ├── __init__.py        # public API (Migration, operations, makemigrations, migrate…)
│   ├── cli.py             # older embeddable CLI (makemigrations / migrate …), still supported
│   ├── commands.py        # the migration commands as Python functions
│   ├── state.py           # in-memory schema (ModelState / ProjectState)
│   ├── autodetector.py    # models vs. history → operations (ordered + verified)
│   ├── optimizer.py       # merges redundant operations
│   ├── questioner.py      # renames / NOT NULL backfills (interactive or explicit)
│   ├── operations/        # CreateModel, AddField, AlterField, RunSQL, RunPython, …
│   ├── backends/          # schema editors: SQLite (table rebuilds), MySQL
│   ├── loader.py          # reads migration files (new + legacy style)
│   ├── graph.py           # dependency graph
│   ├── executor.py        # plans and runs migrations
│   ├── recorder.py        # the `migrations` history table
│   ├── writer.py          # renders migration files
│   └── legacy.py          # function-based Migrator
├── config/
│   ├── __init__.py        # configure(), get_database(), get_setting(), reset()
│   ├── defaults.py        # behaviour defaults (no connection values)
│   ├── loaders.py         # environment / settings-file loaders
│   └── builders.py        # settings → Database (validates required values)
├── drivers/
│   ├── base.py            # DatabaseDriver interface + SQL dialect helpers
│   ├── connection.py      # per-thread connections, transactions, retries, fork safety
│   ├── sqlite.py          # SQLite driver (stdlib)
│   └── mysql.py           # MySQL / MariaDB driver (mysqlclient)
├── fields/
│   ├── base.py            # Field
│   ├── numeric.py         # integers, auto keys, float, decimal
│   ├── text.py            # char, text, slug, email, URL, IP
│   ├── temporal.py        # date, datetime, time, duration
│   ├── special.py         # boolean, UUID, JSON, binary
│   ├── files.py           # file / image / file-path
│   └── related.py         # ForeignKey, OneToOneField
├── model/
│   ├── base.py            # Model: CRUD, validation, locking, assignment
│   ├── meta.py            # ModelMeta (field collection, registry)
│   ├── finders.py         # findBy… / countBy… / existsBy…
│   ├── schema.py          # table naming, create_table / drop_table
│   └── decorators.py      # @table
├── query/
│   ├── queryset.py        # QuerySet
│   ├── compiler.py        # WHERE / ORDER BY / SELECT generation
│   ├── lookups.py         # lookup names, LIKE escaping
│   ├── prefetch.py        # prefetch_related
│   ├── q.py               # Q objects
│   └── utils.py
├── relationships/
│   ├── base.py
│   ├── descriptors.py     # HasMany, HasOne, BelongsTo
│   └── many_to_many.py    # ManyToMany + manager
└── utils/
    └── sql.py             # identifier validation, literal quoting, SQL lexer
```

---

## ✅ Running the Tests

```bash
pip install -e . pytest
python3 -m pytest tests/
```

The suite covers SQLite end-to-end and the MySQL driver against a recording fake of
`MySQLdb` (`tests/fakes.py`), so no database server is needed.

---

## 📝 Full Example

```python
import uuid
from doveorm import (
    Model, configure, table,
    CharField, TextField, BooleanField, UUIDField, ForeignKey,
    HasMany, BelongsTo,
)

configure(DATABASE_PATH="blog.db")

@table(name="users", ordering=["-id"])
class User(Model):
    username = CharField(max_length=80, unique=True)
    email    = CharField(max_length=120, unique=True)
    password = CharField(max_length=255, sensitive=True)
    active   = BooleanField(default=True)
    posts    = HasMany("Post", foreign_key="author_id")

class Post(Model):
    title     = CharField(max_length=200, nullable=False)
    body      = TextField(nullable=False)
    published = BooleanField(default=False)
    author_id = ForeignKey("User", on_delete="CASCADE")
    author    = BelongsTo("User", foreign_key="author_id")

@table(name="tickets")
class Ticket(Model):
    id    = UUIDField(primary_key=True, default=uuid.uuid4)
    title = CharField(max_length=200)

for model in (User, Post, Ticket):
    model.create_table()

user = User.create(username="titus", email="titus@dev.com", password="<hash>")
post = Post.create(title="Hello DoveORM", body="It works!", author_id=user)
ticket = Ticket.create(title="Bug fix")

print(User.findByUsername("titus"))            # <User id=1>
print(User.countByActive(True))                # 1
print(Post.where(title__icontains="imara").count())
print(user.posts)                              # [<Post id=1>]
print(post.author.username)                    # titus
print(user.to_dict())                          # no password key
```

---

## 📄 License

MIT License — use it however you want.

---

<p align="center">
  <strong>Built with ❤️ by the Imara team</strong><br>
  <em>"Imara" — Swahili for strong, solid, resilient.</em>
</p>
