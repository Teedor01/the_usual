from .domain import Agreement, Entry, Person, Policy


def load_policy(conn) -> Policy:
    r = conn.execute("SELECT * FROM policy WHERE id=1").fetchone()
    return Policy(r["per_payment_limit_kobo"], r["recurring_min_count"], r["duplicate_window_hours"],
                  r["draft_ttl_seconds"], r["version"])


def load_persons(conn):
    aliases = {}
    for r in conn.execute("SELECT person_id, alias_normalized FROM person_alias"):
        aliases.setdefault(r["person_id"], []).append(r["alias_normalized"])
    return [Person(r["id"], r["legal_name"], r["role"], r["payee_status"], tuple(sorted(aliases.get(r["id"], []))))
            for r in conn.execute("SELECT * FROM person ORDER BY id")]


def load_agreements(conn, person_id=None):
    q = "SELECT * FROM agreement" + (" WHERE person_id=?" if person_id else "") + " ORDER BY id"
    rows = conn.execute(q, (person_id,) if person_id else ())
    return [Agreement(r["id"], r["person_id"], r["description"], r["total_kobo"], r["agreed_at"],
                      r["valid_until"], r["supersedes_id"]) for r in rows]


def load_entries(conn, person_id):
    rows = conn.execute("SELECT * FROM ledger_entry WHERE person_id=? ORDER BY id", (person_id,))
    return [Entry(r["id"], r["person_id"], r["entry_type"], r["amount_kobo"], r["agreement_id"],
                  r["target_entry_id"], r["occurred_at"], r["reconciliation"], r["override_over_kobo"])
            for r in rows]
