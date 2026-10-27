"""
DoveORM migrations — schema migrations.

Workflow (see ``doveorm --help``)::

    doveorm generate      # write the next migration from model changes
    doveorm apply         # apply pending migrations

``makemigrations`` (``doveorm generate``) compares your models with the schema described by the
existing migration files and writes the next file; ``migrate`` applies
pending files in dependency order and records them in the ``migrations``
table.  Migration files look like::

    from doveorm import fields, migrations

    class Migration(migrations.Migration):
        dependencies = ["0001_initial"]
        operations = [
            migrations.AddField("User", "phone", fields.CharField(max_length=20, nullable=True)),
        ]
"""
from .backends.base import NOT_PROVIDED
from .migration import Migration
from .operations import (
    AddField,
    AlterField,
    AlterIndexes,
    AlterModelTable,
    AlterTableOptions,
    AlterUniqueTogether,
    CreateModel,
    DeleteModel,
    Operation,
    RemoveField,
    RenameField,
    RenameModel,
    RunPython,
    RunSQL,
)
from .state import ModelState, ProjectState
from .autodetector import Autodetector
from .optimizer import optimize
from .questioner import InteractiveQuestioner, Questioner
from .loader import MigrationLoader
from .recorder import MigrationRecord, MigrationRecorder
from .executor import MigrationExecutor
from .commands import (
    MigrationFile,
    makemigrations,
    migrate,
    plan,
    rollback,
    showmigrations,
    sqlmigrate,
    squashmigrations,
)
from .legacy import Migrator, DoveORMMigration

__all__ = [
    "Migration", "Operation", "NOT_PROVIDED",
    "CreateModel", "DeleteModel", "RenameModel", "AlterModelTable",
    "AlterIndexes", "AlterUniqueTogether", "AlterTableOptions",
    "AddField", "RemoveField", "AlterField", "RenameField", "RunSQL", "RunPython",
    "ModelState", "ProjectState", "Autodetector", "optimize",
    "Questioner", "InteractiveQuestioner",
    "MigrationLoader", "MigrationRecord", "MigrationRecorder", "MigrationExecutor",
    "MigrationFile", "makemigrations", "migrate", "plan", "rollback",
    "showmigrations", "sqlmigrate", "squashmigrations",
    "Migrator", "DoveORMMigration",
]
