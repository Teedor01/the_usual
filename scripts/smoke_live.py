import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.adapters import InferenceUnavailable, NebiusAdapter, OutputTruncated  
from app.console import utf8_streams  
from app.envfile import load_dotenv, report 
from app.intent import IntentError, validate_intent  


def main() -> int:
    utf8_streams()
    report(load_dotenv())
    ad = NebiusAdapter.from_env()
    if ad is None:
        import os
        missing = [k for k in ("NEBIUS_API_KEY", "NEBIUS_BASE_URL", "NEBIUS_MODEL") if not os.environ.get(k)]
        print(f"NOT RUN: missing {', '.join(missing)}. Set them in a .env file next to the app folder (see .env.example) "
              "or in this terminal window. No model was called.")
        return 2
    cases = json.loads((Path(__file__).parent / "smoke_cases.json").read_text(encoding="utf-8"))
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
        except OutputTruncated as e:
            row.update(status="truncated", error=f"{type(e).__name__}: {e}  (raise NEBIUS_MAX_TOKENS)")
            rows.append(row); continue
        except InferenceUnavailable as e:
            row.update(status="unavailable", error=f"{type(e).__name__}: {e}")
            rows.append(row); continue
        except Exception as e: 
            row.update(status="error", error=f"{type(e).__name__}: {e}")
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
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    n = len(rows)
    valid = [r for r in rows if r["status"] == "schema_valid_and_verbatim"]
    count = lambda st: sum(r["status"] == st for r in rows)
    print(f"cases={n} unavailable={count('unavailable')} truncated={count('truncated')} error={count('error')} "
          f"rejected={count('rejected')} valid={len(valid)}")
    for r in rows:
        if r["status"] in ("unavailable", "truncated", "error", "rejected"):
            print(f"  {r['id']} [{r['status']}] {r.get('error', '')}")
    print(f"among valid: mention_match={sum(r['mention_match'] for r in valid)}/{len(valid)} "
          f"phrase_match={sum(r['phrase_match'] for r in valid)}/{len(valid)} mode_match={sum(r['mode_match'] for r in valid)}/{len(valid)}")
    print(f"per-case detail written to {out}. These are raw counts from a {n}-case smoke set, not an accuracy estimate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())