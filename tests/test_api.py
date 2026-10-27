import warnings

import pytest

warnings.filterwarnings("ignore")
from fastapi.testclient import TestClient  

from app.api import GONE_BODY, create_app  
from app.config import Settings  
from tests.helpers import ManualTime  


@pytest.fixture
def api(tmp_path):
    t = ManualTime()
    app = create_app(Settings(tmp_path, time_source=t))
    c = TestClient(app)
    sid = c.post("/api/sessions").json()["session_id"]
    return c, sid, t, app


def post(c, sid, path, body):
    return c.post(f"/api/sessions/{sid}{path}", json=body)


def test_http_end_to_end_with_keyless_parser(api):
    c, sid, t, app = api
    r = post(c, sid, "/instructions", {"text": "Pay Emeka the balance for those chairs.", "idempotency_key": "k1"})
    v = r.json()
    assert r.status_code == 200 and v["outcome"] == "STAGE" and v["draft"]["amount_kobo"] == 5_000_000
    assert v["parse"]["model_id"] == "rule-parser-v1"                          # honest: not Nemotron
    d = v["draft"]
    r1 = post(c, sid, f"/actions/{d['action_id']}/commit", {"commit_key": "ck1", "confirmed_payload_hash": d["payload_hash"]})
    r2 = post(c, sid, f"/actions/{d['action_id']}/commit", {"commit_key": "ck1", "confirmed_payload_hash": d["payload_hash"]})
    assert r1.status_code == r2.status_code == 200 and r1.content == r2.content
    led = c.get(f"/api/sessions/{sid}/ledger").json()
    assert any(e["id"] == r1.json()["ledger_entry_id"] and e["amount_kobo"] == 5_000_000 for e in led["entries"])
    assert next(p for p in led["people"] if p["id"] == "EO")["agreements"][0]["outstanding_kobo"] == 0


@pytest.mark.parametrize("text,outcome,reason", [
    ("Send the usual to Emeka.", "ASK", "ambiguous_entity"),
    ("Pay Bassey 80k", "REFUSE", "recipient_not_approved"),
    ("Refund Emeka John 30k.", "REFUSE", "unsupported_operation"),
    ("Pay Amaka $100", "REFUSE", "unsupported_currency"),
    ("Pay Amaka", "ASK", "missing_amount"),
    ("Pay Ngozi 10k", "ASK", "no_entity"),
    ("Abeg pay Emeka the remaining 50k for chairs", "STAGE", None),            
    ("Oga pay Amaka 20k abeg", "STAGE", None),
])
def test_http_rule_parser_cases(api, text, outcome, reason):
    c, sid, *_ = api
    v = post(c, sid, "/instructions", {"text": text, "idempotency_key": "k-" + text}).json()
    assert (v["outcome"], v["reason"]) == (outcome, reason), v["message"]


def test_http_commit_accepts_only_confirmation_fields(api):
    c, sid, *_ = api
    d = post(c, sid, "/instructions", {"text": "Pay Amaka 50k", "idempotency_key": "k"}).json()["draft"]
    base = {"commit_key": "ck", "confirmed_payload_hash": d["payload_hash"]}
    for extra in ({"amount_kobo": 1}, {"recipient_person_id": "BU"}, {"agreement_id": "AG1"}, {"amount": 999}, {"recipient": "x"}):
        r = post(c, sid, f"/actions/{d['action_id']}/commit", {**base, **extra})
        assert r.status_code == 422, extra
    led = c.get(f"/api/sessions/{sid}/ledger").json()
    assert not any(e["id"].startswith("PAY-") for e in led["entries"])          
    assert post(c, sid, f"/actions/{d['action_id']}/commit", base).status_code == 200


def test_http_rejects_malformed_bodies(api):
    c, sid, *_ = api
    assert post(c, sid, "/instructions", {"text": "", "idempotency_key": "k"}).status_code == 422
    assert post(c, sid, "/instructions", {"text": "Pay Amaka 5k"}).status_code == 422
    assert post(c, sid, "/instructions", {"text": "x" * 501, "idempotency_key": "k"}).status_code == 422
    assert post(c, sid, "/instructions", {"text": "Pay Amaka 5k", "idempotency_key": "k", "amount_kobo": 1}).status_code == 422
    assert post(c, sid, "/instructions/x/resolve", {"ask_decision_id": "a", "option_id": "b", "amount_kobo": 1}).status_code == 422
    assert post(c, sid, "/actions/x/commit", {"commit_key": "k", "confirmed_payload_hash": "short"}).status_code == 422


def test_http_ask_resolution_flow(api):
    c, sid, *_ = api
    v = post(c, sid, "/instructions", {"text": "Pay Chidi the balance.", "idempotency_key": "k"}).json()
    assert v["reason"] == "ambiguous_entity"
    r = post(c, sid, f"/instructions/{v['instruction_id']}/resolve", {"ask_decision_id": v["decision_id"], "option_id": "select_person:CN"})
    assert r.status_code == 200 and r.json()["draft"]["amount_kobo"] == 6_000_000
    bad = post(c, sid, f"/instructions/{v['instruction_id']}/resolve", {"ask_decision_id": v["decision_id"], "option_id": "select_person:CN"})
    assert bad.status_code == 409 and bad.json()["error"] == "stale_decision"
    unk = post(c, sid, f"/instructions/{v['instruction_id']}/resolve", {"ask_decision_id": r.json()["decision_id"], "option_id": "cancel"})
    assert unk.status_code == 409 and unk.json()["error"] == "not_an_ask"


def test_http_session_gone_is_identical_for_unknown_and_ended_and_has_no_oracle(api, tmp_path):
    c, sid, t, app = api
    unknown = c.get("/api/sessions/" + "0" * 32 + "/ledger")
    reg = app.state.registry
    path = reg.sessions_dir / [r for r in reg.sessions_dir.iterdir()][0].name
    path.unlink()
    ended = c.get(f"/api/sessions/{sid}/ledger")
    assert unknown.status_code == ended.status_code == 410 and unknown.json() == ended.json() == GONE_BODY
    assert "not restored" in GONE_BODY["message"] or "gone" in GONE_BODY["message"]
    assert c.get("/api/sessions/not-an-id/ledger").status_code == 400
    # SR6 over HTTP: old commit key against a replacement session
    sid2 = c.post("/api/sessions").json()["session_id"]
    r = post(c, sid2, "/actions/anything/commit", {"commit_key": "ck", "confirmed_payload_hash": "0" * 64})
    assert r.status_code == 404
    r_old = post(c, sid, "/actions/anything/commit", {"commit_key": "ck", "confirmed_payload_hash": "0" * 64})
    assert r_old.status_code == 410                                            


def test_http_index_and_info(api):
    c, *_ = api
    assert c.get("/").status_code == 200 and "The Usual" in c.get("/").text
    info = c.get("/api/info").json()
    assert info["live_model"] is False and "no Nemotron credentials" in info["parser"]


def test_http_busy_store_returns_503_and_changes_nothing(api, monkeypatch):
    c, sid, *_ = api
    d = post(c, sid, "/instructions", {"text": "Pay Amaka 50k", "idempotency_key": "k"}).json()["draft"]
    from app import db
    monkeypatch.setattr(db, "begin_immediate", lambda conn: (_ for _ in ()).throw(db.StoreBusy("locked")))
    r = post(c, sid, f"/actions/{d['action_id']}/commit", {"commit_key": "ck", "confirmed_payload_hash": d["payload_hash"]})
    assert r.status_code == 503 and r.json()["error"] == "store_busy"
    monkeypatch.undo()
    assert post(c, sid, f"/actions/{d['action_id']}/commit", {"commit_key": "ck", "confirmed_payload_hash": d["payload_hash"]}).status_code == 200
