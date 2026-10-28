import hashlib
from pathlib import Path

from . import db
from .domain import normalize
from .timeutil import iso, T0

SEED_VERSION = "seed-1"

POLICY = dict(per_payment_limit_kobo=50_000_000, recurring_min_count=3,
              duplicate_window_hours=24, draft_ttl_seconds=900, version=1)

# (id, legal_name, role, approved, aliases)
PEOPLE = [
    ("EO", "Emeka Okoro", "supplier", True, ["Emeka"]),
    ("EJ", "Emeka John", "customer", True, ["Emeka"]),
    ("CE", "Chidi Eze", "supplier", True, ["Chidi"]),
    ("CN", "Chidi Nwosu", "supplier", True, ["Chidi"]),
    ("AO", "Amaka Obi", "supplier", True, ["Amaka"]),
    ("BU", "Bassey Udo", "supplier", False, ["Bassey"]),
]

# (id, person, description, total_kobo, agreed 'MM-DD')
AGREEMENTS = [
    ("AG1", "EO", "100 chairs", 10_000_000, "09-20"),
    ("AG2", "CE", "cement, 20 bags", 18_000_000, "09-25"),
    ("AG3", "CN", "paint, 10 buckets", 9_000_000, "10-01"),
]

# (id, person, type, amount_kobo, agreement, 'MM-DD')
ENTRIES = [
    ("E1", "EO", "payment_out", 5_000_000, "AG1", "09-22"),
    ("E2", "EO", "payment_out", 5_000_000, None, "09-15"),
    ("E3", "EO", "payment_out", 5_000_000, None, "09-08"),
    ("E4", "EO", "payment_out", 5_000_000, None, "09-01"),
    ("E5", "CE", "payment_out", 6_000_000, "AG2", "10-01"),
    ("E6", "CN", "payment_out", 3_000_000, "AG3", "10-02"),
    ("E7", "EJ", "refund_out", 3_000_000, None, "09-10"),
]


def d(mmdd: str, hour: int = 8) -> str:
    m, day = mmdd.split("-")
    return f"2026-{m}-{day}T{hour:02d}:00:00.000000Z"


def add_evidence(conn, ev_id, text, at):
    conn.execute("INSERT INTO evidence(id, source_type, raw_text, captured_at, recorded_at) VALUES (?,?,?,?,?)",
                 (ev_id, "manual_entry", text, at, at))


def build_template(path) -> str:
    path = Path(path)
    conn = db.create_empty(path)
    now = iso(T0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO meta(key,value) VALUES ('seed_version', ?), ('t0', ?)", (SEED_VERSION, now))
        conn.execute("INSERT INTO policy(id, per_payment_limit_kobo, recurring_min_count, duplicate_window_hours,"
                     " draft_ttl_seconds, version) VALUES (1,?,?,?,?,?)",
                     (POLICY["per_payment_limit_kobo"], POLICY["recurring_min_count"],
                      POLICY["duplicate_window_hours"], POLICY["draft_ttl_seconds"], POLICY["version"]))
        for pid, name, role, approved, aliases in PEOPLE:
            conn.execute("INSERT INTO person(id, legal_name, role, payee_status) VALUES (?,?,?,?)",
                         (pid, name, role, "approved" if approved else "not_approved"))
            for a in aliases:
                conn.execute("INSERT INTO person_alias(person_id, alias_normalized) VALUES (?,?)",
                             (pid, normalize(a)))
        for aid, pid, desc, total, mmdd in AGREEMENTS:
            add_evidence(conn, f"ev_{aid}", f"Agreement: {desc}", d(mmdd))
            conn.execute("INSERT INTO agreement(id, person_id, description, total_kobo, agreed_at, valid_until,"
                         " supersedes_id, evidence_id) VALUES (?,?,?,?,?,NULL,NULL,?)",
                         (aid, pid, desc, total, d(mmdd), f"ev_{aid}"))
        for eid, pid, typ, amt, ag, mmdd in ENTRIES:
            add_evidence(conn, f"ev_{eid}", f"Ledger entry {eid}", d(mmdd))
            conn.execute("INSERT INTO ledger_entry(id, person_id, entry_type, amount_kobo, agreement_id,"
                         " target_entry_id, occurred_at, recorded_at, reconciliation, evidence_id)"
                         " VALUES (?,?,?,?,?,NULL,?,?, 'reconciled', ?)",
                         (eid, pid, typ, amt, ag, d(mmdd), d(mmdd), f"ev_{eid}"))
        conn.execute("COMMIT")
        db.audit_schema(conn)
        conn.execute("PRAGMA journal_mode = DELETE")
    finally:
        conn.close()
    return file_hash(path)


def file_hash(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
