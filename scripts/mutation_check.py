import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MUTATIONS = [
    ("ignore reversals in effective amounts", "app/domain.py",
     'return {e.id: e.amount_kobo - rev[e.id] + reinst[e.id] for e', 'return {e.id: e.amount_kobo for e', ["tests/test_scenarios.py"]),
    ("skip unlinked-payment check (P7)", "app/engine.py", "if balance_flow:", "if False:", ["tests/test_scenarios.py"]),
    ("skip payee approval refusal (P2)", "app/engine.py", 'if rcp.payee_status != "approved":', "if False:", ["tests/test_scenarios.py"]),
    ("skip read-set digest comparison at commit", "app/lifecycle.py",
     'elif res.digest != a["read_set_digest"]:', "elif False:", ["tests/test_lifecycle.py"]),
    ("skip payload-hash comparison at commit", "app/lifecycle.py",
     'elif res.draft["payload_hash"] != a["payload_hash"]:', "elif False:", ["tests/test_lifecycle.py"]),
    ("commit trusts stored draft (no revalidation)", "app/lifecycle.py",
     "    res = engine.decide(conn, now, resolved)\n    fail = None", "    res = engine.decide(conn, now, resolved)\n    res.outcome = 'STAGE'; res.draft = {'payload_hash': a['payload_hash'], 'recipient_id': a['recipient_person_id'], 'amount_kobo': a['amount_kobo'], 'agreement_id': a['agreement_id'], 'override_over_kobo': a['override_over_kobo'], 'recipient_name': 'x'}; res.digest = a['read_set_digest']\n    fail = None",
     ["tests/test_lifecycle.py"]),
    ("duplicate check (P8) removed", "app/engine.py", "if todo:\n            ids = [e.id for e in todo]\n            return self.ask(\"possible_duplicate\"", "if False:\n            ids = [e.id for e in todo]\n            return self.ask(\"possible_duplicate\"", ["tests/test_scenarios.py"]),
    ("exceeds-balance check removed (silent overpayment)", "app/engine.py", 'source != "outstanding_balance" and amount > O:', "source != 'outstanding_balance' and False:", ["tests/test_scenarios.py"]),
    ("fuzzy name matching (substring)", "app/domain.py", "if m in names:", "if any(m in n or n in m for n in names):", ["tests/test_domain.py"]),
    ("verbatim-phrase check removed", "app/intent.py", "or _ws(p) not in t:", "or False:", ["tests/test_lifecycle.py", "tests/test_scenarios.py"]),
    ("reversal cap trigger neutered", "app/schema.py", "THEN RAISE(ABORT, 'reversal_cap_exceeded')", "THEN 1", ["tests/test_db_constraints.py"]),
    ("reinstatement cap trigger neutered", "app/schema.py", "THEN RAISE(ABORT, 'reinstatement_cap_exceeded')", "THEN 1", ["tests/test_db_constraints.py"]),
    ("ledger delete protection removed", "app/schema.py", "BEGIN SELECT RAISE(ABORT, 'ledger_append_only'); END;", "BEGIN SELECT 1; END;", ["tests/test_db_constraints.py"]),
    ("payee ledger backstop neutered", "app/schema.py", "THEN RAISE(ABORT, 'payee_not_approved')\n    WHEN NEW.amount_kobo > (SELECT per_payment_limit_kobo FROM policy WHERE id = 1)\n      THEN RAISE(ABORT, 'over_limit')\n  END;\nEND;\n\"\"\"", "THEN 1\n    WHEN NEW.amount_kobo > (SELECT per_payment_limit_kobo FROM policy WHERE id = 1)\n      THEN 1\n  END;\nEND;\n\"\"\"", ["tests/test_db_constraints.py"]),
    ("recursive_triggers off (REPLACE bypasses delete trigger)", "app/db.py", 'conn.execute("PRAGMA recursive_triggers = ON")\n    if not readonly', 'conn.execute("PRAGMA recursive_triggers = OFF")\n    if not readonly', ["tests/test_db_constraints.py"]),
    ("deferred BEGIN instead of BEGIN IMMEDIATE", "app/db.py", 'conn.execute("BEGIN IMMEDIATE")', 'conn.execute("BEGIN")', ["tests/test_concurrency.py", "tests/test_db_constraints.py"]),
    ("commit idempotency lookup removed", "app/lifecycle.py", "    if prev:\n        if prev[\"action_id\"]", "    if False:\n        if prev[\"action_id\"]", ["tests/test_lifecycle.py", "tests/test_concurrency.py"]),
    ("session registry falls back instead of 410", "app/sessions.py", "            if reason:\n                self._end(c, session_id, \"reset\", reason, now)\n                raise SessionGone(session_id)", "            if reason:\n                pass", ["tests/test_sessions.py"]),
]


def run(name, rel, old, new, tests):
    tmp = Path(tempfile.mkdtemp())
    dst = tmp / "p"
    shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "data", "*.db"))
    f = dst / rel
    src = f.read_text(encoding="utf-8")
    if old not in src:
        return name, "BAD MUTATION (pattern not found)"
    f.write_text(src.replace(old, new, 1), encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "pytest", *tests, "-x", "-q", "-W", "ignore", "-p", "no:cacheprovider"],
                       cwd=dst, capture_output=True, text=True, timeout=600)
    shutil.rmtree(tmp, ignore_errors=True)
    return name, ("KILLED" if r.returncode != 0 else "SURVIVED  <-- test gap")


if __name__ == "__main__":
    results = [run(*m) for m in MUTATIONS]
    for n, r in results:
        print(f"{r:<26} {n}")
    bad = [n for n, r in results if r != "KILLED"]
    print(f"\n{len(results) - len(bad)}/{len(results)} mutations killed")
    sys.exit(1 if bad else 0)