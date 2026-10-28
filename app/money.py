import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

_FOREIGN = re.compile(r"[$€£]|\b(?:usd|eur|gbp|dollars?|euros?|pounds?)\b", re.I)
_AMT = re.compile(
    r"(?<![A-Za-z0-9.,])(?:₦|(?<![A-Za-z])N(?=\s?\d))?\s?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<k>\s?[kK](?![A-Za-z]))?"
    r"(?:\s?(?:naira|ngn)(?![A-Za-z]))?",
    re.I,
)

BALANCE_CUES = re.compile(r"\b(?:balance|remaining|remainder|the rest|wetin remain|remain)\b", re.I)
USUAL_CUES = re.compile(r"\b(?:the usual|as usual|usual|same as always|normal one|the normal)\b", re.I)


@dataclass(frozen=True)
class ParsedAmount:
    status: str 
    kobo: Optional[int] = None


def parse_amount_phrase(phrase: str) -> ParsedAmount:
    if _FOREIGN.search(phrase):
        return ParsedAmount("unsupported_currency")
    matches = list(_AMT.finditer(phrase))
    if len(matches) > 1:
        return ParsedAmount("multiple")
    if not matches:
        return ParsedAmount("invalid")
    m = matches[0]
    try:
        naira = Decimal(m.group("num").replace(",", ""))
    except InvalidOperation:
        return ParsedAmount("invalid")
    if m.group("k"):
        naira *= 1000
    kobo = naira * 100
    if kobo != kobo.to_integral_value():
        return ParsedAmount("invalid")  # sub-kobo
    kobo_int = int(kobo)
    if kobo_int <= 0:
        return ParsedAmount("invalid")
    return ParsedAmount("ok", kobo_int)


def fmt_naira(kobo: int) -> str:
    sign = "-" if kobo < 0 else ""
    kobo = abs(kobo)
    naira, rem = divmod(kobo, 100)
    s = f"{sign}₦{naira:,}"
    return s + (f".{rem:02d}" if rem else "")
