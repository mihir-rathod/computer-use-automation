from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from clinic.app import create_app
from clinic.settings import Settings


@pytest.fixture
def app():
    return create_app(Settings())


def client(app, user: str | None = None) -> TestClient:
    c = TestClient(app)
    passwords = {"frontdesk": "desk-demo-123", "frontdesk2": "desk2-demo-123", "supervisor": "super-demo-123"}
    if user:
        assert c.post("/api/auth/login", json={"username": user, "password": passwords[user]}).status_code == 200
    return c


def fx(app):
    return app.state.world.fixtures


def test_requires_login_and_bad_credentials_are_denied_and_audited(app):
    c = TestClient(app)
    assert c.get("/api/patients?mrn=LK-100001").json()["error"] == "not_authenticated"
    bad = c.post("/api/auth/login", json={"username": "frontdesk", "password": "wrong"})
    assert bad.status_code == 403
    assert app.state.world.audit.list(action="auth.login", outcome="denied")


def test_login_me_logout(app):
    c = client(app, "supervisor")
    assert c.get("/api/auth/me").json()["user"]["role"] == "supervisor"
    c.post("/api/auth/logout")
    assert c.get("/api/auth/me").status_code == 401


def test_search_and_detail_and_validation_error_shape(app):
    c = client(app, "frontdesk")
    found = c.get("/api/patients", params={"mrn": "LK-100001"}).json()
    assert found["total"] == 1
    detail = c.get(f"/api/patients/{found['items'][0]['id']}").json()
    assert detail["patient"]["mrn"] == "LK-100001" and detail["appointments"]
    err = c.get("/api/patients")
    assert err.status_code == 400 and err.json()["error"] == "validation_error" and err.json()["problems"]
    assert c.get("/api/patients/99999").status_code == 404


def test_cancel_flow_posts_once_and_second_confirm_is_a_409_with_the_receipt(app):
    c = client(app, "frontdesk")
    appt = fx(app)["standard"]["upcoming_appointment"]
    draft = c.post("/api/transactions/cancel/review", json={"appointment": appt, "reason": "patient_request"}).json()
    first = c.post(f"/api/transactions/{draft['token']}/confirm")
    assert first.status_code == 200 and first.json()["receipt_number"].startswith("CXL-")
    again = c.post(f"/api/transactions/{draft['token']}/confirm")
    assert again.status_code == 409 and again.json()["result"]["receipt_number"] == first.json()["receipt_number"]
    assert len(app.state.world.audit.list(action="appointment.cancel", effects_only=True)) == 1


def test_refund_above_cap_is_a_two_person_flow(app):
    desk, boss = client(app, "frontdesk"), client(app, "supervisor")
    inv = fx(app)["big_refund"]["invoice"]
    draft = desk.post("/api/transactions/refund/review",
                      json={"invoice": inv, "amount_cents": 25_000, "reason": "billing_error"}).json()
    assert draft["review"]["requires_approval"]
    receipt = desk.post(f"/api/transactions/{draft['token']}/confirm").json()
    assert receipt["status"] == "pending_approval"
    assert desk.get("/api/approvals").status_code == 403
    assert desk.post(f"/api/approvals/{receipt['receipt_number']}/decision", json={"decision": "approve"}).status_code == 403
    pending = boss.get("/api/approvals").json()["items"]
    assert [a["number"] for a in pending] == [receipt["receipt_number"]]
    assert boss.post(f"/api/approvals/{receipt['receipt_number']}/decision", json={"decision": "approve", "note": "ok"}).json()["status"] == "approved"
    assert app.state.world.clinic.stats()["refunds_issued"] == 1


def test_writeoff_denied_for_front_desk_at_confirm(app):
    desk = client(app, "frontdesk")
    inv = fx(app)["standard"]["claimable_invoice"]
    draft = desk.post("/api/transactions/writeoff/review", json={"invoice": inv, "amount_cents": 500, "reason": "hardship"}).json()
    denied = desk.post(f"/api/transactions/{draft['token']}/confirm")
    assert denied.status_code == 403 and "SUPERVISOR" in denied.json()["message"]
    assert not app.state.world.audit.list(action="writeoff.apply", effects_only=True)


def test_schedule_csv_is_a_download(app):
    c = client(app, "frontdesk")
    r = c.get("/api/schedule.csv", params={"date": "2026-03-02"})
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert r.text.splitlines()[0].startswith("appointment,time")


def test_reschedule_over_the_api(app):
    c = client(app, "frontdesk")
    appt = fx(app)["standard"]["upcoming_appointment"]
    slot = c.get(f"/api/appointments/{appt}/slots", params={"date": "2026-03-12"}).json()["slots"][0]
    assert c.post(f"/api/appointments/{appt}/reschedule", json={"date": "2026-03-12", "time": slot}).status_code == 200
    assert c.post(f"/api/appointments/{appt}/reschedule", json={"date": "2026-03-14", "time": "09:00"}).status_code == 400


def test_idle_session_expires():
    app = create_app(Settings(session_idle_seconds=0))
    c = client(app, "frontdesk")
    time.sleep(0.01)
    assert c.get("/api/auth/me").json()["error"] == "session_expired"


def test_chaos_maintenance_and_error_and_session_expiry(app):
    world = app.state.world
    c = client(app, "frontdesk")
    world.chaos.add("maintenance", path_glob="/api/patients*")
    assert c.get("/api/patients", params={"mrn": "LK-100001"}).status_code == 503
    assert c.get("/api/patients", params={"mrn": "LK-100001"}).status_code == 200  # one-shot
    world.chaos.add("error500")
    assert c.get("/api/meta").status_code == 500
    world.chaos.add("expire_session", path_glob="/api/patients*")
    assert c.get("/api/patients", params={"mrn": "LK-100001"}).json()["error"] == "session_expired"
    assert [e["kind"] for e in world.chaos.log] == ["maintenance", "error500", "expire_session"]


def test_chaos_latency_and_sticky_rate_limit(app):
    world = app.state.world
    c = client(app, "frontdesk")
    world.chaos.add("latency", {"ms": 150})
    started = time.time()
    c.get("/api/meta")
    assert time.time() - started >= 0.14
    world.chaos.add("rate_limit", {"limit": 2, "window_seconds": 60}, remaining=None)
    statuses = [c.get("/api/meta").status_code for _ in range(4)]
    assert statuses == [200, 200, 429, 429]
