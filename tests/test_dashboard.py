"""Tests for the dashboard (api/dashboard.py) -- ASSIGNMENT_ORIGINAL.md 3.4's "lightweight UI to
watch the system work". No new persistence to test against: a real /invoke call against the
in-process MockBank fixture writes real evidence to disk, and these tests check the dashboard
reads that evidence back correctly -- catalog, run history, and one run's own detail page.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from api.app import app

client = TestClient(app)


def test_catalog_lists_known_capabilities_with_risk_level():
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "mockbank.member_balance_lookup" in resp.text
    assert "READ ONLY" in resp.text.upper()


def test_run_history_shows_a_real_invocation(mockbank_base_url):
    invoke = client.post(
        "/capabilities/mockbank.member_balance_lookup/invoke",
        json={"params": {"member_id": "10002"}, "target": "mockbank", "base_url": mockbank_base_url},
    )
    assert invoke.status_code == 200
    run_id = invoke.json()["evidence_dir"].rsplit("/", 1)[-1]

    resp = client.get("/dashboard/runs")
    assert resp.status_code == 200
    assert run_id in resp.text
    assert "mockbank.member_balance_lookup" in resp.text


def test_run_detail_shows_status_outputs_and_full_timeline(mockbank_base_url):
    invoke = client.post(
        "/capabilities/mockbank.member_balance_lookup/invoke",
        json={"params": {"member_id": "10002"}, "target": "mockbank", "base_url": mockbank_base_url},
    )
    run_id = invoke.json()["evidence_dir"].rsplit("/", 1)[-1]

    resp = client.get(f"/dashboard/runs/{run_id}")
    assert resp.status_code == 200
    assert "success" in resp.text
    assert "150" in resp.text  # the real savings_balance, in Outputs
    assert "action" in resp.text  # at least one step event rendered in the timeline


def test_run_detail_unknown_run_is_404():
    resp = client.get("/dashboard/runs/nonexistent_run_id")
    assert resp.status_code == 404


def test_screenshot_path_traversal_is_blocked():
    resp = client.get("/dashboard/runs/foo/screenshot/..%2f..%2fapi%2fapp.py")
    assert resp.status_code == 404
