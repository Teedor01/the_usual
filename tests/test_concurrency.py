import multiprocessing as mp

import pytest

from app import lifecycle as L
from app.timeutil import iso
from tests import workers as W
from tests.helpers import D, intent

CTX = mp.get_context("spawn")
INVARIANT_SQL = """
SELECT p.id, p.amount_kobo,
  p.amount_kobo
   - COALESCE((SELECT SUM(r.amount_kobo) FROM ledger_entry r WHERE r.entry_type='reversal' AND r.target_entry_id=p.id AND r.reconciliation='reconciled'),0)
   + COALESCE((SELECT SUM(i.amount_kobo) FROM ledger_entry i JOIN ledger_entry r ON i.target_entry_id=r.id
               WHERE i.entry_type='reinstatement' AND i.reconciliation='reconciled' AND r.target_entry_id=p.id),0) AS eff
FROM ledger_entry p WHERE p.entry_type='payment_out'
"""


def violations(s):
    rows = s.q(INVARIANT_SQL)
    bad = [r for r in rows if r["eff"] < 0 or r["eff"] > r["amount_kobo"]]
    over = s.q("SELECT r.id FROM ledger_entry r WHERE r.entry_type='reversal' AND "
               "(SELECT COALESCE(SUM(amount_kobo),0) FROM ledger_entry i WHERE i.entry_type='reinstatement' AND i.target_entry_id=r.id) > r.amount_kobo")
    cap = s.q("SELECT p.id FROM ledger_entry p WHERE p.entry_type='payment_out' AND "
              "(SELECT COALESCE(SUM(amount_kobo),0) FROM ledger_entry r WHERE r.entry_type='reversal' AND r.target_entry_id=p.id)"
              " - (SELECT COALESCE(SUM(i.amount_kobo),0) FROM ledger_entry i JOIN ledger_entry r ON i.target_entry_id=r.id"
              "    WHERE i.entry_type='reinstatement' AND i.reconciliation='reconciled' AND r.target_entry_id=p.id) > p.amount_kobo")
    return bad, over, cap


def race(targets):
    """targets: list of (func, args-without-barrier-and-queue). Returns results in completion order."""
    barrier, q = CTX.Barrier(len(targets)), CTX.Queue()
    procs = [CTX.Process(target=f, args=(*a[:1], barrier, *a[1:], q)) for f, a in targets]
    for p in procs:
        p.start()
    out = [q.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0
    return out


def sql_job(s, id, typ, amt, target, ev, mmdd="10-07"):
    s.add_evidence(ev)
    return (W.sql_worker, (str(s.path), W.INSERT_ADJ, (id, "CN", typ, amt, "AG3", target, D(mmdd), D(mmdd), ev)))


@pytest.mark.parametrize("round_", range(5))
def test_C1_duplicate_reversal_evidence(env, round_):
    s = env.new()
    s.add_entry("P", "CN", "payment_out", 2_500_000, "AG3", mmdd="10-06")
    res = race([sql_job(s, "RA", "reversal", 1_000_000, "P", "ev_dup"), sql_job(s, "RB", "reversal", 1_000_000, "P", "ev_dup")])
    assert sorted(r[0] for r in res) == ["err", "ok"] and any("UNIQUE" in r[1] for r in res)
    assert s.count(where="entry_type='reversal'") == 1 and violations(s) == ([], [], [])


@pytest.mark.parametrize("round_", range(5))
def test_C2_concurrent_partial_reversals_respect_the_cap(env, round_):
    s = env.new()
    s.add_entry("P", "CN", "payment_out", 2_500_000, "AG3", mmdd="10-06")
    res = race([sql_job(s, f"R{i}", "reversal", 1_000_000, "P", f"ev_r{i}") for i in range(4)])
    assert sorted(r[0] for r in res) == ["err", "err", "ok", "ok"]
    assert all("reversal_cap_exceeded" in r[1] for r in res if r[0] == "err")
    assert sum(r["amount_kobo"] for r in s.q("SELECT amount_kobo FROM ledger_entry WHERE entry_type='reversal'")) == 2_000_000
    s.add_entry("P2", "CN", "payment_out", 2_500_000, "AG3", mmdd="10-06")
    res2 = race([sql_job(s, "RX", "reversal", 1_500_000, "P2", "ev_x"), sql_job(s, "RY", "reversal", 1_500_000, "P2", "ev_y")])
    assert sorted(r[0] for r in res2) == ["err", "ok"]
    assert violations(s) == ([], [], [])


@pytest.mark.parametrize("round_", range(8))
def test_C3_reversal_vs_reinstatement_matches_a_serial_order(env, round_):
    s = env.new()
    s.add_entry("P", "CN", "payment_out", 2_500_000, "AG3", mmdd="10-06")
    s.add_entry("R0", "CN", "reversal", 2_500_000, "AG3", "P", "10-07")                      
    res = race([sql_job(s, "I1", "reinstatement", 2_500_000, "R0", "ev_i1", "10-08"),
                sql_job(s, "RN", "reversal", 100, "P", "ev_rn", "10-08")])
    by = {}
    for r in res:
        by.setdefault(r[0], []).append(r[1])
    n_rev = s.count(where="id='RN'")
    assert s.count(where="id='I1'") == 1                                                    
    assert n_rev in (0, 1) and violations(s) == ([], [], [])
    if n_rev == 0:
        assert any("reversal_cap_exceeded" in e for e in by.get("err", []))


@pytest.mark.parametrize("round_", range(4))
def test_C3_mixed_reinstatements_and_reversals_never_break_the_invariant(env, round_):
    s = env.new()
    s.add_entry("P", "CN", "payment_out", 2_500_000, "AG3", mmdd="10-06")
    s.add_entry("R0", "CN", "reversal", 2_500_000, "AG3", "P", "10-07")
    jobs = [sql_job(s, "IA", "reinstatement", 1_000_000, "R0", "ev_ia", "10-08"),
            sql_job(s, "IB", "reinstatement", 1_000_000, "R0", "ev_ib", "10-08"),
            sql_job(s, "IC", "reinstatement", 1_500_000, "R0", "ev_ic", "10-08"),
            sql_job(s, "RA", "reversal", 1_000_000, "P", "ev_ra", "10-08"),
            sql_job(s, "RB", "reversal", 1_000_000, "P", "ev_rb", "10-08")]
    race(jobs)
    assert violations(s) == ([], [], [])
    reinst = sum(r["amount_kobo"] for r in s.q("SELECT amount_kobo FROM ledger_entry WHERE entry_type='reinstatement'"))
    assert reinst <= 2_500_000                                                             


@pytest.mark.parametrize("round_", range(5))
def test_C5_same_key_commits_are_idempotent(env, round_):
    s = env.new()
    v = s.say("Pay Amaka 50k", intent("Amaka", "explicit", "50k"))
    now = iso(s.now())
    res = race([(W.commit_worker, (str(s.path), now, v["draft"]["action_id"], "SAME", v["draft"]["payload_hash"]))
                for _ in range(3)])
    assert [r[0] for r in res] == [200, 200, 200] and len({r[1] for r in res}) == 1         
    assert s.count("ledger_entry", "source_action_id IS NOT NULL") == 1 and s.count("commit_attempt") == 1


@pytest.mark.parametrize("round_", range(5))
def test_C6_different_keys_exactly_one_commits(env, round_):
    s = env.new()
    v = s.say("Pay Amaka 50k", intent("Amaka", "explicit", "50k"))
    now = iso(s.now())
    res = race([(W.commit_worker, (str(s.path), now, v["draft"]["action_id"], f"KEY{i}", v["draft"]["payload_hash"]))
                for i in range(4)])
    codes = sorted(r[0] for r in res)
    assert codes == [200, 409, 409, 409] and s.count("ledger_entry", "source_action_id IS NOT NULL") == 1
    assert all('"already_committed"' in r[1] for r in res if r[0] == 409)


@pytest.mark.parametrize("round_", range(6))
def test_C7_commit_vs_ledger_insert_never_commits_a_stale_amount(env, round_):
    s = env.new()
    v = s.say("Pay Emeka the balance for those chairs.", intent("Emeka", "balance", desc=[("item", "chairs")]))
    assert v["draft"]["amount_kobo"] == 5_000_000
    s.add_evidence("ev_race")
    ins = ("INSERT INTO ledger_entry(id, person_id, entry_type, amount_kobo, agreement_id, target_entry_id, occurred_at,"
           " recorded_at, reconciliation, evidence_id) VALUES ('RACE','EO','payment_out',2000000,'AG1',NULL,?,?,'reconciled','ev_race')")
    res = race([(W.commit_worker, (str(s.path), iso(s.now()), v["draft"]["action_id"], "CK", v["draft"]["payload_hash"])),
                (W.sql_worker, (str(s.path), ins, (D("10-11"), D("10-11"))))])
    commit = next(r for r in res if isinstance(r[0], int))
    rows = s.q("SELECT id, rowid FROM ledger_entry WHERE id='RACE' OR source_action_id IS NOT NULL ORDER BY rowid")
    if commit[0] == 200:
        assert [r["id"] for r in rows][-1] == "RACE"          
    else:
        assert commit[0] == 409 and '"invalidated"' in commit[1] and [r["id"] for r in rows] == ["RACE"]
        assert s.say("Pay Emeka the balance for those chairs.", intent("Emeka", "balance", desc=[("item", "chairs")]))["draft"]["amount_kobo"] == 3_000_000


@pytest.mark.parametrize("round_", range(4))
def test_C8_same_key_concurrent_staging_yields_one_instruction_one_draft(env, round_):
    s = env.new()
    res = race([(W.stage_worker, (str(s.path), iso(s.now()), "Pay Amaka 50k", "SAMEKEY")) for _ in range(3)])
    assert {r[0] for r in res} == {"STAGE"} and len({r[1] for r in res}) == 1 and len({r[2] for r in res}) == 1
    assert s.count("instruction", "idempotency_key='SAMEKEY'") == 1 and s.count("staged_action") == 1


@pytest.mark.parametrize("round_", range(3))
def test_C8_different_keys_same_payload_share_one_live_draft(env, round_):
    s = env.new()
    res = race([(W.stage_worker, (str(s.path), iso(s.now()), "Pay Amaka 50k", f"K{i}")) for i in range(3)])
    assert {r[0] for r in res} == {"STAGE"} and len({r[1] for r in res}) == 1
    assert s.count("instruction") == 3 and s.count("staged_action") == 1
