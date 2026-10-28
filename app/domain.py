import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .timeutil import iso


# ---------- data ------------------------------------------------------------------------------
@dataclass(frozen=True)
class Person:
    id: str
    legal_name: str
    role: str
    payee_status: str
    aliases: tuple = ()


@dataclass(frozen=True)
class Agreement:
    id: str
    person_id: str
    description: str
    total_kobo: int
    agreed_at: str
    valid_until: Optional[str]
    supersedes_id: Optional[str]


@dataclass(frozen=True)
class Entry:
    id: str
    person_id: str
    entry_type: str
    amount_kobo: int
    agreement_id: Optional[str]
    target_entry_id: Optional[str]
    occurred_at: str
    reconciliation: str
    override_over_kobo: Optional[int] = None


@dataclass(frozen=True)
class Policy:
    per_payment_limit_kobo: int
    recurring_min_count: int
    duplicate_window_hours: int
    draft_ttl_seconds: int
    version: int


# ---------- normalization and matching -----------------------------------------------------------
def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower().strip()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def match_persons(persons, mention: str):
    """Exact equality of the normalized mention with a normalized legal name or alias.
    No fuzzy matching: 'Emeke' never resolves to 'Emeka'."""
    m = normalize(mention)
    if not m:
        return []
    out = []
    for p in persons:
        names = {normalize(p.legal_name)} | {normalize(a) for a in p.aliases}
        if m in names:
            out.append(p)
    return sorted(out, key=lambda p: p.id)


_STOP = {"the", "those", "that", "this", "these", "for", "a", "an", "my", "our", "some", "of", "to", "from"}


def _stem(t: str) -> str:
    return t[:-1] if len(t) > 3 and t.endswith("s") else t


def desc_tokens(s: str):
    return [_stem(t) for t in normalize(s).split() if t not in _STOP]


def desc_matches(desc_text: str, agreement_description: str) -> bool:
    """Exact token equality after lowercasing and stripping one trailing 's' (tokens > 3 chars)."""
    dt = desc_tokens(desc_text)
    if not dt:
        return False
    at = {_stem(t) for t in normalize(agreement_description).split()}
    return all(t in at for t in dt)


def role_matches(desc_text: str, role: str) -> bool:
    return _stem(normalize(desc_text)) == _stem(normalize(role))


# ---------- agreements and chains --------------------------------------------------------------------
def current_heads(agreements, now_iso: str):
    """Heads: no successor and not past valid_until. Zero-outstanding heads are still heads."""
    successors = {a.supersedes_id for a in agreements if a.supersedes_id}
    heads = [a for a in agreements
             if a.id not in successors and (a.valid_until is None or a.valid_until > now_iso)]
    return sorted(heads, key=lambda a: a.id)


def chain_of(head: Agreement, agreements):
    by_id = {a.id: a for a in agreements}
    out, seen, cur = [], set(), head
    while cur is not None and cur.id not in seen:
        out.append(cur)
        seen.add(cur.id)
        cur = by_id.get(cur.supersedes_id) if cur.supersedes_id else None
    return out


def chain_start(chain) -> str:
    return min(a.agreed_at for a in chain)


# ---------- effective payments -------------------------------------------------------------------------
def effective_amounts(entries):
    """eff(p) = amount - sum(reconciled reversals of p) + sum(reconciled reinstatements of those
    reversals). Only reconciled adjustments count. Result is in [0, amount] given the DB caps."""
    by_id = {e.id: e for e in entries}
    rev = defaultdict(int)
    reinst = defaultdict(int)
    for e in entries:
        if e.reconciliation != "reconciled":
            continue
        if e.entry_type == "reversal":
            rev[e.target_entry_id] += e.amount_kobo
        elif e.entry_type == "reinstatement":
            r = by_id.get(e.target_entry_id)
            if r is not None:
                reinst[r.target_entry_id] += e.amount_kobo
    return {e.id: e.amount_kobo - rev[e.id] + reinst[e.id] for e in entries if e.entry_type == "payment_out"}


def chain_paid(chain, entries, eff) -> int:
    ids = {a.id for a in chain}
    return sum(eff[e.id] for e in entries if e.entry_type == "payment_out" and e.agreement_id in ids)


def chain_outstanding(head, agreements, entries, eff) -> int:
    ch = chain_of(head, agreements)
    return head.total_kobo - chain_paid(ch, entries, eff)


def usual_window(entries, eff, n: int):
    """Most recent payment_out rows (effective > 0), newest first, up to n. refund_out never counts."""
    pays = [e for e in entries if e.entry_type == "payment_out" and eff[e.id] > 0]
    pays.sort(key=lambda e: (e.occurred_at, e.id), reverse=True)
    return pays[:n]


def unlinked_applicable(entries, eff, start_iso: str):
    """Unlinked effective payments on/after the chain start: possibly meant for this agreement."""
    return sorted(
        [e for e in entries if e.entry_type == "payment_out" and e.agreement_id is None
         and eff[e.id] > 0 and e.occurred_at >= start_iso],
        key=lambda e: e.id)


def unlinked_all(entries, eff):
    return sorted([e for e in entries if e.entry_type == "payment_out" and e.agreement_id is None
                   and eff[e.id] > 0], key=lambda e: e.id)


def duplicates(entries, eff, amount_kobo: int, now: datetime, hours: int):
    lo = iso(now - timedelta(hours=hours))
    hi = iso(now)
    return sorted([e for e in entries if e.entry_type == "payment_out" and eff[e.id] == amount_kobo
                   and lo <= e.occurred_at <= hi], key=lambda e: e.id)


# ---------- hashing ---------------------------------------------------------------------------------------
def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(obj) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


def read_set_digest(*, q1_persons, policy: Policy, recipient: Person, agreements_all, head_ids, entries) -> str:
    """Digest over the RESULT ROWS of the read set (not over previously seen row IDs), so an
    inserted, deleted or changed row inside any queried range changes it.
      Q1 persons matching the mention; Q2 policy; Q3 recipient payee status;
      Q4 agreements (all, for every Q1 person) plus current head ids evaluated at 'now';
      Q5 every ledger entry of the resolved recipient."""
    q1 = [[p.id, p.legal_name, p.role, p.payee_status, sorted(p.aliases)] for p in q1_persons]
    q2 = [policy.per_payment_limit_kobo, policy.recurring_min_count, policy.duplicate_window_hours,
          policy.draft_ttl_seconds, policy.version]
    q3 = [recipient.id, recipient.payee_status]
    q4 = {"agreements": sorted([[a.id, a.person_id, a.description, a.total_kobo, a.agreed_at,
                                 a.valid_until, a.supersedes_id] for a in agreements_all]),
          "heads": sorted(head_ids)}
    q5 = sorted([[e.id, e.entry_type, e.amount_kobo, e.agreement_id, e.target_entry_id,
                  e.occurred_at, e.reconciliation, e.override_over_kobo] for e in entries])
    return sha256_hex({"q1": sorted(q1), "q2": q2, "q3": q3, "q4": q4, "q5": q5})


def payload_hash(*, recipient_id, amount_kobo, currency, agreement_id, source, stated_amount_kobo,
                 amount_resolution, ack_duplicate_ids, ack_unlinked_ids, policy_version) -> str:
    return sha256_hex({
        "recipient": recipient_id, "amount_kobo": amount_kobo, "currency": currency,
        "agreement": agreement_id, "source": source, "stated": stated_amount_kobo,
        "resolution": amount_resolution, "ack_dup": sorted(ack_duplicate_ids),
        "ack_unlinked": sorted(ack_unlinked_ids), "policy_version": policy_version,
    })
