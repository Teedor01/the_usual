"""Model-call budget: caps, atomicity under real concurrency, day rollover, metering only the live model."""
import json
import multiprocessing as mp
import warnings

import httpx
import pytest

warnings.filterwarnings("ignore")
from fastapi.testclient import TestClient  

from app import lifecycle as L  
from app.adapters import NebiusAdapter 
from app.api import create_app  
from app.budget import BudgetedAdapter, BudgetExhausted  
from app.config import Settings  
from tests import workers as W  
from tests.helpers import ManualTime, intent 


def test_per_session_cap(env):
    a, b = env.new(), env.new()
    assert [env.reg.charge_model_call(a.sid, 3, 100) for _ in range(5)] == [True, True, True, False, False]
    assert env.reg.charge_model_call(b.sid, 3, 100) is True                     
    assert env.reg.model_usage(a.sid) == {"session_used": 3, "day_used": 4}


def test_global_daily_cap_and_rollover(env):
    a, b = env.new(), env.new()
    assert env.reg.charge_model_call(a.sid, 99, 2) and env.reg.charge_model_call(b.sid, 99, 2)
    assert env.reg.charge_model_call(a.sid, 99, 2) is False                      
    env.time.advance(24 * 3600)                                                   
    assert env.reg.charge_model_call(a.sid, 99, 2) is True
    assert env.reg.charge_model_call(a.sid, 2, 99) is False                     


def test_denied_charge_changes_nothing(env):
    a = env.new()
    env.reg.charge_model_call(a.sid, 1, 100)
    before = env.reg.model_usage(a.sid)
    assert env.reg.charge_model_call(a.sid, 1, 100) is False
    assert env.reg.model_usage(a.sid) == before


@pytest.mark.parametrize("round_", range(3))
def test_cap_holds_under_real_concurrency(env, round_):
    """8 processes race for 4 allowed calls: exactly 4 succeed, never 5."""
    s = env.new()
    ctx = mp.get_context("spawn")
    barrier, q = ctx.Barrier(8), ctx.Queue()
    procs = [ctx.Process(target=W.charge_worker, args=(str(env.reg.registry_path), barrier, str(env.reg.sessions_dir),
                                                       str(env.template), s.sid, 4, 1000, q)) for _ in range(8)]
    for p in procs:
        p.start()
    res = [q.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0
    assert sorted(r[0] for r in res) == ["denied"] * 4 + ["ok"] * 4, res
    assert env.reg.model_usage(s.sid)["session_used"] == 4


def test_budgeted_adapter_never_calls_the_model_when_denied():
    calls = []
    inner = type("M", (), {"model_id": "m", "parse": lambda self, t: calls.append(t) or "{}"})()
    allowed = iter([True, False])
    a = BudgetedAdapter(inner, lambda: next(allowed))
    assert a.parse("x") == "{}" and a.model_id == "m"
    with pytest.raises(BudgetExhausted):
        a.parse("y")
    assert calls == ["x"]                                                           


def test_exhausted_budget_is_a_wait_with_its_own_reason_and_stages_nothing(s):
    class Denied:
        model_id = "m"
        def parse(self, t):
            raise BudgetExhausted("budget_exhausted")
    v = s.say("Pay Amaka 50k", adapter=Denied())
    assert (v["outcome"], v["reason"], v["draft"]) == ("WAIT", "model_budget_exhausted", None)
    assert "limit on language-model calls" in v["message"] and s.count("staged_action") == 0


def live(counter):
    def handler(req):
        counter.append(1)
        body = json.loads(req.content)
        text = body["messages"][-1]["content"]
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(
            intent("Amaka", "explicit", "50k"))}}]}) if text == "Pay Amaka 50k" else httpx.Response(503)
    return NebiusAdapter("k", "https://x.invalid/v1", "m", transport=httpx.MockTransport(handler))


def client_with(adapter, tmp_path, per_session, per_day=1000):
    s = Settings(tmp_path, time_source=ManualTime(), adapter_factory=lambda ctx: adapter,
                 max_model_calls_per_session=per_session, max_model_calls_per_day=per_day)
    c = TestClient(create_app(s))
    return c, c.post("/api/sessions").json()["session_id"]


def test_http_live_model_is_metered_and_stops_at_the_cap(tmp_path):
    calls = []
    c, sid = client_with(live(calls), tmp_path, per_session=3)
    for i in range(3):
        r = c.post(f"/api/sessions/{sid}/instructions", json={"text": "Pay Amaka 50k", "idempotency_key": f"k{i}"}).json()
        assert r["outcome"] == "STAGE"
    r4 = c.post(f"/api/sessions/{sid}/instructions", json={"text": "Pay Amaka 50k", "idempotency_key": "k4"}).json()
    assert (r4["outcome"], r4["reason"]) == ("WAIT", "model_budget_exhausted")
    assert len(calls) == 3                                                           
    b = c.get(f"/api/sessions/{sid}/budget").json()
    assert b == {"session_used": 3, "day_used": 3, "session_cap": 3, "day_cap": 1000}


def test_http_failed_attempts_still_cost_budget(tmp_path):
    calls = []
    c, sid = client_with(live(calls), tmp_path, per_session=10)
    r = c.post(f"/api/sessions/{sid}/instructions", json={"text": "something else", "idempotency_key": "k"}).json()
    assert (r["outcome"], r["reason"]) == ("WAIT", "inference_unavailable")
    assert len(calls) == 2 and c.get(f"/api/sessions/{sid}/budget").json()["session_used"] == 2   


def test_http_the_free_rule_parser_is_never_charged(tmp_path):
    c = TestClient(create_app(Settings(tmp_path, time_source=ManualTime(), max_model_calls_per_session=0, max_model_calls_per_day=0)))
    sid = c.post("/api/sessions").json()["session_id"]
    r = c.post(f"/api/sessions/{sid}/instructions", json={"text": "Pay Amaka 50k", "idempotency_key": "k"}).json()
    assert r["outcome"] == "STAGE"                                               
    assert c.get(f"/api/sessions/{sid}/budget").json()["session_used"] == 0
    info = c.get("/api/info").json()
    assert info["max_model_calls_per_session"] == 0


def test_budget_endpoint_requires_a_live_session(tmp_path):
    c = TestClient(create_app(Settings(tmp_path, time_source=ManualTime())))
    assert c.get("/api/sessions/" + "0" * 32 + "/budget").status_code == 410
    assert c.get("/api/sessions/bad/budget").status_code == 400


def test_budget_exhaustion_can_be_retried_only_by_the_user_and_is_still_capped(tmp_path):
    calls = []
    c, sid = client_with(live(calls), tmp_path, per_session=1)
    r = c.post(f"/api/sessions/{sid}/instructions", json={"text": "Pay Amaka 50k", "idempotency_key": "a"}).json()
    assert r["outcome"] == "STAGE"
    r2 = c.post(f"/api/sessions/{sid}/instructions", json={"text": "Pay Amaka 50k", "idempotency_key": "b"}).json()
    assert r2["reason"] == "model_budget_exhausted"
    rr = c.post(f"/api/sessions/{sid}/instructions/{r2['instruction_id']}/retry").json()
    assert rr["reason"] == "model_budget_exhausted" and len(calls) == 1               
