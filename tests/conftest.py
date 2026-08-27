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
