from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from clinic.app import create_app
from clinic.settings import Settings


@pytest.fixture
def app():
    return create_app(Settings())


def logged_in(app, user="frontdesk", password="desk-demo-123") -> TestClient:
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": user, "password": password}).status_code == 200
    return c


def test_every_testkit_endpoint_requires_the_token_when_one_is_configured():
    app = create_app(Settings(test_token="s3cret"))
    c = TestClient(app)
    assert c.get("/_test/state").status_code == 403
    assert c.get("/_test/state", headers={"X-Test-Token": "wrong"}).status_code == 403
    assert c.get("/_test/state", headers={"X-Test-Token": "s3cret"}).status_code == 200
    assert c.get("/_test/state?token=s3cret").status_code == 200
    assert c.post("/_test/reset").status_code == 403
    assert c.get("/_test/panel").status_code == 200  # the shell is open; its API calls are not


def test_reset_is_idempotent_and_restores_everything(app):
    c = TestClient(app)
    first = c.post("/_test/reset").json()
    desk = logged_in(app)
    draft = desk.post("/api/transactions/claim/review",
                      json={"invoice": first["fixtures"]["standard"]["claimable_invoice"], "amount_cents": 12400}).json()
    desk.post(f"/api/transactions/{draft['token']}/confirm")
    c.post("/_test/chaos", json={"kind": "latency", "params": {"ms": 1}, "remaining": None})
    c.post("/_test/drift", json={"level": 2, "seed": "x"})
    assert c.get("/_test/state").json()["counts"]["claims"] == 1

    second = c.post("/_test/reset").json()
    assert second["fixtures"] == first["fixtures"]
    assert second["counts"]["claims"] == 0
    assert second["chaos"]["rules"] == [] and second["drift"]["level"] == 0
    assert desk.get("/api/auth/me").status_code == 401  # sessions are cleared too
    assert c.post("/_test/reset", json={"today": "2026-04-06"}).json()["clinic_date"] == "2026-04-06"
    assert c.post("/_test/reset", json={"today": "not-a-date"}).status_code == 400


def test_audit_api_filters_and_incremental_polling(app):
    c, desk = TestClient(app), logged_in(app)
    desk.get("/api/patients", params={"mrn": "LK-100001"})
    first = c.get("/_test/audit").json()
    assert any(r["action"] == "auth.login" for r in first["items"])
    desk.get("/api/patients", params={"mrn": "LK-100002"})
    newer = c.get("/_test/audit", params={"since_id": first["last_id"]}).json()["items"]
    assert [r["action"] for r in newer] == ["patient.search"] and newer[0]["detail"]["total"] == 1
    assert c.get("/_test/audit", params={"effects_only": True}).json()["items"] == []
    assert c.get("/_test/audit", params={"action": "auth.login"}).json()["items"]


def test_chaos_rules_via_the_api_and_clearing(app):
    c, desk = TestClient(app), logged_in(app)
    rule = c.post("/_test/chaos", json={"kind": "maintenance", "path_glob": "/api/meta"}).json()["rule"]
    assert desk.get("/api/meta").status_code == 503
    assert desk.get("/api/meta").status_code == 200
    c.post("/_test/chaos", json={"kind": "error500", "remaining": None})
    assert desk.get("/api/meta").status_code == 500 and desk.get("/api/meta").status_code == 500
    assert c.delete("/_test/chaos").json()["rules"] == []
    assert desk.get("/api/meta").status_code == 200
    assert c.post("/_test/chaos", json={"kind": "bogus"}).status_code == 400
    assert rule["id"] >= 1
    assert any(e["kind"] == "maintenance" for e in c.get("/_test/chaos").json()["log"]) is False  # cleared with the rules


def test_duplicate_guard_switch_decides_whether_a_retry_double_posts(app):
    c = TestClient(app)
    inv = app.state.world.fixtures["standard"]["refundable_invoice"]

    def refund_twice(guard: bool) -> int:
        c.post("/_test/reset")
        c.post("/_test/chaos/duplicate-guard", json={"enabled": guard})
        d = logged_in(app)
        token = d.post("/api/transactions/refund/review", json={"invoice": inv, "amount_cents": 1000, "reason": "billing_error"}).json()["token"]
        d.post(f"/api/transactions/{token}/confirm")
        d.post(f"/api/transactions/{token}/confirm")
        return len(c.get("/_test/audit", params={"action": "refund.issue", "effects_only": True}).json()["items"])

    assert refund_twice(guard=True) == 1
    assert refund_twice(guard=False) == 2


def test_drift_api_changes_the_ui_dictionary(app):
    c = TestClient(app)
    assert c.get("/api/ui").json()["text"]["Search"] == "Search"
    c.post("/_test/drift", json={"level": 2, "seed": "a"})
    assert c.get("/api/ui").json()["text"]["Search"] == "Find patient"
    c.delete("/_test/drift")
    assert c.get("/api/ui").json()["text"]["Search"] == "Search"
