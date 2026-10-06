"""The chat with the real model (Gemini) against the real clinic. Skipped without GEMINI_API_KEY. The model decides which task a sentence means and what
values to pass; everything after that (the argument checks, the run, its result) is the same code as a form submit."""
from __future__ import annotations

import os

import pytest

from tests.test_ui_backend import api, say, wait  # noqa: F401  (fixtures and helpers shared with the console's backend tests)

pytestmark = pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="requires a real GEMINI_API_KEY")


def test_chat_maps_a_real_request_onto_the_right_task_and_runs_it(api):
    reply = say(api, "alex", "look up the patient with MRN LK-100002").json()["messages"][1]
    assert reply["run_id"], reply
    done = wait(api, reply["run_id"])
    assert done["capability_id"] == "clinic.patient_lookup" and done["status"] == "success" and done["requested_by"] == "alex"
    assert done["result"]["outputs"]["patient_name"] == "Pell, Jordan"


def test_chat_reports_a_business_outcome_not_an_error(api):
    reply = say(api, "alex", "check the patient with MRN LK-999999").json()["messages"][1]
    done = wait(api, reply["run_id"])
    assert done["status"] == "business_outcome" and done["result"]["business_outcome"] == "not_found"


def test_chat_declines_an_out_of_scope_request_without_forcing_a_task(api):
    reply = say(api, "alex", "what's the weather like today").json()["messages"][1]
    assert reply["run_id"] is None and reply["text"]
