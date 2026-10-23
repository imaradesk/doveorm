"""
DoveORM – internal SQL helpers.

* Identifier validation (table / column / index names are interpolated
  into SQL, so they must be plain identifiers).
* Literal quoting for DDL ``DEFAULT`` clauses.
* A small lexer that understands quoted strings and comments, used to
  split scripts into statements and to translate ``?`` placeholders
  without touching string literals.
"""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from typing import Iterator

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_IDENTIFIER_LENGTH = 64   # MySQL limit; also a sane cap for SQLite


def validate_identifier(name, kind: str = "identifier") -> str:
    """Return *name* if it is a safe SQL identifier, else raise ``ValueError``."""
    if not isinstance(name, str) or not IDENTIFIER_RE.match(name):
        raise ValueError(
            f"Invalid {kind}: {name!r}. Identifiers must match [A-Za-z_][A-Za-z0-9_]*."
        )
    if len(name) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(
            f"Invalid {kind}: {name!r} is longer than {MAX_IDENTIFIER_LENGTH} characters."
        )
    return name


# Words that are reserved (or need quoting as names) in SQLite, MySQL / MariaDB,
# PostgreSQL or standard SQL.  Quoting a word that one backend would have
# accepted bare is harmless, so this is deliberately a superset.
RESERVED_WORDS = frozenset("""
ABORT ACCESSIBLE ACTION ADD AFTER ALL ALTER ALWAYS ANALYSE ANALYZE AND ANY ARRAY AS ASC
ASENSITIVE ASYMMETRIC AT ATTACH AUTHORIZATION AUTOINCREMENT BEFORE BEGIN BETWEEN BIGINT
BINARY BLOB BOTH BY CALL CASCADE CASE CAST CHANGE CHAR CHARACTER CHECK COLLATE COLLATION
COLUMN COLUMNS COMMIT CONCURRENTLY CONDITION CONFLICT CONSTRAINT CONTINUE CONVERT CREATE
CROSS CUBE CUME_DIST CURRENT CURRENT_CATALOG CURRENT_DATE CURRENT_ROLE CURRENT_SCHEMA
CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER CURSOR DATABASE DATABASES DAY_HOUR
DAY_MICROSECOND DAY_MINUTE DAY_SECOND DEC DECIMAL DECLARE DEFAULT DEFERRABLE DEFERRED
DELAYED DELETE DENSE_RANK DESC DESCRIBE DETACH DETERMINISTIC DISTINCT DISTINCTROW DIV DO
DOUBLE DROP DUAL EACH ELSE ELSEIF EMPTY ENCLOSED END ESCAPE ESCAPED EXCEPT EXCLUDE
EXCLUSIVE EXISTS EXIT EXPLAIN FAIL FALSE FETCH FILTER FIRST_VALUE FLOAT FLOAT4 FLOAT8
FOLLOWING FOR FORCE FOREIGN FREEZE FROM FULL FULLTEXT FUNCTION GENERATED GET GLOB GRANT
GROUP GROUPING GROUPS HAVING HIGH_PRIORITY HOUR_MICROSECOND HOUR_MINUTE HOUR_SECOND IF
IGNORE ILIKE IMMEDIATE IN INDEX INDEXED INFILE INITIALLY INNER INOUT INSENSITIVE INSERT
INSTEAD INT INT1 INT2 INT3 INT4 INT8 INTEGER INTERSECT INTERVAL INTO IO_AFTER_GTIDS
IO_BEFORE_GTIDS IS ISNULL ITERATE JOIN JSON_TABLE KEY KEYS KILL LAG LAST_VALUE LATERAL
LEAD LEADING LEAVE LEFT LIKE LIMIT LINEAR LINES LOAD LOCALTIME LOCALTIMESTAMP LOCK LONG
LONGBLOB LONGTEXT LOOP LOW_PRIORITY MASTER_BIND MASTER_SSL_VERIFY_SERVER_CERT MATCH
MATERIALIZED MAXVALUE MEDIUMBLOB MEDIUMINT MEDIUMTEXT MIDDLEINT MINUTE_MICROSECOND
MINUTE_SECOND MOD MODIFIES NATURAL NO NO_WRITE_TO_BINLOG NOT NOTHING NOTNULL NTH_VALUE
NTILE NULL NULLS NUMERIC OF OFFSET OFFSET ON ONLY OPTIMIZE OPTIMIZER_COSTS OPTION
OPTIONALLY OR ORDER OTHERS OUT OUTER OUTFILE OVER OVERLAPS PARTITION PERCENT_RANK PLACING
PLAN PRAGMA PRECEDING PRECISION PRIMARY PROCEDURE PURGE QUERY RAISE RANGE RANK READ READS
READ_WRITE REAL RECURSIVE REFERENCES REGEXP REINDEX RELEASE RENAME REPEAT REPLACE REQUIRE
RESIGNAL RESTRICT RETURN RETURNING REVOKE RIGHT RLIKE ROLLBACK ROW ROWS ROW_NUMBER
SAVEPOINT SCHEMA SCHEMAS SECOND_MICROSECOND SELECT SENSITIVE SEPARATOR SESSION_USER SET
SHOW SIGNAL SIMILAR SMALLINT SOME SPATIAL SPECIFIC SQL SQLEXCEPTION SQLSTATE SQLWARNING
SQL_BIG_RESULT SQL_CALC_FOUND_ROWS SQL_SMALL_RESULT SSL STARTING STORED STRAIGHT_JOIN
SYMMETRIC SYSTEM SYSTEM_USER TABLE TABLESAMPLE TEMP TEMPORARY TERMINATED THEN TIES TINYBLOB
TINYINT TINYTEXT TO TRAILING TRANSACTION TRIGGER TRUE UNBOUNDED UNDO UNION UNIQUE UNLOCK
UNSIGNED UPDATE USAGE USE USER USING UTC_DATE UTC_TIME UTC_TIMESTAMP VACUUM VALUES
VARBINARY VARCHAR VARCHARACTER VARIADIC VARYING VERBOSE VIEW VIRTUAL WHEN WHERE WHILE
WINDOW WITH WITHOUT WRITE XOR YEAR_MONTH ZEROFILL
""".split())


def quote_name(name: str, quote: str = '"', *, preserve_case: bool = False) -> str:
    """Quote an already-validated identifier only when it needs quoting.

    Reserved words are always quoted.  With ``preserve_case=True``
    (PostgreSQL, which folds bare names to lower case) names containing
    upper-case letters are quoted too.  Any other name is returned bare,
    so ordinary schemas produce exactly the same SQL as before.
    """
    if name.upper() in RESERVED_WORDS or (preserve_case and name != name.lower()):
        return f"{quote}{name}{quote}"
    return name


def index_name(prefix: str, table: str, columns) -> str:
    """Build a deterministic index name that never exceeds the identifier limit."""
    name = f"{prefix}_{table}_{'_'.join(columns)}"
    if len(name) <= MAX_IDENTIFIER_LENGTH:
        return name
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
    return f"{name[:MAX_IDENTIFIER_LENGTH - 11]}_{digest}"


def quote_literal(value, *, backslash_escapes: bool = False) -> str | None:
    """Render a Python value as a SQL literal for a DDL ``DEFAULT`` clause.

    Returns ``None`` when the value cannot be represented safely, in which
    case the caller should omit the DEFAULT clause (the ORM still applies
    the default in Python on insert).
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return repr(value)
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "X'" + bytes(value).hex() + "'"
    if isinstance(value, str):
        if "\x00" in value:
            return None
        escaped = value.replace("'", "''")
        if backslash_escapes:
            escaped = escaped.replace("\\", "\\\\")
        return "'" + escaped + "'"
    return None


# ──────────────────────────────────────────────
# Lexer
# ──────────────────────────────────────────────

CODE, LITERAL, COMMENT = "code", "literal", "comment"


def scan(sql: str, *, backslash_escapes: bool = False) -> Iterator[tuple[str, str]]:
    """Yield ``(kind, text)`` segments of *sql*.

    *kind* is ``CODE``, ``LITERAL`` (quoted string / quoted identifier) or
    ``COMMENT``.  Concatenating all texts reproduces *sql* exactly.
    """
    n = len(sql)
    i = start = 0
    while i < n:
        c = sql[i]
        if c in ("'", '"', "`"):
            if i > start:
                yield CODE, sql[start:i]
            j = i + 1
            while j < n:
                d = sql[j]
                if backslash_escapes and d == "\\" and c != "`":
                    j += 2
                    continue
                if d == c:
                    if j + 1 < n and sql[j + 1] == c:   # doubled quote = escaped quote
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            j = min(j, n)
            yield LITERAL, sql[i:j]
            i = start = j
            continue
        if c == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            j = n if end == -1 else end + 1
        elif c == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            j = n if end == -1 else end + 2
        else:
            i += 1
            continue
        if i > start:
            yield CODE, sql[start:i]
        yield COMMENT, sql[i:j]
        i = start = j
    if start < n:
        yield CODE, sql[start:]


def split_statements(sql: str, *, backslash_escapes: bool = False) -> list[str]:
    """Split a script on top-level ``;`` (ignoring ``;`` in literals/comments).

    Statements that contain only whitespace and comments are dropped.
    """
    statements: list[str] = []
    buf: list[str] = []
    has_code = False
    for kind, text in scan(sql, backslash_escapes=backslash_escapes):
        if kind != CODE:
            buf.append(text)
            has_code = has_code or kind == LITERAL
            continue
        pieces = text.split(";")
        for idx, piece in enumerate(pieces):
            buf.append(piece)
            has_code = has_code or bool(piece.strip())
            if idx < len(pieces) - 1:          # a ';' followed this piece
                if has_code:
                    statements.append("".join(buf).strip())
                buf, has_code = [], False
    if has_code:
        statements.append("".join(buf).strip())
    return statements


def first_keyword(sql: str) -> str:
    """Return the first SQL keyword of *sql* (upper-cased), skipping comments."""
    for kind, text in scan(sql):
        if kind == CODE:
            stripped = text.strip()
            if stripped:
                return stripped.split(None, 1)[0].upper()
        elif kind == LITERAL:
            return ""
    return ""


def translate_qmark(sql: str, placeholder: str, *, backslash_escapes: bool = False) -> str:
    """Translate ``?`` placeholders into a ``%s``-style paramstyle.

    Only ``?`` in code segments are replaced.  Because pyformat drivers
    run ``sql % params``, every literal ``%`` (anywhere in the string)
    is doubled.  If the statement contains no ``?`` placeholder at all it
    is assumed to already use the native paramstyle and is returned as-is.
    """
    segments = list(scan(sql, backslash_escapes=backslash_escapes))
    if not any(kind == CODE and "?" in text for kind, text in segments):
        return sql
    out = []
    for kind, text in segments:
        text = text.replace("%", "%%")
        if kind == CODE:
            text = text.replace("?", placeholder)
        out.append(text)
    return "".join(out)
