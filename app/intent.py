import json
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, ValidationError


class Descriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["role", "item", "time"]
    text: str


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    verb: str
    recipient_mention: str
    descriptors: list[Descriptor] = []
    amount_mode: Literal["explicit", "balance", "usual", "balance_stated", "usual_stated", "none"]
    amount_phrase: Optional[str] = None
    purpose_phrase: Optional[str] = None


class IntentError(Exception):
    pass


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def validate_intent(raw, text: str) -> Intent:
    """raw: JSON string or dict from the model. Raises IntentError on anything invalid."""
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise IntentError(f"invalid_json: {e}") from e
    else:
        data = raw
    if not isinstance(data, dict):
        raise IntentError("not_an_object")
    try:
        intent = Intent.model_validate(data)
    except ValidationError as e:
        raise IntentError(f"schema: {e.errors()[0]['type']}:{'.'.join(map(str, e.errors()[0]['loc']))}") from e
    t = _ws(text)
    phrases = [("verb", intent.verb), ("recipient_mention", intent.recipient_mention)]
    phrases += [(f"descriptor", d.text) for d in intent.descriptors]
    if intent.amount_phrase is not None:
        phrases.append(("amount_phrase", intent.amount_phrase))
    if intent.purpose_phrase is not None:
        phrases.append(("purpose_phrase", intent.purpose_phrase))
    for name, p in phrases:
        if not p.strip() or _ws(p) not in t:
            raise IntentError(f"not_verbatim:{name}")
    return intent
