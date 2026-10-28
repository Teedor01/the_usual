import sqlite3
from pathlib import Path

from .schema import SCHEMA_SQL


class SchemaAuditError(Exception):
    pass


class StoreBusy(Exception):
    """Could not obtain the write lock within busy_timeout. Nothing was changed."""


def connect(path, *, readonly: bool = False, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    """Open a connection with every required PRAGMA. isolation_level=None: we manage
    transactions explicitly and always use BEGIN IMMEDIATE for writes."""
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, isolation_level=None)
    else:
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=busy_timeout_ms / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA recursive_triggers = ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
    return conn


def pragma_state(conn) -> dict:
    return {
        "foreign_keys": conn.execute("PRAGMA foreign_keys").fetchone()[0],
        "recursive_triggers": conn.execute("PRAGMA recursive_triggers").fetchone()[0],
        "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],  # 2 == FULL
        "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        "busy_timeout": conn.execute("PRAGMA busy_timeout").fetchone()[0],
    }


def begin_immediate(conn) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            raise StoreBusy(str(e)) from e
        raise


def _expected_objects():
    mem = sqlite3.connect(":memory:")
    mem.executescript(SCHEMA_SQL)
    rows = mem.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    mem.close()
    return {(r[0], r[1]): (r[2], r[3]) for r in rows}


_EXPECTED = None


def audit_schema(conn) -> None:
    """Refuse to run if any table/index/trigger differs from the expected schema (for example
    a dropped safety trigger)."""
    global _EXPECTED
    if _EXPECTED is None:
        _EXPECTED = _expected_objects()
    rows = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchall()
    actual = {(r[0], r[1]): (r[2], r[3]) for r in rows}
    missing = set(_EXPECTED) - set(actual)
    extra = set(actual) - set(_EXPECTED)
    changed = [k for k in set(actual) & set(_EXPECTED) if actual[k] != _EXPECTED[k]]
    if missing or extra or changed:
        raise SchemaAuditError(
            f"schema audit failed: missing={sorted(missing)} extra={sorted(extra)} changed={sorted(changed)}"
        )


def quick_check(conn) -> bool:
    return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def create_empty(path) -> sqlite3.Connection:
    p = Path(path)
    if p.exists():
        p.unlink()
    conn = sqlite3.connect(str(p), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA recursive_triggers = ON")
    conn.executescript(SCHEMA_SQL)
    return conn
