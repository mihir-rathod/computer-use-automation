"""The capability API -- a callable catalog: an agent invokes a capability by name with typed
args and gets a structured result back, without knowing anything about the underlying UI. This is also the app the chatbot
(api/chatbot.py, mounted below) and dashboard live on -- one process, one port for the whole
demoable surface; simpler is fine while a single process is enough.

Every invocation calls runtime.run_replay() -- the exact same execution path cli.py's `replay`
command uses -- so this can never become a second implementation of "how do I run a capability",
and can never become a way around the safety/evidence/escalation guarantees already built into
that one path.

Route handlers below are plain `def`, not `async def`, on purpose: run_replay() is a blocking,
synchronous Playwright call that can take several seconds. FastAPI runs sync `def` handlers in a
worker thread pool automatically, so a slow replay doesn't block the event loop or other
concurrent requests -- no asyncio wrapping needed for something this simple.
"""
from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import observability
from artifacts_lib.storage import list_artifacts
from runtime import TARGET_PROFILES, ensure_operator_console, run_replay

# Before importing api.chatbot: GeminiClient is only constructed inside a request handler (not
# at import time), but load here anyway so GEMINI_API_KEY is guaranteed present before this
# process ever serves a request, not just before the first chat message happens to arrive.
load_dotenv()

from api.chatbot import router as chatbot_router
from api.dashboard import router as dashboard_router
from api.v1 import router as v1_router
from api.chat_v1 import router as chat_v1_router
from api.teach_v1 import router as teach_router, service as teach_service

def _recover_interrupted_runs() -> None:
    """Runs left `running`/`queued` by a process that died would block their idempotency keys for ever."""
    import runtime
    from artifacts_lib import storage
    from safety.config import irreversible_step_ids

    def has_commit(capability_id: str) -> bool:
        try:
            return bool(irreversible_step_ids(storage.load_artifact_by_id(capability_id)))
        except Exception:
            return True  # cannot tell, so assume it could have committed

    runtime.default_store().recover_interrupted(has_commit)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    observability.configure("INFO")
    _recover_interrupted_runs()
    teach_service().recover()
    yield


app = FastAPI(
    lifespan=_lifespan,
    title="Capability API",
    description="`/v1` is the authenticated, asynchronous API (send `Authorization: Bearer <key>`; create keys with `cli.py keys create`). "
                "`/capabilities/{id}/invoke` is the older synchronous endpoint the chatbot and dashboard still use.",
)


@app.middleware("http")
async def _log_v1_requests(request, call_next):
    """One structured line per /v1 call: who, what, status, how long. Never the body or the key."""
    if not request.url.path.startswith("/v1"):
        return await call_next(request)
    started = time.monotonic()
    response = await call_next(request)
    observability.log("http.request", method=request.method, path=request.url.path, status=response.status_code,
                      ms=round((time.monotonic() - started) * 1000), principal=getattr(request.state, "principal", None))
    return response


app.include_router(chatbot_router)
app.include_router(dashboard_router)
app.include_router(v1_router)
app.include_router(chat_v1_router)
app.include_router(teach_router)

# The console (ui/, a static Next.js export) is served from the same process and port, under /ui. It exists once `npm run build` has run in ui/.
UI_DIR = Path(__file__).resolve().parent.parent / "ui" / "out"
if UI_DIR.is_dir():
    from fastapi.responses import RedirectResponse
    from fastapi.staticfiles import StaticFiles

    app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def _root() -> RedirectResponse:
        return RedirectResponse("/ui/")

# Started here, not left to the first invoke's own lazy start (runtime.run_replay ->
# ensure_operator_console): the chatbot page links to this console as soon as it loads (see
# api/chatbot.py's _operator_console_url()), and that link needs to actually work before anyone
# has triggered a run, not just after.
ensure_operator_console(8010)


class InvokeRequest(BaseModel):
    params: dict[str, Any] = {}
    target: str = "mockbank"
    base_url: str | None = None
    username: str | None = None
    password: str | None = None
    headed: bool = False
    slow_mo: int = 0
    idempotency_key: str | None = Field(default=None, description="A retry with the same key returns the first run's result instead of running again.")
    requested_by: str = "api"
    dry_run: bool = False
    evidence_dir: str | None = Field(
        default=None, description="Pre-computed run id/path -- lets a caller (the chatbot) know "
                                    "the run's id before it starts, e.g. to link to its operator "
                                    "console session while still in flight. Defaults to run_replay's own."
    )


@app.get("/capabilities")
def list_capabilities() -> list[dict[str, Any]]:
    """The callable catalog itself -- everything an agent needs to invoke a capability by name
    with typed args, without knowing anything about the underlying UI, straight from what's
    already on disk under /artifacts/. No separate registry to keep in sync."""
    return [
        {
            "capability_id": a.capability_id,
            "name": a.name,
            "description": a.description,
            "input_schema": a.input_schema.model_dump(),
            "output_schema": a.output_schema.model_dump(),
            "safety": a.safety.model_dump(),
            "target_app_id": a.target.app_id,
        }
        for a in list_artifacts()
    ]


@app.post("/capabilities/{capability_id}/invoke")
def invoke_capability(capability_id: str, body: InvokeRequest) -> dict[str, Any]:
    """Runs the capability for real via runtime.run_replay() and returns its structured result.
    HTTP-level errors (404, 422) are reserved for problems with the *call itself* -- an unknown
    capability_id, a malformed body. A replay that completes as a business outcome or even a
    hard failure is still a successful API call: 200, with that outcome in the body, matching
    the "success, a known business outcome, or a failure with enough detail to debug" contract --
    collapsing a hard failure into an HTTP error would blur exactly the distinction that
    contract exists to keep clear.
    """
    if body.target not in TARGET_PROFILES:
        raise HTTPException(status_code=422, detail=f"unknown target '{body.target}' -- known: {sorted(TARGET_PROFILES)}")
    # This endpoint is unauthenticated, so a caller must not be able to point a run (and the profile's credentials) at an
    # arbitrary host, or make it write evidence anywhere on disk. Those overrides exist for tests and are off by default.
    if (body.base_url or body.username or body.password) and os.environ.get("ALLOW_TARGET_OVERRIDE") != "1":
        raise HTTPException(status_code=422, detail="base_url, username and password cannot be supplied on this endpoint; the target profile decides them")
    if body.evidence_dir:
        from runtime import EVIDENCE_ROOT
        if not Path(body.evidence_dir).resolve().is_relative_to(EVIDENCE_ROOT.resolve()):
            raise HTTPException(status_code=422, detail="evidence_dir must be inside the server's evidence directory")
    try:
        result, evidence_dir = run_replay(
            capability_id, body.params,
            target=body.target, base_url=body.base_url, username=body.username, password=body.password,
            headed=body.headed, slow_mo=body.slow_mo, dry_run=body.dry_run,
            idempotency_key=body.idempotency_key, requested_by=body.requested_by,
            evidence_dir=Path(body.evidence_dir) if body.evidence_dir else None,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"unknown capability '{capability_id}'")

    return {
        "capability_id": result.capability_id,
        "status": result.status.value,
        "outputs": result.outputs,
        "business_outcome": result.business_outcome,
        "error": result.error.model_dump() if result.error else None,
        "steps_completed": result.steps_completed,
        "escalated": result.escalated,
        "recovered": result.recovered,
        "run_id": result.run_id,
        "committed": result.committed,
        "deduplicated": result.deduplicated,
        "approval_tier": result.approval_tier,
        "evidence_dir": str(evidence_dir),
    }
