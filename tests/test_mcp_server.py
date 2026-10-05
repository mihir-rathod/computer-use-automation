"""The MCP server, driven by a real MCP client against the real v1 API, a real browser and the real clinic.

The assistant's view: tools with risk metadata, results that say plainly what happened, and no way to approve its own work."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from mcp.client import Client

import mcp_server
import runtime
from tests.clinic_support import effects, reset


@pytest.fixture
def platform(api_base_url, clinic_base_url, monkeypatch):
    monkeypatch.setitem(runtime.TARGET_PROFILES["clinic"], "base_url", clinic_base_url)
    monkeypatch.setitem(runtime.TARGET_PROFILES["clinic_supervisor"], "base_url", clinic_base_url)
    reset(clinic_base_url)
    store = runtime.default_store()
    keys = {n: store.create_key(n, r) for n, r in [("assistant", "operator"), ("dana", "supervisor"), ("watcher", "viewer")]}

    class P:
        base = api_base_url
        clinic = clinic_base_url

        @staticmethod
        def bridge(who="assistant", wait=60.0):
            return mcp_server.Bridge(api_base_url, keys[who], wait_seconds=wait)

        @staticmethod
        def approve(run_id, reason="checked"):
            return httpx.post(f"{api_base_url}/v1/runs/{run_id}/approve", json={"reason": reason}, headers={"Authorization": f"Bearer {keys['dana']}"}, timeout=30)

    return P


def run_async(coro):
    """asyncio.run on a fresh thread: when the suite's session-wide Playwright browser is alive, the main thread already has a loop."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result()


def with_client(bridge, fn):
    async def go():
        async with Client(mcp_server.build_server(bridge)) as client:
            return await fn(client)
    return run_async(go())


def text(result) -> str:
    return result.content[0].text


def test_tools_carry_schemas_and_risk_metadata_and_hide_sign_on(platform):
    tools = {t.name: t for t in with_client(platform.bridge(), lambda c: c.list_tools()).tools}

    assert "clinic.login" not in tools and "mockbank.login" not in tools and mcp_server.GET_RUN_STATUS in tools
    lookup, refund, contact = tools["clinic.patient_lookup"], tools["clinic.issue_refund"], tools["clinic.update_patient_contact"]

    assert lookup.annotations.read_only_hint and not lookup.annotations.destructive_hint
    assert "idempotency_key" not in lookup.input_schema["properties"] and "Read-only" in lookup.description
    assert refund.annotations.destructive_hint and not refund.annotations.read_only_hint
    assert "idempotency_key" in refund.input_schema["required"] and "supervisor approval" in refund.description and "cannot" in refund.description
    assert refund.input_schema["properties"]["reason"]["enum"]  # enums reach the assistant, so it picks valid values
    assert contact.input_schema["required"] == ["mrn"] and {"required": ["phone"]} in contact.input_schema["anyOf"]
    assert not any("approve" in n for n in tools)  # an assistant has no way to approve


def test_a_read_returns_the_answer_directly(platform):
    result = with_client(platform.bridge(), lambda c: c.call_tool("clinic.patient_lookup", {"mrn": "LK-100002"}))
    assert not result.is_error and result.structured_content["status"] == "success"
    assert result.structured_content["outputs"]["patient_name"] == "Pell, Jordan" and "Done." in text(result)


def test_a_normal_answer_that_is_not_an_error_is_not_flagged_as_one(platform):
    result = with_client(platform.bridge(), lambda c: c.call_tool("clinic.patient_lookup", {"mrn": "LK-999999"}))
    assert not result.is_error and result.structured_content["business_outcome"] == "not_found" and "not a system error" in text(result)


def test_bad_arguments_come_back_as_errors_the_assistant_can_act_on(platform):
    bad = with_client(platform.bridge(), lambda c: c.call_tool("clinic.patient_lookup", {"mrn": "nope"}))
    assert bad.is_error and "pattern" in text(bad)
    unknown = with_client(platform.bridge(), lambda c: c.call_tool("clinic.nothing", {}))
    assert unknown.is_error and "unknown tool" in text(unknown)


def test_a_commit_without_an_idempotency_key_is_refused_before_anything_is_sent(platform):
    args = {"invoice": "INV-30001", "amount": "10.00", "reason": "billing_error"}
    result = with_client(platform.bridge(), lambda c: c.call_tool("clinic.issue_refund", args))
    assert result.is_error and "idempotency_key" in text(result)
    assert runtime.default_store().list_runs() == []


def test_a_refund_waits_for_a_person_and_the_assistant_cannot_approve_it(platform):
    args = {"invoice": "INV-30001", "amount": "20.00", "reason": "duplicate_payment", "idempotency_key": "mcp-refund-0001"}

    async def flow(client):
        first = await client.call_tool("clinic.issue_refund", args)
        run_id = first.structured_content["run_id"]
        assert not first.is_error and first.structured_content["status"] == "pending_approval"
        assert "NOT done" in first.content[0].text and "supervisor approval" in first.content[0].text
        assert effects(platform.clinic) == []

        approved = await asyncio.to_thread(platform.approve, run_id)  # a human, with a supervisor key, approves outside MCP
        assert approved.status_code == 202
        for _ in range(60):
            status = await client.call_tool(mcp_server.GET_RUN_STATUS, {"run_id": run_id})
            if status.structured_content["status"] == "success":
                break
            await asyncio.sleep(1)
        return run_id, status, await client.call_tool("clinic.issue_refund", args)

    run_id, status, retry = with_client(platform.bridge(), flow)

    assert status.structured_content["status"] == "success" and status.structured_content["committed"]
    assert retry.structured_content["deduplicated"] and retry.structured_content["run_id"] == run_id  # the retry did not post again
    assert len(effects(platform.clinic)) == 1


def test_a_lost_response_tells_the_assistant_not_to_retry(platform):
    httpx.post(f"{platform.clinic}/_test/chaos", json={"kind": "error500_after", "method": "POST", "path_glob": "/legacy/transactions/confirm"}, timeout=5)
    args = {"invoice": "INV-30001", "amount": "20.00", "reason": "duplicate_payment", "idempotency_key": "mcp-lost-0001"}

    async def flow(client):
        run_id = (await client.call_tool("clinic.issue_refund", args)).structured_content["run_id"]
        await asyncio.to_thread(platform.approve, run_id)
        for _ in range(60):
            status = await client.call_tool(mcp_server.GET_RUN_STATUS, {"run_id": run_id})
            if status.structured_content["status"] == "needs_review":
                return status
            await asyncio.sleep(1)
        raise AssertionError(status)

    status = with_client(platform.bridge(), flow)
    assert status.is_error and "UNKNOWN" in text(status) and "Do NOT retry" in text(status)
    assert len(effects(platform.clinic)) == 1


def test_dry_run_changes_nothing(platform):
    args = {"invoice": "INV-30001", "amount": "20.00", "reason": "duplicate_payment", "idempotency_key": "mcp-dry-00001", "dry_run": True}
    result = with_client(platform.bridge(), lambda c: c.call_tool("clinic.issue_refund", args))
    assert result.structured_content["status"] == "dry_run" and "Nothing was changed" in text(result) and effects(platform.clinic) == []


def test_a_partial_update_works_through_the_tool(platform):
    result = with_client(platform.bridge(), lambda c: c.call_tool("clinic.update_patient_contact", {"mrn": "LK-100002", "phone": "206-555-0123"}))
    assert result.structured_content["status"] == "success"
    none_given = with_client(platform.bridge(), lambda c: c.call_tool("clinic.update_patient_contact", {"mrn": "LK-100002"}))
    assert none_given.is_error and "at least one of" in text(none_given)


def test_the_keys_role_is_the_ceiling(platform):
    viewer = with_client(platform.bridge("watcher"), lambda c: c.call_tool("clinic.patient_lookup", {"mrn": "LK-100001"}))
    assert viewer.is_error and "403" in text(viewer)
    bad_key = with_client(mcp_server.Bridge(platform.base, "cua_wrong"), lambda c: c.call_tool("clinic.patient_lookup", {"mrn": "LK-100001"}))
    assert bad_key.is_error and "401" in text(bad_key)


def test_an_unreachable_platform_is_reported_not_raised():
    result = with_client(mcp_server.Bridge("http://127.0.0.1:9", "k"), lambda c: c.call_tool("clinic.patient_lookup", {"mrn": "LK-100001"}))
    assert result.is_error and "could not reach" in text(result)


def test_the_real_stdio_server_starts_and_serves_tools_as_claude_desktop_would_run_it(platform):
    """Launches mcp_server.py as a subprocess over stdio, exactly how an MCP client starts it."""
    import sys
    from pathlib import Path

    from mcp.client.stdio import StdioServerParameters

    key = runtime.default_store().create_key("desktop", "operator")
    root = Path(mcp_server.__file__).resolve().parent
    params = StdioServerParameters(command=sys.executable, args=[str(root / "mcp_server.py")], cwd=str(root),
                                   env={"CUA_API_URL": platform.base, "CUA_API_KEY": key})

    async def go():
        async with Client(params) as client:
            tools = await client.list_tools()
            answer = await client.call_tool("clinic.patient_lookup", {"mrn": "LK-100001"})
            return {t.name for t in tools.tools}, answer

    names, answer = run_async(go())
    assert "clinic.issue_refund" in names and answer.structured_content["outputs"]["patient_name"] == "Brennan, Avery"


def test_the_server_refuses_to_start_without_a_key(monkeypatch):
    monkeypatch.delenv("CUA_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="CUA_API_KEY"):
        mcp_server.bridge_from_env()
