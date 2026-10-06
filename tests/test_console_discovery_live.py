"""Discovery with the real model (Gemini), against the real clinic. Skipped without GEMINI_API_KEY. The model is not deterministic, so this asserts the
guarantees that must hold whatever it does: it never changes anything, and if it says the task is ready, the recorded task really replays."""
from __future__ import annotations

import os
import time

import httpx

import pytest

from artifacts_lib import storage
from tests.clinic_support import effects
from tests.test_console_discovery import DETAILS, discover, discover_base, wait_for  # noqa: F401

pytestmark = pytest.mark.skipif(not os.environ.get("GEMINI_API_KEY"), reason="GEMINI_API_KEY not set")


def test_the_real_model_discovers_a_read_only_task_and_the_recording_replays(discover, monkeypatch):
    # the fixture sets a fake key and a scripted model on demand; here the real key and real model must be used
    from dotenv import dotenv_values
    monkeypatch.setenv("GEMINI_API_KEY", dotenv_values(".env").get("GEMINI_API_KEY") or os.environ["GEMINI_API_KEY"])
    sid = discover.post("/v1/discover", json={"contract": DETAILS}, headers=discover.h("dana")).json()["id"]
    done = wait_for(discover, sid, ("ready", "needs_attention", "failed", "stuck", "cancelled"), timeout=240)
    print("\nstatus:", done["status"], "| steps:", done["steps"], "| error:", done["error"], "| verify:", done["verify"])
    for t in done["turns"]:
        print(f"  {t['n']:>2}. {t['summary']}  ok={t['ok']} {t['error'] or ''} {t['value'] or ''}")
    assert effects(discover_base(), "appointment.cancel") == [], "a read-only task must never change anything"
    assert done["status"] in ("ready", "needs_attention", "stuck"), done
    if done["status"] == "ready":
        assert done["verify"]["passed"] is True
        assert storage.list_versions("clinic.appointment_details", discover.adir) == ["1.0.0"]


def test_from_one_sentence_the_real_model_drafts_the_task_and_discovers_it_from_the_main_page(discover, monkeypatch):
    from dotenv import dotenv_values
    monkeypatch.setenv("GEMINI_API_KEY", dotenv_values(".env").get("GEMINI_API_KEY") or os.environ["GEMINI_API_KEY"])
    drafted = discover.post("/v1/discover/draft", json={"target": "clinic", "request": "Look up appointment A-20002 and tell me the patient and the provider"}, headers=discover.h("dana"))
    print("\ndraft:", drafted.status_code, drafted.text[:900])
    assert drafted.status_code == 200 and "contract" in drafted.json()
    contract = drafted.json()["contract"]
    assert contract["effect"] == "read_only"
    sid = discover.post("/v1/discover", json={"contract": contract}, headers=discover.h("dana")).json()["id"]
    done = wait_for(discover, sid, ("ready", "needs_attention", "failed", "stuck", "cancelled"), timeout=300)
    print("status:", done["status"], "| steps:", done["steps"], "| error:", done["error"], "| verify:", done["verify"])
    for t in done["turns"]:
        print(f"  {t['n']:>2}. {t['summary']}  ok={t['ok']} {t['error'] or ''} {t['value'] or ''}")
    assert effects(discover_base(), "appointment.cancel") == []
    if done["status"] == "ready":
        artifact = storage.load_artifact_by_id(contract["capability_id"] if "capability_id" in contract else "clinic." + contract["task_name"], discover.adir, version="1.0.0")
        print("success text:", artifact.success_checkpoint.value, "| note:", artifact.provenance.note)
        assert "A-20002" not in artifact.success_checkpoint.value
        # the real accuracy check: promote it, run it for many OTHER appointments (other patients, providers, statuses) and compare each answer with the clinic's own page
        assert discover.post(f"/v1/discover/{sid}/promote", json={"reason": "live trial"}, headers=discover.h("dana")).status_code == 200
        page = httpx.Client(follow_redirects=True)
        page.post(f"{discover_base()}/legacy/login", data={"username": "frontdesk", "password": "desk-demo-123"})
        name = contract["inputs"][0]["name"]
        checked, wrong = 0, []
        for i in range(1, 15):
            number = f"A-200{i:02d}"
            html = page.get(f"{discover_base()}/legacy/appointments/{number}").text
            if "Patient" not in html:
                continue
            run = discover.post("/v1/runs", json={"capability_id": "clinic." + contract["task_name"], "params": {name: number}, "target": "clinic"}, headers=discover.h("alex")).json()["id"]
            end = time.time() + 60
            while time.time() < end:
                body = discover.get(f"/v1/runs/{run}", headers=discover.h("alex")).json()
                if body["status"] in ("success", "hard_failure", "business_outcome", "needs_review"):
                    break
                time.sleep(0.3)
            out = (body.get("result") or {}).get("outputs") or {}
            checked += 1
            if not (body["status"] == "success" and out.get("provider", "\0") in html and out.get("patient", "\0").split(" (")[0] in html):
                wrong.append((number, body["status"], out, (body.get("result") or {}).get("error")))
        print(f"checked {checked} appointments, {len(wrong)} wrong:", wrong)
        assert checked >= 8 and not wrong


def test_an_example_that_does_not_exist_is_not_recorded_as_a_task(discover, monkeypatch):
    """The six-digit number a person mistyped: the model must say it was not found, not read some other appointment and record that."""
    from dotenv import dotenv_values
    monkeypatch.setenv("GEMINI_API_KEY", dotenv_values(".env").get("GEMINI_API_KEY") or os.environ["GEMINI_API_KEY"])
    drafted = discover.post("/v1/discover/draft", json={"target": "clinic", "request": "Look up appointment A-200002 and tell me the patient and the provider"}, headers=discover.h("dana"))
    assert drafted.status_code == 200, drafted.text
    sid = discover.post("/v1/discover", json={"contract": drafted.json()["contract"]}, headers=discover.h("dana")).json()["id"]
    done = wait_for(discover, sid, ("ready", "needs_attention", "failed", "stuck", "cancelled"), timeout=300)
    print("\nstatus:", done["status"], "| steps:", done["steps"], "| error:", done["error"], "| reasoning:", done["reasoning"])
    for t in done["turns"]:
        print(f"  {t['n']:>2}. {t['summary']}  ok={t['ok']} {t['error'] or ''} {t['value'] or ''}")
    assert done["status"] != "ready", "a task recorded from an appointment that does not exist"
    assert storage.list_versions("clinic." + drafted.json()["contract"]["task_name"], discover.adir) in ([], ["1.0.0"])
