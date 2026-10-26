import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app.adapters import (  # noqa: E402
    InferenceUnavailable,
    NebiusAdapter,
    OutputTruncated,
)
from app.intent import IntentError, validate_intent  # noqa: E402


def main():
    ad = NebiusAdapter.from_env()

    if ad is None:
        print(
            "NOT RUN: set NEBIUS_API_KEY, NEBIUS_BASE_URL, "
            "NEBIUS_MODEL (see .env.example)."
        )
        return 2

    print(
        f"model={ad.model_id}  max_tokens={ad.max_tokens}  "
        f"timeout={ad.client.timeout.read}s"
    )

    instructions = [
        "Pay Amaka 50k",
        "Abeg pay Emeka Okoro the remaining 50k for the chairs",
        "Send the usual to Emeka",
    ]

    for text in instructions:
        print("\n>>>", text)

        try:
            raw = ad.parse(text)
        except OutputTruncated as e:
            print("TRUNCATED:", e, "-> raise NEBIUS_MAX_TOKENS")
            continue
        except InferenceUnavailable as e:
            print("UNAVAILABLE:", e)
            continue

        print("raw:", raw[:400])

        try:
            result = validate_intent(raw, text)
            print("VALID:", result.model_dump())
        except IntentError as e:
            print("REJECTED:", e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
