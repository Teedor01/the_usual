import json
from pathlib import Path

INSERT_ADJ = ("INSERT INTO ledger_entry(id, person_id, entry_type, amount_kobo, agreement_id, target_entry_id,"
              " occurred_at, recorded_at, reconciliation, evidence_id) VALUES (?,?,?,?,?,?,?,?, 'reconciled', ?)")


def sql_worker(path, barrier, sql, params, q):
    from app import db
    c = db.connect(path, busy_timeout_ms=20000)
    barrier.wait()
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute(sql, params)
        c.execute("COMMIT")
        q.put(("ok", ""))
    except Exception as e:  
        try:
            c.execute("ROLLBACK")
        except Exception:  
            pass
        q.put(("err", f"{type(e).__name__}:{e}"))
    finally:
        c.close()


def commit_worker(path, barrier, now_iso, action_id, key, h, q):
    from app import lifecycle as L
    from app.timeutil import parse_iso
    ctx = L.SessionContext(Path(path), lambda: parse_iso(now_iso))
    barrier.wait()
    try:
        status, raw = L.commit_action(ctx, action_id, key, h)
        q.put((status, raw))
    except Exception as e:  # noqa
        q.put((-1, f"{type(e).__name__}:{e}"))


def stage_worker(path, barrier, now_iso, text, key, q):
    from app import db, lifecycle as L, repo
    from app.adapters import RuleParser
    from app.timeutil import parse_iso
    c = db.connect(path)
    names = [n for p in repo.load_persons(c) for n in (p.legal_name, *p.aliases)]
    vocab = [w for a in repo.load_agreements(c) for w in a.description.replace(",", " ").split()]
    c.close()
    ctx = L.SessionContext(Path(path), lambda: parse_iso(now_iso))
    barrier.wait()
    try:
        v = L.submit_instruction(ctx, text, key, RuleParser(lambda: names, lambda: vocab))
        q.put((v["outcome"], v["draft"]["action_id"] if v["draft"] else None, v["instruction_id"]))
    except Exception as e:  # noqa
        q.put(("exc", f"{type(e).__name__}:{e}", ""))


def _fixed_now():
    from datetime import datetime, timezone
    return datetime(2030, 1, 1, tzinfo=timezone.utc)


def charge_worker(registry_path, barrier, sessions_dir, template, session_id, per_session, per_day, q):
    from app.sessions import SessionRegistry
    reg = SessionRegistry(registry_path, sessions_dir, template, _fixed_now)
    barrier.wait()
    try:
        q.put(("ok" if reg.charge_model_call(session_id, per_session, per_day) else "denied", ""))
    except Exception as e:  # noqa
        q.put(("exc", f"{type(e).__name__}:{e}"))
