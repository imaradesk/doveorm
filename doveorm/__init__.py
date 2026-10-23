"""
DoveORM – a lightweight ORM for Python.

Supports SQLite (default), MySQL and PostgreSQL backends.

Quick start (SQLite):
    from doveorm import Model, Database
    db = Database("app.db")

Quick start (MySQL):
    from doveorm import Model, MySQLDatabase
    db = MySQLDatabase(host="localhost", user="app",
                       password="secret", database="myapp")

Quick start (PostgreSQL):
    from doveorm import Model, PostgreSQLDatabase
    db = PostgreSQLDatabase(host="localhost", user="app",
                            password="secret", database="myapp")

Requires Python 3.9+.  MySQL support needs ``pip install "doveorm[mysql]"`` (mysqlclient);
PostgreSQL support needs ``pip install "doveorm[postgres]"`` (psycopg 3, or psycopg2).
"""

from .database import Database, MySQLDatabase, PostgreSQLDatabase
from .model import Model, table
from .query import QuerySet, Q
from .exceptions import (
    ORMError,
    ConfigurationError,
    ValidationError,
    DoesNotExist,
    UnsafeQueryError,
    MigrationError,
    ConcurrencyError,
    TransactionError,
    QueryTimeout,
)
from .config import (
    configure,
    configure_from_dict,
    configure_from_env,
    configure_from_file,
    get_setting,
    get_database,
    is_configured,
    reset as reset_config,
)
from .fields import (
    Field,
    # Auto-increment PKs
    AutoField,
    BigAutoField,
    SmallAutoField,
    # Numeric
    IntegerField,
    BigIntegerField,
    SmallIntegerField,
    PositiveIntegerField,
    PositiveSmallIntegerField,
    PositiveBigIntegerField,
    FloatField,
    DecimalField,
    # String / text
    CharField,
    TextField,
    SlugField,
    EmailField,
    URLField,
    IPAddressField,
    GenericIPAddressField,
    # Boolean
    BooleanField,
    # Date / time
    DateField,
    DateTimeField,
    TimeField,
    DurationField,
    # UUID
    UUIDField,
    # JSON
    JSONField,
    # Binary
    BinaryField,
    # File / image
    FileField,
    ImageField,
    FilePathField,
    # Relationships
    ForeignKey,
    OneToOneField,
)
from .relationships import HasMany, HasOne, BelongsTo, ManyToMany, ManyToManyManager
from .migrations import Migrator

__all__ = [
    "Database",
    "MySQLDatabase",
    "PostgreSQLDatabase",
    "Model",
    "table",
    "QuerySet",
    "Q",
    "Migrator",
    # Exceptions
    "ORMError",
    "ConfigurationError",
    "ValidationError",
    "DoesNotExist",
    "UnsafeQueryError",
    "MigrationError",
    "ConcurrencyError",
    "TransactionError",
    "QueryTimeout",
    # Configuration
    "configure",
    "configure_from_dict",
    "configure_from_env",
    "configure_from_file",
    "get_setting",
    "get_database",
    "is_configured",
    "reset_config",
    # Fields – auto-increment PKs
    "Field",
    "AutoField",
    "BigAutoField",
    "SmallAutoField",
    # Fields – numeric
    "IntegerField",
    "BigIntegerField",
    "SmallIntegerField",
    "PositiveIntegerField",
    "PositiveSmallIntegerField",
    "PositiveBigIntegerField",
    "FloatField",
    "DecimalField",
    # Fields – string / text
    "CharField",
    "TextField",
    "SlugField",
    "EmailField",
    "URLField",
    "IPAddressField",
    "GenericIPAddressField",
    # Fields – boolean
    "BooleanField",
    # Fields – date / time
    "DateField",
    "DateTimeField",
    "TimeField",
    "DurationField",
    # Fields – UUID
    "UUIDField",
    # Fields – JSON
    "JSONField",
    # Fields – binary
    "BinaryField",
    # Fields – file / image
    "FileField",
    "ImageField",
    "FilePathField",
    # Fields – relationships
    "ForeignKey",
    "OneToOneField",
    # Relationships
    "HasMany",
    "HasOne",
    "BelongsTo",
    "ManyToMany",
    "ManyToManyManager",
]

__version__ = "0.5.0"