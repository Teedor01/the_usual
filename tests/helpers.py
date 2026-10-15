import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import db, lifecycle as L, seed
from app.adapters import ScriptedModel
from app.sessions import SessionRegistry
from app.timeutil import T0, iso

R0 = datetime(2030, 1, 1, tzinfo=timezone.utc)  # arbitrary REAL-time origin for the test clock


def naira(n: int) -> int:
    return n * 100


def D(mmdd: str, hour: int = 8) -> str:
    return seed.d(mmdd, hour)


def intent(mention, mode, phrase=None, desc=(), verb="pay", purpose=None):
    return {"verb": verb, "recipient_mention": mention,
            "descriptors": [{"type": t, "text": x} for t, x in desc],
            "amount_mode": mode, "amount_phrase": phrase, "purpose_phrase": purpose}


class ManualTime:
    def __init__(self):
        self.t = R0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


class Env:
    def __init__(self, tmp: Path, template: Path):
        self.tmp, self.template = tmp, template
        self.time = ManualTime()
        self.reg = SessionRegistry(tmp / "registry.db", tmp / "sessions", template, self.time)

    def new(self):
        return Sess(self, self.reg.create_session())


class Sess:
    def __init__(self, env: Env, sid: str):
        self.env, self.sid = env, sid
        self.handle = env.reg.resolve(sid)
        self.path = self.handle.path
        self.ctx = L.SessionContext(self.path, lambda: self.handle.virtual_now(env.time()))
        self.model = ScriptedModel()
        self._k = 0

    # clock
    def now(self):
        return self.ctx.now_fn()

    def at(self, virtual_dt: datetime):
        self.env.time.t = self.handle.created_at + (virtual_dt - T0)

    def advance(self, seconds):
        self.env.time.advance(seconds)

    # db
    def conn(self):
        return db.connect(self.path)

    def op(self, sql, params=()):
        c = self.conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute(sql, params)
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        finally:
            c.close()

    def q(self, sql, params=()):
        c = self.conn()
        try:
            return [dict(r) for r in c.execute(sql, params).fetchall()]
        finally:
            c.close()

    def count(self, table="ledger_entry", where="1=1", params=()):
        return self.q(f"SELECT COUNT(*) n FROM {table} WHERE {where}", params)[0]["n"]

    def add_evidence(self, ev_id):
        self.op("INSERT OR IGNORE INTO evidence(id, source_type, raw_text, captured_at, recorded_at)"
                " VALUES (?, 'manual_entry', ?, ?, ?)", (ev_id, f"test {ev_id}", D("10-01"), D("10-01")))

    def add_agreement(self, id, person, desc, total, mmdd, supersedes=None):
        self.add_evidence("ev_" + id)
        self.op("INSERT INTO agreement(id, person_id, description, total_kobo, agreed_at, valid_until, supersedes_id,"
                " evidence_id) VALUES (?,?,?,?,?,NULL,?,?)", (id, person, desc, total, D(mmdd), supersedes, "ev_" + id))

    def add_entry(self, id, person, typ, amount, agreement=None, target=None, mmdd="10-05", recon="reconciled",
                  evidence=None, hour=8, at=None):
        ev = evidence or "ev_" + id
        self.add_evidence(ev)
        self.op("INSERT INTO ledger_entry(id, person_id, entry_type, amount_kobo, agreement_id, target_entry_id,"
                " occurred_at, recorded_at, reconciliation, evidence_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (id, person, typ, amount, agreement, target, at or D(mmdd, hour), at or D(mmdd, hour), recon, ev))

    # instructions
    def key(self):
        self._k += 1
        return f"key-{self.sid[:6]}-{self._k}"

    def say(self, text, intent_=None, key=None, adapter=None):
        key = key or self.key()
        if adapter is None:
            out = json.dumps(intent_) if isinstance(intent_, dict) else intent_
            self.model.script[text] = out
            adapter = self.model
        return L.submit_instruction(self.ctx, text, key, adapter)

    def pick(self, view, option_id):
        return L.resolve_ask(self.ctx, view["instruction_id"], view["decision_id"], option_id)

    def commit(self, view_or_draft, key=None, confirmed=None):
        d = view_or_draft.get("draft") or view_or_draft
        status, raw = L.commit_action(self.ctx, d["action_id"], key or f"ck-{self.key()}",
                                      confirmed or d["payload_hash"])
        return status, json.loads(raw), raw

    def reload(self, view):
        c = self.conn()
        try:
            return L._latest_view(c, view["instruction_id"])
        finally:
            c.close()


def opt_ids(view):
    return [o["id"] for o in view["options"]]
