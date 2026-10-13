import re

AMT = re.compile(r"(?<![\d.,])(\d+)k\b")


def utterance_mutations(case):
    t = case["text"]
    out = [("case", t.upper()), ("spacing", "  ".join(t.split(" ")) + "  "),
           ("punctuation", t[:-1] if t.endswith(".") else t + "!!"), ("filler_prefix", "Abeg " + t),
           ("filler_suffix", t.rstrip(".") + " abeg")]
    m = AMT.search(t)
    if m:
        n = int(m.group(1))
        for fam, repl in (("amount_N", f"N{n}k"), ("amount_naira_sign", f"₦{n * 1000:,}"), ("amount_words_naira", f"{n * 1000} naira")):
            out.append((fam, t[:m.start()] + repl + t[m.end():]))
    return [(fam, {**case, "id": f"{case['id']}~{fam}", "text": txt, "mutation": fam}) for fam, txt in out if txt != t]


def semantic_mutations(case):
    """Rule: a near-miss spelling of a person must never resolve to anyone: gold becomes ASK no_entity."""
    t = case["text"]
    if "Emeka" in t and case["first"].get("outcome") != "REFUSE":
        non = {"outcome": "ASK", "reason": "no_entity"}
        return [("nearmiss_name", {**case, "id": f"{case['id']}~nearmiss", "text": t.replace("Emeka", "Emeke"),
                                   "first": non, "final": non, "clarify": [], "mutation": "nearmiss_name"})]
    return []


def all_mutations(cases):
    out = []
    for c in cases:
        out += [m for _, m in utterance_mutations(c) + semantic_mutations(c)]
    return out
