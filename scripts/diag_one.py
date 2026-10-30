import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.adapters import InferenceUnavailable, NebiusAdapter, OutputTruncated  
from app.console import utf8_streams  
from app.envfile import load_dotenv, report  
from app.intent import IntentError, validate_intent  

utf8_streams()
report(load_dotenv())
ad = NebiusAdapter.from_env()
if ad is None:
    import os
    missing = [k for k in ("NEBIUS_API_KEY", "NEBIUS_BASE_URL", "NEBIUS_MODEL") if not os.environ.get(k)]
    print(f"NOT RUN: missing {', '.join(missing)}. Put them in a .env file (see .env.example) or set them in this window.")
    sys.exit(2)
print(f"model={ad.model_id}  max_tokens={ad.max_tokens}  timeout={ad.client.timeout.read}s")
for text in ["Pay Amaka 50k", "Abeg pay Emeka Okoro the remaining 50k for the chairs", "Send the usual to Emeka"]:
    print("\n>>>", text)
    try:
        raw = ad.parse(text)
    except OutputTruncated as e:
        print("TRUNCATED:", e, "-> raise NEBIUS_MAX_TOKENS"); continue
    except InferenceUnavailable as e:
        print("UNAVAILABLE:", e); continue
    print("raw:", raw[:400])
    try:
        print("VALID:", validate_intent(raw, text).model_dump())
    except IntentError as e:
        print("REJECTED:", e)