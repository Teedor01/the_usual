from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from . import domain as D
from . import repo
from .intent import Intent
from .money import BALANCE_CUES, USUAL_CUES, fmt_naira, parse_amount_phrase
from .timeutil import iso, parse_iso


def short_date(iso_s: str) -> str:
    d = parse_iso(iso_s)
    return f"{d.day} {d.strftime('%b')} {d.year}"

RULE_VERSION = "1"
PAYMENT_VERBS = {"pay", "send", "settle", "clear", "give", "transfer", "remit", "sort"}
BALANCE_MODES = ("balance", "balance_stated")


@dataclass
class EngineResult:
    outcome: str  # STAGE | ASK | REFUSE | WAIT
    reason: Optional[str]
    message: str
    options: list = field(default_factory=list)
    selections: list = field(default_factory=list)
    stale: bool = False
    details: dict = field(default_factory=dict)
    draft: Optional[dict] = None
    digest: Optional[str] = None


def opt(kind, label, **params):
    oid = kind + (":" + str(next(iter(params.values()))) if kind in ("select_person", "select_agreement") else "")
    return {"id": oid, "kind": kind, "label": label, "params": params}


CANCEL = opt("cancel", "Cancel this instruction")


class Pipeline:
    def __init__(self, conn, now: datetime, resolved: dict):
        self.conn, self.now, self.now_iso = conn, now, iso(now)
        self.resolved = resolved
        self.text = resolved["raw_text"]
        self.sels = [dict(s) for s in resolved.get("selections", [])]
        self.dropped = []
        self.policy = repo.load_policy(conn)
        self._ag, self._en = {}, {}

    # ---- helpers --------------------------------------------------------------------------
    def res(self, outcome, reason, message, options=None, **details):
        return EngineResult(outcome, reason, message, options or [], [dict(s) for s in self.sels],
                            bool(self.dropped), details)

    def ask(self, reason, msg, options=None, **d): return self.res("ASK", reason, msg, options, **d)
    def refuse(self, reason, msg, **d): return self.res("REFUSE", reason, msg, [], **d)
    def wait(self, reason, msg, **d): return self.res("WAIT", reason, msg, [], **d)

    def last(self, kinds):
        for s in reversed(self.sels):
            if s["kind"] in kinds:
                return s
        return None

    def drop(self, sel):
        self.sels = [s for s in self.sels if s is not sel and s != sel]
        self.dropped.append(sel)

    def agreements(self, pid):
        if pid not in self._ag:
            self._ag[pid] = repo.load_agreements(self.conn, pid)
        return self._ag[pid]

    def entries(self, pid):
        if pid not in self._en:
            self._en[pid] = repo.load_entries(self.conn, pid)
        return self._en[pid]

    def heads(self, pid):
        return D.current_heads(self.agreements(pid), self.now_iso)

    def open_balances(self, pid):
        ags, es = self.agreements(pid), self.entries(pid)
        eff = D.effective_amounts(es)
        return [{"agreement_id": h.id, "description": h.description,
                 "outstanding_kobo": D.chain_outstanding(h, ags, es, eff)} for h in self.heads(pid)]

    def person_option(self, p, kind="select_person"):
        bal = self.open_balances(p.id)
        bt = "; ".join(f"{b['description']}: {fmt_naira(b['outstanding_kobo'])} outstanding" for b in bal)
        status = "" if p.payee_status == "approved" else " [NOT APPROVED]"
        label = f"{p.legal_name} ({p.role}){status}" + (f" - {bt}" if bt else "")
        o = opt(kind, label, person_id=p.id)
        o["details"] = {"person_id": p.id, "name": p.legal_name, "role": p.role,
                        "payee_status": p.payee_status, "open_balances": bal}
        return o

    def head_option(self, h, O):
        return opt("select_agreement", f"{h.description} (agreed total {fmt_naira(h.total_kobo)}, "
                   f"outstanding {fmt_naira(O)})", agreement_id=h.id)

    # ---- pipeline ---------------------------------------------------------------------------
    def run(self) -> EngineResult:
        # P0 ------------------------------------------------------------------------------
        parse = self.resolved.get("parse", {})
        if parse.get("status") == "unavailable" and "budget_exhausted" in (parse.get("error") or ""):
            return self.wait("model_budget_exhausted", "This demo has reached its limit on language-model calls "
                             "(a cap that protects the project's credits). Nothing was done and nothing was paid.")
        if parse.get("status") == "unavailable":
            return self.wait("inference_unavailable", "The language model is unavailable. Nothing was done. "
                             "Retry when it is back.")
        if parse.get("status") != "ok" or not self.resolved.get("intent"):
            return self.ask("invalid_model_output", "I could not reliably understand that instruction. "
                            "Please rephrase it.")
        it = Intent.model_validate(self.resolved["intent"])
        if it.verb.strip().lower() not in PAYMENT_VERBS:
            return self.refuse("unsupported_operation", f"'{it.verb}' is not a supported operation. "
                               "The Usual only prepares outgoing payments.")

        # P1 ------------------------------------------------------------------------------
        persons = repo.load_persons(self.conn)
        q1 = D.match_persons(persons, it.recipient_mention)
        if not q1:
            return self.ask("no_entity", f"I could not find anyone called '{it.recipient_mention}'.")
        cands = list(q1)
        sel = self.last({"select_person"})
        if sel:
            if sel["params"].get("person_id") in {p.id for p in q1}:
                cands = [p for p in q1 if p.id == sel["params"]["person_id"]]
            else:
                self.drop(sel)
        if len(cands) > 1:
            descs = [d for d in it.descriptors if d.type in ("role", "item")]
            if descs:
                def satisfies(p):
                    for d in descs:
                        if d.type == "role":
                            if not D.role_matches(d.text, p.role):
                                return False
                        elif not any(D.desc_matches(d.text, h.description) for h in self.heads(p.id)):
                            return False
                    return True
                filt = [p for p in cands if satisfies(p)]
                if filt:
                    cands = filt
                else:
                    return self.ask("descriptor_mismatch",
                                    "None of the people named that match what you described. Who did you mean?",
                                    [self.person_option(p) for p in q1] + [CANCEL])
        if len(cands) > 1:
            return self.ask("ambiguous_entity", f"More than one person matches '{it.recipient_mention}'. "
                            "Which one?", [self.person_option(p) for p in cands] + [CANCEL])
        rcp = cands[0]

        # P2 ------------------------------------------------------------------------------
        if rcp.payee_status != "approved":
            return self.refuse("recipient_not_approved", f"{rcp.legal_name} is not an approved payee. "
                               "No payment can be prepared.", recipient=rcp.id)

        # P3 ------------------------------------------------------------------------------
        stated = None
        if it.amount_phrase:
            pa = parse_amount_phrase(it.amount_phrase)
            if pa.status == "unsupported_currency":
                return self.refuse("unsupported_currency", "Only naira (NGN) payments are supported.")
            if pa.status == "multiple":
                return self.ask("multiple_amounts", "I found more than one amount. Please give one.")
            if pa.status == "invalid":
                return self.ask("invalid_amount", f"I could not read '{it.amount_phrase}' as a naira amount.")
            stated = pa.kobo
        bal = bool(BALANCE_CUES.search(self.text))
        usu = bool(USUAL_CUES.search(self.text))
        if bal and usu:
            return self.ask("mode_cue_mismatch", "That instruction mixes 'balance' and 'usual'. Please be specific.")
        phrase = stated is not None
        mode = ("balance_stated" if bal else "usual_stated" if usu else "explicit") if phrase else \
               ("balance" if bal else "usual" if usu else "none")
        if mode == "none":
            return self.ask("missing_amount", f"How much should I pay {rcp.legal_name}?")
        if it.amount_mode != mode:
            return self.ask("mode_cue_mismatch", "The wording of the amount is inconsistent with how I read "
                            "it. Please restate the instruction.")
        if mode == "explicit" and stated > self.policy.per_payment_limit_kobo:
            return self.refuse("over_limit", f"{fmt_naira(stated)} is above your per-payment limit of "
                               f"{fmt_naira(self.policy.per_payment_limit_kobo)}.")

        # P4 ------------------------------------------------------------------------------
        ents = self.entries(rcp.id)
        eff = D.effective_amounts(ents)
        unrec = [e for e in ents if e.reconciliation != "reconciled"]
        if unrec:
            return self.wait("unreconciled_entry", f"Records for {rcp.legal_name} are still being reconciled "
                             f"({', '.join(e.id for e in unrec)}). Try again after they are reconciled.",
                             entry_ids=[e.id for e in unrec])

        # P5 ------------------------------------------------------------------------------
        ags = self.agreements(rcp.id)
        H = self.heads(rcp.id)
        items = [d for d in it.descriptors if d.type == "item"]
        filtered = [h for h in H if all(D.desc_matches(d.text, h.description) for d in items)] if items else list(H)

        def O_of(h):
            return D.chain_outstanding(h, ags, ents, eff)

        def hopts(hs, link_none):
            o = [self.head_option(h, O_of(h)) for h in hs]
            if link_none:
                o.append(opt("link_none", "Pay without linking to any agreement"))
            return o + [CANCEL]

        balance_mode = mode in BALANCE_MODES
        linked, scope_known = None, False
        sel = self.last({"select_agreement", "link_none"})
        if sel:
            if sel["kind"] == "select_agreement" and sel["params"].get("agreement_id") in {h.id for h in H}:
                linked = next(h for h in H if h.id == sel["params"]["agreement_id"])
                scope_known = True
            elif sel["kind"] == "link_none" and not balance_mode:
                scope_known = True
            else:
                self.drop(sel)
        if not scope_known:
            if balance_mode:
                if not H:
                    return self.ask("no_agreement", f"{rcp.legal_name} has no agreement on record, so there is "
                                    "no balance to pay.", [CANCEL])
                if items and not filtered:
                    return self.ask("no_matching_agreement", "None of this person's agreements match what you "
                                    "described. Pick one:", hopts(H, False))
                if len(filtered) == 1:
                    linked = filtered[0]
                else:
                    return self.ask("ambiguous_agreement", "More than one agreement matches. Which one?",
                                    hopts(filtered, False))
            else:
                if not H:
                    linked = None
                elif items:
                    if len(filtered) == 1:
                        linked = filtered[0]
                    elif len(filtered) >= 2:
                        return self.ask("ambiguous_agreement", "More than one agreement matches. Which one, "
                                        "or pay without linking?", hopts(filtered, True))
                    else:
                        return self.ask("no_matching_agreement", "None of this person's agreements match what "
                                        "you described. Pick one, or pay without linking:", hopts(H, True))
                else:
                    linked = None
        O = O_of(linked) if linked else None
        chain = D.chain_of(linked, ags) if linked else []

        # P6 ------------------------------------------------------------------------------
        asel = self.last({"pay_outstanding", "keep_stated", "override_overpayment"})
        source, resolution, amount = None, {"kind": "none"}, None
        window = []
        eff_mode = mode

        def usual_amount():
            nonlocal window
            window = D.usual_window(ents, eff, self.policy.recurring_min_count)
            if len(window) < self.policy.recurring_min_count:
                return None, self.ask("insufficient_history", f"There are only {len(window)} earlier payments to "
                                      f"{rcp.legal_name}; I need {self.policy.recurring_min_count} identical ones "
                                      "to know what 'the usual' means.")
            amts = {eff[e.id] for e in window}
            if len(amts) != 1:
                return None, self.ask("inconsistent_history", "Recent payments differ: " +
                                      ", ".join(fmt_naira(eff[e.id]) for e in window) +
                                      ". I will not guess which is 'the usual'. Give an amount.")
            return next(iter(amts)), None

        if mode in BALANCE_MODES:
            if O <= 0:
                return self.ask("nothing_owed", f"Nothing is outstanding on {linked.description} "
                                f"({fmt_naira(O)}).", [CANCEL])
            if mode == "balance" or stated == O:
                amount, source = O, "outstanding_balance"
            elif asel and asel["kind"] == "pay_outstanding":
                amount, source, resolution = O, "outstanding_balance", {"kind": "pay_outstanding"}
            elif asel and asel["kind"] == "keep_stated":
                amount, source, resolution, eff_mode = stated, "explicit", {"kind": "keep_stated"}, "explicit"
            else:
                return self.ask("stated_amount_conflicts_ledger",
                                f"You said {fmt_naira(stated)} but the ledger says {fmt_naira(O)} is outstanding.",
                                [opt("pay_outstanding", f"Pay the computed outstanding {fmt_naira(O)}"),
                                 opt("keep_stated", f"Pay the {fmt_naira(stated)} I said"), CANCEL],
                                stated_kobo=stated, outstanding_kobo=O)
        elif mode in ("usual", "usual_stated"):
            pat, err = usual_amount()
            if err:
                return err
            if mode == "usual_stated" and stated != pat:
                if asel and asel["kind"] == "keep_stated":
                    amount, source, resolution, eff_mode = stated, "explicit", {"kind": "keep_stated"}, "explicit"
                else:
                    return self.ask("stated_amount_conflicts_ledger",
                                    f"You said {fmt_naira(stated)} but 'the usual' to {rcp.legal_name} is "
                                    f"{fmt_naira(pat)}.",
                                    [opt("keep_stated", f"Pay the {fmt_naira(stated)} I said"), CANCEL],
                                    stated_kobo=stated, pattern_kobo=pat)
            else:
                amount, source = pat, "recurring_pattern"
        else:
            amount, source = stated, "explicit"

        if linked is not None and source != "outstanding_balance" and amount > O:
            excess = amount - max(O, 0)
            if asel and asel["kind"] == "override_overpayment":
                if asel["params"].get("over_by_kobo") == excess:
                    resolution = {"kind": "override", "over_by_kobo": excess}
                else:
                    self.drop(asel)
                    asel = None
            if resolution["kind"] != "override":
                if asel and asel["kind"] == "pay_outstanding" and O > 0:
                    amount, source, resolution = O, "outstanding_balance", {"kind": "pay_outstanding"}
                else:
                    options = []
                    if O > 0:
                        options.append(opt("pay_outstanding", f"Pay only the outstanding {fmt_naira(O)}"))
                    options.append(opt("override_overpayment",
                                       f"OVERRIDE: pay {fmt_naira(amount)}, which is {fmt_naira(excess)} MORE "
                                       "than is owed", over_by_kobo=excess))
                    options.append(CANCEL)
                    return self.ask("exceeds_balance", f"{fmt_naira(amount)} is more than the "
                                    f"{fmt_naira(max(O, 0))} outstanding on {linked.description}.", options,
                                    amount_kobo=amount, outstanding_kobo=O, over_by_kobo=excess)

        if amount > self.policy.per_payment_limit_kobo:
            return self.refuse("over_limit", f"{fmt_naira(amount)} is above your per-payment limit of "
                               f"{fmt_naira(self.policy.per_payment_limit_kobo)}.")

        # P7 ------------------------------------------------------------------------------
        acked_unlinked = sorted({i for s in self.sels if s["kind"] == "ack_unlinked"
                                 for i in s["params"].get("entry_ids", [])})
        balance_flow = mode in BALANCE_MODES and eff_mode != "explicit"
        if balance_flow:
            app = D.unlinked_applicable(ents, eff, D.chain_start(chain))
            todo = [e for e in app if e.id not in acked_unlinked]
            if todo:
                ids = [e.id for e in todo]
                return self.ask("unlinked_payment_possible",
                                "Earlier payments to this person are not linked to this agreement and may "
                                "already cover part of it: " + ", ".join(f"{e.id} ({fmt_naira(eff[e.id])})"
                                                                         for e in todo) + ".",
                                [opt("ack_unlinked", "Treat them as unrelated and continue", entry_ids=ids), CANCEL],
                                entry_ids=ids)

        # P8 ------------------------------------------------------------------------------
        acked_dup = sorted({i for s in self.sels if s["kind"] == "ack_duplicate"
                            for i in s["params"].get("entry_ids", [])})
        dups = D.duplicates(ents, eff, amount, self.now, self.policy.duplicate_window_hours)
        todo = [e for e in dups if e.id not in acked_dup]
        if todo:
            ids = [e.id for e in todo]
            return self.ask("possible_duplicate", f"A payment of {fmt_naira(amount)} to {rcp.legal_name} was "
                            f"already made recently ({', '.join(ids)}).",
                            [opt("ack_duplicate", "Yes, pay again deliberately", entry_ids=ids), CANCEL],
                            entry_ids=ids)

        # P9 ------------------------------------------------------------------------------
        return self.build(it, rcp, q1, linked, chain, amount, source, resolution, stated, window, ents, eff,
                          ags, acked_dup if dups else [], acked_unlinked if balance_flow else [], O)

    def build(self, it, rcp, q1, linked, chain, amount, source, resolution, stated, window, ents, eff,
              ags, ack_dup, ack_unl, O):
        override = resolution.get("over_by_kobo") if resolution["kind"] == "override" else None
        evidence, excluded, computation = [], [], {}
        evidence.append({"table": "instruction", "id": self.resolved.get("instruction_id", ""),
                         "role": "user_statement", "summary": self.text})
        if source == "outstanding_balance":
            ids = {a.id for a in chain}
            pays = [e for e in ents if e.entry_type == "payment_out" and e.agreement_id in ids]
            for a in chain:
                evidence.append({"table": "agreement", "id": a.id, "role": "agreement",
                                 "summary": f"{a.description}: agreed {fmt_naira(a.total_kobo)}"})
            rows = []
            for e in pays:
                evidence.append({"table": "ledger_entry", "id": e.id, "role": "payment",
                                 "summary": f"paid {fmt_naira(e.amount_kobo)} on {short_date(e.occurred_at)}"})
                rows.append({"entry_id": e.id, "amount_kobo": e.amount_kobo, "effective_kobo": eff[e.id]})
                for adj in ents:
                    if adj.entry_type == "reversal" and adj.target_entry_id == e.id:
                        evidence.append({"table": "ledger_entry", "id": adj.id, "role": "reversal",
                                         "summary": f"reversed {fmt_naira(adj.amount_kobo)}"})
                        for ri in ents:
                            if ri.entry_type == "reinstatement" and ri.target_entry_id == adj.id:
                                evidence.append({"table": "ledger_entry", "id": ri.id, "role": "reinstatement",
                                                 "summary": f"reinstated {fmt_naira(ri.amount_kobo)}"})
            computation = {"agreement_id": linked.id, "chain": [a.id for a in chain],
                           "agreed_total_kobo": linked.total_kobo, "payments": rows,
                           "paid_kobo": D.chain_paid(chain, ents, eff), "outstanding_kobo": O}
        elif source == "recurring_pattern":
            computation = {"window": [{"entry_id": e.id, "amount_kobo": e.amount_kobo, "effective_kobo": eff[e.id]}
                                      for e in window], "pattern_kobo": amount}
            for e in window:
                evidence.append({"table": "ledger_entry", "id": e.id, "role": "pattern_payment",
                                 "summary": f"paid {fmt_naira(eff[e.id])} on {short_date(e.occurred_at)}"})
        else:
            computation = {"stated_kobo": stated, "outstanding_kobo_if_linked": O}
        if linked is not None and source != "outstanding_balance":
            evidence.append({"table": "agreement", "id": linked.id, "role": "agreement",
                             "summary": f"{linked.description}: outstanding {fmt_naira(O)}"})
        cs = D.chain_start(chain) if chain else None
        for e in D.unlinked_all(ents, eff):
            excluded.append({"entry_id": e.id, "amount_kobo": eff[e.id], "occurred_at": e.occurred_at,
                             "reason": "not linked to any agreement; excluded from agreement balances",
                             "possibly_applicable": bool(cs and e.occurred_at >= cs)})
        not_linked = []
        if linked is None:
            not_linked = [b for b in self.open_balances(rcp.id)]
        heads = self.heads(rcp.id)
        all_ags = []
        for p in q1:
            all_ags += self.agreements(p.id)
        for p in q1:
            if p.id != rcp.id:
                self.entries(p.id)
        digest = D.read_set_digest(q1_persons=q1, policy=self.policy, recipient=rcp, agreements_all=all_ags,
                                   head_ids=[h.id for p in q1 for h in self.heads(p.id)], entries=ents)
        ph = D.payload_hash(recipient_id=rcp.id, amount_kobo=amount, currency="NGN",
                            agreement_id=linked.id if linked else None, source=source,
                            stated_amount_kobo=stated, amount_resolution=resolution,
                            ack_duplicate_ids=ack_dup, ack_unlinked_ids=ack_unl,
                            policy_version=self.policy.version)
        draft = {"recipient_id": rcp.id, "recipient_name": rcp.legal_name, "amount_kobo": amount,
                 "amount_display": fmt_naira(amount), "currency": "NGN", "source": source,
                 "agreement_id": linked.id if linked else None,
                 "agreement_description": linked.description if linked else None,
                 "stated_amount_kobo": stated, "amount_resolution": resolution,
                 "override_over_kobo": override, "ack_duplicate_ids": sorted(ack_dup),
                 "ack_unlinked_ids": sorted(ack_unl), "purpose_text": it.purpose_phrase or "",
                 "computation": computation, "evidence": evidence, "excluded": excluded,
                 "not_linked_balances": not_linked, "payload_hash": ph, "policy_version": self.policy.version}
        r = self.res("STAGE", None, f"Prepared: pay {fmt_naira(amount)} to {rcp.legal_name}. "
                     "Nothing has been paid; confirm to commit.", [])
        r.draft, r.digest = draft, digest
        return r


def decide(conn, now: datetime, resolved: dict) -> EngineResult:
    return Pipeline(conn, now, resolved).run()
