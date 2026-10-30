# The Usual

A personal agent for small merchants and freelancers that turns vague payment instructions
("Pay Emeka the balance for those chairs", "send the usual to Emeka") into **evidence-backed drafts**,
and never turns uncertainty into an irreversible action.

**All payments are simulated. No real money moves and no bank is integrated.**

## The idea in one paragraph
A language model only *reads* the instruction into mentions (who, which order, what amount phrase).
Everything that matters is deterministic code against an append-only SQLite ledger: who the recipient is,
what is owed (integer kobo, reversals and reinstatements applied), whether the payee is approved, whether the
amount is within limits, whether this duplicates a recent payment. If anything is ambiguous the answer is a
question, not a guess. A draft can only be committed by an explicit confirmation, and the commit **re-decides
everything against live state** inside a `BEGIN IMMEDIATE` transaction, so a ledger that changed after staging
invalidates the draft instead of paying a stale amount.

Outcomes: `STAGE` (draft ready, nothing paid), `ASK` (one answer needed), `REFUSE` (hard rule), `WAIT`
(records unreconciled or inference unavailable). The decision order P0..P9 is in `app/engine.py`.

## Run
    pip install -r requirements.txt
    USUAL_DATA_DIR=./data python3 -m uvicorn app.api:create_app --factory --port 8000
    # open http://127.0.0.1:8000

Without model credentials the app uses a deterministic **rule-based parser** and says so in the UI.
To use Nemotron on Nebius Token Factory set `NEBIUS_API_KEY`, `NEBIUS_BASE_URL`, `NEBIUS_MODEL`
(and optionally `NEBIUS_STRUCTURED_OUTPUT=1`). Nothing in this repo has been run against the real API.

## Verify
    python3 -m pytest tests -q          # unit, database-boundary, lifecycle, multi-process concurrency, sessions, API
    python3 scripts/mutation_check.py   # breaks 18 safety rules one at a time; every one must be caught
    sh scripts/ui_smoke.sh              # real server + the real page in jsdom (interrupted-confirm, reset banner, ...)
    sh scripts/http_smoke.sh            # real server + curl, one full stage -> commit cycle
    python3 scripts/smoke_live.py       # live model smoke test: prints NOT RUN without credentials
    python3 -m polygraph.runner --split dev --systems B1 --mutations

## Deploy notes
* `render.yaml` is a Render blueprint (not yet deployed); `USUAL_REQUIRE_LIVE_MODEL=1` makes missing Nebius credentials a startup error instead of a silent rule-parser fallback. `Dockerfile` is untested. Storage is ephemeral by default (a restart resets sessions, visibly). See `DEPLOY.md`.
* Model-call caps: `USUAL_MAX_CALLS_PER_SESSION` and `USUAL_MAX_CALLS_PER_DAY` (live model only; the rule parser is free).
  When a cap is reached the answer is `WAIT model_budget_exhausted` and nothing is staged. See `app/budget.py`.
* `DEMO.md` has the 3-minute script. `sh scripts/ui_smoke.sh` drives the real page in a DOM (needs node and npm).

## Layout
    app/schema.py      tables, CHECKs, UNIQUE indexes and TRIGGERS (the database backstop)
    app/domain.py      pure functions: matching, chains, effective payments, balances, digests, hashes
    app/engine.py      the P0..P9 decision pipeline (never calls a model)
    app/lifecycle.py   instruction -> decision -> staged action -> commit (idempotent, revalidated)
    app/adapters.py    ScriptedModel (test double), RuleParser, NebiusAdapter (live, untested vs real API)
    app/sessions.py    per-session databases copied from an immutable template; tombstones; no fallback
    app/api.py         FastAPI surface. Commit accepts ONLY {commit_key, confirmed_payload_hash}
    polygraph/         internal benchmark harness (B1 rule parser, B2 naive same-model, B3 full system)

## Honest limits
* Session clocks are virtual: each session starts at 2026-10-12 10:00 Lagos time and advances in real time.
* SQLite WAL needs one host with durable local disk; there is no multi-host story.
* No model-call budget/abuse cap is implemented, so a public deployment with live credentials needs one.
* Descriptor matching and the balance/usual cue lexicon are heuristics with no measured coverage on real Pidgin.
* Agents never create reversals or reinstatements; they enter only through the operator seed/import path.
* A `disputed` entry blocks payments to that recipient until an operator reconciles it. There is no background worker.
