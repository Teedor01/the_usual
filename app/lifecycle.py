import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import db, engine, repo
from .adapters import InferenceUnavailable, OutputTruncated
from .intent import IntentError, validate_intent
from .money import fmt_naira
from .timeutil import iso, parse_iso

MAX_MODEL_ATTEMPTS = 2 


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "", extra=None):
        super().__init__(code)
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}


@dataclass
class SessionContext:
    path: Path
    now_fn: Callable[[], datetime]
    session_id: str = ""


def jd(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def open_db(ctx: SessionContext):
    conn = db.connect(ctx.path)
    db.audit_schema(conn)
    return conn


class Tx:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK. Failing to get the lock raises ApiError 503."""
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        try:
            db.begin_immediate(self.conn)
        except db.StoreBusy as e:
            raise ApiError(503, "store_busy", "The store is busy; nothing was changed. Retry.") from e
        return self.conn

    def __exit__(self, et, ev, tb):
        if et is None:
            self.conn.execute("COMMIT")
        else:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
        return False


# ---- model parsing (outside any write transaction) ----------------------------------------------
def run_parse(adapter, text: str) -> dict:
    calls, raw, err = 0, None, None
    for _ in range(MAX_MODEL_ATTEMPTS):
        calls += 1
        try:
            raw = adapter.parse(text)
        except InferenceUnavailable as e:
            err = f"unavailable:{e}"
            continue
        except OutputTruncated as e:  
            raw, err = None, f"truncated:{e}"
            continue
        try:
            intent = validate_intent(raw, text)
            return {"status": "ok", "intent": intent.model_dump(), "raw": raw, "calls": calls,
                    "model_id": getattr(adapter, "model_id", "unknown")}
        except IntentError as e:
            err = str(e)
            continue
    status = "unavailable" if err and err.startswith("unavailable") else "invalid"
    return {"status": status, "intent": None, "raw": raw if isinstance(raw, (str, type(None))) else jd(raw),
            "error": err, "calls": calls, "model_id": getattr(adapter, "model_id", "unknown")}


# ---- helpers ------------------------------------------------------------------------------------
def _next_seq(conn, instruction_id):
    r = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM decision WHERE instruction_id=?", (instruction_id,)).fetchone()
    return r[0]


def _transition(conn, action_id, to_state, actor, reason, now, ledger_entry_id=None):
    cur = conn.execute("SELECT state FROM staged_action WHERE id=?", (action_id,)).fetchone()["state"]
    seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM action_event WHERE action_id=?", (action_id,)).fetchone()[0]
    conn.execute("UPDATE staged_action SET state=?, state_reason=?, finalized_at=?, ledger_entry_id=? WHERE id=?",
                 (to_state, reason, iso(now), ledger_entry_id, action_id))
    conn.execute("INSERT INTO action_event(action_id, seq, from_state, to_state, actor, reason, at) VALUES (?,?,?,?,?,?,?)",
                 (action_id, seq, cur, to_state, actor, reason, iso(now)))


def _decision_view(instruction_id, decision_id, seq, res: engine.EngineResult, parse, action=None):
    return {"instruction_id": instruction_id, "decision_id": decision_id, "seq": seq,
            "outcome": res.outcome, "reason": res.reason, "message": res.message,
            "options": res.options, "stale_selection": res.stale, "details": res.details,
            "draft": action, "parse": {"status": parse.get("status"), "model_id": parse.get("model_id"),
                                       "calls": parse.get("calls")}}


def _decide_and_store(conn, ctx, instruction_id):
    row = conn.execute("SELECT * FROM instruction WHERE id=?", (instruction_id,)).fetchone()
    resolved = json.loads(row["resolved_intent_json"])
    now = ctx.now_fn()
    res = engine.decide(conn, now, resolved)
    resolved["selections"] = res.selections
    seq = _next_seq(conn, instruction_id)
    decision_id = uuid.uuid4().hex
    action = None
    new_action = False
    if res.outcome == "STAGE":
        d = res.draft
        now_iso = iso(now)
        for r in conn.execute("SELECT id, expires_at FROM staged_action WHERE payload_hash=? AND state='STAGED'",
                              (d["payload_hash"],)).fetchall():
            if r["expires_at"] <= now_iso:
                _transition(conn, r["id"], "EXPIRED", "system", "ttl_elapsed", now)
        live = conn.execute("SELECT id, expires_at FROM staged_action WHERE payload_hash=? AND state='STAGED'",
                            (d["payload_hash"],)).fetchone()
        if live:
            action_id, expires_at = live["id"], live["expires_at"]
        else:
            action_id = uuid.uuid4().hex
            expires_at = iso(now + timedelta(seconds=engine_ttl(conn)))
            new_action = True
        action = dict(d, action_id=action_id, expires_at=expires_at, state="STAGED")
    payload = {"options": res.options, "details": res.details, "message": res.message,
               "draft": action, "stale_selection": res.stale}
    conn.execute("INSERT INTO decision(id, instruction_id, seq, outcome, reason_code, rule_version, payload_json, created_at)"
                 " VALUES (?,?,?,?,?,?,?,?)",
                 (decision_id, instruction_id, seq, res.outcome, res.reason, engine.RULE_VERSION, jd(payload), iso(now)))
    if new_action:
        d = res.draft
        conn.execute(
            "INSERT INTO staged_action(id, instruction_id, decision_id, action_type, recipient_person_id, amount_kobo,"
            " currency, source, agreement_id, stated_amount_kobo, amount_resolution_json, ack_duplicate_json,"
            " ack_unlinked_json, override_over_kobo, purpose_text, computation_json, evidence_json,"
            " resolved_intent_json, payload_hash, read_set_digest, rule_version, policy_version, created_at,"
            " expires_at, state) VALUES (?,?,?,'PAYMENT',?,?,'NGN',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'STAGED')",
            (action_id, instruction_id, decision_id, d["recipient_id"], d["amount_kobo"], d["source"],
             d["agreement_id"], d["stated_amount_kobo"], jd(d["amount_resolution"]), jd(d["ack_duplicate_ids"]),
             jd(d["ack_unlinked_ids"]), d["override_over_kobo"], d["purpose_text"], jd(d["computation"]),
             jd(d["evidence"]), jd(resolved), d["payload_hash"], res.digest, engine.RULE_VERSION,
             d["policy_version"], iso(now), expires_at))
        conn.execute("INSERT INTO action_event(action_id, seq, from_state, to_state, actor, reason, at)"
                     " VALUES (?,1,NULL,'STAGED','system','staged',?)", (action_id, iso(now)))
    status = "waiting" if res.outcome == "WAIT" else "decided"
    conn.execute("UPDATE instruction SET resolved_intent_json=?, status=? WHERE id=?",
                 (jd(resolved), status, instruction_id))
    return _decision_view(instruction_id, decision_id, seq, res, resolved.get("parse", {}), action)


def engine_ttl(conn):
    return repo.load_policy(conn).draft_ttl_seconds


def _latest_view(conn, instruction_id):
    row = conn.execute("SELECT * FROM decision WHERE instruction_id=? ORDER BY seq DESC LIMIT 1", (instruction_id,)).fetchone()
    ins = conn.execute("SELECT * FROM instruction WHERE id=?", (instruction_id,)).fetchone()
    if row is None:
        return {"instruction_id": instruction_id, "outcome": None, "status": ins["status"]}
    p = json.loads(row["payload_json"])
    parse = json.loads(ins["resolved_intent_json"]).get("parse", {})
    draft = p.get("draft")
    if draft:
        a = conn.execute("SELECT state, expires_at FROM staged_action WHERE id=?", (draft["action_id"],)).fetchone()
        draft = dict(draft, state=a["state"], expires_at=a["expires_at"])
    return {"instruction_id": instruction_id, "decision_id": row["id"], "seq": row["seq"], "outcome": row["outcome"],
            "reason": row["reason_code"], "message": p["message"], "options": p["options"],
            "stale_selection": p.get("stale_selection", False), "details": p["details"], "draft": draft,
            "parse": {"status": parse.get("status"), "model_id": parse.get("model_id"), "calls": parse.get("calls")},
            "status": ins["status"]}


# ---- submit / retry --------------------------------------------------------------------------------
def submit_instruction(ctx, text: str, idem_key: str, adapter):
    conn = open_db(ctx)
    try:
        existing = conn.execute("SELECT * FROM instruction WHERE idempotency_key=?", (idem_key,)).fetchone()
        if existing:
            return _existing(ctx, conn, existing, text, adapter)
        parse = run_parse(adapter, text) 
        try:
            with Tx(conn):
                existing = conn.execute("SELECT * FROM instruction WHERE idempotency_key=?", (idem_key,)).fetchone()
                if existing:
                    pass
                else:
                    iid = uuid.uuid4().hex
                    resolved = {"raw_text": text, "instruction_id": iid, "parse": _parse_meta(parse),
                                "intent": parse.get("intent"), "selections": []}
                    conn.execute("INSERT INTO instruction(id, idempotency_key, raw_text, created_at, parsed_intent_json,"
                                 " parse_valid, model_id, status, resolved_intent_json) VALUES (?,?,?,?,?,?,?,?,?)",
                                 (iid, idem_key, text, iso(ctx.now_fn()), parse.get("raw") if isinstance(parse.get("raw"), str) else None,
                                  1 if parse["status"] == "ok" else 0, parse.get("model_id"), "decided", jd(resolved)))
                    return _decide_and_store(conn, ctx, iid)
        except sqlite3.IntegrityError:
            pass
        existing = conn.execute("SELECT * FROM instruction WHERE idempotency_key=?", (idem_key,)).fetchone()
        return _existing(ctx, conn, existing, text, adapter)
    finally:
        conn.close()


def _parse_meta(parse):
    return {"status": parse["status"], "model_id": parse.get("model_id"), "calls": parse.get("calls"),
            "error": parse.get("error")}


def _existing(ctx, conn, row, text, adapter):
    if row["raw_text"] != text:
        raise ApiError(409, "key_reuse_mismatch", "That idempotency key was used with different text.")
    if row["status"] == "waiting":
        return retry_instruction(ctx, row["id"], adapter, _conn=conn)
    return _latest_view(conn, row["id"])


def retry_instruction(ctx, instruction_id: str, adapter, _conn=None):
    """Explicit user-initiated retry of a WAIT. Re-parses only if inference was the blocker."""
    conn = _conn or open_db(ctx)
    try:
        row = conn.execute("SELECT * FROM instruction WHERE id=?", (instruction_id,)).fetchone()
        if row is None:
            raise ApiError(404, "unknown_instruction")
        if row["status"] != "waiting":
            return _latest_view(conn, instruction_id)
        resolved = json.loads(row["resolved_intent_json"])
        parse = None
        if resolved.get("parse", {}).get("status") == "unavailable":
            parse = run_parse(adapter, row["raw_text"])
        with Tx(conn):
            row = conn.execute("SELECT * FROM instruction WHERE id=?", (instruction_id,)).fetchone()
            if row["status"] != "waiting":
                return _latest_view(conn, instruction_id)
            resolved = json.loads(row["resolved_intent_json"])
            if parse is not None:
                resolved["parse"], resolved["intent"] = _parse_meta(parse), parse.get("intent")
                conn.execute("UPDATE instruction SET resolved_intent_json=?, parse_valid=?, parsed_intent_json=?, model_id=? WHERE id=?",
                             (jd(resolved), 1 if parse["status"] == "ok" else 0,
                              parse.get("raw") if isinstance(parse.get("raw"), str) else None, parse.get("model_id"), instruction_id))
            return _decide_and_store(conn, ctx, instruction_id)
    finally:
        if _conn is None:
            conn.close()


# ---- ASK resolution ----------------------------------------------------------------------------------
def resolve_ask(ctx, instruction_id: str, ask_decision_id: str, option_id: str):
    conn = open_db(ctx)
    try:
        with Tx(conn):
            ins = conn.execute("SELECT * FROM instruction WHERE id=?", (instruction_id,)).fetchone()
            if ins is None:
                raise ApiError(404, "unknown_instruction")
            if ins["status"] == "cancelled":
                raise ApiError(409, "instruction_cancelled")
            latest = conn.execute("SELECT * FROM decision WHERE instruction_id=? ORDER BY seq DESC LIMIT 1",
                                  (instruction_id,)).fetchone()
            if latest is None or latest["id"] != ask_decision_id:
                raise ApiError(409, "stale_decision", "That question has been superseded.")
            if latest["outcome"] != "ASK":
                raise ApiError(409, "not_an_ask", "Only an open question accepts a selection.")
            options = json.loads(latest["payload_json"])["options"]
            chosen = next((o for o in options if o["id"] == option_id), None)
            if chosen is None:
                raise ApiError(422, "unknown_option", "That option was not offered.")
            if chosen["kind"] == "cancel":
                conn.execute("UPDATE instruction SET status='cancelled' WHERE id=?", (instruction_id,))
                return {"instruction_id": instruction_id, "outcome": None, "status": "cancelled",
                        "message": "Cancelled. Nothing was prepared or paid."}
            resolved = json.loads(ins["resolved_intent_json"])
            resolved.setdefault("selections", []).append({"kind": chosen["kind"], "params": chosen["params"]})
            conn.execute("UPDATE instruction SET resolved_intent_json=? WHERE id=?", (jd(resolved), instruction_id))
            return _decide_and_store(conn, ctx, instruction_id)
    finally:
        conn.close()


# ---- cancel ---------------------------------------------------------------------------------------------
def cancel_action(ctx, action_id: str):
    conn = open_db(ctx)
    try:
        with Tx(conn):
            a = conn.execute("SELECT state FROM staged_action WHERE id=?", (action_id,)).fetchone()
            if a is None:
                raise ApiError(404, "unknown_action")
            if a["state"] != "STAGED":
                raise ApiError(409, {"COMMITTED": "already_committed", "CANCELLED": "cancelled",
                                     "EXPIRED": "expired", "INVALIDATED": "invalidated"}[a["state"]])
            _transition(conn, action_id, "CANCELLED", "user", "user_cancelled", ctx.now_fn())
            return {"action_id": action_id, "state": "CANCELLED"}
    finally:
        conn.close()


# ---- commit -------------------------------------------------------------------------------------------------
def commit_action(ctx, action_id: str, commit_key: str, confirmed_hash: str):
    """Returns (status_code, raw_json_text). Same key + same hash replays the stored bytes."""
    conn = open_db(ctx)
    try:
        with Tx(conn):
            return _commit_inner(conn, ctx, action_id, commit_key, confirmed_hash)
    finally:
        conn.close()


def _err(status, code, message="", **extra):
    return status, jd({"status": "error", "error": code, "message": message, **extra})


def _commit_inner(conn, ctx, action_id, commit_key, confirmed_hash):
    now = ctx.now_fn()
    prev = conn.execute("SELECT * FROM commit_attempt WHERE commit_key=?", (commit_key,)).fetchone()
    if prev:
        if prev["action_id"] == action_id and prev["payload_hash"] == confirmed_hash:
            return prev["status_code"], prev["response_json"]
        return _err(409, "key_reuse_mismatch", "That commit key was used for a different confirmation.")
    a = conn.execute("SELECT * FROM staged_action WHERE id=?", (action_id,)).fetchone()
    if a is None:
        return _err(404, "unknown_action")
    st = a["state"]
    if st == "COMMITTED":
        return _err(409, "already_committed")
    if st == "CANCELLED":
        return _err(409, "cancelled")
    if st == "EXPIRED":
        return _err(410, "expired", "This draft expired. Please ask again.")
    if st == "INVALIDATED":
        return _err(409, "invalidated", a["state_reason"] or "")
    if iso(now) >= a["expires_at"]:
        _transition(conn, action_id, "EXPIRED", "system", "ttl_elapsed", now)
        return _err(410, "expired", "This draft expired. Please ask again.")
    if confirmed_hash != a["payload_hash"]:
        return _err(409, "payload_mismatch", "The confirmation does not match this draft.")
    # Re-decide against LIVE state using the stored resolved intent and structured selections.
    resolved = json.loads(a["resolved_intent_json"])
    res = engine.decide(conn, now, resolved)
    fail = None
    if res.outcome != "STAGE":
        fail = f"revalidation_{res.outcome.lower()}:{res.reason}"
    elif res.draft["payload_hash"] != a["payload_hash"]:
        fail = "payload_changed"
    elif res.digest != a["read_set_digest"]:
        fail = "read_set_changed"
    if fail:
        _transition(conn, action_id, "INVALIDATED", "system", fail, now)
        return _err(409, "invalidated", fail)
    d = res.draft
    ev_id = "ev_" + uuid.uuid4().hex[:12]
    conn.execute("INSERT INTO evidence(id, source_type, raw_text, captured_at, recorded_at) VALUES (?,?,?,?,?)",
                 (ev_id, "pasted_message", resolved["raw_text"], iso(now), iso(now)))
    entry_id = "PAY-" + uuid.uuid4().hex[:10]
    conn.execute("INSERT INTO ledger_entry(id, person_id, entry_type, amount_kobo, agreement_id, target_entry_id,"
                 " occurred_at, recorded_at, reconciliation, override_over_kobo, source_action_id, evidence_id)"
                 " VALUES (?,?,'payment_out',?,?,NULL,?,?,'reconciled',?,?,?)",
                 (entry_id, d["recipient_id"], d["amount_kobo"], d["agreement_id"], iso(now), iso(now),
                  d["override_over_kobo"], action_id, ev_id))
    _transition(conn, action_id, "COMMITTED", "user", "confirmed", now, ledger_entry_id=entry_id)
    body = jd({"status": "committed", "action_id": action_id, "ledger_entry_id": entry_id,
               "recipient": d["recipient_name"], "amount_kobo": d["amount_kobo"],
               "amount_display": fmt_naira(d["amount_kobo"]), "agreement_id": d["agreement_id"],
               "override_over_kobo": d["override_over_kobo"], "committed_at": iso(now),
               "simulated": True})
    conn.execute("INSERT INTO commit_attempt(commit_key, action_id, payload_hash, status_code, response_json, created_at)"
                 " VALUES (?,?,?,?,?,?)", (commit_key, action_id, confirmed_hash, 200, body, iso(now)))
    return 200, body


# ---- read-only views --------------------------------------------------------------------------------------------
def ledger_view(ctx):
    from . import domain as D
    conn = open_db(ctx)
    try:
        now = ctx.now_fn()
        out = {"now": iso(now), "people": [], "entries": []}
        for p in repo.load_persons(conn):
            ags, es = repo.load_agreements(conn, p.id), repo.load_entries(conn, p.id)
            eff = D.effective_amounts(es)
            out["people"].append({"id": p.id, "name": p.legal_name, "role": p.role, "payee_status": p.payee_status,
                                  "agreements": [{"id": h.id, "description": h.description, "total_kobo": h.total_kobo,
                                                  "outstanding_kobo": D.chain_outstanding(h, ags, es, eff)}
                                                 for h in D.current_heads(ags, iso(now))]})
            for e in es:
                out["entries"].append({"id": e.id, "person": p.legal_name, "type": e.entry_type,
                                       "amount_kobo": e.amount_kobo, "agreement_id": e.agreement_id,
                                       "occurred_at": e.occurred_at, "reconciliation": e.reconciliation})
        out["entries"].sort(key=lambda e: e["occurred_at"], reverse=True)
        return out
    finally:
        conn.close()


def _pending_status(latest, action_state, lapsed):
    """Which incomplete state an instruction is in, from stored state only. None = nothing left to do."""
    if latest.get("status") == "cancelled" or latest.get("outcome") is None:
        return None
    o = latest["outcome"]
    if o == "STAGE":
        if action_state == "STAGED":
            return "expired" if lapsed else "awaiting_confirmation"
        return {"EXPIRED": "expired", "INVALIDATED": "invalidated"}.get(action_state)  # COMMITTED/CANCELLED: done
    return {"ASK": "needs_answer", "REFUSE": "refused", "WAIT": "waiting"}[o]


def activity_view(ctx, limit: int = 30):
    """Read-only history of what the system did, taken only from rows that already exist:
    instruction, decision (immutable), staged_action and action_event. Writes nothing, decides nothing."""
    conn = open_db(ctx)
    try:
        now_iso = iso(ctx.now_fn())
        out = {"now": now_iso, "instructions": []}
        rows = conn.execute("SELECT * FROM instruction ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        for ins in rows:
            iid = ins["id"]
            resolved = json.loads(ins["resolved_intent_json"])
            decisions = []
            for d in conn.execute("SELECT * FROM decision WHERE instruction_id=? ORDER BY seq", (iid,)).fetchall():
                p = json.loads(d["payload_json"])
                decisions.append({"id": d["id"], "seq": d["seq"], "outcome": d["outcome"], "reason": d["reason_code"],
                                  "rule_version": d["rule_version"], "at": d["created_at"], "message": p.get("message"),
                                  "options": [{"id": o["id"], "kind": o["kind"], "label": o["label"]} for o in p.get("options", [])],
                                  "stale_selection": p.get("stale_selection", False),
                                  "action_id": (p.get("draft") or {}).get("action_id")})
            actions = []
            for a in conn.execute("SELECT * FROM staged_action WHERE instruction_id=? ORDER BY created_at", (iid,)).fetchall():
                events = [{"seq": e["seq"], "from_state": e["from_state"], "to_state": e["to_state"], "actor": e["actor"],
                           "reason": e["reason"], "at": e["at"]}
                          for e in conn.execute("SELECT * FROM action_event WHERE action_id=? ORDER BY seq", (a["id"],)).fetchall()]
                actions.append({"id": a["id"], "decision_id": a["decision_id"], "state": a["state"], "state_reason": a["state_reason"],
                                "amount_kobo": a["amount_kobo"], "recipient_id": a["recipient_person_id"],
                                "expires_at": a["expires_at"], "ledger_entry_id": a["ledger_entry_id"],
                                "lapsed": a["state"] == "STAGED" and a["expires_at"] <= now_iso, "events": events})
            latest = _latest_view(conn, iid)
            rid = (latest.get("draft") or {}).get("recipient_id") or (latest.get("details") or {}).get("recipient")
            person = conn.execute("SELECT id, legal_name, payee_status FROM person WHERE id=?", (rid,)).fetchone() if rid else None
            la = next((a for a in actions if latest.get("draft") and a["id"] == latest["draft"]["action_id"]), None)
            out["instructions"].append({
                "instruction_id": iid, "raw_text": ins["raw_text"], "created_at": ins["created_at"], "status": ins["status"],
                "parse": resolved.get("parse", {}), "intent": resolved.get("intent"),
                "decisions": decisions, "actions": actions, "latest": latest,
                "recipient": {"id": person["id"], "name": person["legal_name"], "payee_status": person["payee_status"]} if person else None,
                "pending_status": _pending_status(latest, la["state"] if la else None, bool(la and la["lapsed"]))})
        return out
    finally:
        conn.close()
