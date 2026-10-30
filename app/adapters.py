import json
import os
import re
from typing import Callable, Optional

import httpx

from .domain import normalize
from .money import BALANCE_CUES, USUAL_CUES


class InferenceUnavailable(Exception):
    pass


# ---- test double -------------------------------------------------------------------------------
class ScriptedModel:
    model_id = "scripted-double"

    def __init__(self, script=None, default=None):
        """script: {instruction_text: output | Exception | [outputs...]}; each call pops the next
        output (the last one repeats). default: callable(text) -> output."""
        self.script = script or {}
        self.default = default
        self.calls = []

    def parse(self, text):
        self.calls.append(text)
        if text in self.script:
            v = self.script[text]
            if isinstance(v, list):
                n = sum(1 for c in self.calls if c == text) - 1
                v = v[min(n, len(v) - 1)]
        elif self.default:
            v = self.default(text)
        else:
            raise InferenceUnavailable("no script for text")
        if isinstance(v, Exception):
            raise v
        return v


# ---- rule parser (keyless) ----------------------------------------------------------------------
NON_PAYMENT_VERBS = {"refund", "lend", "borrow", "invoice", "bill", "charge", "reverse"}
PAY_VERBS = {"pay", "send", "settle", "clear", "give", "transfer", "remit", "sort"}
_NUM = re.compile(r"(?:[$€£₦]|(?<![A-Za-z])N(?=\d))?\s?\d[\d,]*(?:\.\d+)?\s?(?:k(?![A-Za-z])|naira|ngn)?", re.I)


class RuleParser:
    """Naive rule-based parser. Uses the ledger only for exact name/alias vocabulary."""
    model_id = "rule-parser-v1"

    def __init__(self, names: Callable[[], list] = lambda: [], vocab: Callable[[], list] = lambda: []):
        self.names, self.vocab = names, vocab

    def parse(self, text):
        low = text.lower()
        words = re.findall(r"[A-Za-z']+", text)
        verb = next((w for w in words if w.lower() in PAY_VERBS | NON_PAYMENT_VERBS), words[0] if words else "")
        nt = normalize(text)
        mention = ""
        best = 0
        for n in self.names():
            nn = normalize(n)
            if nn and re.search(rf"\b{re.escape(nn)}\b", nt) and len(nn) > best:
                best, mention = len(nn), n
        if mention:
            m = re.search(re.escape(mention), text, re.I)
            mention = m.group(0) if m else mention
        else:
            after = re.search(rf"\b{re.escape(verb)}\s+(?:am\s+)?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", text) if verb else None
            mention = after.group(1) if after else (words[1] if len(words) > 1 else "")
        nums = list(_NUM.finditer(text))
        phrase = text[nums[0].start():nums[-1].end()].strip() if nums else None
        bal, usu = bool(BALANCE_CUES.search(text)), bool(USUAL_CUES.search(text))
        mode = ("balance_stated" if bal else "usual_stated" if usu else "explicit") if phrase else \
               ("balance" if bal else "usual" if usu else "none")
        descs = []
        for w in words:
            if w.lower() in ("supplier", "suppliers", "customer", "customers"):
                descs.append({"type": "role", "text": w})
        vocab = {normalize(v) for v in self.vocab()}
        skip = set(normalize(mention).split()) | {verb.lower()}
        for w in words:
            wl = normalize(w)
            if wl and wl not in skip and (wl in vocab or (wl.endswith("s") and wl[:-1] in vocab)):
                descs.append({"type": "item", "text": w})
        return json.dumps({"verb": verb, "recipient_mention": mention, "descriptors": descs,
                           "amount_mode": mode, "amount_phrase": phrase, "purpose_phrase": None})


# ---- live adapter ---------------------------------------------------------------------------------
INTENT_JSON_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["verb", "recipient_mention", "descriptors", "amount_mode", "amount_phrase", "purpose_phrase"],
    "properties": {
        "verb": {"type": "string"},
        "recipient_mention": {"type": "string"},
        "descriptors": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["type", "text"],
            "properties": {"type": {"enum": ["role", "item", "time"]}, "text": {"type": "string"}}}},
        "amount_mode": {"enum": ["explicit", "balance", "usual", "balance_stated", "usual_stated", "none"]},
        "amount_phrase": {"type": ["string", "null"]},
        "purpose_phrase": {"type": ["string", "null"]},
    },
}

SYSTEM_PROMPT = (
    "You extract structure from a short payment instruction written by a small merchant in Nigeria. "
    "The text may be informal, may use Nigerian Pidgin, or may mix English with other languages.\n\n"
    "Output ONLY one JSON object with exactly these six keys and no others:\n"
    '{"verb": string, "recipient_mention": string, '
    '"descriptors": [{"type": "role" | "item" | "time", "text": string}], '
    '"amount_mode": "explicit" | "balance" | "usual" | "balance_stated" | "usual_stated" | "none", '
    '"amount_phrase": string | null, "purpose_phrase": string | null}\n\n'
    "Rules: every string you output (verb, recipient_mention, descriptor text, amount_phrase, purpose_phrase) "
    "MUST be copied exactly from the instruction. NEVER invent, convert or compute amounts. NEVER output IDs. "
    "amount_phrase is the amount exactly as written (for example '50k' or 'N12.5k') or null if no amount is "
    "written. amount_mode: 'explicit' = an amount is written and there is no balance/usual cue; 'balance' = asks "
    "to pay what remains (balance, remaining, the rest, wetin remain) and NO amount is written; 'usual' = asks "
    "for 'the usual' and NO amount is written; 'balance_stated' / 'usual_stated' = an amount is written AND a "
    "balance/usual cue is present; 'none' = no amount and no cue. descriptors is a list (use [] when empty): "
    "role words (supplier, customer) or item words that identify the order (for example 'chairs'); include a "
    "'time' descriptor only for a clear time phrase.\n\n"
    "Examples:\n"
    'Instruction: Abeg pay Tunde 15k\n'
    '{"verb":"pay","recipient_mention":"Tunde","descriptors":[],"amount_mode":"explicit",'
    '"amount_phrase":"15k","purpose_phrase":null}\n'
    'Instruction: Send Kemi the remaining 20k for the roofing sheets\n'
    '{"verb":"Send","recipient_mention":"Kemi","descriptors":[{"type":"item","text":"roofing sheets"}],'
    '"amount_mode":"balance_stated","amount_phrase":"20k","purpose_phrase":null}\n'
    'Instruction: Pay Tunde the usual\n'
    '{"verb":"Pay","recipient_mention":"Tunde","descriptors":[],"amount_mode":"usual",'
    '"amount_phrase":null,"purpose_phrase":null}'
)


REQUIRED_LIVE_ENV = ("NEBIUS_API_KEY", "NEBIUS_BASE_URL", "NEBIUS_MODEL")


class MissingCredentials(RuntimeError):
    """Strict mode (USUAL_REQUIRE_LIVE_MODEL=1) is on but the Nebius credentials are absent or still placeholders."""


def live_model_required() -> bool:
    """Opt-in strict mode for hosted deployments. Off by default so local runs and the test suite keep the
    documented keyless rule-parser behaviour."""
    return os.environ.get("USUAL_REQUIRE_LIVE_MODEL", "").strip().lower() in ("1", "true", "yes", "on")


def problems_with_live_env() -> list:
    """Names (never values) of required Nebius variables that are unset, empty or still 'paste-...' placeholders."""
    bad = []
    for k in REQUIRED_LIVE_ENV:
        v = (os.environ.get(k) or "").strip()
        if not v or v.lower().startswith(("paste-", "paste_", "paste ")):
            bad.append(k)
    return bad


def require_live_model_or_raise() -> None:
    """No-op unless strict mode is on. In strict mode a missing/placeholder credential is a startup error,
    never a silent switch to the rule parser."""
    if not live_model_required():
        return
    bad = problems_with_live_env()
    if bad:
        raise MissingCredentials(
            "USUAL_REQUIRE_LIVE_MODEL is set, but these model settings are missing or still placeholders: "
            + ", ".join(bad) + ". Set them in the hosting dashboard's environment settings. "
            "Refusing to start instead of silently falling back to the rule parser.")


class OutputTruncated(Exception):
    """The model hit max_tokens before finishing (finish_reason == 'length'). For a reasoning model this
    means the answer was never written; any text in `content` is unfinished thinking and must not be parsed."""


class NebiusAdapter:
    def __init__(self, api_key: str, base_url: str, model: str, *, structured_output: bool = False,
                 timeout: float = 120.0, max_tokens: int = 4096, extra_body: Optional[dict] = None,
                 transport: Optional[httpx.BaseTransport] = None):
        self.model_id = model
        self.base_url = base_url.rstrip("/")
        self.structured = structured_output
        self.max_tokens = max_tokens
        self.extra_body = extra_body or {}
        self.client = httpx.Client(timeout=timeout, transport=transport,
                                   headers={"Authorization": f"Bearer {api_key}"})

    @classmethod
    def from_env(cls) -> Optional["NebiusAdapter"]:
        """NEBIUS_API_KEY, NEBIUS_BASE_URL, NEBIUS_MODEL are required. Optional: NEBIUS_STRUCTURED_OUTPUT=1,
        NEBIUS_MAX_TOKENS (default 4096: reasoning models spend ~1.5k tokens thinking before the JSON),
        NEBIUS_TIMEOUT seconds (default 120), NEBIUS_EXTRA_BODY (a JSON object merged into every request,
        for provider-specific switches such as reasoning controls)."""
        key, base, model = (os.environ.get(k) for k in ("NEBIUS_API_KEY", "NEBIUS_BASE_URL", "NEBIUS_MODEL"))
        if not (key and base and model):
            return None
        extra = json.loads(os.environ["NEBIUS_EXTRA_BODY"]) if os.environ.get("NEBIUS_EXTRA_BODY") else None
        return cls(key, base, model, structured_output=os.environ.get("NEBIUS_STRUCTURED_OUTPUT") == "1",
                   timeout=float(os.environ.get("NEBIUS_TIMEOUT", "120")),
                   max_tokens=int(os.environ.get("NEBIUS_MAX_TOKENS", "4096")), extra_body=extra)

    def list_models(self):
        r = self.client.get(f"{self.base_url}/models")
        r.raise_for_status()
        return [m.get("id") for m in r.json().get("data", [])]

    def parse(self, text):
        return self.chat([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text}],
                         structured=self.structured)

    def chat(self, messages, *, structured=False, max_tokens=None):
        """Generic chat call (also used by the benchmark's B2 baseline). Returns message.content only.
        Raises OutputTruncated if the model did not finish, InferenceUnavailable on transport/HTTP/shape errors."""
        body = {**self.extra_body, "model": self.model_id, "temperature": 0,
                "max_tokens": max_tokens or self.max_tokens, "messages": messages}
        if structured:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "payment_intent", "schema": INTENT_JSON_SCHEMA, "strict": True}}
        try:
            r = self.client.post(f"{self.base_url}/chat/completions", json=body)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            raise InferenceUnavailable(f"transport: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise InferenceUnavailable(f"http_{r.status_code}")
        try:
            choice = r.json()["choices"][0]
            content = choice["message"]["content"]
            finish = choice.get("finish_reason")
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise InferenceUnavailable("bad_response_shape") from e
        if finish == "length":
            raise OutputTruncated(f"finish_reason=length at max_tokens={body['max_tokens']}")
        return _extract_json_text(content)


def _extract_json_text(content) -> str:
    """Strip an optional markdown code fence. Does NOT repair or reinterpret content."""
    if not isinstance(content, str):
        return ""
    s = content.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.S)
    return m.group(1) if m else s
