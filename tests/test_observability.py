"""Metrics, structured logs and traces: what an operator looks at to see how the platform is doing and why a run failed."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

import observability
import runtime
from replay.result import ReplayError, ReplayResult, ReplayStatus
from runs import metrics
from runs.store import RunStore
from tests.clinic_support import reset

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def settle(store: RunStore, cap: str, status: ReplayStatus, seconds: float = 2.0, *, escalated=False, recovered=False, committed=False,
           code: str | None = None, key: str | None = None) -> str:
    run = store.begin(cap, "1.0.0", {"x": "1"}, "alex", key).run["id"]
    store.finish(run, ReplayResult(
        status=status, capability_id=cap, escalated=escalated, recovered=recovered, committed=committed,
        error=ReplayError(message="x", code=code) if code else None, started_at=NOW, finished_at=NOW + timedelta(seconds=seconds)))
    return run


# ---- metrics ------------------------------------------------------------------------------------------------

def test_metrics_follow_their_stated_definitions():
    store = RunStore()
    for secs in (1, 2, 3, 4):
        settle(store, "clinic.patient_lookup", ReplayStatus.SUCCESS, secs)
    settle(store, "clinic.patient_lookup", ReplayStatus.BUSINESS_OUTCOME, 5)              # a correct answer: counts as good
    settle(store, "clinic.patient_lookup", ReplayStatus.HARD_FAILURE, 10, code="timeout", escalated=True)
    settle(store, "clinic.patient_lookup", ReplayStatus.NEEDS_REVIEW, 6, code="ambiguous_commit")
    store.begin("clinic.patient_lookup", "1.0.0", {}, "alex", status="pending_approval")  # not settled: excluded from rates
    settle(store, "clinic.issue_refund", ReplayStatus.SUCCESS, 7, committed=True, recovered=True)

    out = metrics.compute(store)
    lookup = next(c for c in out["capabilities"] if c["capability_id"] == "clinic.patient_lookup")

    assert lookup["runs"] == 8 and lookup["settled"] == 7 and lookup["statuses"]["pending_approval"] == 1
    assert lookup["success_rate"] == round(5 / 7, 4) and lookup["failure_rate"] == round(1 / 7, 4) and lookup["needs_review"] == 1
    assert lookup["escalation_rate"] == round(1 / 7, 4)
    assert lookup["latency_s"] == {"p50": 4.0, "p95": 10.0, "max": 10.0, "n": 7}
    assert lookup["top_error_codes"] == {"timeout": 1, "ambiguous_commit": 1}
    refund = next(c for c in out["capabilities"] if c["capability_id"] == "clinic.issue_refund")
    assert refund["committed"] == 1 and refund["auto_recovered"] == 1 and refund["success_rate"] == 1.0


def test_metrics_with_no_runs_report_none_not_zero():
    assert metrics.compute(RunStore())["capabilities"] == []
    store = RunStore()
    store.begin("clinic.patient_lookup", "1.0.0", {}, "alex")  # running, nothing settled
    only = metrics.compute(store)["capabilities"][0]
    assert only["settled"] == 0 and only["success_rate"] is None and only["latency_s"]["p50"] is None


def test_metrics_can_be_limited_by_capability_and_window():
    store = RunStore()
    settle(store, "a.one", ReplayStatus.SUCCESS)
    old = settle(store, "a.two", ReplayStatus.SUCCESS)
    store._conn.execute("UPDATE runs SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (old,))
    assert [c["capability_id"] for c in metrics.compute(store, capability_id="a.one")["capabilities"]] == ["a.one"]
    assert [c["capability_id"] for c in metrics.compute(store, since_hours=24)["capabilities"]] == ["a.one"]


def test_prometheus_text_has_the_expected_series():
    store = RunStore()
    settle(store, "clinic.patient_lookup", ReplayStatus.SUCCESS)
    text = metrics.prometheus(metrics.compute(store), {"size": 2, "active": 0})
    assert 'cua_runs_total{capability="clinic.patient_lookup",status="success"} 1' in text
    assert 'cua_success_rate{capability="clinic.patient_lookup"} 1.0' in text and "cua_browser_pool_size 2" in text


def test_metrics_endpoints_need_a_key_and_serve_both_formats():
    store = runtime.default_store()
    settle(store, "clinic.patient_lookup", ReplayStatus.SUCCESS)
    viewer = store.create_key("vic", "viewer")
    from api.app import app
    with TestClient(app) as c:
        assert c.get("/v1/metrics").status_code == 401
        body = c.get("/v1/metrics", headers={"Authorization": f"Bearer {viewer}"}).json()
        assert body["capabilities"][0]["capability_id"] == "clinic.patient_lookup" and "browser_pool" in body
        prom = c.get("/v1/metrics.prom", headers={"Authorization": f"Bearer {viewer}"})
        assert prom.status_code == 200 and "cua_runs_total" in prom.text


# ---- structured logs ------------------------------------------------------------------------------------------

@pytest.fixture
def log_lines(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(observability, "_configured", False)
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    observability.configure(stream=stream)
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    observability.LOGGER.handlers = []
    monkeypatch.setattr(observability, "_configured", False)


def test_every_line_of_a_run_carries_its_run_id_and_no_parameter_values(log_lines, clinic):
    res, _ = runtime.run_replay("clinic.patient_lookup", {"mrn": "LK-100002"}, target="clinic", base_url=clinic,
                                requested_by="alex", enable_operator_console=False)
    lines = [l for l in log_lines() if l.get("run_id") == res.run_id]
    events = [l["event"] for l in lines]

    assert events[:2] == ["run.accepted", "run.started"] and events[-1] == "run.finished"
    assert "run.signed_in" in events  # logged on the browser worker thread, so the run id crossed threads
    accepted = next(l for l in lines if l["event"] == "run.accepted")
    assert accepted["requested_by"] == "alex" and accepted["params"] == ["mrn"]
    finished = lines[-1]
    assert finished["status"] == "success" and finished["duration_s"] > 0 and finished["error_code"] is None
    assert "LK-100002" not in json.dumps(log_lines())  # values are never logged


def test_failures_log_at_warning_with_their_error_code(log_lines, clinic):
    httpx.post(f"{clinic}/_test/drift", json={"level": 2, "seed": "l"}, timeout=5)
    try:
        res, _ = runtime.run_replay("clinic.patient_lookup", {"mrn": "LK-100001"}, target="clinic", base_url=clinic, enable_operator_console=False)
    finally:
        httpx.delete(f"{clinic}/_test/drift", timeout=5)
    by_event = {l["event"]: l for l in log_lines() if l.get("run_id") == res.run_id}
    assert by_event["run.finished"]["level"] == "WARNING" and by_event["run.finished"]["error_code"] == "login_failed"
    assert by_event["repair.proposed"]["confident"] is True


def test_approvals_and_auth_failures_are_logged_without_the_key(log_lines):
    from api.app import app
    store = runtime.default_store()
    good = store.create_key("alex", "operator")
    with TestClient(app) as c:
        c.get("/v1/me")
        c.get("/v1/me", headers={"Authorization": "Bearer cua_notarealkey"})
        c.get("/v1/me", headers={"Authorization": f"Bearer {good}"})
    lines = log_lines()
    assert [l["reason"] for l in lines if l["event"] == "auth.rejected"] == ["no_key", "unknown_or_revoked_key"]
    requests = [l for l in lines if l["event"] == "http.request"]
    assert [r["status"] for r in requests] == [401, 401, 200] and requests[-1]["principal"] == "alex"
    assert good not in json.dumps(lines) and "cua_notarealkey" not in json.dumps(lines)


def test_text_format_is_readable(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(observability, "_configured", False)
    monkeypatch.setenv("LOG_FORMAT", "text")
    observability.configure(stream=stream)
    with observability.bind(run_id="run_x"):
        observability.log("run.finished", status="success")
    assert "run.finished" in stream.getvalue() and "run=run_x" in stream.getvalue() and "status=success" in stream.getvalue()
    observability.LOGGER.handlers = []
    monkeypatch.setattr(observability, "_configured", False)


# ---- traces ---------------------------------------------------------------------------------------------------------

def run_lookup(clinic, **kw):
    return runtime.run_replay("clinic.patient_lookup", {"mrn": "LK-100001"}, target="clinic", base_url=clinic, enable_operator_console=False, **kw)


def test_a_failed_run_keeps_a_trace_a_successful_one_does_not(clinic):
    ok, ok_dir = run_lookup(clinic)
    assert ok.status == ReplayStatus.SUCCESS and ok.trace is None and not (ok_dir / "trace.zip").exists()

    httpx.post(f"{clinic}/_test/drift", json={"level": 2, "seed": "t"}, timeout=5)
    try:
        bad, bad_dir = run_lookup(clinic)
    finally:
        httpx.delete(f"{clinic}/_test/drift", timeout=5)
    assert bad.status == ReplayStatus.HARD_FAILURE and bad.trace == "trace.zip"
    path = bad_dir / "trace.zip"
    assert path.exists() and (path.stat().st_mode & 0o777) == 0o600
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
    assert any(n.endswith(".trace") for n in names) and any("resources/" in n for n in names)  # events plus screenshots/snapshots


def test_trace_policy_off_always_and_non_sandbox(clinic, monkeypatch):
    from safety.config import PolicyConfig

    policy = PolicyConfig.load()
    policy.tracing.mode = "off"
    off, d = run_lookup(clinic, policy=policy)
    assert off.trace is None and not (d / "trace.zip").exists()

    policy.tracing.mode = "always"
    always, d = run_lookup(clinic, policy=policy)
    assert always.status == ReplayStatus.SUCCESS and always.trace == "trace.zip" and (d / "trace.zip").exists()

    monkeypatch.setitem(runtime.TARGET_PROFILES["clinic"], "sandbox", False)  # a real system: a trace would hold real typed values
    real, d = run_lookup(clinic, policy=policy)
    assert real.trace is None and not (d / "trace.zip").exists()
    policy.tracing.non_sandbox = True
    allowed, d = run_lookup(clinic, policy=policy)
    assert allowed.trace == "trace.zip"


def test_the_trace_download_is_for_operators_and_only_when_one_exists(clinic, monkeypatch, tmp_path):
    from api.app import app

    monkeypatch.setitem(runtime.TARGET_PROFILES["clinic"], "base_url", clinic)
    store = runtime.default_store()
    keys = {n: store.create_key(n, r) for n, r in [("vic", "viewer"), ("alex", "operator")]}
    h = lambda who: {"Authorization": f"Bearer {keys[who]}"}  # noqa: E731
    httpx.post(f"{clinic}/_test/drift", json={"level": 2, "seed": "d"}, timeout=5)
    try:
        failed, _ = run_lookup(clinic)
    finally:
        httpx.delete(f"{clinic}/_test/drift", timeout=5)
    ok, _ = run_lookup(clinic)
    with TestClient(app) as c:
        assert c.get(f"/v1/runs/{failed.run_id}", headers=h("vic")).json()["has_trace"] is True
        assert c.get(f"/v1/runs/{failed.run_id}/trace", headers=h("vic")).status_code == 403
        got = c.get(f"/v1/runs/{failed.run_id}/trace", headers=h("alex"))
        assert got.status_code == 200 and got.headers["content-type"] == "application/zip" and zipfile.is_zipfile(io.BytesIO(got.content))
        assert c.get(f"/v1/runs/{ok.run_id}/trace", headers=h("alex")).status_code == 404
