"""Command-line interface for migrations.

Embed it in your project's manage script::

    from doveorm.migrations.cli import main
    main(directory="migrations", setup=boot)     # boot() configures the ORM and imports models

For a standalone command with project config, use ``doveorm`` (see
:mod:`doveorm.cli`) instead.  Or run this one directly::

    python -m doveorm.migrations --dir migrations --setup myapp.boot:setup makemigrations
    python -m doveorm.migrations --dir migrations --setup myapp.boot:setup migrate
"""
from __future__ import annotations

import argparse
import importlib
import logging
import sys

from ..exceptions import MigrationError
from . import commands
from .questioner import InteractiveQuestioner, Questioner


def _parser(prog=None, with_globals=True) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description="DoveORM migrations")
    if with_globals:
        p.add_argument("--dir", dest="directory", help="migrations directory")
        p.add_argument("--setup", help="module:function that configures the ORM and imports models")
    sub = p.add_subparsers(dest="command", required=True)

    mk = sub.add_parser("makemigrations", help="create a migration from model changes")
    mk.add_argument("--name", help="migration name suffix")
    mk.add_argument("--empty", action="store_true", help="create an empty migration")
    mk.add_argument("--dry-run", action="store_true", help="print the migration, write nothing")
    mk.add_argument("--merge", action="store_true", help="merge conflicting migrations")
    mk.add_argument("--check", action="store_true", help="exit 1 if a migration is missing (CI)")
    mk.add_argument("--noinput", action="store_true", help="never prompt")

    mg = sub.add_parser("migrate", help="apply migrations (or roll back to TARGET)")
    mg.add_argument("target", nargs="?", help="migration name/prefix, or 'zero'")
    mg.add_argument("--fake", action="store_true", help="record without running SQL")
    mg.add_argument("--fake-initial", action="store_true",
                    help="skip initial migrations whose tables already exist")
    mg.add_argument("--plan", action="store_true", help="show the plan, run nothing")

    rb = sub.add_parser("rollback", help="unapply the last N migrations")
    rb.add_argument("steps", nargs="?", type=int, default=1)
    rb.add_argument("--fake", action="store_true")

    sub.add_parser("showmigrations", help="list migrations and whether they are applied")

    sq = sub.add_parser("sqlmigrate", help="print the SQL of a migration")
    sq.add_argument("name")
    sq.add_argument("--backwards", action="store_true")

    sm = sub.add_parser("squashmigrations", help="squash a range of migrations into one")
    sm.add_argument("start_or_end")
    sm.add_argument("end", nargs="?")
    sm.add_argument("--name")
    sm.add_argument("--dry-run", action="store_true")
    return p


def _run_setup(spec: str):
    module, _, func = spec.partition(":")
    target = importlib.import_module(module)
    if func:
        getattr(target, func)()


def main(argv=None, *, directory: str | None = None, setup=None, models=None,
         db=None, prog: str | None = None) -> int:
    """Parse *argv* and run one command.  Returns the exit code."""
    parser = _parser(prog, with_globals=directory is None)
    args = parser.parse_args(argv)
    directory = directory or args.directory
    if not directory:
        parser.error("--dir is required")
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.WARNING, format="%(message)s")
    mig_log = logging.getLogger("doveorm.migrations")
    if mig_log.getEffectiveLevel() > logging.INFO:
        mig_log.setLevel(logging.INFO)          # show migration progress
    try:
        if callable(setup):
            setup()
        elif getattr(args, "setup", None):
            _run_setup(args.setup)
        return _dispatch(args, directory, models, db)
    except MigrationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _dispatch(args, directory, models, db) -> int:
    cmd = args.command
    if cmd == "makemigrations":
        interactive = not args.noinput and sys.stdin.isatty()
        questioner = InteractiveQuestioner() if interactive else Questioner()
        if args.check:
            try:
                commands.makemigrations(directory, models=models, check=True, questioner=Questioner(
                    defaults=_AnyDefault()))
            except MigrationError as exc:
                print(exc)
                return 1
            print("No changes detected.")
            return 0
        result = commands.makemigrations(
            directory, models=models, name=args.name, empty=args.empty, dry_run=args.dry_run,
            merge=args.merge, questioner=questioner)
        if result is None:
            print("No conflicts to merge." if args.merge else "No changes detected.")
        elif args.dry_run:
            print(result.source)
        else:
            print(f"Created {result.path}")
            for op in result.operations:
                print(f"  - {op.describe()}")
        return 0

    if cmd == "migrate":
        if args.plan:
            steps = commands.plan(directory, args.target, db=db)
            if not steps:
                print("No planned migration operations.")
            for name, backwards in steps:
                print(f"  {'UNAPPLY' if backwards else 'APPLY  '} {name}")
            return 0
        # Progress ("Applying …") is reported by the doveorm.migrations logger.
        commands.migrate(directory, args.target, db=db, fake=args.fake,
                         fake_initial=args.fake_initial)
        return 0

    if cmd == "rollback":
        if not commands.rollback(directory, args.steps, db=db, fake=args.fake):
            print("Nothing to roll back.")
        return 0

    if cmd == "showmigrations":
        rows = commands.showmigrations(directory, db=db)
        if not rows:
            print("(no migrations)")
        for name, applied in rows:
            print(f" [{'X' if applied else ' '}] {name}")
        return 0

    if cmd == "sqlmigrate":
        for stmt in commands.sqlmigrate(directory, args.name, db=db, backwards=args.backwards):
            print(stmt)
        return 0

    if cmd == "squashmigrations":
        start, end = (args.start_or_end, args.end) if args.end else (None, args.start_or_end)
        result = commands.squashmigrations(directory, end, start=start, name=args.name,
                                           dry_run=args.dry_run)
        print(result.source if args.dry_run else f"Created {result.path}")
        return 0
    raise AssertionError(cmd)  # pragma: no cover


class _AnyDefault(dict):
    """For --check: answer every NOT NULL question so detection can finish."""
    def __contains__(self, key):
        return True

    def __getitem__(self, key):
        return None

    def get(self, key, default=None):
        return None
