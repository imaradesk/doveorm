"""The ``doveorm`` command.

    doveorm init                 create doveorm.toml and the migrations folder
    doveorm generate             write a migration for model changes
    doveorm apply [TARGET]       apply pending migrations (or move to TARGET)
    doveorm revert [N]           unapply the last N migrations
    doveorm history              list migrations, applied or not
    doveorm status               database and migration overview
    doveorm doctor               run health checks (exit 1 on failure)
    doveorm verify               CI gate: fail if models have unmigrated changes

Run ``doveorm <command> --help`` for the options of each command.
"""
from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile

from ..exceptions import ConfigurationError, ORMError
from . import health
from .project import CONFIG_FILE, PYPROJECT, Project

PROG = "doveorm"


# ──────────────────────────────────────────────
# Parser
# ──────────────────────────────────────────────

def _common() -> argparse.ArgumentParser:
    # Accepted before or after the command name; SUPPRESS keeps a value given
    # before the command from being reset by the subcommand's defaults.
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("-c", "--config", default=argparse.SUPPRESS,
                   help=f"path to {CONFIG_FILE} or {PYPROJECT} (default: search upwards)")
    c.add_argument("-s", "--settings", default=argparse.SUPPRESS,
                   help="settings as module or module:attribute")
    c.add_argument("-m", "--models", action="append", default=argparse.SUPPRESS, metavar="MODULE",
                   help="module that defines models (repeatable, or comma-separated)")
    c.add_argument("--migrations", default=argparse.SUPPRESS, metavar="DIR", help="migrations folder")
    c.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                   help="also log executed SQL")
    return c


def build_parser(prog: str = PROG) -> argparse.ArgumentParser:
    from .. import __version__
    common = _common()
    p = argparse.ArgumentParser(
        prog=prog, parents=[common],
        description="DoveORM project management: migrations and database tools.",
        epilog=f"Project settings come from {CONFIG_FILE} or [tool.doveorm] in {PYPROJECT}.",
    )
    p.add_argument("--version", action="version", version=f"DoveORM {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND", required=True)

    def cmd(name, help_text):
        return sub.add_parser(name, help=help_text, parents=[common],
                              description=help_text[0].upper() + help_text[1:] + ".")

    s = cmd("init", "create doveorm.toml and the migrations folder")
    s.add_argument("--pyproject", action="store_true",
                   help=f"add a [tool.doveorm] table to {PYPROJECT} instead")

    s = cmd("generate", "write the next migration from model changes")
    s.add_argument("-n", "--name", help="name suffix (default: derived from the changes)")
    s.add_argument("--empty", action="store_true", help="write an empty migration to fill in by hand")
    s.add_argument("--dry-run", action="store_true", help="print the migration, write nothing")
    s.add_argument("--no-input", action="store_true", help="never prompt (fail instead)")
    s.add_argument("--default", action="append", default=[], metavar="MODEL.FIELD=VALUE",
                   help="value for existing rows of a new NOT NULL column (Python literal)")
    s.add_argument("--rename", action="append", default=[], metavar="NEW=OLD",
                   help="declare a rename: Model.new_field=old_field or NewModel=OldModel")
    s.add_argument("-y", "--yes", action="store_true",
                   help="allow dropping models when the models were auto-discovered")

    s = cmd("merge", "join conflicting migration branches with a merge migration")
    s.add_argument("-n", "--name")
    s.add_argument("--dry-run", action="store_true")

    s = cmd("apply", "apply pending migrations, or move the database to TARGET")
    s.add_argument("target", nargs="?", help="migration name or unique prefix (e.g. 0003)")
    s.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    s.add_argument("--fake", action="store_true", help="record the migrations without running them")
    s.add_argument("--adopt", action="store_true",
                   help="record initial migrations whose tables already exist instead of creating them")

    s = cmd("revert", "unapply the most recent migrations")
    s.add_argument("steps", nargs="?", type=int, default=1, help="how many (default 1)")
    s.add_argument("--all", action="store_true", help="unapply every migration")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--fake", action="store_true", help="only remove the history records")

    s = cmd("history", "list migrations and whether each is applied")
    s.add_argument("--pending", action="store_true", help="only pending migrations")

    s = cmd("sql", "print the SQL a migration runs")
    s.add_argument("name", help="migration name or unique prefix")
    s.add_argument("--backwards", action="store_true", help="the SQL that unapplies it")

    s = cmd("squash", "combine a range of migrations into one")
    s.add_argument("start", nargs="?", help="first migration (default: the first one)")
    s.add_argument("end", help="last migration")
    s.add_argument("-n", "--name")
    s.add_argument("--dry-run", action="store_true")

    s = cmd("verify", "exit 1 if a model change has no migration (for CI)")
    s.add_argument("--database", action="store_true",
                   help="also fail on pending migrations and schema drift")

    cmd("status", "show the database connection and migration state")

    s = cmd("doctor", "run database health checks")
    s.add_argument("--strict", action="store_true", help="treat warnings as failures")

    cmd("models", "list models, their tables and whether those exist")

    s = cmd("console", "interactive Python shell with the models loaded")
    s.add_argument("--plain", action="store_true", help="plain Python shell even if IPython is installed")

    cmd("connect", "open the database's own command-line client (sqlite3 / mysql / psql)")
    return p


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def main(argv=None, prog: str = PROG) -> int:
    args = build_parser(prog).parse_args(argv)
    for name, default in (("config", None), ("settings", None), ("models", None),
                          ("migrations", None), ("verbose", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    _setup_logging(args.verbose)
    cwd = os.getcwd()                       # the project setup may chdir to the project root
    try:
        if args.command == "init":
            return cmd_init(args)
        models = [m.strip() for spec in (args.models or []) for m in spec.split(",") if m.strip()]
        project = Project.load(config=args.config, settings=args.settings, models=models,
                               migrations=args.migrations)
        project.setup()
        return COMMANDS[args.command](project, args)
    except ORMError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    finally:
        from ..config import get_database, is_configured
        if is_configured():
            get_database().close_all()      # connections reopen lazily if used again
        os.chdir(cwd)


def _setup_logging(verbose: bool) -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.WARNING, format="%(message)s")
    mig_log = logging.getLogger("doveorm.migrations")
    if mig_log.getEffectiveLevel() > logging.INFO:
        mig_log.setLevel(logging.INFO)          # "Applying 0002_…" progress lines
    if verbose:
        mig_log.setLevel(logging.DEBUG)         # executed migration SQL


# ──────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────

def cmd_init(args) -> int:
    migrations = args.migrations or "migrations"
    models = [m.strip() for spec in (args.models or []) for m in spec.split(",") if m.strip()]
    lines = [
        f"models = {_toml_list(models)}" + ("" if models else
                                            '                  # e.g. ["myapp.models"]'),
        f"migrations = {json.dumps(migrations)}",
    ]
    if args.settings:
        lines.append(f"settings = {json.dumps(args.settings)}")
    else:
        lines.append('# settings = "myapp.settings:DOVEORM"   # else DOVEORM_* environment variables')

    if args.pyproject:
        path = args.config or PYPROJECT
        if not os.path.isfile(path):
            raise ConfigurationError(f"{path} not found.")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if "[tool.doveorm]" in text:
            raise ConfigurationError(f"{path} already has a [tool.doveorm] table.")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(("" if text.endswith("\n") or not text else "\n") + "\n[tool.doveorm]\n"
                     + "\n".join(lines) + "\n")
        print(f"Added [tool.doveorm] to {path}")
    else:
        path = args.config or CONFIG_FILE
        if os.path.exists(path):
            raise ConfigurationError(f"{path} already exists.")
        with open(path, "x", encoding="utf-8") as fh:
            fh.write("# DoveORM project settings (see 'doveorm --help')\n" + "\n".join(lines) + "\n")
        print(f"Created {path}")
    base = os.path.dirname(os.path.abspath(path))
    folder = os.path.join(base, migrations)
    if not os.path.isdir(folder):
        os.makedirs(folder)
        open(os.path.join(folder, ".gitkeep"), "w").close()
        print(f"Created {os.path.relpath(folder)}/")
    if not models:
        print("Next: list your model modules under 'models', then run 'doveorm generate'.")
    return 0


def _toml_list(items) -> str:
    return "[" + ", ".join(json.dumps(i) for i in items) + "]"


def _require_models() -> None:
    """Refuse to diff against an empty model set: that would 'delete' every table."""
    from ..migrations.commands import project_models
    if not project_models():
        raise ConfigurationError(
            "No models are registered (none found named models.py). List your model modules under 'models' in "
            f"{CONFIG_FILE} (or pass --models myapp.models)."
        )


def _refuse_discovered_drops(project: Project) -> None:
    """A model the search missed looks deleted; make the user confirm real drops."""
    from ..migrations.operations.models import DeleteModel
    ops = health.unmigrated_operations(project.migrations)
    drops = [op.describe() for op in ops if isinstance(op, DeleteModel)]
    if drops:
        raise ConfigurationError(
            "Models were auto-discovered and this migration would drop: "
            + "; ".join(drops) + ". If a models module was missed, list it under 'models' in "
            f"{CONFIG_FILE}; if the drop is intended, re-run with --yes."
        )


def cmd_generate(project: Project, args) -> int:
    from ..migrations import commands
    from ..migrations.questioner import InteractiveQuestioner, Questioner
    defaults = {}
    for item in args.default:
        key, sep, raw = item.partition("=")
        if not sep or "." not in key:
            raise ConfigurationError(f"--default expects MODEL.FIELD=VALUE, got {item!r}.")
        try:
            defaults[key.strip()] = ast.literal_eval(raw.strip())
        except (ValueError, SyntaxError):
            raise ConfigurationError(f"--default {key}: {raw!r} is not a Python literal "
                                     "(quote strings: 'pending').") from None
    renames = {}
    for item in args.rename:
        new, sep, old = item.partition("=")
        if not sep or not new.strip() or not old.strip():
            raise ConfigurationError(f"--rename expects NEW=OLD, got {item!r}.")
        renames[new.strip()] = old.strip()
    if not args.empty:
        _require_models()
        if project.discovered and not args.yes:
            _refuse_discovered_drops(project)
    interactive = not args.no_input and sys.stdin.isatty()
    questioner = (InteractiveQuestioner if interactive else Questioner)(defaults=defaults, renames=renames)
    result = commands.makemigrations(project.migrations, name=args.name, empty=args.empty,
                                     dry_run=args.dry_run, questioner=questioner)
    if result is None:
        print("No changes detected.")
    elif args.dry_run:
        print(result.source)
    else:
        print(f"Created {os.path.relpath(result.path)}")
        for op in result.operations:
            print(f"  - {op.describe()}")
    return 0


def cmd_merge(project: Project, args) -> int:
    from ..migrations import commands
    result = commands.makemigrations(project.migrations, name=args.name, merge=True,
                                     dry_run=args.dry_run)
    if result is None:
        print("Nothing to merge: the history has a single head.")
    else:
        print(result.source if args.dry_run else f"Created {os.path.relpath(result.path)}")
    return 0


def _show_plan(steps) -> None:
    if not steps:
        print("Nothing to do.")
    for name, backwards in steps:
        print(f"  {'revert' if backwards else 'apply '}  {name}")


def cmd_apply(project: Project, args) -> int:
    from ..migrations import commands
    db = project.database()
    if args.dry_run:
        _show_plan(commands.plan(project.migrations, args.target, db=db))
        return 0
    done = commands.migrate(project.migrations, args.target, db=db, fake=args.fake,
                            fake_initial=args.adopt)
    if done:
        verb = "Reverted" if done[0][1] else "Applied"
        print(f"{verb} {len(done)} migration(s).")
    else:
        print("Database is up to date.")
    return 0


def cmd_revert(project: Project, args) -> int:
    from ..migrations import commands
    db = project.database()
    if args.all:
        target = "zero"
    else:
        if args.steps < 1:
            raise ConfigurationError("steps must be at least 1.")
        applied = [n for n, ok in commands.showmigrations(project.migrations, db=db) if ok]
        if not applied:
            print("Nothing to revert.")
            return 0
        keep = applied[:-args.steps]
        target = keep[-1] if keep else "zero"
    if args.dry_run:
        _show_plan(commands.plan(project.migrations, target, db=db))
        return 0
    done = commands.migrate(project.migrations, target, db=db, fake=args.fake)
    print(f"Reverted {len(done)} migration(s)." if done else "Nothing to revert.")
    return 0


def cmd_history(project: Project, args) -> int:
    from ..migrations import commands
    rows = commands.showmigrations(project.migrations, db=project.database())
    if args.pending:
        rows = [r for r in rows if not r[1]]
    if not rows:
        print("No pending migrations." if args.pending else f"No migrations in {project.display_migrations}.")
    for name, applied in rows:
        print(f"  [{'x' if applied else ' '}] {name}")
    return 0


def cmd_sql(project: Project, args) -> int:
    from ..migrations import commands
    for stmt in commands.sqlmigrate(project.migrations, args.name, db=project.database(),
                                    backwards=args.backwards):
        print(stmt.rstrip(";") + ";")
    return 0


def cmd_squash(project: Project, args) -> int:
    from ..migrations import commands
    result = commands.squashmigrations(project.migrations, args.end, start=args.start,
                                       name=args.name, dry_run=args.dry_run)
    if args.dry_run:
        print(result.source)
    else:
        print(f"Created {os.path.relpath(result.path)}")
        print("Keep the old files until every database has applied it, then delete them.")
    return 0


def cmd_verify(project: Project, args) -> int:
    _require_models()
    problems =[f"no migration for: {c}" for c in health.unmigrated_changes(project.migrations)]
    if args.database:
        db = project.database()
        mig = health.migration_info(db, project.migrations)
        if mig.error:
            problems.append(mig.error)
        if mig.pending:
            problems.append("pending migrations: " + ", ".join(mig.pending))
        problems.extend(f"schema: {d}" for d in health.schema_drift(db))
    if problems:
        for p in problems:
            print(f"  x {p}")
        return 1
    print("OK: models, migrations" + (" and database" if args.database else "") + " are in sync.")
    return 0


def cmd_status(project: Project, args) -> int:
    from ..config import get_setting
    db = project.database()
    info = health.database_info(db)
    rows = [("Engine", info.engine), ("Location", info.location)]
    if info.reachable:
        rows.append(("Server", info.server_version or "?"))
        rows.append(("Connection", f"ok ({info.latency_ms:.1f} ms)"))
        rows.extend((k.replace("_", " ").capitalize(), str(v)) for k, v in info.extra.items())
    else:
        rows.append(("Connection", f"FAILED — {info.error}"))
    rows.append(("Query logging", "on" if get_setting("LOG_QUERIES", False) else "off"))
    _print_pairs("Database", rows)

    mig_rows = [("Folder", project.display_migrations)]
    if info.reachable:
        mig = health.migration_info(db, project.migrations)
        if mig.error:
            mig_rows.append(("Error", mig.error))
        else:
            mig_rows.append(("Applied", str(len(mig.applied))))
            mig_rows.append(("Latest", mig.applied[-1] if mig.applied else "none"))
            mig_rows.append(("Pending", ", ".join(mig.pending) if mig.pending else "none"))
            if len(mig.heads) > 1:
                mig_rows.append(("Conflict", f"{len(mig.heads)} heads — run 'doveorm merge'"))
            if mig.unknown:
                mig_rows.append(("Unknown", ", ".join(mig.unknown) + " (applied, no file)"))
    print()
    _print_pairs("Migrations", mig_rows)
    return 0 if info.reachable else 1


def cmd_doctor(project: Project, args) -> int:
    checks = health.run_checks(project.database(), project.migrations)
    marks = {health.OK: "ok  ", health.WARN: "warn", health.FAIL: "FAIL"}
    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"  [{marks[c.level]}] {c.name.ljust(width)}  {c.detail}")
    fails = sum(c.level == health.FAIL for c in checks)
    warns = sum(c.level == health.WARN for c in checks)
    print(f"\n{len(checks)} checks: {fails} failed, {warns} warning(s).")
    return 1 if fails or (args.strict and warns) else 0


def cmd_models(project: Project, args) -> int:
    from ..migrations.commands import project_models
    from ..migrations.state import ModelState
    models = project_models()
    if not models:
        print("No models registered. List your model modules under 'models' in doveorm.toml.")
        return 0
    try:
        db = project.database()
        tables = set(health.list_tables(db))
    except ORMError:
        db, tables = None, None
    except Exception as exc:                    # unreachable database: still list the models
        print(f"(database unavailable: {exc})", file=sys.stderr)
        db, tables = None, None
    out = []
    for model in models:
        ms = ModelState.from_model(model)
        if tables is None:
            state = "?"
        elif ms.table not in tables:
            state = "missing"
        else:
            missing = set(ms.fields) - health.column_names(db, ms.table)
            state = f"{len(missing)} column(s) missing" if missing else "ok"
        out.append((ms.name, ms.table, str(len(ms.fields)), state, model.__module__))
    _print_table(("Model", "Table", "Fields", "Database", "Module"), out)
    return 0


def cmd_console(project: Project, args) -> int:
    import doveorm
    from ..migrations.commands import project_models
    ns = {"doveorm": doveorm}
    try:
        ns["db"] = project.database()
    except ConfigurationError:
        pass
    for model in project_models():
        ns.setdefault(model.__name__, model)
    names = sorted(k for k in ns if k not in ("doveorm", "db"))
    banner = (f"DoveORM {doveorm.__version__} console — "
              f"{'db, ' if 'db' in ns else ''}{', '.join(names) or 'no models'} loaded.")
    if not args.plain:
        try:
            from IPython import start_ipython
        except ImportError:
            pass
        else:                                   # pragma: no cover - optional dependency
            print(banner)
            start_ipython(argv=[], user_ns=ns)
            return 0
    import code
    code.interact(banner=banner, local=ns, exitmsg="")
    return 0


def cmd_connect(project: Project, args) -> int:
    db = project.database()
    driver = db.driver
    if driver.name == "sqlite":
        if getattr(driver, "_memory", False):
            raise ConfigurationError("An in-memory database has no client to connect to.")
        return _run_client("sqlite3", [driver.path])
    if driver.name == "postgresql":
        return _connect_psql(driver)
    cfg = dict(driver._cfg)
    # The password goes in a private option file, never on the command line
    # (where other users could read it from the process list).
    fd, option_file = tempfile.mkstemp(prefix="doveorm-", suffix=".cnf")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            password = str(cfg.get("passwd") or "").replace("\\", "\\\\").replace('"', '\\"')
            fh.write(f'[client]\npassword="{password}"\n')
        argv = [f"--defaults-extra-file={option_file}", f"--user={cfg['user']}",
                f"--default-character-set={cfg.get('charset', 'utf8mb4')}"]
        if cfg.get("unix_socket"):
            argv.append(f"--socket={cfg['unix_socket']}")
        else:
            argv += [f"--host={cfg['host']}", f"--port={cfg['port']}"]
        if cfg.get("ssl_mode"):
            argv.append(f"--ssl-mode={cfg['ssl_mode']}")
        argv.append(cfg["db"])
        return _run_client("mysql", argv)
    finally:
        os.unlink(option_file)


def _connect_psql(driver) -> int:
    cfg = dict(driver._cfg)
    # The password goes in a private passfile, never on the command line.
    fd, passfile = tempfile.mkstemp(prefix="doveorm-", suffix=".pgpass")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            esc = lambda v: str(v).replace("\\", "\\\\").replace(":", "\\:")  # noqa: E731
            fh.write(":".join(esc(v) for v in ("*", "*", "*", cfg["user"], cfg.get("password") or "")) + "\n")
        env = dict(os.environ, PGPASSFILE=passfile)
        if cfg.get("sslmode"):
            env["PGSSLMODE"] = str(cfg["sslmode"])
        argv = [f"--host={cfg['host']}", f"--port={cfg['port']}",
                f"--username={cfg['user']}", f"--dbname={cfg['dbname']}"]
        if driver.schema:
            env["PGOPTIONS"] = f"-c search_path={driver.schema}"
        return _run_client("psql", argv, env=env)
    finally:
        os.unlink(passfile)


def _run_client(program: str, argv: list[str], env: dict | None = None) -> int:
    path = shutil.which(program)
    if path is None:
        raise ConfigurationError(f"'{program}' was not found on PATH; install the {program} client.")
    if env is None:
        return subprocess.call([path, *argv])
    return subprocess.call([path, *argv], env=env)


# ──────────────────────────────────────────────
# Output helpers
# ──────────────────────────────────────────────

def _print_pairs(title: str, rows) -> None:
    print(title)
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"  {key.ljust(width)}  {value}")


def _print_table(header, rows) -> None:
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    line = lambda r: "  ".join(str(v).ljust(w) for v, w in zip(r, widths)).rstrip()
    print(line(header))
    print(line(["-" * w for w in widths]))
    for row in rows:
        print(line(row))


COMMANDS = {
    "generate": cmd_generate,
    "merge": cmd_merge,
    "apply": cmd_apply,
    "revert": cmd_revert,
    "history": cmd_history,
    "sql": cmd_sql,
    "squash": cmd_squash,
    "verify": cmd_verify,
    "status": cmd_status,
    "doctor": cmd_doctor,
    "models": cmd_models,
    "console": cmd_console,
    "connect": cmd_connect,
}
