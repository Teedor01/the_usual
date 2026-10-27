import pytest

from app import domain as D
from app.money import fmt_naira, parse_amount_phrase
from app.timeutil import T0, iso


@pytest.mark.parametrize("phrase,status,kobo", [
    ("50k", "ok", 5_000_000), ("N50k", "ok", 5_000_000), ("₦50,000", "ok", 5_000_000),
    ("50,000", "ok", 5_000_000), ("50000 naira", "ok", 5_000_000), ("12.5k", "ok", 1_250_000),
    ("120k", "ok", 12_000_000), ("500,001", "ok", 50_000_100), ("$100", "unsupported_currency", None),
    ("fifty thousand", "invalid", None), ("0", "invalid", None), ("50k 60k", "multiple", None),
    ("0.001k", "ok", 100), ("0.0001k", "ok", 10), ("50.123", "invalid", None),
])
def test_L18_amount_parsing(phrase, status, kobo):
    r = parse_amount_phrase(phrase)
    assert (r.status, r.kobo) == (status, kobo)
    assert r.kobo is None or isinstance(r.kobo, int)


def test_fmt_naira_is_integer_arithmetic():
    assert fmt_naira(5_000_000) == "₦50,000" and fmt_naira(1_250_050) == "₦12,500.50"


def P(i, name, role="supplier", aliases=(), status="approved"):
    return D.Person(i, name, role, status, tuple(aliases))


def test_matching_is_exact_never_fuzzy():
    ps = [P("EO", "Emeka Okoro", aliases=["emeka"]), P("EJ", "Emeka John", aliases=["emeka"]), P("CE", "Chidi Eze")]
    assert [p.id for p in D.match_persons(ps, "Emeka")] == ["EJ", "EO"]
    assert [p.id for p in D.match_persons(ps, "  EMEKA okoro. ")] == ["EO"]
    assert D.match_persons(ps, "Emeke") == []         
    assert D.match_persons(ps, "Emek") == []
    assert D.match_persons(ps, "Chidi") == []         
    assert D.match_persons(ps, "") == []


def test_descriptor_matching():
    assert D.desc_matches("chairs", "100 chairs")
    assert D.desc_matches("those chairs", "100 chairs, revised")
    assert D.desc_matches("paint", "paint, 10 buckets")
    assert not D.desc_matches("transport", "100 chairs")
    assert not D.desc_matches("the", "100 chairs")           
    assert D.role_matches("suppliers", "supplier")


def E(id, typ, amt, ag=None, tgt=None, at="2026-10-01T08:00:00.000000Z", rec="reconciled", person="X"):
    return D.Entry(id, person, typ, amt, ag, tgt, at, rec)


def test_effective_amounts_full_partial_reinstatement_and_pending():
    pay = E("P", "payment_out", 100)
    assert D.effective_amounts([pay])["P"] == 100
    assert D.effective_amounts([pay, E("R", "reversal", 100, tgt="P")])["P"] == 0
    assert D.effective_amounts([pay, E("R", "reversal", 40, tgt="P")])["P"] == 60
    full = [pay, E("R", "reversal", 100, tgt="P"), E("I", "reinstatement", 100, tgt="R")]
    assert D.effective_amounts(full)["P"] == 100
    part = [pay, E("R", "reversal", 100, tgt="P"), E("I", "reinstatement", 30, tgt="R")]
    assert D.effective_amounts(part)["P"] == 30
    assert D.effective_amounts([pay, E("R", "reversal", 100, tgt="P", rec="pending")])["P"] == 100
    assert D.effective_amounts([pay, E("R", "reversal", 100, tgt="P", rec="disputed")])["P"] == 100


def A(id, total, supersedes=None, at="2026-09-20T08:00:00.000000Z", until=None, person="X"):
    return D.Agreement(id, person, "100 chairs", total, at, until, supersedes)


def test_heads_and_chains():
    a1, a1b = A("A1", 100), A("A1b", 120, "A1", "2026-10-05T08:00:00.000000Z")
    now = iso(T0)
    assert [a.id for a in D.current_heads([a1, a1b], now)] == ["A1b"]
    assert [a.id for a in D.chain_of(a1b, [a1, a1b])] == ["A1b", "A1"]
    assert D.chain_start(D.chain_of(a1b, [a1, a1b])) == a1.agreed_at
    expired = A("A9", 10, until="2026-10-01T00:00:00.000000Z")
    assert D.current_heads([expired], now) == []      


def test_chain_outstanding_counts_only_linked_and_applies_reversals():
    a1, a1b = A("A1", 10_000_000), A("A1b", 12_000_000, "A1", "2026-10-05T08:00:00.000000Z")
    es = [E("E1", "payment_out", 5_000_000, "A1"), E("E2", "payment_out", 5_000_000, None)]
    eff = D.effective_amounts(es)
    assert D.chain_outstanding(a1b, [a1, a1b], es, eff) == 7_000_000  
    es2 = es + [E("R", "reversal", 5_000_000, tgt="E1", at="2026-10-06T08:00:00.000000Z")]
    assert D.chain_outstanding(a1b, [a1, a1b], es2, D.effective_amounts(es2)) == 12_000_000


def test_usual_window_skips_refunds_and_zero_effective():
    es = [E("A", "payment_out", 50, at="2026-09-01T08:00:00.000000Z"), E("B", "payment_out", 50, at="2026-09-08T08:00:00.000000Z"),
          E("C", "payment_out", 50, at="2026-09-15T08:00:00.000000Z"), E("F", "refund_out", 30, at="2026-09-20T08:00:00.000000Z"),
          E("Z", "payment_out", 70, at="2026-09-25T08:00:00.000000Z"), E("RZ", "reversal", 70, tgt="Z", at="2026-09-26T08:00:00.000000Z")]
    eff = D.effective_amounts(es)
    assert [e.id for e in D.usual_window(es, eff, 3)] == ["C", "B", "A"]


def test_unlinked_applicability_and_duplicates():
    es = [E("U1", "payment_out", 10, None, at="2026-09-28T08:00:00.000000Z"),
          E("U2", "payment_out", 10, None, at="2026-10-08T08:00:00.000000Z"), E("L", "payment_out", 10, "A1")]
    eff = D.effective_amounts(es)
    assert [e.id for e in D.unlinked_applicable(es, eff, "2026-10-01T08:00:00.000000Z")] == ["U2"]
    from datetime import timedelta
    es3 = [E("D1", "payment_out", 50, at=iso(T0 - timedelta(hours=1))), E("D2", "payment_out", 50, at=iso(T0 - timedelta(hours=30)))]
    assert [e.id for e in D.duplicates(es3, D.effective_amounts(es3), 50, T0, 24)] == ["D1"]


def test_digest_changes_on_inserted_rows_but_not_unrelated_ones():
    rc, other = P("EO", "Emeka Okoro"), P("CE", "Chidi Eze")
    pol = D.Policy(50_000_000, 3, 24, 900, 1)
    ags = [A("A1", 100, person="EO")]
    base = [E("E1", "payment_out", 5, "A1", person="EO")]
    def dg(entries, persons=(rc,), agreements=ags):
        return D.read_set_digest(q1_persons=list(persons), policy=pol, recipient=rc, agreements_all=agreements,
                                 head_ids=["A1"], entries=entries)
    d0 = dg(base)
    assert d0 == dg(list(base))                                
    assert d0 != dg(base + [E("E2", "payment_out", 1, person="EO")])   
    assert d0 != dg(base, persons=(rc, P("EX", "Emeka X")))           
    assert d0 != dg(base, agreements=ags + [A("A2", 5, person="EO")]) 


def test_payload_hash_covers_every_mutable_field():
    base = dict(recipient_id="EO", amount_kobo=5, currency="NGN", agreement_id="A1", source="explicit",
                stated_amount_kobo=5, amount_resolution={"kind": "none"}, ack_duplicate_ids=[], ack_unlinked_ids=[],
                policy_version=1)
    h0 = D.payload_hash(**base)
    assert h0 == D.payload_hash(**base)
    for k, v in [("recipient_id", "EJ"), ("amount_kobo", 6), ("agreement_id", None), ("source", "outstanding_balance"),
                 ("stated_amount_kobo", 6), ("amount_resolution", {"kind": "override", "over_by_kobo": 1}),
                 ("ack_duplicate_ids", ["E1"]), ("ack_unlinked_ids", ["E2"]), ("policy_version", 2)]:
        assert D.payload_hash(**{**base, k: v}) != h0, k
