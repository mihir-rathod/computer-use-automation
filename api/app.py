"""The capability API -- ASSIGNMENT_ORIGINAL.md 3.2: "a callable catalog... an agent invokes a
capability by name with typed args and gets a structured result back, without knowing anything
about the underlying UI." This is also the app the chatbot (api/chatbot.py, mounted below) and
dashboard (Phase 5) live on -- one process, one port, for the whole demoable surface, per the
brief's own "simpler is fine if justified" and "we do not reward... scaling infrastructure."

Every invocation calls runtime.run_replay() -- the exact same execution path cli.py's `replay`
command uses -- so this can never become a second implementation of "how do I run a capability",
and can never become a way around the safety/evidence/escalation guarantees already built into
that one path (3.5).

Route handlers below are plain `def`, not `async def`, on purpose: run_replay() is a blocking,
synchronous Playwright call that can take several seconds. FastAPI runs sync `def` handlers in a
worker thread pool automatically, so a slow replay doesn't block the event loop or other
concurrent requests -- no asyncio wrapping needed for something this simple.
"""
from __future__ import annotations

from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from artifacts_lib.storage import list_artifacts
from runtime import TARGET_PROFILES, run_replay

# Before importing api.chatbot: GeminiClient is only constructed inside a request handler (not
# at import time), but load here anyway so GEMINI_API_KEY is guaranteed present before this
# process ever serves a request, not just before the first chat message happens to arrive.
load_dotenv()

from api.chatbot import router as chatbot_router

app = FastAPI(title="Capability API")
app.include_router(chatbot_router)


class InvokeRequest(BaseModel):
    params: dict[str, Any] = {}
    target: str = "mockbank"
    base_url: str | None = None
    username: str | None = None
    password: str | None = None
    headed: bool = False
    slow_mo: int = 0


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
    the brief's own "success, a known business outcome, or a failure with enough detail to
    debug" contract (3.3) -- collapsing a hard failure into an HTTP error would blur exactly the
    distinction that contract exists to keep clear.
    """
    if body.target not in TARGET_PROFILES:
        raise HTTPException(status_code=422, detail=f"unknown target '{body.target}' -- known: {sorted(TARGET_PROFILES)}")
    try:
        result, evidence_dir = run_replay(
            capability_id, body.params,
            target=body.target, base_url=body.base_url, username=body.username, password=body.password,
            headed=body.headed, slow_mo=body.slow_mo,
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
        "evidence_dir": str(evidence_dir),
    }
