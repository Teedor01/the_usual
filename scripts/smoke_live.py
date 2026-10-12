import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.adapters import NebiusAdapter  # noqa: E402
from app.intent import IntentError, validate_intent  # noqa: E402


def main() -> int:
    ad = NebiusAdapter.from_env()
    if ad is None:
        print("NOT RUN: NEBIUS_API_KEY, NEBIUS_BASE_URL and NEBIUS_MODEL are not all set. No model was called.")
        return 2
    cases = json.loads((Path(__file__).parent / "smoke_cases.json").read_text())
    try:
        models = ad.list_models()
        print(f"models endpoint reachable; {len(models)} models listed; configured model present: {ad.model_id in models}")
    except Exception as e:  # noqa
        print(f"models endpoint failed: {type(e).__name__}: {e}")
    rows, out = [], Path("smoke_results.json")
    for c in cases:
        row = {"id": c["id"], "family": c["family"], "text": c["text"]}
        try:
            raw = ad.parse(c["text"])
            row["raw"] = raw
        except Exception as e:  # noqa
            row.update(status="unavailable", error=f"{type(e).__name__}: {e}")
            rows.append(row); continue
        try:
            it = validate_intent(raw, c["text"])
            row["status"] = "schema_valid_and_verbatim"
            e = c["expect"]
            row["mention_match"] = it.recipient_mention.strip().lower() == e["mention"].lower()
            row["phrase_match"] = (it.amount_phrase or None) == e["phrase"]
            row["mode_match"] = it.amount_mode == e["mode"]
        except IntentError as e:
            row.update(status="rejected", error=str(e))
        rows.append(row)
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    n = len(rows)
    valid = [r for r in rows if r["status"] == "schema_valid_and_verbatim"]
    print(f"cases={n} unavailable={sum(r['status']=='unavailable' for r in rows)} rejected={sum(r['status']=='rejected' for r in rows)} valid={len(valid)}")
    print(f"among valid: mention_match={sum(r['mention_match'] for r in valid)}/{len(valid)} "
          f"phrase_match={sum(r['phrase_match'] for r in valid)}/{len(valid)} mode_match={sum(r['mode_match'] for r in valid)}/{len(valid)}")
    print(f"per-case detail written to {out}. These are raw counts from a {n}-case smoke set, not an accuracy estimate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
