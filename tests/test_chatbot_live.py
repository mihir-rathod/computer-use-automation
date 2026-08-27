"""The chatbot's own real end-to-end test -- makes a REAL Gemini function-calling call, and a
REAL HTTP round-trip from the chatbot to its own capability API (api/chatbot.py's whole point:
it's just another client of that API, not a shortcut around it). Skipped automatically without
GEMINI_API_KEY, same reasoning as test_discovery_live.py.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from api.app import app

pytestmark = pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="requires a real GEMINI_API_KEY")

client = TestClient(app)


def _send(message: str, monkeypatch, api_base_url: str, mockbank_base_url: str) -> str:
    monkeypatch.setenv("CAPABILITY_API_BASE_URL", api_base_url)
    monkeypatch.setenv("CAPABILITY_TARGET_BASE_URL_OVERRIDE", mockbank_base_url)
    resp = client.post("/chat", data={"message": message}, follow_redirects=False)
    assert resp.status_code == 303
    page = client.get("/chat").text
    # last assistant bubble on the page -- good enough for a single-exchange test
    return page.rsplit('<div class="msg assistant">', 1)[-1].split("</div>", 1)[0]


def test_chatbot_maps_a_real_request_onto_the_right_capability_and_runs_it(monkeypatch, api_base_url, mockbank_base_url):
    reply = _send("look up the balance for mockbank member 10002", monkeypatch, api_base_url, mockbank_base_url)
    assert "mockbank.member_balance_lookup" in reply
    assert "150" in reply  # the real savings_balance for member 10002


def test_chatbot_reports_a_business_outcome_not_an_error(monkeypatch, api_base_url, mockbank_base_url):
    reply = _send("check mockbank member 99999's balance", monkeypatch, api_base_url, mockbank_base_url)
    assert "not_found" in reply


def test_chatbot_declines_an_out_of_scope_request_without_forcing_a_tool_call(monkeypatch, api_base_url, mockbank_base_url):
    reply = _send("what's the weather like today", monkeypatch, api_base_url, mockbank_base_url)
    assert "mockbank." not in reply and "meridian." not in reply
