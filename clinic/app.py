"""Larkspur Clinic Ops: a self-hosted, fictional clinic front-desk and billing portal, built as a
test target for the automation platform. One FastAPI app serves a legacy server-rendered skin
(/legacy), a modern React skin (/app), a JSON API (/api), and a test kit (/_test)."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from clinic.api import register_error_handlers
from clinic.api import router as api_router
from clinic.chaos import ChaosMiddleware
from clinic.legacy import register_legacy
from clinic.settings import Settings
from clinic.state import World
from clinic.testkit import panel_router
from clinic.testkit import router as testkit_router

MODERN_DIST = Path(__file__).parent / "modern" / "dist"

_NOT_BUILT = (
    "<h1>Modern skin not built</h1><p>Run <code>npm install &amp;&amp; npm run build</code> in <code>clinic/modern</code>, "
    "or use the Docker image, which builds it.</p>"
)

_INDEX = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Larkspur Clinic Ops</title>
<style>body{font-family:system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem;color:#1f2933}
a{color:#0b6bcb}li{margin:.4rem 0}code{background:#eef2f6;padding:.1rem .3rem;border-radius:3px}</style></head>
<body><h1>Larkspur Clinic Ops</h1>
<p>A fictional clinic front-desk and billing portal. Synthetic data only; it exists as a test target for
the computer-use automation platform.</p>
<ul><li><a href="/legacy/">Legacy skin</a> &mdash; server-rendered, table layout</li>
<li><a href="/app/">Modern skin</a> &mdash; single-page app</li>
<li><a href="/docs">JSON API docs</a></li>
<li><a href="/_test/panel">Test panel</a> &mdash; chaos, drift and audit log</li></ul>
<p>Demo logins: <code>frontdesk</code> / <code>desk-demo-123</code> and <code>supervisor</code> / <code>super-demo-123</code>.</p>
</body></html>"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="Larkspur Clinic Ops", docs_url="/docs", redoc_url=None)
    app.state.world = World(settings)
    app.add_middleware(ChaosMiddleware)
    register_error_handlers(app)
    app.include_router(api_router)
    register_legacy(app)
    app.include_router(testkit_router)
    app.include_router(panel_router)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    if (MODERN_DIST / "assets").is_dir():
        app.mount("/app/assets", StaticFiles(directory=str(MODERN_DIST / "assets")), name="modern-assets")

    @app.get("/app", include_in_schema=False)
    def modern_root() -> RedirectResponse:
        return RedirectResponse("/app/", status_code=307)

    @app.get("/app/{path:path}", response_class=HTMLResponse, include_in_schema=False)
    def modern(path: str) -> HTMLResponse:
        index = MODERN_DIST / "index.html"
        if not index.is_file():
            return HTMLResponse(_NOT_BUILT, status_code=503)
        return HTMLResponse(index.read_text())

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "clinic_date": app.state.world.clock.today().isoformat()}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _INDEX

    return app


app = create_app()
