from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import db, lifecycle as L, repo, seed
from .adapters import NebiusAdapter, RuleParser
from .budget import BudgetedAdapter
from .config import Settings
from .envfile import load_dotenv, report as report_dotenv
from .sessions import BadSessionId, Capacity, SessionGone, SessionRegistry
from .timeutil import iso

STATIC = Path(__file__).parent / "static"
GONE_BODY = {"error": "session_gone",
             "message": "Your previous session ended. This is not restored: earlier drafts, ledger entries and "
                        "history are gone. Start a new session."}


class InstructionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=100)


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ask_decision_id: str
    option_id: str


class CommitIn(BaseModel):
    """The ONLY fields commit accepts. No recipient, amount, agreement or any payment field."""
    model_config = ConfigDict(extra="forbid")
    commit_key: str = Field(min_length=1, max_length=100)
    confirmed_payload_hash: str = Field(min_length=64, max_length=64)


def ensure_template(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    tpl, hf = data_dir / "template.db", data_dir / "template.sha256"
    if not tpl.exists():
        hf.write_text(seed.build_template(tpl), encoding="utf-8")
    elif hf.exists() and seed.file_hash(tpl) != hf.read_text(encoding="utf-8").strip():
        raise RuntimeError("template database was modified: refusing to start")
    return tpl


def default_adapter(ctx: L.SessionContext):
    live = NebiusAdapter.from_env()
    if live is not None:
        return live
    conn = db.connect(ctx.path)
    try:
        names = [n for p in repo.load_persons(conn) for n in (p.legal_name, *p.aliases)]
        vocab = [w for a in repo.load_agreements(conn) for w in a.description.replace(",", " ").split()]
    finally:
        conn.close()
    return RuleParser(lambda: names, lambda: vocab)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    if settings is None:                       # real entry point (uvicorn --factory): read .env. Tests always pass Settings.
        report_dotenv(load_dotenv())
        settings = Settings.from_env()
    tpl = ensure_template(settings.data_dir)
    registry = SessionRegistry(settings.data_dir / "registry.db", settings.data_dir / "sessions", tpl,
                               settings.time_source, settings.max_sessions, settings.idle_seconds)
    app = FastAPI(title="The Usual", docs_url=None, redoc_url=None)
    app.state.registry, app.state.settings = registry, settings

    def err(status, code, message="", **extra):
        return JSONResponse({"error": code, "message": message, **extra}, status_code=status)

    @app.exception_handler(L.ApiError)
    async def _api_error(_, e: L.ApiError):
        return err(e.status, e.code, e.message, **e.extra)

    def ctx_for(session_id: str) -> L.SessionContext:
        h = registry.resolve(session_id)
        return L.SessionContext(h.path, lambda: h.virtual_now(settings.time_source()), session_id=session_id)

    def guarded(fn):
        def run(session_id, *a, **k):
            try:
                return fn(ctx_for(session_id), *a, **k)
            except BadSessionId:
                return err(400, "bad_session_id", "Malformed session id.")
            except SessionGone:
                return JSONResponse(GONE_BODY, status_code=410)
        return run

    def adapter_for(ctx):
        ad = (settings.adapter_factory or default_adapter)(ctx)
        if isinstance(ad, NebiusAdapter):   # only the live, paid model is metered; the rule parser is free
            return BudgetedAdapter(ad, lambda: registry.charge_model_call(
                ctx.session_id, settings.max_model_calls_per_session, settings.max_model_calls_per_day))
        return ad

    @app.get("/api/info")
    def info():
        live = NebiusAdapter.from_env()
        return {"parser": live.model_id if live else "rule-parser (no Nemotron credentials configured)",
                "live_model": bool(live), "max_model_calls_per_session": settings.max_model_calls_per_session,
                "max_model_calls_per_day": settings.max_model_calls_per_day}

    @app.get("/api/sessions/{session_id}/budget")
    def budget(session_id: str):
        def go(ctx):
            u = registry.model_usage(ctx.session_id)
            return {**u, "session_cap": settings.max_model_calls_per_session, "day_cap": settings.max_model_calls_per_day}
        return guarded(go)(session_id)

    @app.post("/api/sessions")
    def create_session():
        try:
            sid = registry.create_session()
        except Capacity:
            return err(503, "capacity", "Too many active sessions. Try again later.")
        live = NebiusAdapter.from_env()
        return {"session_id": sid, "parser": live.model_id if live else "rule-parser (no Nemotron credentials configured)",
                "live_model": bool(live)}

    @app.get("/api/sessions/{session_id}/ledger")
    def ledger(session_id: str):
        return guarded(lambda ctx: L.ledger_view(ctx))(session_id)

    @app.post("/api/sessions/{session_id}/instructions")
    def post_instruction(session_id: str, body: InstructionIn):
        return guarded(lambda ctx: L.submit_instruction(ctx, body.text, body.idempotency_key, adapter_for(ctx)))(session_id)

    @app.post("/api/sessions/{session_id}/instructions/{iid}/retry")
    def retry(session_id: str, iid: str):
        return guarded(lambda ctx: L.retry_instruction(ctx, iid, adapter_for(ctx)))(session_id)

    @app.post("/api/sessions/{session_id}/instructions/{iid}/resolve")
    def resolve(session_id: str, iid: str, body: ResolveIn):
        return guarded(lambda ctx: L.resolve_ask(ctx, iid, body.ask_decision_id, body.option_id))(session_id)

    @app.post("/api/sessions/{session_id}/actions/{aid}/commit")
    def commit(session_id: str, aid: str, body: CommitIn):
        def go(ctx):
            status, raw = L.commit_action(ctx, aid, body.commit_key, body.confirmed_payload_hash)
            return Response(content=raw, status_code=status, media_type="application/json")
        return guarded(go)(session_id)

    @app.post("/api/sessions/{session_id}/actions/{aid}/cancel")
    def cancel(session_id: str, aid: str):
        return guarded(lambda ctx: L.cancel_action(ctx, aid))(session_id)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app