A = lambda *a: ("agreement", *a)
E = lambda *a: ("entry", *a)

S6 = [E("E8", "CN", "payment_out", 2_500_000, "AG3", None, "10-06", "reconciled")]
S6R = S6 + [E("E9", "CN", "reversal", 2_500_000, "AG3", "E8", "10-07", "reconciled"),
            E("E10", "CN", "payment_out", 1_000_000, None, None, "09-28", "reconciled")]
NG = [("person", "NG", "Ngozi Bello", "supplier", True, ["Ngozi"]),
      A("AG4", "NG", "20 tables", 8_000_000, "09-25", None),
      E("N1", "NG", "payment_out", 2_000_000, "AG4", None, "10-01", "reconciled")]


def C(id, split, family, ops, text, first, final=None, clarify=(), why=""):
    return dict(id=id, split=split, family=family, ops=list(ops), text=text, first=first,
                final=final or first, clarify=list(clarify), why=why)


def stage(rcp, amt, ag=None):
    return dict(outcome="STAGE", reason=None, recipient=rcp, amount_kobo=amt, agreement_id=ag)


def non(outcome, reason):
    return dict(outcome=outcome, reason=reason)


DEV = [
    C("D01", "dev", "balance", [], "Pay Emeka the balance for those chairs.", stage("EO", 5_000_000, "AG1"), why="AG1 100k minus linked E1 50k"),
    C("D02", "dev", "ambiguity", [], "Pay Chidi the balance.", non("ASK", "ambiguous_entity"), stage("CN", 6_000_000, "AG3"), ["select_person:CN"], "two Chidis; CN: 90k-30k"),
    C("D03", "dev", "usual", [], "Send the usual to Emeka.", non("ASK", "ambiguous_entity"), stage("EO", 5_000_000, None), ["select_person:EO"], "two Emekas; EO last 3 payments all 50k"),
    C("D04", "dev", "explicit", [], "Pay Amaka 50k", stage("AO", 5_000_000, None), why="approved, within limit, no agreements"),
    C("D05", "dev", "authorization", [], "Pay Bassey 80k for the transport.", non("REFUSE", "recipient_not_approved"), why="Bassey not approved"),
    C("D06", "dev", "authorization", [], "Pay Amaka 650k for the fabric.", non("REFUSE", "over_limit"), why="650k > 500k limit"),
    C("D07", "dev", "operation", [], "Refund Emeka John 30k.", non("REFUSE", "unsupported_operation"), why="refunds unsupported"),
    C("D08", "dev", "missing", [], "Pay Ngozi 10k", non("ASK", "no_entity"), why="no such person in base ledger"),
    C("D09", "dev", "missing", [], "Pay Amaka", non("ASK", "missing_amount"), why="no amount and no cue"),
    C("D10", "dev", "conflict", [A("AG1b", "EO", "100 chairs, revised", 12_000_000, "10-05", "AG1")], "Pay Emeka the remaining 50k for the chairs.",
      non("ASK", "stated_amount_conflicts_ledger"), stage("EO", 7_000_000, "AG1b"), ["pay_outstanding"], "stated 50k vs ledger 120k-50k=70k"),
    C("D11", "dev", "reversal", S6R, "Pay Chidi Nwosu the balance for the paint.", stage("CN", 6_000_000, "AG3"), why="full reversal restores: 90-30-0"),
    C("D12", "dev", "reconciliation", S6 + [E("E9", "CN", "reversal", 2_500_000, "AG3", "E8", "10-07", "pending")],
      "Pay Chidi Nwosu the balance for the paint.", non("WAIT", "unreconciled_entry"), why="pending reversal"),
    C("D13", "dev", "overpayment", [], "Pay Chidi Eze 150k for the cement.", non("ASK", "exceeds_balance"), stage("CE", 12_000_000, "AG2"), ["pay_outstanding"], "150k > outstanding 120k"),
    C("D14", "dev", "descriptor", [], "Pay Emeka 10k for the cement.", non("ASK", "descriptor_mismatch"), why="neither Emeka has a cement agreement"),
    C("D15", "dev", "agreement", [A("AG2b", "CE", "cement, 20 bags", 20_000_000, "10-03", None)], "Pay Chidi Eze the balance for the cement.",
      non("ASK", "ambiguous_agreement"), stage("CE", 12_000_000, "AG2"), ["select_agreement:AG2"], "two active cement agreements"),
    C("D16", "dev", "usual", [], "Send the usual to Chidi Eze.", non("ASK", "insufficient_history"), why="only one prior payment"),
    C("D17", "dev", "pidgin", [], "Abeg pay Emeka the remaining 50k for chairs", stage("EO", 5_000_000, "AG1"), why="stated 50k equals computed outstanding"),
    C("D18", "dev", "usual", [], "Pay Amaka the usual", non("ASK", "insufficient_history"), why="no prior payments to Amaka"),
    C("D19", "dev", "pidgin", [], "Oga pay Amaka 20k abeg", stage("AO", 2_000_000, None), why="filler words only"),
    C("D20", "dev", "currency", [], "Pay Amaka $100", non("REFUSE", "unsupported_currency"), why="not naira"),
]

HELD = [
    C("H01", "heldout", "balance", NG, "Pay Ngozi the balance for the tables", stage("NG", 6_000_000, "AG4"), why="80k-20k"),
    C("H02", "heldout", "ambiguity", NG + [("person", "NG2", "Ngozi Ade", "customer", True, ["Ngozi"])], "Pay Ngozi 5k",
      non("ASK", "ambiguous_entity"), stage("NG", 500_000, None), ["select_person:NG"], "two Ngozis; no agreement descriptor"),
    C("H03", "heldout", "usual", NG[:1] + [E(f"U{i}", "NG", "payment_out", 2_500_000, None, None, d, "reconciled") for i, d in enumerate(["09-01", "09-08", "09-15"])],
      "Send the usual to Ngozi Bello", stage("NG", 2_500_000, None), why="three identical earlier payments"),
    C("H04", "heldout", "usual", NG[:1] + [E("U0", "NG", "payment_out", 2_500_000, None, None, "09-01", "reconciled"), E("U1", "NG", "payment_out", 3_000_000, None, None, "09-08", "reconciled"),
                                           E("U2", "NG", "payment_out", 2_500_000, None, None, "09-15", "reconciled")], "Send the usual to Ngozi Bello",
      non("ASK", "inconsistent_history"), why="amounts differ"),
    C("H05", "heldout", "authorization", [("status", "AO", "not_approved")], "Send Amaka 45k", non("REFUSE", "recipient_not_approved"), why="approval revoked"),
    C("H06", "heldout", "limit", [], "Pay Amaka 500k", stage("AO", 50_000_000, None), why="limit is inclusive"),
    C("H07", "heldout", "limit", [], "Pay Amaka 500,001", non("REFUSE", "over_limit"), why="1 naira over"),
    C("H08", "heldout", "shorthand", [], "Pay Amaka 12.5k", stage("AO", 1_250_000, None), why="12,500 naira"),
    C("H09", "heldout", "shorthand", [], "Pay Amaka N20k", stage("AO", 2_000_000, None), why="N prefix"),
    C("H10", "heldout", "reversal", [E("E8", "CN", "payment_out", 2_500_000, "AG3", None, "10-06", "reconciled"),
                                     E("E9p", "CN", "reversal", 1_000_000, "AG3", "E8", "10-07", "reconciled")],
      "Send Chidi Nwosu the remaining paint balance", stage("CN", 4_500_000, "AG3"), why="partial reversal: 90-30-15"),
    C("H11", "heldout", "reconciliation", [E("E8", "CN", "payment_out", 2_500_000, "AG3", None, "10-06", "disputed")],
      "Settle Chidi Nwosu balance for paint", non("WAIT", "unreconciled_entry"), why="disputed entry blocks the recipient"),
    C("H12", "heldout", "unlinked", S6R + [E("E11", "CN", "payment_out", 1_000_000, None, None, "10-08", "reconciled")],
      "Clear the paint balance for Chidi Nwosu", non("ASK", "unlinked_payment_possible"), stage("CN", 6_000_000, "AG3"), ["ack_unlinked"], "unlinked payment after agreement date"),
    C("H13", "heldout", "reversal", S6R + [E("E12", "CN", "reinstatement", 2_500_000, "AG3", "E9", "10-08", "reconciled")],
      "Abeg pay Chidi Nwosu the rest for the paint", stage("CN", 3_500_000, "AG3"), why="reinstatement undoes the reversal: 90-30-25"),
    C("H14", "heldout", "descriptor", [], "Pay Emeka Okoro 10k for transport", non("ASK", "no_matching_agreement"), stage("EO", 1_000_000, None), ["link_none"], "no agreement matches 'transport'"),
    C("H15", "heldout", "overpayment", [], "Pay Emeka Okoro 80k for chairs", non("ASK", "exceeds_balance"), stage("EO", 5_000_000, "AG1"), ["pay_outstanding"], "80k > outstanding 50k"),
    C("H16", "heldout", "verb", [], "Settle Emeka Okoro the balance for chairs", stage("EO", 5_000_000, "AG1"), why="'settle' is a payment verb"),
    C("H17", "heldout", "missing", [], "Clear that chair money for Emeka", non("ASK", "missing_amount"), why="no amount, no balance/usual cue"),
    C("H18", "heldout", "amount", [], "Pay Chidi Nwosu 20k 30k", non("ASK", "multiple_amounts"), why="two amounts"),
    C("H19", "heldout", "missing", [], "Pay Amaka the balance", non("ASK", "no_agreement"), why="Amaka has no agreement"),
    C("H20", "heldout", "mode", [], "Pay Emeka the usual and the balance", non("ASK", "mode_cue_mismatch"), why="contradictory cues"),
]

ALL = DEV + HELD
