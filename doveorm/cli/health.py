"""Database status and health checks behind ``doveorm status`` / ``doveorm doctor``.

Everything here only *reads*: no statement changes data or schema (the
migration history table is not even created when it is missing).
"""
from __future__ import annotations

import os
import sqlite3
import stat
import time
from dataclasses import dataclass, field

from ..exceptions import MigrationError

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    level: str                 # OK | WARN | FAIL
    detail: str = ""


@dataclass
class DatabaseInfo:
    engine: str
    location: str
    reachable: bool = False
    latency_ms: float | None = None
    server_version: str | None = None
    error: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class MigrationInfo:
    applied: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)      # recorded, but no file
    heads: list[str] = field(default_factory=list)
    error: str | None = None


# ── database ──

def location(db) -> str:
    """Where the data lives, never including a password."""
    driver = db.driver
    if driver.name == "sqlite":
        return ":memory:" if getattr(driver, "_memory", False) else driver.path
    return getattr(driver, "_display", driver.name)


def database_info(db) -> DatabaseInfo:
    info = DatabaseInfo(engine=db.driver_name, location=location(db))
    start = time.perf_counter()
    try:
        db.execute("SELECT 1")
    except Exception as exc:                    # report, never raise
        info.error = f"{type(exc).__name__}: {exc}"
        return info
    info.reachable = True
    info.latency_ms = (time.perf_counter() - start) * 1000
    try:
        if info.engine == "sqlite":
            info.server_version = "SQLite " + sqlite3.sqlite_version
            info.extra["journal_mode"] = _scalar(db, "PRAGMA journal_mode")
            info.extra["foreign_keys"] = "on" if _scalar(db, "PRAGMA foreign_keys") else "off"
            path = db.driver.path
            if not getattr(db.driver, "_memory", False) and os.path.isfile(path):
                info.extra["size"] = _size(os.path.getsize(path))
        else:
            row = db.execute("SELECT VERSION(), @@character_set_database, @@SESSION.sql_mode")
            if row:
                info.server_version = "MySQL " + str(row[0][0])
                info.extra["charset"] = row[0][1]
                info.extra["sql_mode"] = row[0][2]
    except Exception as exc:                    # pragma: no cover - details are best effort
        info.extra["details_error"] = str(exc)
    info.extra["tables"] = len(list_tables(db))
    return info


def list_tables(db) -> list[str]:
    if db.driver_name == "sqlite":
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    else:
        rows = db.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() "
            "ORDER BY TABLE_NAME"
        )
    return [r[0] for r in rows]


def column_names(db, table: str) -> set[str]:
    from ..migrations.backends import editor_for
    return editor_for(db, collect_sql=True).column_names(table)


def _scalar(db, sql):
    rows = db.execute(sql)
    return rows[0][0] if rows else None


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)  # pragma: no cover


# ── migrations ──

def migration_info(db, directory: str) -> MigrationInfo:
    from ..migrations.loader import MigrationLoader
    from ..migrations.recorder import MigrationRecorder
    info = MigrationInfo()
    try:
        recorded = MigrationRecorder(db).applied()
        graph, applied = MigrationLoader(directory).build_graph(recorded)
    except (MigrationError, OSError) as exc:
        info.error = str(exc)
        return info
    order = graph.order()
    info.applied = [n for n in order if n in applied]
    info.pending = [n for n in order if n not in applied]
    replaced = {r for m in graph.nodes.values() for r in m.replaces}
    info.unknown = [n for n in recorded if n not in graph.nodes and n not in replaced]
    info.heads = graph.leaves()
    return info


def unmigrated_changes(directory: str, models=None) -> list[str]:
    """Model changes that no migration file covers yet (descriptions)."""
    from ..migrations.autodetector import Autodetector
    from ..migrations.commands import _history, project_models
    from ..migrations.loader import MigrationLoader
    from ..migrations.questioner import Questioner
    from ..migrations.state import ProjectState

    class _Silent(Questioner):              # never ask; detection only needs *a* value
        def ask_not_null_addition(self, model, field):
            return None

    _graph, history = _history(MigrationLoader(directory))
    target = ProjectState.from_models(project_models(models))
    return [op.describe() for op in Autodetector(history, target, _Silent()).changes()]


def schema_drift(db, models=None) -> list[str]:
    """Tables / columns the models expect but the database lacks."""
    from ..migrations.commands import project_models
    from ..migrations.state import ModelState
    problems = []
    tables = set(list_tables(db))
    for model in project_models(models):
        ms = ModelState.from_model(model)
        if ms.table not in tables:
            problems.append(f"table {ms.table!r} (model {ms.name}) is missing")
            continue
        missing = sorted(set(ms.fields) - column_names(db, ms.table))
        if missing:
            problems.append(f"{ms.table}: missing column(s) {', '.join(missing)}")
    return problems


# ── doctor ──

def run_checks(db, directory: str, models=None) -> list[Check]:
    """Every health check, most fundamental first."""
    checks: list[Check] = []
    info = database_info(db)
    if not info.reachable:
        checks.append(Check("connection", FAIL, info.error or "unreachable"))
        return checks
    checks.append(Check("connection", OK,
                        f"{info.server_version} at {info.location} ({info.latency_ms:.1f} ms)"))

    # migrations
    pending = []
    if not os.path.isdir(directory):
        checks.append(Check("migrations directory", WARN, f"{directory} does not exist"))
    else:
        mig = migration_info(db, directory)
        if mig.error:
            checks.append(Check("migration history", FAIL, mig.error))
        else:
            pending = mig.pending
            if len(mig.heads) > 1:
                checks.append(Check("migration history", FAIL,
                                    f"conflicting heads {', '.join(mig.heads)}; run 'doveorm merge'"))
            elif mig.unknown:
                checks.append(Check("migration history", WARN,
                                    "applied in the database but no file: " + ", ".join(mig.unknown)))
            else:
                checks.append(Check("migration history", OK, f"{len(mig.applied)} applied"))
            checks.append(Check("pending migrations", WARN if pending else OK,
                                ", ".join(pending) if pending else "none"))
        from ..migrations.commands import project_models
        try:
            changes = unmigrated_changes(directory, models) if project_models(models) else None
        except MigrationError as exc:
            checks.append(Check("models vs migrations", FAIL, str(exc)))
        else:
            if changes is None:
                checks.append(Check("models vs migrations", WARN, "no models registered"))
            else:
                checks.append(Check("models vs migrations", WARN if changes else OK,
                                    "; ".join(changes) + " — run 'doveorm generate'" if changes
                                    else "every model change has a migration"))

    drift = schema_drift(db, models)
    if drift:
        level = WARN if pending else FAIL
        hint = " (pending migrations may fix this)" if pending else ""
        checks.append(Check("database schema", level, "; ".join(drift) + hint))
    else:
        checks.append(Check("database schema", OK, "every model table and column exists"))

    checks.extend(_sqlite_checks(db) if info.engine == "sqlite" else _mysql_checks(db))
    checks.extend(_settings_checks())
    return checks


def _sqlite_checks(db) -> list[Check]:
    out = []
    result = [r[0] for r in db.execute("PRAGMA quick_check")]
    out.append(Check("integrity", OK if result == ["ok"] else FAIL,
                     "PRAGMA quick_check passed" if result == ["ok"] else "; ".join(result[:5])))
    broken = db.execute("PRAGMA foreign_key_check")
    if broken:
        tables = sorted({r[0] for r in broken})
        out.append(Check("foreign keys", FAIL,
                         f"{len(broken)} row(s) point at missing parents in {', '.join(tables)}"))
    else:
        out.append(Check("foreign keys", OK, "no dangling references"))
    driver = db.driver
    if not getattr(driver, "_memory", False) and os.path.isfile(driver.path):
        journal = str(_scalar(db, "PRAGMA journal_mode")).lower()
        out.append(Check("journal mode", OK if journal == "wal" else WARN,
                         journal if journal == "wal" else f"{journal} (WAL is safer for concurrent access)"))
        mode = stat.S_IMODE(os.stat(driver.path).st_mode)
        out.append(Check("file permissions", WARN if mode & 0o077 else OK,
                         f"{oct(mode)}" + (" — readable by other users; chmod 600" if mode & 0o077 else "")))
    return out


def _mysql_checks(db) -> list[Check]:
    out = []
    try:
        row = db.execute("SELECT @@SESSION.sql_mode, @@character_set_database")
    except Exception as exc:                    # pragma: no cover - server specific
        return [Check("server settings", WARN, f"could not read: {exc}")]
    if row:
        sql_mode, charset = str(row[0][0] or ""), str(row[0][1] or "")
        strict = "STRICT_TRANS_TABLES" in sql_mode or "STRICT_ALL_TABLES" in sql_mode
        out.append(Check("sql_mode", OK if strict else WARN,
                         sql_mode if strict else f"{sql_mode or '(empty)'} — not strict; bad data is truncated silently"))
        out.append(Check("charset", OK if charset == "utf8mb4" else WARN,
                         charset if charset == "utf8mb4" else f"{charset} — utf8mb4 is recommended"))
    try:
        ssl = db.execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
    except Exception:                           # pragma: no cover
        ssl = []
    cipher = ssl[0][1] if ssl else ""
    host = str(getattr(db.driver, "_cfg", {}).get("host") or "")
    local = host in ("", "localhost", "127.0.0.1", "::1")
    if cipher:
        out.append(Check("tls", OK, cipher))
    else:
        out.append(Check("tls", OK if local else WARN,
                         "local connection" if local else "connection is not encrypted"))
    return out


def _settings_checks() -> list[Check]:
    from ..config import get_setting
    out = []
    if get_setting("LOG_QUERY_PARAMS", False):
        out.append(Check("settings", WARN, "LOG_QUERY_PARAMS is on: query values (secrets, PII) reach the logs"))
    return out
