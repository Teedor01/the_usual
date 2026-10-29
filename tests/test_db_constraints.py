import re
import sqlite3
from pathlib import Path

import pytest

from app import db, lifecycle as L
from tests.helpers import D, intent, naira, opt_ids

M = 250_000_0  


@pytest.fixture
def w(s):
    """A world with payments to CN on AG3 plus assorted entry types for negative tests."""
    s.add_entry("P", "CN", "payment_out", 2_500_000, "AG3", mmdd="10-06")
    s.add_entry("P2", "CN", "payment_out", 2_500_000, "AG3", mmdd="10-06")
    s.add_entry("PI", "CN", "payment_in", 100_000, None, mmdd="10-06")
    s.add_entry("PP", "CN", "payment_out", 100_000, None, mmdd="10-06", recon="pending")
    s.add_entry("PD", "CN", "payment_out", 100_000, None, mmdd="10-06", recon="disputed")
    return s


def rejects(s, fn, match):
    before = s.count()
    with pytest.raises(sqlite3.DatabaseError) as ei:
        fn()
    assert re.search(match, str(ei.value)), str(ei.value)
    assert s.count() == before


def adj(s, id, typ, amt, target, person="CN", ag="AG3", mmdd="10-07", recon="reconciled", ev=None):
    return lambda: s.add_entry(id, person, typ, amt, ag, target, mmdd, recon, ev)


# ---------------- L16: valid rows accepted ---------------------------------------------------------
def test_L16_valid_reversals_and_reinstatements(w):
    s = w
    s.add_entry("R1", "CN", "reversal", 1_000_000, "AG3", "P", "10-07")          
    s.add_entry("R2", "CN", "reversal", 1_500_000, "AG3", "P", "10-07")          
    s.add_entry("I1", "CN", "reinstatement", 400_000, "AG3", "R1", "10-08")     
    s.add_entry("I2", "CN", "reinstatement", 600_000, "AG3", "R1", "10-08")      
    s.add_entry("R3", "CN", "reversal", 1_000_000, "AG3", "P", "10-09")       
    assert s.count(where="entry_type IN ('reversal','reinstatement')") == 5


def test_L16_reconciliation_transitions_allowed(w):
    w.op("UPDATE ledger_entry SET reconciliation='reconciled' WHERE id='PP'")       
    w.add_entry("PP2", "CN", "payment_out", 100_000, None, mmdd="10-06", recon="pending")
    w.op("UPDATE ledger_entry SET reconciliation='disputed' WHERE id='PP2'")      
    w.op("UPDATE ledger_entry SET reconciliation='reconciled' WHERE id='PP2'")      
    assert w.q("SELECT reconciliation r FROM ledger_entry WHERE id='PP2'")[0]["r"] == "reconciled"


# ---------------- L16: invalid rows rejected ---------------------------------------------------------
def test_L16_over_reversal_by_one_kobo(w):
    w.add_entry("R1", "CN", "reversal", 1_000_000, "AG3", "P", "10-07")
    rejects(w, adj(w, "RX", "reversal", 1_500_001, "P"), "reversal_cap_exceeded")
    w.add_entry("R2", "CN", "reversal", 1_500_000, "AG3", "P", "10-07")             
    rejects(w, adj(w, "RY", "reversal", 1, "P"), "reversal_cap_exceeded")


@pytest.mark.parametrize("target,person,ag,match", [
    ("E7", "EJ", None, "bad_target_type"),       
    ("PI", "CN", None, "bad_target_type"),       
    ("PP", "CN", None, "target_not_reconciled"), 
    ("PD", "CN", None, "target_not_reconciled"),  
])
def test_L16_reversal_target_rules(w, target, person, ag, match):
    rejects(w, adj(w, "RX", "reversal", 1_000, target, person, ag), match)


def test_L16_reversal_of_a_reversal(w):
    w.add_entry("R1", "CN", "reversal", 1_000_000, "AG3", "P", "10-07")
    rejects(w, adj(w, "RX", "reversal", 10, "R1"), "bad_target_type")


def test_L16_cross_person_and_cross_agreement(w):
    rejects(w, adj(w, "RX", "reversal", 1_000, "P", person="EO", ag=None), "target_person_mismatch")
    rejects(w, adj(w, "RX", "reversal", 1_000, "P", ag=None), "target_agreement_mismatch")   
    w.add_entry("U1", "CN", "payment_out", 500_000, None, mmdd="10-06")
    rejects(w, adj(w, "RY", "reversal", 1_000, "U1", ag="AG3"), "target_agreement_mismatch")    


def test_L16_reversal_before_target(w):
    rejects(w, adj(w, "RX", "reversal", 1_000, "P", mmdd="10-05"), "adjustment_before_target")


def test_L16_duplicate_evidence(w):
    w.add_entry("R1", "CN", "reversal", 1_000, "AG3", "P", "10-07", evidence="ev_same")
    rejects(w, adj(w, "RX", "reversal", 1_000, "P", ev="ev_same"), "UNIQUE")
    rejects(w, adj(w, "RY", "reversal", 1_000, "P2", ev="ev_same"), "UNIQUE")


def test_L16_shape_checks(w):
    rejects(w, lambda: w.add_entry("RX", "CN", "reversal", 10, "AG3", None, "10-07"), "CHECK|target_missing") 
    rejects(w, lambda: w.add_entry("PX", "CN", "payment_out", 10, "AG3", "P", "10-07"), "CHECK")       
    rejects(w, lambda: w.add_entry("PY", "CN", "payment_out", 0, None, mmdd="10-07"), "CHECK")         
    rejects(w, lambda: w.add_entry("PZ", "CN", "payment_out", -5, None, mmdd="10-07"), "CHECK")


def test_L16_reinstatement_rules(w):
    w.add_entry("R1", "CN", "reversal", 1_000_000, "AG3", "P", "10-07")
    w.add_entry("RP", "CN", "reversal", 100, "AG3", "P2", "10-07", recon="pending")
    rejects(w, adj(w, "IX", "reinstatement", 10, "P", mmdd="10-08"), "bad_target_type")             
    rejects(w, adj(w, "IX", "reinstatement", 10, "RP", mmdd="10-08"), "target_not_reconciled")      
    w.add_entry("I1", "CN", "reinstatement", 100_000, "AG3", "R1", "10-08")
    rejects(w, adj(w, "IX", "reinstatement", 10, "I1", mmdd="10-09"), "bad_target_type")         
    rejects(w, adj(w, "IY", "reinstatement", 900_001, "R1", mmdd="10-09"), "reinstatement_cap_exceeded")
    w.add_entry("I2", "CN", "reinstatement", 900_000, "AG3", "R1", "10-09")                 
    rejects(w, adj(w, "IZ", "reinstatement", 1, "R1", mmdd="10-09"), "reinstatement_cap_exceeded")


def test_L16_only_reconciliation_may_change_and_only_along_allowed_edges(w):
    for sql, match in [
        ("UPDATE ledger_entry SET amount_kobo=1 WHERE id='P'", "ledger_immutable"),
        ("UPDATE ledger_entry SET person_id='EO' WHERE id='P'", "ledger_immutable"),
        ("UPDATE ledger_entry SET agreement_id=NULL WHERE id='P'", "ledger_immutable"),
        ("UPDATE ledger_entry SET occurred_at='2020' WHERE id='P'", "ledger_immutable"),
        ("UPDATE ledger_entry SET id='PX' WHERE id='P'", "ledger_immutable"),
        ("UPDATE ledger_entry SET reconciliation='pending' WHERE id='P'", "bad_reconciliation_transition"),
        ("UPDATE ledger_entry SET reconciliation='disputed' WHERE id='P'", "bad_reconciliation_transition"),
        ("UPDATE ledger_entry SET reconciliation='pending' WHERE id='PD'", "bad_reconciliation_transition"),
        ("UPDATE ledger_entry SET reconciliation='pending' WHERE id='PP'", "bad_reconciliation_transition"),
    ]:
        rejects(w, lambda sql=sql: w.op(sql), match)


def test_L16_delete_and_insert_or_replace_rejected(w):
    rejects(w, lambda: w.op("DELETE FROM ledger_entry WHERE id='P'"), "ledger_append_only")
    rejects(w, lambda: w.op("DELETE FROM ledger_entry"), "ledger_append_only")
    rejects(w, lambda: w.op(
        "INSERT OR REPLACE INTO ledger_entry(id, person_id, entry_type, amount_kobo, occurred_at, recorded_at,"
        " reconciliation, evidence_id) VALUES ('P','CN','payment_out',1,'2026-10-06T08:00:00.000000Z',"
        "'2026-10-06T08:00:00.000000Z','reconciled','ev_P')"), "ledger_append_only")
    assert w.q("SELECT amount_kobo a FROM ledger_entry WHERE id='P'")[0]["a"] == 2_500_000


def test_L16_override_only_on_payments(w):
    rejects(w, lambda: w.op(
        "INSERT INTO ledger_entry(id, person_id, entry_type, amount_kobo, occurred_at, recorded_at, evidence_id,"
        " override_over_kobo) VALUES ('X1','EJ','refund_out',5,?,?,'ev_E1',5)", (D("10-06"), D("10-06"))), "CHECK")


def test_L16_other_tables_protected(w):
    for sql, match in [("UPDATE agreement SET total_kobo=1 WHERE id='AG1'", "agreement_immutable"),
                       ("DELETE FROM agreement WHERE id='AG1'", "agreement_immutable"),
                       ("UPDATE evidence SET raw_text='x' WHERE id='ev_E1'", "evidence_immutable"),
                       ("DELETE FROM person WHERE id='EO'", "person_no_delete"),
                       ("UPDATE person SET legal_name='x' WHERE id='EO'", "person_immutable")]:
        rejects(w, lambda sql=sql: w.op(sql), match)


def test_agreement_supersession_rules(w):
    w.add_agreement("AGX", "CN", "paint 2", 100, "10-05", supersedes="AG3")
    with pytest.raises(sqlite3.IntegrityError):                                   
        w.add_agreement("AGY", "CN", "paint 3", 100, "10-06", supersedes="AG3")
    with pytest.raises(sqlite3.IntegrityError, match="supersede_person_mismatch"):
        w.add_agreement("AGZ", "EO", "x", 100, "10-06", supersedes="AGX")
    with pytest.raises(sqlite3.IntegrityError, match="supersede_before_predecessor"):
        w.add_agreement("AGW", "CN", "x", 100, "09-01", supersedes="AGX")


# ---------------- L22: reinstatement restores balances -------------------------------------------------
def test_L22_reinstatement_restores_balances_and_reopens_capacity(s):
    s.add_entry("E8", "CN", "payment_out", 2_500_000, "AG3", mmdd="10-06")
    s.add_entry("E9", "CN", "reversal", 2_500_000, "AG3", "E8", "10-07")
    t = "Pay Chidi Nwosu the balance for the paint."
    it = intent("Chidi Nwosu", "balance", desc=[("item", "paint")])
    assert s.say(t, it)["draft"]["amount_kobo"] == 6_000_000               
    s.add_entry("E12", "CN", "reinstatement", 2_500_000, "AG3", "E9", "10-08")
    v = s.say(t, it)
    assert v["draft"]["amount_kobo"] == 3_500_000                       
    s.add_entry("E13", "CN", "reversal", 2_500_000, "AG3", "E8", "10-09")
    with pytest.raises(sqlite3.IntegrityError, match="reversal_cap_exceeded"):
        s.add_entry("E14", "CN", "reversal", 1, "AG3", "E8", "10-09")


def test_L22_partial_reinstatement(s):
    s.add_entry("E8", "CN", "payment_out", 2_500_000, "AG3", mmdd="10-06")
    s.add_entry("E9", "CN", "reversal", 2_500_000, "AG3", "E8", "10-07")
    s.add_entry("E12", "CN", "reinstatement", 1_000_000, "AG3", "E9", "10-08")
    v = s.say("Pay Chidi Nwosu the balance for the paint.", intent("Chidi Nwosu", "balance", desc=[("item", "paint")]))
    assert v["draft"]["amount_kobo"] == 5_000_000                             # 90-30-10


# ---------------- L17: payee provisioning --------------------------------------------------------------
def test_L17_default_and_seed_approvals(s):
    assert {r["id"]: r["payee_status"] for r in s.q("SELECT id, payee_status FROM person")} == {
        "EO": "approved", "EJ": "approved", "CE": "approved", "CN": "approved", "AO": "approved", "BU": "not_approved"}
    s.op("INSERT INTO person(id, legal_name, role) VALUES ('NEW','New Person','supplier')")
    assert s.q("SELECT payee_status p FROM person WHERE id='NEW'")[0]["p"] == "not_approved"   
    s.op("INSERT INTO person_alias(person_id, alias_normalized) VALUES ('NEW','amaka')")      
    v = s.say("Pay Amaka 10k", intent("Amaka", "explicit", "10k"))
    assert v["outcome"] == "ASK" and v["reason"] == "ambiguous_entity"                       
    assert any("NOT APPROVED" in o["label"] for o in v["options"])
    v2 = s.pick(v, "select_person:NEW")
    assert (v2["outcome"], v2["reason"]) == ("REFUSE", "recipient_not_approved")


def test_L17_no_application_route_writes_payee_status():
    src = "".join(p.read_text(encoding="utf-8") for p in Path("app").glob("*.py") if p.name not in ("schema.py", "seed.py"))
    assert not re.search(r"UPDATE\s+person", src, re.I)
    assert not re.search(r"INSERT\s+(OR\s+\w+\s+)?INTO\s+person\b", src, re.I)
    assert not re.search(r"payee_status\s*=(?!=)", src)      
    from app.api import create_app  # noqa
    import tempfile
    from app.config import Settings
    routes = {r.path for r in create_app(Settings(Path(tempfile.mkdtemp()))).routes}
    assert not any("payee" in r or "person" in r for r in routes)


def test_L17_ledger_trigger_is_the_backstop(s):
    v = s.say("Pay Amaka 50k", intent("Amaka", "explicit", "50k"))
    aid = v["draft"]["action_id"]
    s.op("UPDATE person SET payee_status='not_approved' WHERE id='AO'")                     
    with pytest.raises(sqlite3.IntegrityError, match="payee_not_approved"):
        s.op("INSERT INTO ledger_entry(id, person_id, entry_type, amount_kobo, occurred_at, recorded_at, evidence_id,"
             " source_action_id) VALUES ('BAD','AO','payment_out',5000000,?,?,'ev_E1',?)", (D("10-12"), D("10-12"), aid))
    s.op("UPDATE person SET payee_status='approved' WHERE id='AO'")
    s.op("UPDATE policy SET per_payment_limit_kobo=1000 WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError, match="over_limit"):
        s.op("INSERT INTO ledger_entry(id, person_id, entry_type, amount_kobo, occurred_at, recorded_at, evidence_id,"
             " source_action_id) VALUES ('BAD','AO','payment_out',5000000,?,?,'ev_E1',?)", (D("10-12"), D("10-12"), aid))


def _copy_staged(s, from_id, new_id, new_hash, recipient):
    cols = [r["name"] for r in s.q("PRAGMA table_info(staged_action)")]
    sel = ", ".join("?" if c == "id" else "?" if c == "payload_hash" else "?" if c == "recipient_person_id" else c for c in cols)
    s.op(f"INSERT INTO staged_action({', '.join(cols)}) SELECT {sel} FROM staged_action WHERE id=?",
         (new_id, recipient, new_hash, from_id))


def test_L17_staged_action_insert_trigger_blocks_unapproved_recipient_and_overlimit(s):
    v = s.say("Pay Amaka 50k", intent("Amaka", "explicit", "50k"))
    aid = v["draft"]["action_id"]
    with pytest.raises(sqlite3.IntegrityError, match="payee_not_approved"):
        _copy_staged(s, aid, "SA2", "h" * 64, "BU")
    s.op("UPDATE policy SET per_payment_limit_kobo=1000 WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError, match="over_limit"):
        _copy_staged(s, aid, "SA3", "i" * 64, "AO")


def test_staged_state_machine_and_immutability(s):
    v = s.say("Pay Amaka 50k", intent("Amaka", "explicit", "50k"))
    aid = v["draft"]["action_id"]
    for sql, match in [("UPDATE staged_action SET amount_kobo=1, state='CANCELLED' WHERE id=?", "staged_immutable"),
                       ("UPDATE staged_action SET payload_hash='x', state='CANCELLED' WHERE id=?", "staged_immutable"),
                       ("UPDATE staged_action SET recipient_person_id='EO', state='COMMITTED' WHERE id=?", "staged_immutable"),
                       ("UPDATE staged_action SET amount_kobo=1 WHERE id=?", "bad_transition"), 
                       ("UPDATE staged_action SET state='STAGED' WHERE id=?", "bad_transition"),
                       ("DELETE FROM staged_action WHERE id=?", "staged_no_delete")]:
        with pytest.raises(sqlite3.IntegrityError, match=match):
            s.op(sql, (aid,))
    assert s.commit(v)[0] == 200
    for st in ("STAGED", "CANCELLED", "EXPIRED", "INVALIDATED", "COMMITTED"):
        with pytest.raises(sqlite3.IntegrityError, match="terminal_state"):
            s.op("UPDATE staged_action SET state=? WHERE id=?", (st, aid))
    with pytest.raises(sqlite3.IntegrityError):
        s.op("UPDATE action_event SET actor='x'")
    with pytest.raises(sqlite3.IntegrityError):
        s.op("UPDATE commit_attempt SET status_code=500")
    with pytest.raises(sqlite3.IntegrityError):
        s.op("DELETE FROM decision")


def test_one_live_draft_per_payload_unique_index(s):
    v = s.say("Pay Amaka 50k", intent("Amaka", "explicit", "50k"))
    aid = v["draft"]["action_id"]
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _copy_staged(s, aid, "SA2", v["draft"]["payload_hash"], "AO")


# ---------------- C4 (single-process parts) ---------------------------------------------------------------
def test_C4_pragmas_read_back(s):
    c = s.conn()
    st = db.pragma_state(c)
    assert st["foreign_keys"] == 1 and st["recursive_triggers"] == 1
    assert st["synchronous"] == 2 and st["journal_mode"] == "wal" and st["busy_timeout"] >= 1000


def test_C4_begin_immediate_excludes_second_writer_immediately(s):
    a, b = db.connect(s.path), db.connect(s.path, busy_timeout_ms=0)
    a.execute("BEGIN IMMEDIATE")
    with pytest.raises(db.StoreBusy):
        db.begin_immediate(b)
    a.execute("ROLLBACK")
    db.begin_immediate(b)
    b.execute("ROLLBACK")


def test_C4_deferred_read_then_write_fails_closed_after_competing_commit(s):
    a, b = db.connect(s.path, busy_timeout_ms=0), db.connect(s.path, busy_timeout_ms=0)
    b.execute("BEGIN")                                         
    b.execute("SELECT COUNT(*) FROM ledger_entry").fetchone()     
    a.execute("BEGIN IMMEDIATE")
    a.execute("UPDATE policy SET draft_ttl_seconds=901 WHERE id=1")
    a.execute("COMMIT")
    with pytest.raises(sqlite3.OperationalError):                
        b.execute("UPDATE policy SET draft_ttl_seconds=902 WHERE id=1")
    b.execute("ROLLBACK")
    assert s.q("SELECT draft_ttl_seconds t FROM policy")[0]["t"] == 901
    assert "BEGIN IMMEDIATE" in Path("app/db.py").read_text(encoding="utf-8")
    assert not re.search(r'execute\("BEGIN"\)', Path("app/lifecycle.py").read_text(encoding="utf-8"))


def test_C4_startup_audit_refuses_a_dropped_trigger(s):
    s.op("DROP TRIGGER tr_ledger_reversal_cap")
    c = s.conn()
    with pytest.raises(db.SchemaAuditError):
        db.audit_schema(c)
    with pytest.raises(db.SchemaAuditError):
        L.open_db(s.ctx)
    with pytest.raises(Exception):
        s.env.reg.resolve(s.sid)                                