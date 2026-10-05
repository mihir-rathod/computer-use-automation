"""Watching a run: pacing, the outline on the element being touched, and that the settings survive an approval."""
from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

import runtime
from api.app import app
from artifacts_lib.schema import ActionType, Locator, LocatorStrategy, Target
from replay.result import ReplayStatus
from runs.store import RunStore
from surface.base import Action
from surface.web import WebSurface
from tests.clinic_support import copy_artifacts, reset
from tests.test_api_v1 import submit, wait  # noqa: F401


def button(name: str) -> Target:
    return Target(semantic_description=name, locators=[Locator(strategy=LocatorStrategy.ROLE, value=f"button[name='{name}']")])


def test_watch_mode_paces_each_action_and_outlines_only_the_latest_element(page, tmp_path):
    page.set_content("<button id=a>One</button><button id=b>Two</button>")
    quiet = WebSurface(page, base_url="about:blank", screenshot_dir=tmp_path)
    quiet.act(Action(kind=ActionType.CLICK, target=button("One")))
    assert page.evaluate("document.getElementById('a').style.outline") == ""            # off by default: no decoration at all

    watched = WebSurface(page, base_url="about:blank", screenshot_dir=tmp_path, pace_ms=400)
    started = time.monotonic()
    watched.act(Action(kind=ActionType.CLICK, target=button("One")))
    assert time.monotonic() - started >= 0.58                                           # a pause before (400ms) and after (200ms) the click
    assert "245, 165, 36" in page.evaluate("document.getElementById('a').style.outline")  # amber outline on what was just touched

    watched.act(Action(kind=ActionType.CLICK, target=button("Two")))
    assert page.evaluate("document.getElementById('a').style.outline") == ""            # the previous one is cleared
    assert "245, 165, 36" in page.evaluate("document.getElementById('b').style.outline")


def test_watch_mode_never_changes_what_a_run_does(clinic):
    fast, _ = runtime.run_replay("clinic.patient_lookup", {"mrn": "LK-100002"}, target="clinic", base_url=clinic, enable_operator_console=False)
    started = time.monotonic()
    slow, _ = runtime.run_replay("clinic.patient_lookup", {"mrn": "LK-100002"}, target="clinic", base_url=clinic, enable_operator_console=False, pace_ms=250)
    took = time.monotonic() - started
    assert fast.status == slow.status == ReplayStatus.SUCCESS and fast.outputs == slow.outputs
    assert took > 9 * 0.25                                                               # at least the pauses across the nine steps


@pytest.fixture
def api(tmp_path, monkeypatch, clinic_base_url):
    monkeypatch.setenv("ARTIFACTS_DIR", str(copy_artifacts(tmp_path)))
    monkeypatch.setitem(runtime.TARGET_PROFILES["clinic"], "base_url", clinic_base_url)
    reset(clinic_base_url)
    store = runtime.default_store()
    keys = {n: store.create_key(n, r) for n, r in [("alex", "operator"), ("dana", "supervisor"), ("vic", "viewer")]}
    with TestClient(app) as client:
        client.h = lambda who: {"Authorization": f"Bearer {keys[who]}"}
        yield client


def test_the_server_says_whether_it_may_open_a_window(api, monkeypatch):
    monkeypatch.delenv("CUA_ALLOW_WINDOW", raising=False)
    assert api.get("/v1/features", headers=api.h("vic")).json() == {"show_window": False, "max_pace_ms": 3000, "watch_presets": {"slow": 700, "step_by_step": 1800}}
    monkeypatch.setenv("CUA_ALLOW_WINDOW", "1")
    assert api.get("/v1/features", headers=api.h("vic")).json()["show_window"] is True


def test_a_window_is_refused_unless_the_server_allows_it_and_pace_is_bounded(api, monkeypatch):
    monkeypatch.delenv("CUA_ALLOW_WINDOW", raising=False)
    body = {"capability_id": "clinic.patient_lookup", "target": "clinic", "params": {"mrn": "LK-100001"}}
    refused = api.post("/v1/runs", json={**body, "show_window": True}, headers=api.h("alex"))
    assert refused.status_code == 422 and "CUA_ALLOW_WINDOW" in refused.json()["detail"]
    assert api.post("/v1/runs", json={**body, "pace_ms": 99999}, headers=api.h("alex")).status_code == 422
    assert api.post("/v1/runs", json={**body, "pace_ms": -5}, headers=api.h("alex")).status_code == 422


def test_a_watched_run_records_its_settings_and_a_window_run_opens_a_headed_browser(api, monkeypatch):
    monkeypatch.setenv("CUA_ALLOW_WINDOW", "1")
    launched = {}

    def fake_dedicated(job, *, headed, slow_mo):
        launched.update(headed=headed, slow_mo=slow_mo)
        raise RuntimeError("no real window in tests")

    monkeypatch.setattr(runtime, "_run_on_dedicated_thread", fake_dedicated)
    run = submit(api, "alex", "clinic.patient_lookup", {"mrn": "LK-100001"}, pace_ms=700, show_window=True).json()
    assert run["pace_ms"] == 700 and run["show_window"] is True
    done = wait(api, run["id"])
    assert launched == {"headed": True, "slow_mo": 0}                                    # our own pacing, never Playwright's slow_mo
    assert done["status"] == "hard_failure" and done["error_code"] == "runner_error"    # the fake refused to open a window; the run closed cleanly


def test_watching_is_remembered_through_an_approval(api, monkeypatch):
    seen = {}

    def fake(artifact, params, **kw):
        seen.update(pace_ms=kw["pace_ms"], headed=kw["headed"])
        from datetime import UTC, datetime
        from replay.result import ReplayResult
        now = datetime.now(UTC)
        return ReplayResult(status=ReplayStatus.SUCCESS, capability_id=artifact.capability_id, committed=True, started_at=now, finished_at=now)

    monkeypatch.setattr(runtime, "_replay_in_browser", fake)
    refund = {"invoice": "INV-30001", "amount": "10.00", "reason": "duplicate_payment"}
    run = submit(api, "alex", "clinic.issue_refund", refund, pace_ms=1200).json()
    assert run["status"] == "pending_approval" and run["pace_ms"] == 1200
    api.post(f"/v1/runs/{run['id']}/approve", json={"reason": "ok"}, headers=api.h("dana"))
    wait(api, run["id"])
    assert seen == {"pace_ms": 1200, "headed": False}                                    # asked at submit, applied when it finally runs


def test_too_many_visible_windows_are_refused(monkeypatch):
    store = runtime.default_store()
    for key in ("a", "b"):
        store.begin("clinic.patient_lookup", "1.0.0", {"mrn": "LK-100001"}, "alex", key, show_window=True)  # two already open (status running)
    early = runtime.prepare_run("clinic.patient_lookup", {"mrn": "LK-100001"}, target="clinic", requested_by="alex", show_window=True, enable_operator_console=False)
    assert isinstance(early, runtime.Early) and early.result.error.code == "window_busy"
    assert not isinstance(runtime.prepare_run("clinic.patient_lookup", {"mrn": "LK-100001"}, target="clinic", requested_by="alex", enable_operator_console=False), runtime.Early)


def test_chat_passes_watch_settings_to_the_run(api, monkeypatch):
    from api import chat_v1
    from tests.test_ui_backend import FakeModel

    fake = FakeModel()
    fake.script = [("call", ("clinic__patient_lookup", {"mrn": "LK-100001"}))]
    monkeypatch.setattr(chat_v1, "get_client", lambda: fake)
    out = api.post("/v1/chat", json={"message": "look up LK-100001", "pace_ms": 700}, headers=api.h("alex")).json()["messages"]
    assert api.get(f"/v1/runs/{out[1]['run_id']}", headers=api.h("vic")).json()["pace_ms"] == 700
    wait(api, out[1]["run_id"])   # let the background run finish so it cannot leak into the next test


def test_an_older_database_gains_the_watch_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE runs (id TEXT PRIMARY KEY, capability_id TEXT NOT NULL, version TEXT, idempotency_key TEXT, requested_by TEXT NOT NULL,"
                      " params_json TEXT NOT NULL, status TEXT NOT NULL, committed INTEGER NOT NULL DEFAULT 0, commit_step TEXT, error_code TEXT,"
                      " created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, evidence_dir TEXT, result_json TEXT, resolution TEXT);")
    con.execute("INSERT INTO runs(id,capability_id,requested_by,params_json,status,created_at) VALUES('r1','a.b','x','{}','success','2026-01-01')")
    con.commit()
    con.close()
    store = RunStore(path)
    assert store.get("r1")["pace_ms"] == 0 and store.get("r1")["show_window"] == 0


def test_watch_pacing_starts_after_sign_on_not_before(clinic, monkeypatch):
    """Sign-on is plumbing: pacing it made a watched run sit for ten seconds before the task's first step."""
    from surface import web

    seen = []
    original = web.WebSurface._pause

    def spy(self, factor):
        seen.append((self.pace_ms, factor))
        return original(self, factor)

    monkeypatch.setattr(web.WebSurface, "_pause", spy)
    res, _ = runtime.run_replay("clinic.patient_lookup", {"mrn": "LK-100001"}, target="clinic", base_url=clinic, enable_operator_console=False, pace_ms=120)
    assert res.status == ReplayStatus.SUCCESS
    first_paced = next(i for i, (pace, _) in enumerate(seen) if pace == 120)
    assert first_paced >= 4 and all(pace == 0 for pace, _ in seen[:first_paced])        # the sign-on's own actions were all unpaced
