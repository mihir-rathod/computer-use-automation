"""The platform's API server: the authenticated `/v1` API, the web console under `/ui`, and the discovery and chat routes that sit on `/v1`.

Every run goes through runtime.run_replay() / prepare_run() -- the exact path cli.py uses -- so no front door can become a way around the safety, evidence and
approval guarantees built into it. (An older, unauthenticated, synchronous `/capabilities/{id}/invoke` endpoint, with its own chat page and dashboard, used to
live here; the console and `/v1` replaced them. They are kept on the `mockbank` branch.)
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI

import observability

# So GEMINI_API_KEY is in the environment before this process serves its first request, not only before the first chat message happens to arrive.
load_dotenv()

from api.chat_v1 import router as chat_v1_router  # noqa: E402
from api.teach_v1 import router as teach_router, service as teach_service  # noqa: E402
from api.v1 import router as v1_router  # noqa: E402


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
    description="`/v1` is the authenticated, asynchronous API: send `Authorization: Bearer <key>`, and create keys with `cli.py keys create`.",
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
