"""Shared pytest fixtures.

The WebSurface integration tests need a real running MockBank to drive a real browser
against -- but the README promises `pytest` needs no live services. Both stay true by starting
MockBank ourselves, in-process, on an OS-assigned free port, scoped to the test session --
nothing external to start by hand, and no collision with whatever's on 8000/5000 already.
"""
from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
from pathlib import Path

# Before anything imports the app: session-scoped fixtures (the in-process API server) start before any per-test fixture and would
# otherwise run their startup recovery against the developer's real run database (data/runs.db).
os.environ.setdefault("RUN_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="cua-tests-"), "runs.db"))

import httpx
import pytest
import uvicorn
from dotenv import load_dotenv

from tests.support.mockbank.app import app as mockbank_app

# So GEMINI_API_KEY (and anything else in .env) is visible the same way for `pytest` as for
# the `discover`/`replay` CLI -- without this, tests/test_discovery_live.py would always
# skip even with a real key sitting in .env, since pytest doesn't load it on its own.
load_dotenv()


@pytest.fixture(autouse=True)
def _isolated_run_store(tmp_path, monkeypatch):
    """Every test gets its own run database, so runs recorded by one never dedupe or cap another."""
    monkeypatch.setenv("RUN_DB_PATH", str(tmp_path / "runs.db"))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def mockbank_base_url():
    port = _free_port()
    config = uvicorn.Config(mockbank_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            httpx.get(f"{base_url}/login", timeout=0.5)
            break
        except httpx.ConnectError:
            time.sleep(0.1)
    else:
        raise RuntimeError("MockBank test server did not start in time")

    yield base_url

    server.should_exit = True
    thread.join(timeout=5)


FIXTURE_ARTIFACTS = Path(__file__).parent / "fixtures" / "artifacts"      # the hand-written MockBank artifacts the engine and runtime tests replay
MOCKBANK_ALLOWLIST = Path(__file__).parent / "support" / "mockbank" / "allowlist.json"


@pytest.fixture
def mockbank_runtime(tmp_path, monkeypatch, mockbank_base_url):
    """Lets the runtime run MockBank, a small test-only site that is not part of the product: a target profile for it, and its artifacts beside the clinic's.
    Used by the tests that exercise the runtime's own rules (idempotency, caps, approvals) with a browser that is faked or very fast."""
    import shutil

    import runtime
    from tests.clinic_support import copy_artifacts

    artifacts = copy_artifacts(tmp_path)
    shutil.copytree(FIXTURE_ARTIFACTS, artifacts, dirs_exist_ok=True)
    monkeypatch.setenv("ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setitem(runtime.TARGET_PROFILES, "mockbank", {
        "base_url": mockbank_base_url, "username": "operator", "password": "bankdemo123", "allowlist": MOCKBANK_ALLOWLIST,
        "login_capability": "mockbank.login", "login_path": "/login", "home_path": "/search", "sandbox": True, "app_id": "mockbank"})

    real_run = runtime.run_replay

    def run_replay(capability_id, params, **kw):
        if capability_id.startswith("mockbank.") and "target" not in kw:
            kw["target"] = "mockbank"  # the default target is the clinic; a MockBank capability belongs to its own
        return real_run(capability_id, params, **kw)

    monkeypatch.setattr(runtime, "run_replay", run_replay)
    return artifacts


@pytest.fixture(scope="session")
def api_base_url():
    """The platform API in-process on a free port, for tests that need a real server: the console's browser tests and the MCP bridge, both of which make real HTTP calls."""
    from api.app import app as api_app

    port = _free_port()
    config = uvicorn.Config(api_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            httpx.get(f"{base_url}/v1/health", timeout=0.5)
            break
        except httpx.ConnectError:
            time.sleep(0.1)
    else:
        raise RuntimeError("capability API test server did not start in time")

    yield base_url

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    pg = context.new_page()
    yield pg
    context.close()


def login(page, base_url: str, next_path: str = "/search") -> None:
    page.goto(f"{base_url}/login?next={next_path}")
    page.get_by_role("textbox", name="Username").fill("operator")
    page.get_by_role("textbox", name="Password").fill("bankdemo123")
    page.get_by_role("button", name="Log In").click()
    page.wait_for_url(f"**{next_path}")


@pytest.fixture(scope="session")
def clinic_base_url():
    """The Larkspur Clinic target in-process on a free port, for platform tests that replay against it."""
    from clinic.app import create_app
    from clinic.settings import Settings

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(Settings()), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            httpx.get(f"{base_url}/healthz", timeout=0.5)
            break
        except httpx.ConnectError:
            time.sleep(0.1)
    else:
        raise RuntimeError("clinic test server did not start in time")
    yield base_url
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def clinic(clinic_base_url):
    """The clinic reset to its seed, with the target's own duplicate guard off."""
    from tests.clinic_support import reset

    reset(clinic_base_url)
    return clinic_base_url


@pytest.fixture
def signed_in(page, clinic):
    from tests.clinic_support import sign_in

    sign_in(page, clinic)
    return page


@pytest.fixture
def params(clinic, clinic_base_url):
    """Inputs for a small refund against the seeded paid invoice."""
    import httpx

    fixtures = httpx.get(f"{clinic_base_url}/_test/fixtures", timeout=5).json()
    return {"invoice": fixtures["standard"]["refundable_invoice"], "amount": "20.00", "reason": "duplicate_payment"}
