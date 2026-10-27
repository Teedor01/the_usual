import json

import httpx
import pytest

from app import lifecycle as L
from app.adapters import InferenceUnavailable, NebiusAdapter, OutputTruncated, SYSTEM_PROMPT, _extract_json_text
from tests.helpers import intent


def make(handler, **kw):
    return NebiusAdapter("test-key", "https://example.invalid/v1", "test-model", transport=httpx.MockTransport(handler), **kw)


def chat(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_request_shape_and_success():
    seen = {}
    def h(req):
        seen.update(url=str(req.url), body=json.loads(req.content), auth=req.headers["authorization"])
        return chat(json.dumps(intent("Amaka", "explicit", "50k")))
    out = make(h).parse("Pay Amaka 50k")
    assert json.loads(out)["recipient_mention"] == "Amaka"
    assert seen["url"].endswith("/chat/completions") and seen["auth"] == "Bearer test-key"
    b = seen["body"]
    assert b["model"] == "test-model" and b["temperature"] == 0 and b["messages"][-1] == {"role": "user", "content": "Pay Amaka 50k"}
    assert "copied exactly" in b["messages"][0]["content"] and "response_format" not in b


def test_structured_output_flag_adds_response_format():
    seen = {}
    def h(req):
        seen.update(json.loads(req.content)); return chat("{}")
    make(h, structured_output=True).parse("x")
    assert seen["response_format"]["json_schema"]["strict"] is True


@pytest.mark.parametrize("content,expected", [('```json\n{"a":1}\n```', '{"a":1}'), ('```\n{"a":1}\n```', '{"a":1}'), ('{"a":1}', '{"a":1}'), ("  {}  ", "{}")])
def test_fence_stripping_only(content, expected):
    assert _extract_json_text(content) == expected


def test_failures_map_to_unavailable():
    def boom_transport(req): raise httpx.ConnectTimeout("t")
    for handler in (lambda r: httpx.Response(500), lambda r: httpx.Response(429), lambda r: httpx.Response(401),
                    lambda r: httpx.Response(200, json={"nope": 1}), lambda r: httpx.Response(200, text="not json"), boom_transport):
        with pytest.raises(InferenceUnavailable):
            make(handler).parse("Pay Amaka 50k")


def test_list_models():
    a = make(lambda r: httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]}))
    assert a.list_models() == ["m1", "m2"]


def test_from_env(monkeypatch):
    for k in ("NEBIUS_API_KEY", "NEBIUS_BASE_URL", "NEBIUS_MODEL"):
        monkeypatch.delenv(k, raising=False)
    assert NebiusAdapter.from_env() is None                                      
    monkeypatch.setenv("NEBIUS_API_KEY", "k"); monkeypatch.setenv("NEBIUS_BASE_URL", "https://x.invalid/v1/")
    assert NebiusAdapter.from_env() is None                                      
    monkeypatch.setenv("NEBIUS_MODEL", "m")
    a = NebiusAdapter.from_env()
    assert a.model_id == "m" and a.base_url == "https://x.invalid/v1"


def test_adapter_through_the_parse_pipeline(s):
    good = make(lambda r: chat("```json\n" + json.dumps(intent("Amaka", "explicit", "50k")) + "\n```"))
    assert L.run_parse(good, "Pay Amaka 50k")["status"] == "ok"
    invented = make(lambda r: chat(json.dumps(intent("Amaka", "explicit", "75k"))))        
    p = L.run_parse(invented, "Pay Amaka 50k")
    assert p["status"] == "invalid" and p["calls"] == 2 and "not_verbatim" in p["error"]
    assert L.run_parse(make(lambda r: httpx.Response(503)), "Pay Amaka 50k")["status"] == "unavailable"
    v = s.say("Pay Amaka 50k", adapter=make(lambda r: httpx.Response(503)))
    assert (v["outcome"], v["reason"]) == ("WAIT", "inference_unavailable")
    v2 = s.say("Pay Bassey 80k", adapter=make(lambda r: chat(json.dumps({**intent("Bassey", "explicit", "80k"), "amount_kobo": 8000000}))))
    assert v2["reason"] == "invalid_model_output"                                        


# ---- regressions for failures observed against the real endpoint ----------------------------------------
def reasoning_reply(content, finish="stop"):
    return httpx.Response(200, json={"choices": [{"finish_reason": finish, "message": {
        "role": "assistant", "content": content, "reasoning": "Here's a thinking process: ...",
        "reasoning_content": "Here's a thinking process: ...", "tool_calls": None}}]})


def test_truncated_reasoning_is_never_parsed_as_an_answer():
    """Observed: at max_tokens=400 a reasoning model spent all 400 tokens thinking (finish_reason=length) and its
    unfinished thinking appeared in `content`."""
    a = make(lambda r: reasoning_reply("Here's a thinking process:\n1. **Analyze User Input:** ...", finish="length"))
    with pytest.raises(OutputTruncated):
        a.parse("Pay Amaka 50k")


def test_truncation_is_invalid_output_not_an_outage(s):
    a = make(lambda r: reasoning_reply("Here's a thinking process: ...", finish="length"))
    p = L.run_parse(a, "Pay Amaka 50k")
    assert p["status"] == "invalid" and p["error"].startswith("truncated") and p["calls"] == 2
    v = s.say("Pay Amaka 50k", adapter=a)
    assert (v["outcome"], v["reason"]) == ("ASK", "invalid_model_output")       
    assert s.count("staged_action") == 0


def test_reasoning_model_reply_uses_content_only():
    good = json.dumps(intent("Amaka", "explicit", "50k"))
    a = make(lambda r: reasoning_reply(good))
    assert json.loads(a.parse("Pay Amaka 50k"))["amount_phrase"] == "50k"
    assert L.run_parse(a, "Pay Amaka 50k")["status"] == "ok"


def test_observed_invented_field_is_rejected_not_repaired(s):
    """Observed: the model, never shown the schema, returned 'descriptor_text' instead of 'descriptors'."""
    wrong = json.dumps({"verb": "Pay", "recipient_mention": "Amaka", "descriptor_text": None, "amount_phrase": "50k",
                        "purpose_phrase": None, "amount_mode": "explicit"})
    v = s.say("Pay Amaka 50k", adapter=make(lambda r: reasoning_reply(wrong)))
    assert (v["outcome"], v["reason"]) == ("ASK", "invalid_model_output") and s.count("staged_action") == 0


def test_prompt_shows_the_exact_schema_and_examples():
    for key in ("verb", "recipient_mention", "descriptors", "amount_mode", "amount_phrase", "purpose_phrase"):
        assert f'"{key}"' in SYSTEM_PROMPT
    for mode in ("explicit", "balance", "usual", "balance_stated", "usual_stated", "none"):
        assert mode in SYSTEM_PROMPT
    assert "descriptor_text" not in SYSTEM_PROMPT and "Examples:" in SYSTEM_PROMPT


def test_prompt_examples_do_not_leak_benchmark_or_smoke_texts():
    from pathlib import Path
    from polygraph import cases as C
    examples = [l.split("Instruction: ", 1)[1] for l in SYSTEM_PROMPT.splitlines() if l.startswith("Instruction: ")]
    assert len(examples) == 3
    smoke = [c["text"] for c in json.loads(Path("scripts/smoke_cases.json").read_text())]
    pool = {c["text"].lower() for c in C.ALL} | {t.lower() for t in smoke}
    assert not {e.lower() for e in examples} & pool
    names = {"tunde", "kemi", "roofing"}
    assert not any(n in t for t in pool for n in names)                      


def test_token_budget_timeout_and_extra_body(monkeypatch):
    seen = {}
    def h(req):
        seen.update(json.loads(req.content)); return chat("{}")
    a = make(h)
    assert a.max_tokens == 4096 and a.client.timeout.read == 120.0
    a.parse("x")
    assert seen["max_tokens"] == 4096
    b = NebiusAdapter("k", "https://x.invalid/v1", "m", max_tokens=777, extra_body={"model": "HIJACK", "messages": [], "foo": 1, "x": {"y": 2}},
                      transport=httpx.MockTransport(h))
    b.parse("hello")
    assert seen["max_tokens"] == 777 and seen["foo"] == 1 and seen["x"] == {"y": 2}
    assert seen["model"] == "m" and seen["messages"][-1]["content"] == "hello"      
    for k, v in (("NEBIUS_API_KEY", "k"), ("NEBIUS_BASE_URL", "https://x.invalid/v1"), ("NEBIUS_MODEL", "m"),
                 ("NEBIUS_MAX_TOKENS", "6000"), ("NEBIUS_TIMEOUT", "30"), ("NEBIUS_EXTRA_BODY", '{"foo": 2}')):
        monkeypatch.setenv(k, v)
    e = NebiusAdapter.from_env()
    assert e.max_tokens == 6000 and e.client.timeout.read == 30.0 and e.extra_body == {"foo": 2}
