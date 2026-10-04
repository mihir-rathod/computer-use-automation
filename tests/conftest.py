"""Shared pytest fixtures.

The WebSurface integration tests need a real running MockBank to drive a real browser
against -- but the README promises `pytest` needs no live services. Both stay true by starting
MockBank ourselves, in-process, on an OS-assigned free port, scoped to the test session --
nothing external to start by hand, and no collision with whatever's on 8000/5000 already.
"""
from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from dotenv import load_dotenv

from mockbank.app import app as mockbank_app

# So GEMINI_API_KEY (and anything else in .env) is visible the same way for `pytest` as for
# the `discover`/`replay` CLI -- without this, tests/test_discovery_live.py would always
# skip even with a real key sitting in .env, since pytest doesn't load it on its own.
load_dotenv()


@pytest.fixture(autouse=True)
def _isolated_run_store(tmp_path, monkeypatch):
    """Every test gets its own run database, so runs recorded by one never dedupe or cap another."""
    monkeypatch.setenv("RUN_DB_PATH", str(tmp_path / "runs.db"))
    monkeypatch.setenv("ALLOW_TARGET_OVERRIDE", "1")  # the legacy API refuses base_url/credential overrides unless this is set


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


@pytest.fixture(scope="session")
def api_base_url():
    """Same pattern as mockbank_base_url -- the capability API in-process on a free port, for
    tests that need a real server (e.g. the chatbot's self-call to its own /invoke endpoint,
    which is a genuine HTTP round-trip, not a direct function call -- see api/chatbot.py)."""
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
            httpx.get(f"{base_url}/capabilities", timeout=0.5)
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
