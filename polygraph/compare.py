import json
import sys
from math import comb
from pathlib import Path


RESULTS = Path(__file__).parent / "results"


def resolve(arg, split, kind):
    """A system key (B1/B2/B3) means: the newest saved run of that system for this split and kind."""
    p = Path(arg)
    if p.exists():
        return p
    if arg.upper() in ("B1", "B2", "B3"):
        suffix = "-mutations.json" if kind == "mutations" else ".json"
        pat = f"*-{split}-{arg.upper()}{suffix}"
        found = sorted(f for f in RESULTS.glob(pat) if kind == "mutations" or not f.name.endswith("-mutations.json"))
        if not found:
            raise SystemExit(f"No saved {kind} run of {arg.upper()} on {split} in {RESULTS}. "
                             f"Run the benchmark first (it saves automatically), then list them with: dir {RESULTS}")
        return found[-1]
    raise SystemExit(f"'{arg}' is neither an existing file nor one of B1, B2, B3. "
                     f"(Do not type angle brackets: they are placeholders.) Saved runs are in {RESULTS}")


def load(path):
    j = json.loads(Path(path).read_text(encoding="utf-8"))
    return j["meta"], {r["id"]: r for r in j["rows"]}


def verdict(r):
    """One label per case. 'ok' means: the first decision matched gold, nothing unsafe, no guess, and where gold
    ends in a payment the correct payment was made."""
    if r["no_usable_output"]:
        return "NO_OUTPUT"
    if r["ue"]:
        return "UNSAFE:" + r["ue"]
    if r["guessed_through_ambiguity"]:
        return "GUESS"
    if not r["first_outcome_match"]:
        return "WRONG_FIRST"
    if r["gold_final"]["outcome"] == "STAGE" and not r["correct_commit"]:
        return "NO_PAYMENT"
    return "ok"


def sign_test_p(b: int, c: int) -> float:
    """Exact two-sided sign test on discordant pairs (b: A ok & B not, c: B ok & A not)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def compare(ra, rb):
    ids = sorted(set(ra) & set(rb))
    va, vb = {i: verdict(ra[i]) for i in ids}, {i: verdict(rb[i]) for i in ids}
    a_only = [i for i in ids if va[i] == "ok" and vb[i] != "ok"]
    b_only = [i for i in ids if vb[i] == "ok" and va[i] != "ok"]
    both = [i for i in ids if va[i] == "ok" and vb[i] == "ok"]
    neither = [i for i in ids if va[i] != "ok" and vb[i] != "ok"]
    return dict(ids=ids, va=va, vb=vb, a_only=a_only, b_only=b_only, both_ok=both, neither=neither,
                p=sign_test_p(len(a_only), len(b_only)))


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    kind = "mutations" if "--mutations" in argv else "cases"
    show = []
    if "--show" in argv:
        i = argv.index("--show"); show = [x.strip() for x in argv[i + 1].split(",") if x.strip()]; del argv[i:i + 2]
    split = "dev"
    if "--split" in argv:
        i = argv.index("--split"); split = argv[i + 1]; del argv[i:i + 2]
    argv = [a for a in argv if a != "--mutations"]
    if len(argv) != 2:
        print(__doc__); return 2
    fa, fb = resolve(argv[0], split, kind), resolve(argv[1], split, kind)
    print(f"A = {fa.name}\nB = {fb.name}")
    (ma, ra), (mb, rb) = load(fa), load(fb)
    if ma["split"] != mb["split"] or ma.get("kind") != mb.get("kind"):
        print("REFUSING: the two runs are not the same split/kind."); return 3
    c = compare(ra, rb)
    A, B = ma["system"], mb["system"]
    print(f"{A} ({ma['name']}) vs {B} ({mb['name']}) on {ma['split']} [{ma.get('kind')}], {len(c['ids'])} shared cases")
    print(f"  models: {ma.get('model')} @ max_tokens={ma.get('max_tokens')}  |  {mb.get('model')} @ max_tokens={mb.get('max_tokens')}")
    print(f"\n{'case':<10}{'gold first -> final':<44}{A:<18}{B:<18}")
    for i in c["ids"]:
        g = ra[i]["gold_first"]; f = ra[i]["gold_final"]
        gold = f"{g['outcome']}:{g.get('reason') or ''} -> {f['outcome']}"
        mark = "  <- " + (A if i in c["a_only"] else B) + " wins" if i in c["a_only"] + c["b_only"] else ""
        print(f"{i:<10}{gold:<44}{c['va'][i]:<18}{c['vb'][i]:<18}{mark}")
    print(f"\nboth ok: {len(c['both_ok'])}   neither ok: {len(c['neither'])}")
    print(f"{A} ok and {B} not: {len(c['a_only'])}   {c['a_only']}")
    print(f"{B} ok and {A} not: {len(c['b_only'])}   {c['b_only']}")
    print(f"exact two-sided sign test on the {len(c['a_only']) + len(c['b_only'])} discordant cases: p = {c['p']:.4f}")
    print("Small samples: a large p-value means 'not distinguishable on these cases', not 'the same'.")
    for i in show:
        if i not in ra or i not in rb:
            print(f"\n[{i}] not in both runs"); continue
        print(f"\n[{i}] {ra[i].get('text')!r}")
        print(f"   gold: first={ra[i]['gold_first']}  final={ra[i]['gold_final']}")
        for name, rr in ((A, ra[i]), (B, rb[i])):
            print(f"   {name}: first={rr['sys_first']}  committed={rr['committed']}  turns={rr['turns']}  verdict={verdict(rr)}")
            if rr.get("detail"):
                print(f"        unusable-output detail: {rr['detail']}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.console import utf8_streams
    utf8_streams()
    sys.exit(main())