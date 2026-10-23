# DoveORM (`doveorm/`) — Code Audit Report

**Date:** 2026-10-23
**Scope:** `doveorm/` only (`__init__.py`, `config.py`, `database.py`, `model.py`, `queryset.py`, `fields.py`, `relationships.py`, `migrations.py`, `drivers/*`). The Flask app (`makao-api/`) and root scripts were excluded.
**Method:** Full manual read of every file, plus runtime probes against SQLite to confirm suspected bugs. Findings marked **✅ Confirmed** were reproduced; the others come from reading the code.

**Test suite status:** On Python 3.13, 120 passed and 1 failed (`TestMigratorRollback::test_migration_timestamp_is_tz_aware`, see B7). On Python 3.9, which is the default `python3` on this machine, the package fails to import (see B2).

---

## Remediation status (2026-10-23)

All findings below have been addressed in `doveorm/` (v0.2.0). Verification: **225 tests pass on Python 3.9.5 and 3.13** (121 original + 104 new regression tests in `tests/test_hardening.py`, one or more per finding). The original exploit probes now fail safely: the `limit()` payload raises `TypeError`, and the `DEFAULT` payload is stored as an inert string with the `users` table intact.

| Area | Status |
|---|---|
| S1–S6, S8–S10 | Fixed. S7 (`configure_from_file` executes Python) is by design; the docs now carry a warning and the file must exist. |
| B1–B20 | Fixed. |
| P1–P7, P9–P12 | Fixed. |
| P8 | Partially fixed. `BelongsTo` now caches per instance, and `ManyToMany` loads lazily. `HasMany`/`HasOne` deliberately re-query, because related rows change; use `prefetch_related()`. |
| Packaging / hygiene | Fixed: Python 3.9+ supported, `mysqlclient` optional, `__pycache__` untracked and ignored. |

**Known limits, documented in code:**
- MySQL behaviour was tested against a recording fake `MySQLdb`, not a live server.
- MySQL DDL commits implicitly, so it is not transactional.
- `contains`/`startswith`/`endswith` follow the backend's collation for case sensitivity. The `i*` variants are always case-insensitive.
- Shared-cache `:memory:` SQLite uses table-level locks, so heavy concurrent writers can see `database table is locked`. Use a file database for multi-threaded production use.

**Behaviour changes callers may notice:**
- Unfiltered `where().update()/.delete()` raises `UnsafeQueryError`; use `.all()` instead. Bulk writes combined with `limit()`/`offset()` also raise.
- Unknown lookups, unknown `Model(**kwargs)` keys, unknown settings and unknown `update()` columns now raise instead of being ignored.
- `len(qs)` and `bool(qs)` now evaluate the query and cache the rows (no extra `COUNT`).
- `Model.delete()` returns the number of rows deleted.
- Models without a primary key get `id = AutoField()` injected.
- `IntegerField` enforces the 32-bit range, and booleans are rejected by integer fields.
- On MySQL, `DateTimeField` is now `DATETIME(6)` and `TimeField` is `TIME(6)`. A `ForeignKey` column takes the target primary key's type.
- `Migrator` logs through `logging` instead of `print`. `rollback()` refuses migrations that have no rollback function unless `force=True`.
- On MySQL, UPDATE row counts are now rows *matched* (`CLIENT.FOUND_ROWS`).

---

## Hardening round 2: drivers & models (v0.3.0)

Verified by **278 tests on Python 3.9.5 and 3.13**, 53 of them new in `tests/test_driver_model_hardening.py`. pyflakes is clean, and every relative import in `doveorm/` resolves.

**Bug fixed:** `doveorm/model/base.py` imported `.config` instead of `..config` after the restructure. This broke `Model._ensure_db()` for models relying on the global `configure()`, and no test covered that path. It now has a regression test.

**Drivers**
| Hardening | Where |
|---|---|
| Fork safety: a child process never uses or closes a connection inherited from its parent | `drivers/connection.py` |
| `max_lifetime`: connections are recycled once older than N seconds, never mid-transaction | `drivers/connection.py` |
| `disconnect()` refuses while a transaction is open (`force=True` to override) | `drivers/connection.py` |
| `run_in_transaction(fn, retries=…)`: deadlocks, lock-wait timeouts and SQLite "database is locked" errors are retried with jittered backoff | `drivers/connection.py`, `database.py` |
| `Database` and drivers cannot be pickled or copied (they hold live sockets and credentials) | `drivers/base.py`, `database.py` |
| SQLite: new DB files are created `0600`; `trusted_schema=OFF`; optional `statement_timeout` raises `QueryTimeout`; path/option validation | `drivers/sqlite.py` |
| MySQL: strict `sql_mode` per session (bad data errors out instead of being silently truncated or zeroed); whitelisted `isolation_level`; literal escaping follows `NO_BACKSLASH_ESCAPES`; warning for remote connections without TLS; `repr()` never includes the password | `drivers/mysql.py` |

**Models**
| Hardening | Where |
|---|---|
| Field names may not start with `_` or shadow the Model API (`save`, `delete`, `where`, …) | `model/meta.py` |
| Strict attribute assignment: `user.emial = …` raises; relationships are read-only | `model/base.py` |
| `sensitive=True` fields are left out of `to_dict()` and shown as `<redacted>` in every validation error | `fields/*`, `model/base.py` |
| Mass-assignment protection: `Model.from_input(data)` / `obj.assign(data, allowed=…)` never set the PK, version, `auto_now` or `editable=False` fields | `model/base.py` |
| Optimistic locking via `Meta.version_field` (or `@table(version_field=…)`): stale `save()` / `delete()` raise `ConcurrencyError`; bulk `update()` bumps the version; `select()` always loads it | `model/*`, `query/queryset.py` |
| `save(update_fields=[…])`, a `clean()` hook run before every save, `find(None)` returns `None`, and abstract models cannot be queried | `model/base.py`, `query/queryset.py` |

New exceptions: `ConcurrencyError`, `TransactionError`, `QueryTimeout`.

---

## Migrations system (v0.4.0)

The old `doveorm/migrations.py` was a list of named functions. It is now a `doveorm/migrations/` package with Django-style migrations. Verified by **339 tests on Python 3.9.5 and 3.13**, 53 of them new in `tests/test_migrations.py`. The new code is pyflakes-clean.

| Piece | What it does |
|---|---|
| `makemigrations` | Compares the models with the schema rebuilt from existing migration files and writes the next file. No snapshot file, no DB connection needed. Renames and NOT NULL backfills are asked for, never guessed. |
| Autodetector safety net | Replays the detected operations and requires an exact match with the models; otherwise nothing is written |
| Optimizer | Merges create+add/alter/rename, drops add+remove, collapses repeated alters, only across independent operations |
| `migrate` / `rollback` / `migrate <target>` / `migrate zero` | Dependency-graph planning, unique-prefix targets, single-pass state building |
| SQLite | Each migration plus its history row is atomic. Unsupported `ALTER`s use a table rebuild with FK enforcement suspended (no cascade deletes) and `foreign_key_check` before commit |
| MySQL | `GET_LOCK` against concurrent deploys; FK/index names introspected; a PK type change also updates referencing FK columns; strict TEXT/BLOB index check |
| `sqlmigrate`, `--plan`, `--check` (CI), `--fake`, `--fake-initial`, `--merge`, `squashmigrations` | Tooling |
| Back-compat | `Migrator` and the old `up()`/`down()` files keep working on the same `migrations` table |

Supporting change: `Field.deconstruct()` / `Field.clone()` (`fields/base.py`) describe a field's *schema*. Arguments such as `help_text`, `choices` or `sensitive` never trigger a migration.

**App bug found while testing against `makao-api`:** `model/__init__.py` declared `id = UUIDField(primary_key=True, default=uuid.uuid4())`. The call runs once, so every `Property` got the same id and the second insert failed. It is now `default=uuid.uuid4`.

---

## Management command and packaging (v0.5.0)

The ORM was renamed from SwiftORM to **DoveORM** (package `doveorm`, `from doveorm import …`). It now ships its own `doveorm` command and installs with pip, so it no longer depends on a framework or a per-project manage script. Verified by **367 tests on Python 3.9.5 and 3.13** (28 new in `tests/test_cli.py`), a wheel built and installed into fresh 3.9 and 3.13 virtualenvs, and the full `init → generate → apply → doctor` flow run with the installed command.

| Piece | What it does |
|---|---|
| `pyproject.toml` | `pip install "doveorm @ git+…"`; `[mysql]` extra; `doveorm` console script; only `doveorm/` is packaged (no tests or demo apps); `tomli` only on Python < 3.11 |
| Project config | `doveorm.toml` or `[tool.doveorm]` in `pyproject.toml`, found by searching upwards; `models`, `migrations`, `settings` (dict, module or function), else `DOVEORM_*` env vars; flags override |
| Commands | `init`, `generate`, `merge`, `apply`, `revert`, `history`, `sql`, `squash`, `verify`, `status`, `doctor`, `models`, `console`, `connect` |
| `doctor` | connection, history consistency, pending migrations, unmigrated model changes, missing tables/columns; SQLite integrity, dangling FKs, WAL, file mode; MySQL strict mode, utf8mb4, TLS |
| Safety | `generate`/`verify` refuse to run with no models registered (that would generate a migration deleting every table); `status`/`doctor` are read-only and never print the password; `connect` passes the MySQL password through a 0600 temporary option file, not argv |

The older `doveorm.migrations.cli.main` (used by `makao-api/manager.py`) keeps working; makao-api also has a `doveorm.toml` now.

## Summary

| Severity | Count | Highlights |
|---|---|---|
| 🔴 Critical | 3 | SQL injection via `limit()`/`offset()`, DDL injection via field `default`, stale reads on MySQL |
| 🟠 High | 7 | Wrong PK kept after insert, partial `select()` + `save()` overwrites data, unknown lookups silently turn into `=`, `:memory:` not shared between threads, MySQL DDL breaks |
| 🟡 Medium | 16 | Logs leak secrets, bulk delete with no filter, migration atomicity, tz loss, `count()` ignores limit, Decimal sorting… |
| 🔵 Low / Quality | 15+ | Dead config, doc drift, hygiene |
| ⚡ Performance | 12 | No result cache, O(n²) M2M add, no bulk insert, connection leaks, unbounded IN lists |

### Top 5 to fix first
1. **S1:** Coerce `limit()`/`offset()` to `int` (blind SQL injection confirmed).
2. **S2:** Escape `DEFAULT` literals and stop using `executescript` for single statements (a crafted default dropped a table in testing).
3. **B1:** MySQL reads never commit, so each thread reads a frozen snapshot.
4. **B3 / B4:** `save()` keeps an ID that differs from the stored row's ID. `select()` + `save()` silently overwrites unselected columns with defaults.
5. **B5:** Unknown lookup operators (`age__gtee=18`) silently become `=`.

---

## 1. Security

### 🔴 S1. SQL injection via `QuerySet.limit()` / `.offset()`: ✅ Confirmed
`doveorm/queryset.py:430-433`
```python
sql += f" LIMIT {self._limit_val}"
sql += f" OFFSET {self._offset_val}"
```
The values are inserted into the SQL string without type checks. If any caller passes a request parameter (for example `?limit=…`), an attacker controls the SQL. Confirmed blind data exfiltration from a different table:
```python
p = "(SELECT CASE WHEN substr((SELECT pw FROM secrets),1,1)='h' THEN 1 ELSE 0 END)"
len(Post.all().limit(p).get())   # → 1 for 'h', 0 for anything else
```
**Fix:** In `limit()`/`offset()`, use `n = int(n)` and reject negative values. Better still, bind them as parameters (`LIMIT ? OFFSET ?`).

### 🔴 S2. DDL injection through field `default` + multi-statement `executescript`: ✅ Confirmed
`doveorm/fields.py:85-88`, `doveorm/drivers/sqlite.py:89`, `doveorm/model.py:400`
```python
val = f"'{db_val}'" if isinstance(db_val, str) else str(db_val)
parts.append(f"DEFAULT {val}")
```
Quotes are not escaped, and `create_table()` sends the DDL through `executescript`, which runs **multiple statements**. With `CharField(default="x'); DROP TABLE users; --")`, `create_table()` **dropped the `users` table**. Even a harmless default such as `"it's"` produces a syntax error.
**Fix:** Escape literals (`db_val.replace("'", "''")`) or reject defaults that contain quotes. Run single DDL statements with `execute()`, not `executescript()`.

### 🟠 S3. Identifiers are interpolated into SQL without validation
Column names in `where()`/`order_by()`/`select()` are checked against `_SAFE_COL_RE`. The following identifiers are **not** checked and go straight into SQL:
- `Meta.table_name` / `@table(name=…)` → every query (`model.py:242`)
- `Meta.indexes`, `Meta.unique_together` column lists (`model.py:413-426`)
- `ForeignKey.on_delete` (`model.py:386`). This should be checked against a whitelist (`CASCADE | SET NULL | RESTRICT | NO ACTION | SET DEFAULT`).
- `ManyToMany.through`, `source_fk`, `target_fk` (`relationships.py:168,181,222-224`; `queryset.py:311-313`)
- `Meta.engine` / `Meta.charset` (`model.py:397`)

These come from developers, not end users, so the risk is lower. The ORM still advertises "defence-in-depth", so it should check them all.
**Fix:** In `ModelMeta.__new__` and the relationship `__init__`s, check every identifier against `_SAFE_COL_RE` (and the `on_delete` whitelist).

### 🟡 S4. Query logging writes sensitive values to the log
`doveorm/database.py:96-101` logs `params=%s` in full when `LOG_QUERIES=True`. That puts password hashes, tokens, emails and other PII into application logs.
**Fix:** By default, log only the number of parameters or redact them. Add a separate `LOG_QUERY_PARAMS` opt-in.

### 🟡 S5. Unfiltered bulk `update()` / `delete()`
`QuerySet.delete()` and `.update()` with no filters run `DELETE FROM t` / `UPDATE t SET …` across the whole table (`queryset.py:206-234, 436-442`). A pattern like `Model.where(**request_filters).delete()` wipes the table when the filters are empty.
**Fix:** Require an explicit `.all()` or a `force=True` flag when no WHERE clause is present.

### 🟡 S6. Insecure connection defaults
`config.py:76-77`: the defaults are `DATABASE_USER="root"` and `DATABASE_PASSWORD=""`. A config with a typo silently falls back to root with no password instead of failing.
**Fix:** Make the user and password required for MySQL, or at least log a warning.

### 🔵 S7. `configure_from_file()` executes arbitrary Python
`config.py:165-167` imports and executes the given file. That is fine by design, but the docs should say it must never receive a path the user controls.

### 🔵 S8. Error messages list the full schema
`queryset.py:88-92` and `model.py:296-300` include every field name in exceptions. If an API returns exception text to clients, it reveals the schema. Keep these messages in logs and return generic errors.

### 🔵 S9. Callables passed in as values are executed
`model.py:210-213` calls any callable found in `kwargs`, not only callable defaults. `UUIDField.to_db` (`fields.py:573`) does the same. The intent is to resolve `default=uuid.uuid4`, so only `field.default` should be called.

### 🔵 S10. MySQL `adapt_sql` replaces every `?`
`drivers/base.py:71-73` turns every `?` into `%s`, including `?` inside string literals and in `db.raw()` SQL. A literal `%` in raw SQL that also has params breaks MySQLdb formatting. Ask callers to write `%s` directly, or use a tokenizer that skips literals.

---

## 2. Correctness bugs

### 🔴 B1. MySQL: reads stay inside a transaction that is never committed (stale data)
`drivers/mysql.py:100-138`: the connection uses `autocommit(False)`, and `execute()` never commits. The first SELECT on a thread opens an InnoDB **REPEATABLE READ** snapshot that lasts until that thread's next write. Long-lived worker threads therefore see **frozen data**. The open transaction also holds metadata locks, which can block migrations (`ALTER`/`DROP`).
**Fix:** Commit after each read outside an explicit `transaction()`, or enable autocommit and issue `BEGIN` only inside `transaction()`.

### 🟠 B2. Package does not import on Python < 3.10: ✅ Confirmed
`doveorm/database.py:45` uses `DatabaseDriver | None` in a signature and has no `from __future__ import annotations`. On 3.9 it fails with `TypeError: unsupported operand type(s) for |`. No `python_requires` is declared anywhere.
**Fix:** Add the `__future__` import, or declare `>=3.10` in packaging.

### 🟠 B3. Explicit PK on an `AutoField` is dropped, but the instance keeps it: ✅ Confirmed
`model.py:493-503`: `User(id=99, name="c").save()` leaves `id` out of the INSERT, and the DB assigns 3. `new_id` is only stored when `pk_val is None`, so the instance still says **99**. A later `save()` runs `UPDATE … WHERE id = 99`. That silently does nothing, or **updates another row** if id 99 exists.
**Fix:** Always set `self.pk = new_id` for auto-increment PKs, or include the explicit PK in the INSERT.

### 🟠 B4. Partial `select()` + `save()` overwrites unselected columns with defaults
`model.py:558-563`: when a column wasn't selected, `_from_row` fills it with the field default (and **calls callable defaults**, e.g. a fresh `uuid4`). It also marks the instance as persisted. `save()` then UPDATEs **every** non-PK column:
```python
u = User.where(id=1).select("id", "name").first()
u.name = "x"; u.save()      # age, email, … reset to defaults in the DB
```
**Fix:** Track which fields were loaded or changed, and UPDATE only those.

### 🟠 B5. Unknown lookup operators silently become `=`: ✅ Confirmed
`queryset.py:356`: `self._OP_MAP.get(op, ("=", False))`. `where(age__gtee=18)` runs `age = 18`, and `where(name__contains="a")` runs an equality match. Typos return wrong results instead of raising.
**Fix:** Raise `ValueError` for unknown ops. Also add `contains`/`icontains`/`startswith` if they're wanted.

### 🟠 B6. `:memory:` SQLite (the default) is not shared between threads
`drivers/sqlite.py:31-61`: connections are thread-local, and each `sqlite3.connect(":memory:")` creates a **separate, empty** database. With the default config (`DATABASE_PATH=":memory:"`), a threaded server or worker sees missing tables and data. `get_database()` also builds this database silently when nothing is configured (`config.py:191-192`), so a misconfigured production app can "work" while losing every write.
**Fix:** Use `file::memory:?cache=shared` with `uri=True`, or share one connection guarded by a lock for memory DBs. Make the silent fallback a warning or an error. (`_ensure_db` already raises in this case, so the two paths are inconsistent.)

### 🟠 B7. MySQL DDL generation is broken in several places
- `CREATE INDEX IF NOT EXISTS` (`model.py:407,417,425`) is **not valid MySQL** (MariaDB only). Any `db_index=True` field breaks `create_table()`. Every `SlugField` has `db_index=True` by default.
- `ForeignKey.sql_type()` is always `INTEGER` (`fields.py:716`). An FK to a `BigAutoField` (BIGINT) or a `UUIDField` (CHAR(32)) PK has the wrong type, and MySQL rejects mismatched FK types.
- `execute_script` splits on `;` without parsing (`mysql.py:162`), so a `;` inside a default or string literal breaks it.
- `TextField(unique=True)` → `LONGTEXT UNIQUE` is rejected by MySQL (it needs a key length).

### 🟡 B8. `DateTimeField` drops timezone and microseconds: ✅ Confirmed (failing test)
`fields.py:470-476` uses `strftime("%Y-%m-%d %H:%M:%S")`, which discards `tzinfo` and microseconds. `test_migration_timestamp_is_tz_aware` fails because of this. The values come back as naive datetimes, so code mixes naive and aware values.
**Fix:** On SQLite, store `isoformat()`. On MySQL, convert to UTC and add the tz back on read when the field is configured as tz-aware.

### 🟡 B9. `Migrator.rollback()` order is not reliable, and migrations are not atomic
`migrations.py:59` orders by `applied_at`, which only has second resolution (B8). Migrations applied in the same second roll back in arbitrary order. Order by `-id` instead.
`migrate()` (`migrations.py:41-50`) runs `fn()` and the `Migration.create` record outside a transaction. A failure partway through leaves the schema and the history out of sync.
`rollback()` deletes the history record even when no rollback function exists (`migrations.py:69-71`). The next `migrate()` then re-runs the migration against a schema that already has those changes.

### 🟡 B10. `executescript` commits any open transaction
`sqlite3.Connection.executescript` issues a `COMMIT` first. Calling `create_table()`/`drop_table()` inside `db.transaction()` silently commits the outer transaction, so a later rollback cannot undo the earlier writes.

### 🟡 B11. `count()` / `len()` ignore `limit`/`offset`
`queryset.py:428-433`: `len(User.all().limit(10))` returns the total row count, but iterating yields 10 rows. Wrap the query: `SELECT COUNT(*) FROM (SELECT … LIMIT …)`.

### 🟡 B12. `update()` drops unknown columns and skips validation: ✅ Confirmed
`queryset.py:223-231`: unknown keys are dropped without notice. If **every** key is unknown, the SQL is `UPDATE t SET  WHERE…`, which is a syntax error. Bulk updates also skip `field.validate()`, so `max_length`, `choices` and type checks never run.

### 🟡 B13. Empty `__in` lists
`queryset.py:365-368` emits `IN ()`. SQLite accepts it, but MySQL raises a syntax error. Short-circuit to `1=0` (`in`) or `1=1` (`not_in`).

### 🟡 B14. `auto_now` / `auto_now_add` are accepted but never applied
`fields.py:435-438, 463-466, 492-495` store the flags, but nothing in `model.py` reads them. Timestamps are never set automatically.

### 🟡 B15. `DecimalField` stored as TEXT on SQLite sorts and compares as strings
`fields.py:196-199`: `"10.00" < "9.00"` lexicographically, so `order_by("price")` and `price__gte=…` give wrong results on SQLite.

### 🟡 B16. `get_or_create` race condition
`model.py:476-480` runs SELECT and then INSERT with no locking. Two concurrent callers both insert, giving a duplicate row or an `IntegrityError` when a unique constraint exists. Catch `IntegrityError` and re-fetch.

### 🟡 B17. Model identity, `__eq__` and `__hash__`
`model.py:570-578`: all unsaved instances compare equal because their PKs are all `None` (✅ Confirmed). The hash changes after `save()`/`delete()`, which corrupts sets and dicts that hold the instance.

### 🟡 B18. Missing primary key only produces a warning, and the docs disagree
`model.py:62-73` only warns, but `_pk_field()` falls back to `"id"`, and then `save()` raises `KeyError: 'id'` (`model.py:492`). The `ModelMeta` docstring (line 40) and the `AutoField` docstring (`fields.py:230`) both say an `id` is injected automatically. Either inject it or raise at class definition.

### 🟡 B19. Global model registry keyed by bare class name
`model.py:119`: two models with the same name in different modules overwrite each other. A user model called `Migration` collides with the ORM's internal `doveorm.migrations.Migration`. Key the registry by `module.qualname`, or by table name.

### 🔵 B20. Smaller correctness issues
- `QuerySet.last()` (`queryset.py:191-195`) adds `-pk` *after* any existing ordering, so the result isn't "last". It should reverse the existing order.
- `Meta.ordering` only applies to `Model.all()`, not to `where()`/`find()`/dynamic finders (`model.py:449-451`), which is inconsistent.
- `Q` filters skip the "known field" check that `where()` does (`queryset.py:385-390` vs. `116`), so typos only fail inside the database.
- `is_null` uses truthiness, so `name__is_null="false"` → `IS NULL`.
- `IntegerField.validate` accepts `bool`, because `isinstance(True, int)`.
- `Field.validate` lets an explicit `None` through when `default` is set, so the INSERT then fails with a NOT NULL error.
- `Model.use()` docstring says it applies "to all subclasses", but each subclass gets its own `_db = None` (`model.py:76`).
- `configure()` calls `_wire_all_models`, which overrides models that were explicitly bound with `.use(other_db)`. It also leaves the old database's connections open.
- `RelationshipDescriptor.__set_name__` (`relationships.py:20-22`): `hasattr(owner, "_relationships")` is true for subclasses, so they **mutate the parent's dict**.
- FK fallback table name `ref_cls_name.lower() + "s"` (`model.py:383`) doesn't match `_table_name()` (no snake_case or pluralisation rules).
- `configure_from_env` can't set `DATABASE_OPTIONS`/`DATABASES` (dicts), and a bad int raises an unhandled `ValueError`.
- The `QuerySet`/`Q` docstrings advertise `User.filter(Q(...))` (`queryset.py:25,163`), but `Model` has no `filter` classmethod (✅ Confirmed `AttributeError`). Only `User.all().filter(...)` works.

---

## 3. Performance

| # | Issue | Location | Fix |
|---|---|---|---|
| P1 | **No result cache.** `__iter__` re-runs the query, `__len__` runs COUNT, and `bool(qs)` runs COUNT. `if qs: for x in qs:` costs 2 queries. | `queryset.py:236-240` | Cache results in `_result_cache` on first evaluation |
| P2 | `exists()` uses `COUNT(*)`, which scans every match | `queryset.py:203-204` | `SELECT 1 … LIMIT 1` |
| P3 | **One commit per statement.** Every `execute_write` outside `transaction()` commits (an fsync on SQLite). Loops of `create()` are slow. | `sqlite.py:79-85`, `mysql.py:140-151` | Document `with db.transaction():` for batches; add `bulk_create()` using `executemany` |
| P4 | `ManyToManyManager.add` runs 1 INSERT + 1 COMMIT per item and rebuilds `existing_pks` on every iteration, which is **O(n²)** | `relationships.py:166-173` | Build the set once, then a single `executemany` in one transaction |
| P5 | **Unbounded `IN (...)` lists** in prefetch. SQLite caps bound variables (999 on older builds, 32766 on newer), and MySQL is limited by `max_allowed_packet` | `queryset.py:269,282,295,310,320` | Chunk IDs into batches of about 500 |
| P6 | **Thread-local connections are never closed.** Every thread that touches the DB leaks a connection or file descriptor. There's no pooling or max-connection limit. | `sqlite.py:55-61`, `mysql.py:100-105` | Offer a pool, or a `close_connection()` hook per request or thread |
| P7 | **No reconnect on MySQL.** `connect()` skips the reconnect when a connection object exists. After `wait_timeout` (8 h by default), you get "MySQL server has gone away" (2006) forever. | `mysql.py:100-105` | Ping and reconnect, or catch 2006/2013 and retry once |
| P8 | Relationship descriptors query on **every attribute access**: `len(s.courses)` followed by iterating = 2 queries, and loops make N+1 | `relationships.py:53-63, 206-237` | Cache on the instance after the first load |
| P9 | `ManyToMany.__get__` calls `_ensure_db()` twice and rebuilds everything per access | `relationships.py:226-231` | Minor cleanup |
| P10 | Function-level `import datetime/json/uuid/decimal` inside `to_db`/`from_db`/`validate`, which run for every value of every row | `fields.py` (≈15 sites) | Move the imports to module level |
| P11 | `_pk_field()` (linear scan) and `_table_name()` (regex) are recomputed on every call; dynamic `findBy*` re-parses on each access | `model.py:240-262, 134-164` | Compute once in the metaclass; memoize finders |
| P12 | `_DictRow.__getitem__(int)` builds `list(self.values())` per lookup, which is O(cols) | `mysql.py:41-44`, `sqlite.py:20-23` (SQLite class is unused) | Store the row tuple and index into it; delete the unused SQLite `_DictRow` |

Also: queries always use `SELECT *` unless `.select()` is used. That's fine, but combined with B4 partial selects aren't safe either. Fix B4 so `select()` becomes a safe optimisation.

---

## 4. Code quality & maintainability

- **Dead / unused code:** `_table_decorator_*` pops in `ModelMeta` (`model.py:83-88`; the decorator sets attributes directly), the no-op `ForeignKey.sql_definition` override (`fields.py:718-720`), the no-op `FileField.validate`, `MEDIA_ROOT`/`MEDIA_URL` settings (never read), `FilePathField.path/match/recursive` (never enforced), `ImageField.width_field/height_field`, `Field.verbose_name/help_text`, `SQLite _DictRow`, and `foreign_key_pragma()` (never called; SQLite sets the pragma inline).
- **Docstring typo:** `"Lazy, chainable query builder.what error"` (`queryset.py:59`).
- **Duplicated logic:** the int-type check is copied three times (`IntegerField`/`BigIntegerField`/`SmallIntegerField`). The `≥ 0` check is also copied three times. `transaction()` is nearly identical in both drivers and could live in the base class with a `_exec_raw` hook. The three dynamic finder factories could share one helper.
- **`print()` in library code** (`migrations.py:45,51,67,70`): use the `doveorm` logger.
- **Packaging:** there's no `pyproject.toml`/`setup.py` and no `python_requires`. `doveorm/requirements.txt` makes `mysqlclient` a hard dependency even though it's optional and imported lazily. Declare it as an extra (`doveorm[mysql]`).
- **Repo hygiene:** `doveorm/__pycache__/` and `doveorm/drivers/__pycache__/*.pyc` are committed. Add `__pycache__/` to `.gitignore` and `git rm -r --cached` them.
- **Tests:** there are no MySQL tests, no tests for injection or identifier validation, no threading tests, and nothing covers the bugs above. Add regression tests for S1, S2, B3, B4, B5 and B6 when fixing them.

---

## 5. Suggested remediation order

1. **Now (security):** S1 (int-coerce limit/offset), S2 (escape defaults, avoid `executescript`), S3 (identifier validation), S4 (redact logged params).
2. **Next (data integrity):** B1 (MySQL snapshot), B3 (PK after insert), B4 (dirty-field UPDATE), B5 (unknown ops raise), B6 (`:memory:` threads / silent fallback), B2 (Python version).
3. **Then:** B7 (MySQL DDL), B8/B9/B10 (timestamps and migrations), P1/P3/P4/P7 (result cache, bulk ops, reconnect).
4. **Ongoing:** the remaining medium/low items, dead-code cleanup, packaging, and regression tests.
