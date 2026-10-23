"""Render a migration as Python source."""
from __future__ import annotations

import datetime
import decimal
import math
import types
import uuid

from .. import fields as orm_fields
from ..exceptions import MigrationError
from ..fields import Field
from .operations.base import Operation

INDENT = "    "


class Serializer:
    """Python-literal rendering of the values that appear in operations."""

    def __init__(self):
        self.imports: set[str] = set()

    def serialize(self, value, depth: int = 0) -> str:
        if isinstance(value, Operation):
            return self._operation(value, depth)
        if isinstance(value, Field):
            return self._field(value)
        if value is None or isinstance(value, (bool, int, str, bytes)):
            return repr(value)
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                raise MigrationError(f"Cannot write {value!r} to a migration file.")
            return repr(value)
        if isinstance(value, decimal.Decimal):
            self.imports.add("import decimal")
            return f"decimal.Decimal({str(value)!r})"
        if isinstance(value, (datetime.datetime, datetime.date, datetime.time, datetime.timedelta)):
            tz = getattr(value, "tzinfo", None)
            if tz is not None and tz is not datetime.timezone.utc:
                raise MigrationError(f"Only UTC-aware datetimes can be written, got {value!r}.")
            self.imports.add("import datetime")
            return repr(value)
        if isinstance(value, uuid.UUID):
            self.imports.add("import uuid")
            return f"uuid.UUID({str(value)!r})"
        if isinstance(value, (list, tuple, set, frozenset)):
            return self._sequence(value, depth)
        if isinstance(value, dict):
            items = ", ".join(f"{self.serialize(k, depth)}: {self.serialize(v, depth)}"
                              for k, v in value.items())
            return "{" + items + "}"
        if isinstance(value, types.FunctionType):
            return self._function(value)
        raise MigrationError(
            f"Cannot write {value!r} ({type(value).__name__}) to a migration file."
        )

    def _sequence(self, value, depth):
        items = [self.serialize(v, depth) for v in (sorted(value, key=repr)
                                                   if isinstance(value, (set, frozenset)) else value)]
        if isinstance(value, tuple):
            return "(" + ", ".join(items) + ("," if len(items) == 1 else "") + ")"
        if isinstance(value, (set, frozenset)):
            return "{" + ", ".join(items) + "}" if items else "set()"
        return "[" + ", ".join(items) + "]"

    def _field(self, field: Field) -> str:
        cls, kwargs = field.deconstruct()
        if getattr(orm_fields, cls.__name__, None) is cls:
            path = f"fields.{cls.__name__}"
        else:
            if "<locals>" in cls.__qualname__:
                raise MigrationError(f"Field class {cls.__qualname__} must be importable.")
            self.imports.add(f"import {cls.__module__}")
            path = f"{cls.__module__}.{cls.__qualname__}"
        args = ", ".join(f"{k}={self.serialize(v)}" for k, v in kwargs.items())
        return f"{path}({args})"

    def _function(self, fn) -> str:
        if fn.__name__ == "<lambda>" or "<locals>" in fn.__qualname__:
            raise MigrationError(
                f"{fn.__qualname__} cannot be written to a migration file; "
                "use a module-level function."
            )
        if fn.__module__.startswith("doveorm_migrations_"):
            raise MigrationError(
                f"{fn.__qualname__} lives inside a migration file and cannot be referenced "
                "from another one; copy it into the new migration by hand."
            )
        self.imports.add(f"import {fn.__module__}")
        return f"{fn.__module__}.{fn.__qualname__}"

    def _operation(self, op: Operation, depth: int) -> str:
        pad = INDENT * (depth + 1)
        lines = [f"migrations.{type(op).__name__}("]
        for key, value in op.deconstruct().items():
            if key == "fields":
                lines.append(f"{pad}fields=[")
                for fname, field in value:
                    lines.append(f"{pad}{INDENT}({fname!r}, {self._field(field)}),")
                lines.append(f"{pad}],")
            elif key == "state_operations":
                lines.append(f"{pad}state_operations=[")
                for sub in value:
                    lines.append(f"{pad}{INDENT}{self._operation(sub, depth + 1)},")
                lines.append(f"{pad}],")
            else:
                lines.append(f"{pad}{key}={self.serialize(value, depth + 1)},")
        lines.append(f"{INDENT * depth})")
        return "\n".join(lines)


def write_migration(*, operations, dependencies, initial=False, replaces=(), version="") -> str:
    """Python source for a migration file."""
    ser = Serializer()
    body = [f"{INDENT * 2}{ser.serialize(op, 2)}," for op in operations]
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = [f'"""Generated by DoveORM {version} on {now}."""'.replace("  ", " "),
              "from doveorm import fields, migrations"]
    header.extend(sorted(ser.imports))
    lines = header + ["", "", "class Migration(migrations.Migration):", ""]
    if initial:
        lines += [f"{INDENT}initial = True", ""]
    if replaces:
        lines.append(f"{INDENT}replaces = [")
        lines += [f"{INDENT * 2}{r!r}," for r in replaces]
        lines += [f"{INDENT}]", ""]
    if dependencies:
        lines.append(f"{INDENT}dependencies = [")
        lines += [f"{INDENT * 2}{d!r}," for d in dependencies]
        lines.append(f"{INDENT}]")
    else:
        lines.append(f"{INDENT}dependencies = []")
    lines += ["", f"{INDENT}operations = ["]
    lines += body
    lines += [f"{INDENT}]", ""]
    source = "\n".join(lines)
    compile(source, "<migration>", "exec")      # never write a file that does not parse
    return source
