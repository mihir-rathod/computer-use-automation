"""Supervised commit recording, end to end, with no LLM: a scripted stand-in for the model drives the real
clinic through discovery, the commit gate authorises the irreversible step, the recorder builds the artifact,
and that artifact then replays against ground truth (the clinic's audit log).

The scripted model only decides *what to do next*; everything else is the production code path."""
from __future__ import annotations

import re
from types import SimpleNamespace

import httpx
import pytest
from google.genai import types

from agent.catalog import get_spec
from agent.commit_gate import CommitDecision, auto_sandbox_gate
from agent.loop import DiscoveryLoop
from agent.recorder import build_artifact
from artifacts_lib.lint import has_errors, lint_artifact
from replay.engine import ReplayEngine
from replay.result import ReplayStatus
from surface.web import WebSurface
from tests.clinic_support import effects, reset, sign_in


class ScriptedModel:
    """Each entry is (tool, args-builder); a builder receives the element-list text and returns args."""

    def __init__(self, script):
        self.script, self.calls = list(script), 0

    def generate(self, contents, tools=None, system_instruction=None):
        prompt = contents[-1].parts[0].text
        name, build = self.script[self.calls]
        self.calls += 1
        part = types.Part.from_function_call(name=name, args=build(prompt))
        return SimpleNamespace(candidates=[SimpleNamespace(content=types.Content(role="model", parts=[part]))])


def ref(prompt: str, pattern: str) -> str:
    for line in prompt.splitlines():
        match = re.match(r"\s*(\w+): (.*)", line)
        if match and re.search(pattern, match.group(2)):
            return match.group(1)
    raise AssertionError(f"no element matching {pattern!r} in:\n{prompt}")


def refund_script(invoice: str, amount: str, reason: str):
    return [
        ("type_text", lambda p: {"ref": ref(p, r'textbox.*name="number"'), "text": invoice}),
        ("click", lambda p: {"ref": ref(p, r'button "Continue"')}),
        ("type_text", lambda p: {"ref": ref(p, r'textbox.*name="amount"'), "text": amount}),
        ("select_option", lambda p: {"ref": ref(p, r"combobox.*name=\"reason\""), "value": reason}),
        ("click", lambda p: {"ref": ref(p, r'button "Continue"')}),
        ("click", lambda p: {"ref": ref(p, r'button "Confirm"')}),
        ("extract", lambda p: {"ref": ref(p, r'cell "RFD-'), "output_name": "receipt_number"}),
        ("finish", lambda p: {"reasoning": "refund issued and receipt read"}),
    ]


@pytest.fixture
def discovery_setup(page, clinic, tmp_path):
    from safety.allowlist import AllowlistConfig, AllowlistPolicy
    from safety.policy import SafetyPolicy
    from safety.risk import RiskClassifier

    sign_in(page, clinic)
    config = AllowlistConfig(allowed_base_urls=[clinic], allowed_route_patterns=["/legacy/*"],
                             allowed_action_types=["navigate", "click", "type", "select", "extract", "wait_for", "dismiss_dialog"])
    surface = WebSurface(page, base_url=clinic, screenshot_dir=tmp_path / "shots", safety_policy=SafetyPolicy(AllowlistPolicy(config), RiskClassifier()))
    return surface, get_spec("clinic.issue_refund", clinic)


def discover(surface, spec, model, gate, params):
    return DiscoveryLoop(surface, model, max_steps=15, commit_gate=gate, capability_id=spec.capability_id).run(
        goal=spec.goal, parameters=params, start_path=spec.start_path)


def test_without_a_gate_discovery_cannot_commit(discovery_setup, clinic, params):
    surface, spec = discovery_setup
    script = refund_script(params["invoice"], "20.00", "duplicate_payment")[:6] + [("give_up", lambda p: {"reasoning": "blocked at confirm"})]

    result = discover(surface, spec, ScriptedModel(script), None, params)

    assert result.stop_reason == "give_up"
    assert effects(clinic) == []


def test_a_declined_commit_is_not_executed_and_not_recorded(discovery_setup, clinic, params):
    surface, spec = discovery_setup
    script = refund_script(params["invoice"], "20.00", "duplicate_payment")[:6] + [("give_up", lambda p: {"reasoning": "supervisor declined"})]

    result = discover(surface, spec, ScriptedModel(script), lambda req: CommitDecision(False, mode="supervised"), params)

    assert result.stop_reason == "give_up"
    assert effects(clinic) == []
    assert all(r.commit_approval is None for r in result.transcript)


def test_approved_commit_is_recorded_with_provenance_and_replays_exactly_once(discovery_setup, page, clinic, params, tmp_path):
    surface, spec = discovery_setup
    asked = []

    def gate(req):
        asked.append(req)
        return CommitDecision(True, approver="dana.okafor", mode="supervised")

    result = discover(surface, spec, ScriptedModel(refund_script(params["invoice"], "20.00", "duplicate_payment")), gate, params)

    assert result.stop_reason == "finished"
    assert len(asked) == 1 and "Confirm" in asked[0].description and asked[0].capability_id == "clinic.issue_refund"
    assert len(effects(clinic)) == 1  # the discovery run itself issued one real refund

    artifact = build_artifact(
        result, params, capability_id=spec.capability_id, version="1.0.0", name=spec.name, description=spec.description,
        target=spec.target, preconditions=spec.preconditions, input_schema=spec.input_schema, output_schema=spec.output_schema,
        success_checkpoint=spec.success_checkpoint, error_handling=spec.error_handling, safety=spec.safety,
        success_output_defaults=spec.success_output_defaults, discovered_by="scripted-model", discovery_run_id="test",
    )

    commit = next(s for s in artifact.steps if s.risk_level == "irreversible")
    assert commit.idempotent is False and commit.target.semantic_description == "Confirm"
    [approval] = artifact.provenance.commit_approvals
    assert (approval.step_id, approval.approver, approval.mode) == (commit.step_id, "dana.okafor", "supervised")
    assert not has_errors(lint_artifact(artifact)), [str(f) for f in lint_artifact(artifact) if f.level == "error"]
    assert not any(f.code == "commit-not-approved" for f in lint_artifact(artifact))

    # unlabeled form fields are recorded by their HTML name, and the receipt cell is anchored to its label, not its text
    typed = [s for s in artifact.steps if s.action == "type"]
    assert "{{invoice}}" in typed[0].params["text"] and "{{amount}}" in typed[1].params["text"]
    extract = next(s for s in artifact.steps if s.action == "extract")
    assert extract.target.locators[0].strategy == "xpath" and "Receipt number" in extract.target.locators[0].value
    assert not any("R-" in loc.value for loc in extract.target.locators)

    # replay on a fresh clinic with different inputs: the recorded flow issues exactly one new refund
    fixtures = reset(clinic)
    sign_in(page, clinic)
    replay_params = {"invoice": fixtures["standard"]["refundable_invoice"], "amount": "35.00", "reason": "billing_error"}
    replay = ReplayEngine(WebSurface(page, base_url=clinic, screenshot_dir=tmp_path / "r"), confirmed_steps={commit.step_id}).run(artifact, replay_params)

    assert replay.status == ReplayStatus.SUCCESS, replay.error
    assert re.fullmatch(r"RFD-\d+", replay.outputs["receipt_number"]), replay.outputs
    [row] = effects(clinic)
    assert "3500" in str(row) or "35" in str(row["detail"])


def test_auto_sandbox_gate_refuses_a_non_sandbox_target():
    with pytest.raises(PermissionError):
        auto_sandbox_gate(sandbox=False)
    decision = auto_sandbox_gate(sandbox=True)(SimpleNamespace())
    assert decision.approved and decision.mode == "auto_sandbox" and decision.approver == "auto:sandbox"


def test_label_anchored_cell_locator_reads_the_right_record(page, clinic):
    """A value cell is anchored to the label beside it, so one locator reads whichever patient is on screen."""
    from artifacts_lib.schema import Target
    from surface.locator_resolver import resolve_target

    sign_in(page, clinic)
    surface = WebSurface(page, base_url=clinic)
    page.goto(f"{clinic}/legacy/patients?mrn=LK-100001")
    page.get_by_role("link", name="Select").click()
    state = surface.perceive()
    phone_ref = next(e.ref for e in state.elements if e.role == "cell" and e.name and e.name.startswith("(206)"))
    target: Target = surface.compute_target(phone_ref)

    assert [l.strategy.value for l in target.locators] == ["xpath"]
    here = resolve_target(page, target)[0].inner_text().strip()
    page.goto(f"{clinic}/legacy/patients?mrn=LK-100002")
    page.get_by_role("link", name="Select").click()
    there = resolve_target(page, target)[0].inner_text().strip()

    assert here == "(206) 555-0111" and there != here and re.fullmatch(r"\(\d{3}\) \d{3}-\d{4}", there)


def test_unlabeled_form_fields_are_told_apart_in_the_prompt(page, clinic):
    sign_in(page, clinic)
    page.goto(f"{clinic}/legacy/patients")
    text = WebSurface(page, base_url=clinic).perceive().to_prompt_text()
    for field in ("mrn", "last", "dob"):
        assert f'name="{field}"' in text


def test_password_typed_during_discovery_never_reaches_the_evidence_log(page, clinic, tmp_path):
    """Found live: a ref-based type action into an unlabeled password field was logged in clear text, because the
    redaction keyed on the accessible name and a legacy form has none."""
    from evidence_lib.logger import EvidenceLogger

    page.goto(f"{clinic}/legacy/login")
    with EvidenceLogger(tmp_path) as log:
        surface = WebSurface(page, base_url=clinic, evidence_logger=log)
        state = surface.perceive()
        user = next(e.ref for e in state.elements if e.html_name == "username")
        pw = next(e.ref for e in state.elements if e.html_name == "password")
        from artifacts_lib.schema import ActionType
        from surface.base import Action

        surface.act(Action(kind=ActionType.TYPE, ref=user, params={"text": "frontdesk"}, actor="agent"))
        surface.act(Action(kind=ActionType.TYPE, ref=pw, params={"text": "desk-demo-123"}, actor="agent"))
    text = (tmp_path / "log.jsonl").read_text()
    assert "desk-demo-123" not in text and "frontdesk" in text


def test_select_by_label_is_applied_and_recorded_by_its_real_value(page, clinic):
    """Found live: the model chose a <select> option by its visible label ('Weather'); the recorder then kept the
    literal label, so the capability's `reason` input was never used. The surface now reports the real value."""
    from artifacts_lib.schema import ActionType
    from surface.base import Action

    sign_in(page, clinic)
    page.goto(f"{clinic}/legacy/fn/cancel")
    page.locator('input[name="number"]').fill("A-20002")
    page.get_by_role("button", name="Continue").click()
    surface = WebSurface(page, base_url=clinic)
    ref_ = next(e.ref for e in surface.perceive().elements if e.role == "combobox")

    result = surface.act(Action(kind=ActionType.SELECT, ref=ref_, params={"value": "Weather"}, actor="agent"))

    assert result.success and result.applied_params == {"value": "weather"}
    assert page.locator('select[name="reason"]').input_value() == "weather"
