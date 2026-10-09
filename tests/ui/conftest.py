from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

import runtime
from tests.clinic_support import copy_artifacts, reset

UI_BUILT = (Path(__file__).resolve().parents[2] / "ui" / "out" / "index.html").exists()
pytestmark = pytest.mark.skipif(not UI_BUILT, reason="the console has not been built (npm run build in ui/)")
expect.set_options(timeout=30000)


class Console:
    def __init__(self, base, clinic, keys):
        self.base, self.clinic, self.keys = base, clinic, keys

    def open(self, page, who: str, path: str = ""):
        """Signs in as `who` by storing their key, the way the sign-in form does, then opens a console page."""
        page.goto(f"{self.base}/ui/")
        page.evaluate("k => localStorage.setItem('cua.apiKey', k)", self.keys[who])
        page.goto(f"{self.base}/ui/{path}")
        return page


@pytest.fixture
def console(api_base_url, clinic_base_url, tmp_path, monkeypatch):
    monkeypatch.setenv("ARTIFACTS_DIR", str(copy_artifacts(tmp_path)))
    monkeypatch.setattr(runtime, "EVIDENCE_ROOT", tmp_path / "evidence")  # runs and discovery sessions started from the console must not leave evidence in the repo
    monkeypatch.setitem(runtime.TARGET_PROFILES["clinic"], "base_url", clinic_base_url)
    monkeypatch.setitem(runtime.TARGET_PROFILES["clinic_supervisor"], "base_url", clinic_base_url)
    reset(clinic_base_url)
    store = runtime.default_store()
    keys = {n: store.create_key(n, r) for n, r in [("alex", "operator"), ("sam", "operator"), ("dana", "supervisor"), ("vic", "viewer"), ("root", "admin")]}
    yield Console(api_base_url, clinic_base_url, keys)
    # A run left waiting at its irreversible step holds a browser for up to 15 minutes, takes the place of the next test's run, and keeps this process from
    # exiting. Reject whatever a test left waiting.
    for waiting in httpx.get(f"{api_base_url}/v1/approvals", headers={"Authorization": f"Bearer {keys['dana']}"}, timeout=10).json()["approvals"]:
        if waiting["at_step"]:
            httpx.post(f"{api_base_url}/v1/runs/{waiting['run_id']}/approval/reject", json={"reason": "test cleanup"}, headers={"Authorization": f"Bearer {keys['dana']}"}, timeout=10)
            end = time.time() + 30  # and let it end: the next test must not find the limit on waiting runs still taken
            while time.time() < end and httpx.get(f"{api_base_url}/v1/runs/{waiting['run_id']}", headers={"Authorization": f"Bearer {keys['dana']}"}, timeout=10).json()["status"] in ("queued", "running"):
                time.sleep(0.2)
    httpx.delete(f"{clinic_base_url}/_test/chaos", timeout=5)
    httpx.delete(f"{clinic_base_url}/_test/drift", timeout=5)
