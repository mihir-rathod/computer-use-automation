"""Tests for the capability API (api/app.py) -- ASSIGNMENT_ORIGINAL.md 3.2's "callable catalog".
/invoke drives a real Playwright browser against the in-process MockBank fixture (same pattern
as tests/test_web_surface.py), through the exact same runtime.run_replay() the CLI uses -- these
tests are the automated proof that the API isn't a second implementation of "how do I run a
capability", not just a manual curl check.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from api.app import app

client = TestClient(app)


def test_list_capabilities_includes_known_ids_with_typed_schemas():
    resp = client.get("/capabilities")
    assert resp.status_code == 200
    by_id = {c["capability_id"]: c for c in resp.json()}
    assert "mockbank.member_balance_lookup" in by_id
    cap = by_id["mockbank.member_balance_lookup"]
    assert cap["input_schema"]["properties"]["member_id"]["type"] == "string"
    assert "found" in cap["output_schema"]["properties"]["status"]["enum"]
    assert cap["safety"]["risk_level"] == "read_only"


def test_invoke_unknown_capability_is_404():
    resp = client.post("/capabilities/nonexistent/invoke", json={"params": {}})
    assert resp.status_code == 404


def test_invoke_unknown_target_is_422():
    resp = client.post(
        "/capabilities/mockbank.member_balance_lookup/invoke",
        json={"params": {"member_id": "10001"}, "target": "bogus"},
    )
    assert resp.status_code == 422


def test_invoke_runs_a_real_replay_and_returns_structured_success(mockbank_base_url):
    resp = client.post(
        "/capabilities/mockbank.member_balance_lookup/invoke",
        json={"params": {"member_id": "10002"}, "target": "mockbank", "base_url": mockbank_base_url},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["outputs"]["savings_balance"] == 150.0
    assert body["evidence_dir"]


def test_invoke_returns_business_outcome_as_200_not_an_http_error(mockbank_base_url):
    resp = client.post(
        "/capabilities/mockbank.member_balance_lookup/invoke",
        json={"params": {"member_id": "99999"}, "target": "mockbank", "base_url": mockbank_base_url},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "business_outcome"
    assert body["business_outcome"] == "not_found"
