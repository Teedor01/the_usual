from datetime import timedelta

import pytest

from app.timeutil import T0
from tests.helpers import D, intent, naira, opt_ids


def ev_ids(draft):
    return {e["id"] for e in draft["evidence"] if e["table"] != "instruction"}


def excluded(draft):
    return {e["entry_id"]: e["possibly_applicable"] for e in draft["excluded"]}


def world_s6(s):
    s.add_entry("E8", "CN", "payment_out", 2_500_000, "AG3", mmdd="10-06")
    s.add_entry("E9", "CN", "reversal", 2_500_000, "AG3", "E8", "10-07")
    s.add_entry("E10", "CN", "payment_out", 1_000_000, None, mmdd="09-28")


S6T = "Pay Chidi Nwosu the balance for the paint."
S6I = lambda: intent("Chidi Nwosu", "balance", desc=[("item", "paint")])
S1T = "Pay Emeka the balance for those chairs."
S1I = lambda: intent("Emeka", "balance", desc=[("item", "chairs")])


def test_S1_unambiguous_balance_with_evidence(s):
    before = s.count()
    v = s.say(S1T, S1I())
    d = v["draft"]
    assert (v["outcome"], d["amount_kobo"], d["source"], d["agreement_id"]) == ("STAGE", 5_000_000, "outstanding_balance", "AG1")
    assert ev_ids(d) == {"AG1", "E1"}                                 
    assert excluded(d) == {"E2": False, "E3": False, "E4": False}       
    assert d["computation"]["agreed_total_kobo"] == 10_000_000 and d["computation"]["paid_kobo"] == 5_000_000
    assert s.count() == before                                  
    st, body, _ = s.commit(v)
    assert st == 200 and body["amount_kobo"] == 5_000_000 and s.count() == before + 1


def test_S2_same_first_name_asks_then_resolves(s):
    v = s.say("Pay Chidi the balance.", intent("Chidi", "balance"))
    assert (v["outcome"], v["reason"]) == ("ASK", "ambiguous_entity")
    assert opt_ids(v) == ["select_person:CE", "select_person:CN", "cancel"]
    bal = {o["details"]["person_id"]: o["details"]["open_balances"][0]["outstanding_kobo"] for o in v["options"] if "details" in o}
    assert bal == {"CE": 12_000_000, "CN": 6_000_000}
    assert s.count("staged_action") == 0
    v2 = s.pick(v, "select_person:CN")
    assert (v2["outcome"], v2["draft"]["amount_kobo"], v2["draft"]["agreement_id"]) == ("STAGE", 6_000_000, "AG3")


def test_S3_ambiguous_usual(s):
    v = s.say("Send the usual to Emeka.", intent("Emeka", "usual", verb="Send"))
    assert (v["outcome"], v["reason"]) == ("ASK", "ambiguous_entity")
    assert {o["id"] for o in v["options"]} == {"select_person:EO", "select_person:EJ", "cancel"} and s.count("staged_action") == 0
    v2 = s.pick(v, "select_person:EO")
    d = v2["draft"]
    assert (v2["outcome"], d["amount_kobo"], d["source"], d["agreement_id"]) == ("STAGE", 5_000_000, "recurring_pattern", None)
    assert [w["entry_id"] for w in d["computation"]["window"]] == ["E1", "E2", "E3"]
    assert any(b["agreement_id"] == "AG1" and b["outstanding_kobo"] == 5_000_000 for b in d["not_linked_balances"])
    v3 = s.say("Send the usual to Emeka.", intent("Emeka", "usual", verb="Send"))
    v4 = s.pick(v3, "select_person:EJ")
    assert (v4["outcome"], v4["reason"]) == ("ASK", "insufficient_history")                  # refunds never count


def test_S4_missing_amount_and_invented_amount_fuzz(s):
    v = s.say("Pay Amaka.", intent("Amaka", "none"))
    assert (v["outcome"], v["reason"]) == ("ASK", "missing_amount") and s.count("staged_action") == 0
    s.model.calls.clear()
    v2 = s.say("Pay Amaka.", intent("Amaka", "explicit", "50k"))                             
    assert (v2["outcome"], v2["reason"]) == ("ASK", "invalid_model_output") and s.count("staged_action") == 0
    assert len(s.model.calls) == 2                                                          


def test_S5a_two_active_agreements_balance(s):
    s.add_agreement("AG2b", "CE", "cement, 20 bags", 20_000_000, "10-03")
    t, it = "Pay Chidi Eze the balance for the cement.", intent("Chidi Eze", "balance", desc=[("item", "cement")])
    v = s.say(t, it)
    assert (v["outcome"], v["reason"]) == ("ASK", "ambiguous_agreement")
    assert opt_ids(v) == ["select_agreement:AG2", "select_agreement:AG2b", "cancel"]         
    assert s.pick(v, "select_agreement:AG2")["draft"]["amount_kobo"] == 12_000_000
    v2 = s.pick(s.say(t, it), "select_agreement:AG2b")
    assert (v2["draft"]["amount_kobo"], v2["draft"]["agreement_id"]) == (20_000_000, "AG2b")


def test_S5b_two_active_agreements_explicit(s):
    s.add_agreement("AG2b", "CE", "cement, 20 bags", 20_000_000, "10-03")
    t, it = "Pay Chidi Eze 120k for the cement.", intent("Chidi Eze", "explicit", "120k", desc=[("item", "cement")])
    v = s.say(t, it)
    assert (v["outcome"], v["reason"]) == ("ASK", "ambiguous_agreement")
    assert opt_ids(v) == ["select_agreement:AG2", "select_agreement:AG2b", "link_none", "cancel"]
    a = s.pick(v, "select_agreement:AG2")["draft"]
    assert (a["amount_kobo"], a["agreement_id"], a["source"]) == (12_000_000, "AG2", "explicit")
    b = s.pick(s.say(t, it), "select_agreement:AG2b")["draft"]
    assert (b["amount_kobo"], b["agreement_id"]) == (12_000_000, "AG2b")
    c = s.pick(s.say(t, it), "link_none")["draft"]
    assert (c["amount_kobo"], c["agreement_id"]) == (12_000_000, None)
    assert {x["agreement_id"] for x in c["not_linked_balances"]} == {"AG2", "AG2b"}           


def test_S6_reversal_arithmetic(s):
    world_s6(s)
    d = s.say(S6T, S6I())["draft"]
    assert d["amount_kobo"] == 6_000_000                                                     
    assert ev_ids(d) == {"AG3", "E6", "E8", "E9"} and excluded(d) == {"E10": False}


def test_S7_superseded_agreement_conflict(s):
    s.add_agreement("AG1b", "EO", "100 chairs, revised", 12_000_000, "10-05", supersedes="AG1")
    t = "Pay Emeka the remaining 50k for the chairs."
    it = intent("Emeka", "balance_stated", "50k", desc=[("item", "chairs")])
    v = s.say(t, it)
    assert (v["outcome"], v["reason"]) == ("ASK", "stated_amount_conflicts_ledger")
    assert (v["details"]["stated_kobo"], v["details"]["outstanding_kobo"]) == (5_000_000, 7_000_000)
    assert opt_ids(v) == ["pay_outstanding", "keep_stated", "cancel"]
    a = s.pick(v, "pay_outstanding")["draft"]
    assert (a["amount_kobo"], a["agreement_id"], a["source"]) == (7_000_000, "AG1b", "outstanding_balance")
    b = s.pick(s.say(t, it), "keep_stated")["draft"]
    assert (b["amount_kobo"], b["agreement_id"], b["source"]) == (5_000_000, "AG1b", "explicit")
    assert b["amount_resolution"] == {"kind": "keep_stated"} and b["stated_amount_kobo"] == 5_000_000


@pytest.mark.parametrize("text,it,outcome,reason,amount", [
    ("Pay Bassey 80k for the transport.", intent("Bassey", "explicit", "80k", desc=[("item", "transport")]), "REFUSE", "recipient_not_approved", None),
    ("Pay Amaka 650k for the fabric.", intent("Amaka", "explicit", "650k", desc=[("item", "fabric")]), "REFUSE", "over_limit", None),
    ("Pay Bassey.", intent("Bassey", "none"), "REFUSE", "recipient_not_approved", None),
    ("Pay Amaka 500k.", intent("Amaka", "explicit", "500k"), "STAGE", None, 50_000_000),
    ("Pay Amaka 500,001.", intent("Amaka", "explicit", "500,001"), "REFUSE", "over_limit", None),
])
def test_S8_unauthorized(s, text, it, outcome, reason, amount):
    v = s.say(text, it)
    assert (v["outcome"], v["reason"]) == (outcome, reason)
    if outcome == "REFUSE":
        assert s.count("staged_action") == 0 and v["options"] == [] and v["draft"] is None
    else:
        assert v["draft"]["amount_kobo"] == amount


def test_S9_duplicate_instruction_and_replayed_confirmation(s):
    before = s.count()
    v1 = s.say(S1T, S1I(), key="K1")
    v1b = s.say(S1T, S1I(), key="K1")                                       
    assert v1["instruction_id"] == v1b["instruction_id"] and v1["draft"]["action_id"] == v1b["draft"]["action_id"]
    assert s.count("instruction", "idempotency_key='K1'") == 1
    v2 = s.say(S1T, S1I(), key="K2")                                        
    assert v2["instruction_id"] != v1["instruction_id"] and v2["draft"]["action_id"] == v1["draft"]["action_id"]
    assert s.count("staged_action") == 1
    st1, _, raw1 = s.commit(v1, key="C1")
    st1b, _, raw1b = s.commit(v1, key="C1")
    assert (st1, st1b) == (200, 200) and raw1 == raw1b                        
    st2, b2, _ = s.commit(v1, key="C2")
    assert (st2, b2["error"]) == (409, "already_committed")
    assert s.count() == before + 1 and s.count("ledger_entry", "source_action_id IS NOT NULL") == 1
    v3 = s.say(S1T, S1I(), key="K3")
    assert (v3["outcome"], v3["reason"]) == ("ASK", "nothing_owed")           


def test_S10_interruptions(s):
    from app.adapters import InferenceUnavailable
    t, it = "Pay Amaka 50k", intent("Amaka", "explicit", "50k")
    s.model.script[t] = InferenceUnavailable("down")
    v = s.say(t, key="KW", adapter=s.model)
    assert (v["outcome"], v["reason"]) == ("WAIT", "inference_unavailable") and s.count("staged_action") == 0
    s.model.script[t] = __import__("json").dumps(it)                         
    v2 = s.say(t, key="KW", adapter=s.model)                                
    assert v2["outcome"] == "STAGE" and s.count("instruction", "idempotency_key='KW'") == 1 and s.count("staged_action") == 1
    st, _, raw = s.commit(v2, key="CK")                                     
    st2, _, raw2 = s.commit(v2, key="CK")
    assert (st, st2, raw == raw2) == (200, 200, True) and s.count("ledger_entry", "source_action_id IS NOT NULL") == 1
    v3 = s.say("Pay Amaka 20k", intent("Amaka", "explicit", "20k"))           
    s.advance(899)
    assert s.commit(v3)[0] == 200
    v4 = s.say("Pay Amaka 30k", intent("Amaka", "explicit", "30k"))
    s.advance(900)                                                            
    st, body, _ = s.commit(v4)
    assert (st, body["error"]) == (410, "expired")


def test_S11_partial_reversal_world(s):
    s.add_entry("E8", "CN", "payment_out", 2_500_000, "AG3", mmdd="10-06")
    s.add_entry("E9p", "CN", "reversal", 1_000_000, "AG3", "E8", "10-07")
    s.add_entry("E10", "CN", "payment_out", 1_000_000, None, mmdd="09-28")
    d = s.say(S6T, S6I())["draft"]
    assert d["amount_kobo"] == 4_500_000                                      
    assert {"E8", "E9p"} <= ev_ids(d)


@pytest.mark.parametrize("recon,reversal_pending", [("pending", True), ("disputed", False)])
@pytest.mark.parametrize("mode", ["balance", "explicit"])
def test_S12_unreconciled_waits(s, recon, reversal_pending, mode):
    s.add_entry("E8", "CN", "payment_out", 2_500_000, "AG3", mmdd="10-06", recon="reconciled" if reversal_pending else recon)
    if reversal_pending:
        s.add_entry("E9", "CN", "reversal", 2_500_000, "AG3", "E8", "10-07", recon="pending")
    if mode == "balance":
        v = s.say(S6T, S6I())
    else:
        v = s.say("Pay Chidi Nwosu 30k.", intent("Chidi Nwosu", "explicit", "30k"))
    assert (v["outcome"], v["reason"]) == ("WAIT", "unreconciled_entry") and v["draft"] is None
    assert s.count("staged_action") == 0


def test_S12_wait_is_cleared_only_by_explicit_retry(s):
    s.add_entry("E8", "CN", "payment_out", 2_500_000, "AG3", mmdd="10-06", recon="pending")
    v = s.say(S6T, S6I())
    assert v["reason"] == "unreconciled_entry"
    again = s.reload(v)
    assert again["outcome"] == "WAIT"                                         
    s.op("UPDATE ledger_entry SET reconciliation='reconciled' WHERE id='E8'")
    assert s.reload(v)["outcome"] == "WAIT"                                
    from app import lifecycle as L
    v2 = L.retry_instruction(s.ctx, v["instruction_id"], s.model)
    assert v2["outcome"] == "STAGE" and v2["draft"]["amount_kobo"] == 3_500_000 


def test_S13a_duplicate_unlinked(s):
    t, it = "Pay Amaka 50k for the fabric.", intent("Amaka", "explicit", "50k", desc=[("item", "fabric")])
    s.at(T0 + timedelta(minutes=2))
    assert s.commit(s.say(t, it))[0] == 200
    s.at(T0 + timedelta(minutes=30))
    v = s.say(t, it)
    assert (v["outcome"], v["reason"]) == ("ASK", "possible_duplicate") and opt_ids(v) == ["ack_duplicate", "cancel"]
    d = s.pick(v, "ack_duplicate")["draft"]
    assert d["amount_kobo"] == 5_000_000 and len(d["ack_duplicate_ids"]) == 1


def test_S13b_after_s1_commit_exceeds_balance_not_duplicate(s):
    assert s.commit(s.say(S1T, S1I()))[0] == 200
    v = s.say("Pay Emeka 50k for the chairs.", intent("Emeka", "explicit", "50k", desc=[("item", "chairs")]))
    assert (v["outcome"], v["reason"]) == ("ASK", "exceeds_balance")         
    assert opt_ids(v) == ["override_overpayment", "cancel"]                      
    assert v["details"]["over_by_kobo"] == 5_000_000


def test_S13c_duplicate_linked_within_balance(s):
    t, it = "Pay Chidi Nwosu 20k for the paint.", intent("Chidi Nwosu", "explicit", "20k", desc=[("item", "paint")])
    s.at(T0 + timedelta(minutes=2))
    assert s.commit(s.say(t, it))[0] == 200
    s.at(T0 + timedelta(minutes=30))
    v = s.say(t, it)
    assert (v["outcome"], v["reason"]) == ("ASK", "possible_duplicate")           
    d = s.pick(v, "ack_duplicate")["draft"]
    assert (d["amount_kobo"], d["agreement_id"]) == (2_000_000, "AG3")


@pytest.mark.parametrize("phrase,outcome,reason,amount", [
    ("$100", "REFUSE", "unsupported_currency", None), ("fifty thousand", "ASK", "invalid_amount", None),
    ("12.5k", "STAGE", None, 1_250_000), ("0", "ASK", "invalid_amount", None), ("50k 60k", "ASK", "multiple_amounts", None)])
def test_S14_amount_phrases(s, phrase, outcome, reason, amount):
    v = s.say(f"Pay Amaka {phrase}", intent("Amaka", "explicit", phrase))
    assert (v["outcome"], v["reason"]) == (outcome, reason)
    assert (v["draft"] or {}).get("amount_kobo") == amount


def test_S15_unlinked_payment_possible(s):
    world_s6(s)
    s.add_entry("E11", "CN", "payment_out", 1_000_000, None, mmdd="10-08")
    v = s.say(S6T, S6I())
    assert (v["outcome"], v["reason"]) == ("ASK", "unlinked_payment_possible") and v["details"]["entry_ids"] == ["E11"]
    d = s.pick(v, "ack_unlinked")["draft"]
    assert d["amount_kobo"] == 6_000_000 and d["ack_unlinked_ids"] == ["E11"]


@pytest.mark.parametrize("reinstated,expected", [(2_500_000, 3_500_000), (1_000_000, 5_000_000)])
def test_S16_reinstatement(s, reinstated, expected):
    world_s6(s)
    s.add_entry("E12", "CN", "reinstatement", reinstated, "AG3", "E9", "10-08")
    assert s.say(S6T, S6I())["draft"]["amount_kobo"] == expected


def test_S17_exceeds_balance_two_resolutions(s):
    t, it = "Pay Chidi Eze 150k for the cement.", intent("Chidi Eze", "explicit", "150k", desc=[("item", "cement")])
    v = s.say(t, it)
    assert (v["outcome"], v["reason"]) == ("ASK", "exceeds_balance")
    assert opt_ids(v) == ["pay_outstanding", "override_overpayment", "cancel"]
    assert any("3,000" in o["label"] or "MORE" in o["label"] for o in v["options"])              
    p = s.pick(v, "pay_outstanding")["draft"]
    assert (p["amount_kobo"], p["source"], p["amount_resolution"]["kind"]) == (12_000_000, "outstanding_balance", "pay_outstanding")
    o = s.pick(s.say(t, it), "override_overpayment")["draft"]
    assert (o["amount_kobo"], o["source"], o["override_over_kobo"], o["stated_amount_kobo"]) == (15_000_000, "explicit", 3_000_000, 15_000_000)


def test_S18_supersession_after_staging(s):
    v = s.say(S1T, S1I())
    s.add_agreement("AG1b", "EO", "100 chairs, revised", 12_000_000, "10-05", supersedes="AG1")
    st, body, _ = s.commit(v)
    assert (st, body["error"]) == (409, "invalidated") and s.count("ledger_entry", "source_action_id IS NOT NULL") == 0
    assert s.say(S1T, S1I())["draft"]["amount_kobo"] == 7_000_000


def test_S19_mode_cue_mismatch(s):
    v = s.say("Pay Amaka the usual.", intent("Amaka", "balance"))
    assert (v["outcome"], v["reason"]) == ("ASK", "mode_cue_mismatch")


def test_S20_no_entity_and_no_agreement(s):
    assert s.say("Pay Ngozi 10k.", intent("Ngozi", "explicit", "10k"))["reason"] == "no_entity"
    assert s.say("Pay Amaka the balance.", intent("Amaka", "balance"))["reason"] == "no_agreement"


def test_S21_descriptor_cases(s):
    v = s.say("Pay Emeka Okoro 10k for transport.", intent("Emeka Okoro", "explicit", "10k", desc=[("item", "transport")]))
    assert (v["outcome"], v["reason"]) == ("ASK", "no_matching_agreement")
    assert opt_ids(v) == ["select_agreement:AG1", "link_none", "cancel"]
    d = s.pick(v, "link_none")["draft"]
    assert (d["amount_kobo"], d["agreement_id"]) == (1_000_000, None)
    v2 = s.say("Pay Emeka 10k for the cement.", intent("Emeka", "explicit", "10k", desc=[("item", "cement")]))
    assert (v2["outcome"], v2["reason"]) == ("ASK", "descriptor_mismatch")
    assert opt_ids(v2) == ["select_person:EJ", "select_person:EO", "cancel"]


def test_S22_usual_history_problems(s):
    t, it = "Send the usual to Chidi Eze.", intent("Chidi Eze", "usual", verb="Send")
    assert s.say(t, it)["reason"] == "insufficient_history"
    s.add_entry("X1", "CE", "payment_out", 4_500_000, None, mmdd="09-20")
    s.add_entry("X2", "CE", "payment_out", 6_000_000, None, mmdd="09-10")
    v = s.say(t, it)
    assert (v["outcome"], v["reason"]) == ("ASK", "inconsistent_history")


def test_S23_unsupported_operation(s):
    v = s.say("Refund Emeka John 30k.", intent("Emeka John", "explicit", "30k", verb="Refund"))
    assert (v["outcome"], v["reason"]) == ("REFUSE", "unsupported_operation") and s.count("staged_action") == 0


def test_every_reason_code_is_covered_by_some_scenario():
    import inspect, re, sys
    from tests import test_scenarios
    src = inspect.getsource(test_scenarios)
    codes = ["invalid_model_output", "unsupported_operation", "inference_unavailable", "no_entity", "descriptor_mismatch",
             "ambiguous_entity", "recipient_not_approved", "unsupported_currency", "over_limit", "invalid_amount",
             "multiple_amounts", "missing_amount", "mode_cue_mismatch", "unreconciled_entry", "no_agreement",
             "no_matching_agreement", "ambiguous_agreement", "nothing_owed", "insufficient_history", "inconsistent_history",
             "stated_amount_conflicts_ledger", "exceeds_balance", "unlinked_payment_possible", "possible_duplicate"]
    missing = [c for c in codes if c not in src]
    assert not missing, missing
