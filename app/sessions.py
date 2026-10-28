import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import db
from .timeutil import T0, iso, parse_iso

SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")

REGISTRY_SQL = """
CREATE TABLE IF NOT EXISTS model_usage (
  day TEXT NOT NULL,
  session_id TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0 CHECK (calls >= 0),
  PRIMARY KEY (day, session_id)
);
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK (state IN ('active','reset','expired')),
  db_filename TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  last_active_at TEXT NOT NULL,
  ended_at TEXT,
  end_reason TEXT
);
"""


class BadSessionId(Exception):
    pass


class SessionGone(Exception):
    pass


class Capacity(Exception):
    pass


@dataclass
class SessionHandle:
    session_id: str
    path: Path
    created_at: datetime  # real time at creation

    def virtual_now(self, real_now: datetime) -> datetime:
        """Session-virtual clock: T0 + real time elapsed since creation."""
        return T0 + (real_now - self.created_at)


class SessionRegistry:
    def __init__(self, registry_path, sessions_dir, template_path,
                 time_source: Callable[[], datetime], max_active=200, idle_seconds=6 * 3600):
        self.registry_path = Path(registry_path)
        self.sessions_dir = Path(sessions_dir)
        self.template_path = Path(template_path)
        self.time_source = time_source
        self.max_active = max_active
        self.idle_seconds = idle_seconds
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        c = self._conn()
        c.executescript(REGISTRY_SQL)
        c.close()

    def _conn(self):
        c = sqlite3.connect(str(self.registry_path), isolation_level=None, timeout=5)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA busy_timeout = 5000")
        return c

    # ---- creation -------------------------------------------------------------------------
    def create_session(self) -> str:
        now = self.time_source()
        c = self._conn()
        try:
            self._expire_idle(c, now)
            n = c.execute("SELECT COUNT(*) FROM sessions WHERE state='active'").fetchone()[0]
            if n >= self.max_active:
                raise Capacity("too many active sessions")
            session_id = secrets.token_hex(16)
            filename = secrets.token_hex(16) + ".db"
            dest = self.sessions_dir / filename
            self._copy_template(dest)
            c.execute("INSERT INTO sessions(session_id,state,db_filename,created_at,last_active_at)"
                      " VALUES (?, 'active', ?, ?, ?)", (session_id, filename, iso(now), iso(now)))
            return session_id
        finally:
            c.close()

    def _copy_template(self, dest: Path):
        src = sqlite3.connect(f"file:{self.template_path}?mode=ro&immutable=1", uri=True)
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        conn = db.connect(dest)
        try:
            db.audit_schema(conn)
        finally:
            conn.close()

    def _expire_idle(self, c, now):
        cutoff = iso(now - timedelta(seconds=self.idle_seconds))
        c.execute("UPDATE sessions SET state='expired', ended_at=?, end_reason='idle'"
                  " WHERE state='active' AND last_active_at < ?", (iso(now), cutoff))

    # ---- resolution -----------------------------------------------------------------------
    def resolve(self, session_id: str) -> SessionHandle:
        if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
            raise BadSessionId(session_id)
        now = self.time_source()
        c = self._conn()
        try:
            row = c.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None or row["state"] != "active":
                raise SessionGone(session_id)
            if parse_iso(row["last_active_at"]) < now - timedelta(seconds=self.idle_seconds):
                self._end(c, session_id, "expired", "idle", now)
                raise SessionGone(session_id)
            path = self.sessions_dir / row["db_filename"]
            reason = None
            if not path.exists():
                reason = "missing_file"
            else:
                try:
                    probe = db.connect(path)
                    try:
                        if not db.quick_check(probe):
                            reason = "corrupt"
                        else:
                            db.audit_schema(probe)
                    finally:
                        probe.close()
                except (sqlite3.DatabaseError, db.SchemaAuditError):
                    reason = "corrupt_or_schema_mismatch"
            if reason:
                self._end(c, session_id, "reset", reason, now)
                raise SessionGone(session_id)
            c.execute("UPDATE sessions SET last_active_at=? WHERE session_id=?", (iso(now), session_id))
            return SessionHandle(session_id, path, parse_iso(row["created_at"]))
        finally:
            c.close()

    def _end(self, c, session_id, state, reason, now):
        c.execute("UPDATE sessions SET state=?, ended_at=?, end_reason=? WHERE session_id=? AND state='active'",
                  (state, iso(now), reason, session_id))

    # ---- model-call budget ------------------------------------------------------------------
    def charge_model_call(self, session_id: str, per_session_cap: int, per_day_cap: int) -> bool:
        """Atomically reserve one model call. False (and nothing charged) if either cap is reached."""
        day = iso(self.time_source())[:10]
        c = self._conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            used_s = c.execute("SELECT COALESCE(SUM(calls),0) FROM model_usage WHERE session_id=?", (session_id,)).fetchone()[0]
            used_d = c.execute("SELECT COALESCE(SUM(calls),0) FROM model_usage WHERE day=?", (day,)).fetchone()[0]
            if used_s >= per_session_cap or used_d >= per_day_cap:
                c.execute("ROLLBACK")
                return False
            c.execute("INSERT INTO model_usage(day, session_id, calls) VALUES (?,?,1)"
                      " ON CONFLICT(day, session_id) DO UPDATE SET calls = calls + 1", (day, session_id))
            c.execute("COMMIT")
            return True
        except Exception:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise
        finally:
            c.close()

    def model_usage(self, session_id: str) -> dict:
        day = iso(self.time_source())[:10]
        c = self._conn()
        try:
            return {"session_used": c.execute("SELECT COALESCE(SUM(calls),0) FROM model_usage WHERE session_id=?", (session_id,)).fetchone()[0],
                    "day_used": c.execute("SELECT COALESCE(SUM(calls),0) FROM model_usage WHERE day=?", (day,)).fetchone()[0]}
        finally:
            c.close()

    def state_of(self, session_id: str):
        c = self._conn()
        try:
            r = c.execute("SELECT state, end_reason FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return None if r is None else (r["state"], r["end_reason"])
        finally:
            c.close()
